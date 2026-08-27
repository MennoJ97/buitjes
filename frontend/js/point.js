/**
 * Building a point forecast, for a named location or for any coordinate.
 *
 * Shared by the map's trend panel and the standalone page so a click gives the
 * same answer in both. A named location is served precomputed; a coordinate is
 * assembled here from the on-demand conditions proxy plus the radar frames.
 *
 * The difference between the two is not cosmetic and callers should surface it.
 * A named location is sampled from KNMI's 20 members at its own square
 * kilometre, while the ingestor holds the timestep in memory. A coordinate is
 * read back off the published frames, which carry the probability-matched mean
 * and, where the server publishes a spread layer, a band taken over a small
 * radius around each pixel. So a coordinate now gets a real band - but one
 * answering "near here" rather than "here", and with no probability behind it.
 */

import { conditionsFromEnsemble } from './ensemble.js';
import { FrameStore } from './radar.js';
import { apiFetch } from './key.js';
import { centreValue } from './chart.js';
import { formatRate } from './ramp.js';
import { formatClock } from './time.js';

const WET_THRESHOLD_MM_H = 0.1;

export async function pointForName(name) {
    const response = await apiFetch(`/api/point/${encodeURIComponent(name)}`, { cache: 'no-store' });
    if (response.status === 401) throw new Error('this server requires an API key for point forecasts');
    if (response.status === 404) throw new Error(`no forecast is published for "${name}"`);
    if (!response.ok) throw new Error(`server returned ${response.status}`);
    return response.json();
}

/**
 * Assemble a forecast for a coordinate nobody configured.
 *
 * Pass an already-loaded `frames` store (the map has one) to avoid downloading
 * the frames a second time.
 */
export async function pointForCoordinates({ lat, lon }, options = {}) {
    const [manifest, ensemble] = await Promise.all([
        options.manifest
            ?? apiFetch('/api/config', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null)),
        apiFetch(`/api/conditions?lat=${lat}&lon=${lon}`, { cache: 'no-store' })
            .then((r) => (r.ok ? r.json() : null))
            .catch(() => null),
    ]);

    const document_ = {
        generated_at: Math.floor(Date.now() / 1000),
        reference_time: manifest?.reference_time ?? Math.floor(Date.now() / 1000),
        location: { name: 'this point', lat, lon, ad_hoc: true },
        source: manifest?.source ?? {},
        summary: { text: '' },
        ...(ensemble ? conditionsFromEnsemble(ensemble) : {}),
    };
    if (ensemble) {
        document_.conditions_source = {
            model: 'on-demand ensemble',
            attribution: 'Open-Meteo (CC BY 4.0)',
        };
    }

    let frames = options.frames;
    if (!frames && manifest?.frames?.length) {
        frames = new FrameStore();
        frames.setManifest(manifest);
        await frames.prefetch();
    }

    if (frames?.frames?.length) {
        // The band comes from a second set of frames, so ask for them before
        // sampling. A click is the reader saying they want this point in
        // detail, which is the moment to spend the download.
        const spread = frames.spreadInfo;
        if (spread) await frames.prefetchSpread();

        const bands = spread ? new Map(
            frames.spreadSeries(lon, lat).map((entry) => [entry.t, entry.band])
        ) : null;
        const sampled = frames.series(lon, lat).filter((point) => point.mmh !== null);
        if (sampled.length) {
            // Read off the frames. The median is the field the map draws; the
            // band, where there is one, is the ensemble within the published
            // radius. No quartiles - the frames carry three percentiles, not
            // five - and no probability, which needs the members themselves.
            document_.precipitation = {
                unit: 'mm/h',
                frame_only: !spread,
                // Its own key, not `neighbourhood_km`. A named location carries
                // that one too, and it means something else there: the radius
                // for `probability_nearby`, while its percentiles are the
                // members at one square kilometre. Sharing the name made the
                // detail page describe a per-cell band as a 10 km one.
                band_radius_km: spread?.radius_km,
                // `field`, not `median`. What comes off a frame is whatever
                // the ingestor reduced the members into - a probability-matched
                // mean unless configured otherwise - and none of those is the
                // median of anything. The manifest says which.
                field_product: manifest?.source?.reducer ?? manifest?.source?.product,
                nearby_radius_km: spread?.radius_km,
                // Named the same as a configured location's, so both draw the
                // same statistic through the same path: the neighbourhood
                // median as the line, its own p10–p90 around it. `field` rides
                // along for anyone who wants the number the map paints.
                series: frameSeries(sampled, bands),
            };
            // The same sentence a named location gets, off the same number the
            // chart draws. It used to be a one-liner built from the field, which
            // is how the headline came to say 40 mm/h over a PEAK RATE of 3.1.
            const block = document_.precipitation;
            document_.summary = {
                text: summarise(block.series, centreOf(block).centreKeys,
                                document_.reference_time),
            };
        }
    }

    // The outlook only earns its place past where KNMI stops.
    const knmiEnds = document_.precipitation?.series?.slice(-1)[0]?.t;
    if (knmiEnds && document_.precipitation_outlook) {
        document_.precipitation_outlook.series =
            document_.precipitation_outlook.series.filter((entry) => entry.t > knmiEnds);
    }
    return document_;
}


/**
 * Frame samples as the entries a published document carries.
 *
 * One definition, because the detail page and the map's popup both read points
 * off the same frames and must not disagree about what they found there.
 *
 * An observed frame is the radar composite - a measurement, with no ensemble
 * behind it and so never a band. Under its own key because `field_product`
 * names what the *forecast* frames were reduced into, and the tooltip would
 * otherwise caption an hour of measured rain as a probability-matched mean of
 * members that were never involved in it.
 */
function frameSeries(sampled, bands) {
    return sampled.map((point) => {
        const value = point.kind === 'observed'
            ? { measured: point.mmh }
            : { field: point.mmh };
        const band = bands?.get(point.t);
        return band
            ? { t: point.t, ...value, nearby_p10: band.p10,
                nearby_median: band.p50, nearby_p90: band.p90 }
            : { t: point.t, ...value };
    });
}


/**
 * Which entry fields carry a block's line, what to call each of them, and which
 * pair of keys the band around it comes from.
 *
 * The line and its band have to be the same kind of number or they look
 * unrelated, which is exactly what happened when the line was the map's field
 * and the band was the members at that one cell: the field is dealt by rank, so
 * it sat at zero through the band's whole peak and then spiked after it. The
 * neighbourhood band is local and smooth, and its median is a line that rises
 * when rain arrives near you.
 *
 * A chain rather than one key, and every link checked against the series rather
 * than against the block's metadata. Both matter:
 *
 * - A clicked point's series is not homogeneous. The hour of observed radar in
 *   front of the forecast is measurement, with no ensemble behind it and so no
 *   neighbourhood median, while the forecast steps have one. Naming the key the
 *   forecast half carries left the measured half with no number at all.
 * - `field` used to be offered only when the manifest said what the field was.
 *   A manifest without `source.reducer` costs a tooltip label, which is not a
 *   reason to fall through to `median` — a key a clicked point never carries.
 *
 * Falling through the chain per entry keeps the drawn value the best one that
 * step has, under a label that is true of it: the neighbourhood median where
 * the ensemble reaches, the measured rate over the observed hour, the reduced
 * field where neither is published, the members' own median for the blocks that
 * are genuinely a median of their members and say nothing extra.
 */
export function centreOf(block, fallbackLabel = '') {
    const has = (key) => block?.series?.some((entry) => entry[key] != null);
    const chain = [];
    const radius = Math.round(block?.nearby_radius_km ?? 0);
    if (block?.nearby_radius_km && has('nearby_median')) {
        chain.push(['nearby_median', `median within ${radius} km`]);
    }
    if (has('measured')) chain.push(['measured', 'measured by radar']);
    if (has('field')) chain.push(['field', block.field_product ?? '']);
    if (has('median') || !chain.length) chain.push(['median', fallbackLabel]);

    const centre = {
        centreKeys: chain.map(([key]) => key),
        centreLabels: chain.map(([, label]) => label),
    };
    // Only alongside the neighbourhood median: the per-cell percentiles are a
    // different kind of number from it, and drawing them around it is the very
    // mismatch this function exists to avoid.
    if (centre.centreKeys[0] === 'nearby_median') {
        centre.bands = [['nearby_p10', 'nearby_p90', 0.16,
                         `80% of members within ${radius} km`]];
    }
    return centre;
}


/**
 * Plain-language read of a precipitation series, for the one line of room a
 * headline, a map popup or a dashboard widget has.
 *
 * Reads through `centreKeys`, so it describes whatever the page beside it
 * draws. That is the whole point of it living here: the number in the sentence
 * and the number in the chart under it have to be the same number, and they
 * were not while this function existed twice.
 *
 * Describes the *spell* — the contiguous run of wet steps — rather than the
 * moment it begins. The onset rate alone is close to useless: it is by
 * construction the instant the rate crosses the wet threshold, so it is always
 * a small number whether what follows is a five-minute drizzle or the leading
 * edge of a downpour. The peak, and how far ahead of the onset it sits, is what
 * says which.
 *
 * The peak is taken from the current spell only, not the whole series. A second
 * shower three hours later is a different event and should not be quoted as
 * this one's severity.
 *
 * Three clauses at the outside — when, how hard, until when — and that ceiling
 * is deliberate. This has to fit a widget with a single line, so the peak earns
 * its clause only by being meaningfully heavier than the onset, and the onset
 * rate drops out when the peak is already saying it. The longest sentence it
 * can produce is around ninety characters.
 */
export function summarise(series, centreKeys, reference) {
    const valueOf = (entry) => centreValue(entry, centreKeys);
    const future = (series ?? []).filter(
        (entry) => entry.t >= reference && valueOf(entry) !== null
    );
    if (!future.length) return 'No forecast for this point.';

    const onsetIndex = future.findIndex((entry) => (valueOf(entry) ?? 0) >= WET_THRESHOLD_MM_H);
    if (onsetIndex === -1) return 'Staying dry for the whole forecast.';

    const dryAfter = future.findIndex(
        (entry, i) => i > onsetIndex && (valueOf(entry) ?? 0) < WET_THRESHOLD_MM_H
    );
    const spell = future.slice(onsetIndex, dryAfter === -1 ? undefined : dryAfter);
    const clearsAt = dryAfter === -1 ? null : future[dryAfter].t;

    const onset = spell[0];
    const peak = spell.reduce(
        (best, entry) => ((valueOf(entry) ?? 0) > (valueOf(best) ?? 0) ? entry : best),
        spell[0],
    );

    // Only worth its own clause if it is meaningfully heavier than the start —
    // otherwise it is the same number printed twice.
    const peakMatters = peak.t !== onset.t
        && (valueOf(peak) ?? 0) >= (valueOf(onset) ?? 0) * 1.5 + 0.05;

    const leadMinutes = Math.round((onset.t - reference) / 60);
    // Whether the sentence is counting minutes from now or naming clock times.
    // Mixing the two is what makes "rain at 11:25, up to 4 mm/h within 30 min"
    // ambiguous — thirty minutes from now, or from the onset two hours away?
    const relative = onsetIndex === 0 || leadMinutes < 90;

    const parts = [];
    if (onsetIndex === 0) {
        // "now" is load-bearing: while the timeline is scrubbed, the line above
        // this one shows the rate at the frame being looked at, and the two are
        // different numbers. Without it they read as a contradiction.
        parts.push(`Raining now at ${formatRate(valueOf(onset))} mm/h`);
    } else {
        const when = relative ? `in ${leadMinutes} min` : `at ${formatClock(onset.t)}`;
        // With no peak clause coming, the onset rate is the only figure there
        // is, so it stays. With one, it would just be the smaller of two.
        parts.push(peakMatters
            ? `Dry now — rain ${when}`
            : `Dry now — rain ${when} at ${formatRate(valueOf(onset))} mm/h`);
    }

    if (peakMatters) {
        // How fast it builds is the other half of the question, and it is the
        // gap between onset and peak — so say it in minutes when the sentence
        // is already relative and the gap is short, rather than making the
        // reader subtract two clock times.
        const climb = Math.round((peak.t - onset.t) / 60);
        parts.push(relative && climb <= 30
            ? `up to ${formatRate(valueOf(peak))} mm/h within ${climb} min`
            : `peaking ${formatRate(valueOf(peak))} mm/h around ${formatClock(peak.t)}`);
    }

    parts.push(clearsAt
        ? `easing off around ${formatClock(clearsAt)}`
        : 'lasting past the end of the forecast');

    return `${parts.join(', ')}.`;
}


/**
 * The same sentence, for a point read straight off the frames — the map's
 * inspect popup, which has no published document behind it.
 *
 * Takes whatever spread frames have arrived rather than waiting for them: the
 * popup answers a click immediately, and an entry with no band falls through
 * `centreKeys` to the field on its own. Clicking through to the detail page is
 * what buys the full band, and that path does wait.
 */
export function summariseFrames(frames, lng, lat, reference) {
    const spread = frames.spreadInfo;
    const bands = spread ? new Map(
        frames.spreadSeries(lng, lat).map((entry) => [entry.t, entry.band])
    ) : null;
    const series = frameSeries(
        frames.series(lng, lat).filter((point) => point.mmh !== null), bands
    );
    const block = { series, nearby_radius_km: spread?.radius_km };
    return summarise(series, centreOf(block).centreKeys, reference);
}
