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
                series: sampled.map((point) => {
                    // An observed frame is the radar composite - a measurement,
                    // with no ensemble behind it and so never a band. Under its
                    // own key because `field_product` names what the *forecast*
                    // frames were reduced into, and the tooltip would otherwise
                    // caption an hour of measured rain as a probability-matched
                    // mean of members that were never involved in it.
                    const value = point.kind === 'observed'
                        ? { measured: point.mmh }
                        : { field: point.mmh };
                    const band = bands?.get(point.t);
                    return band
                        ? { t: point.t, ...value, nearby_p10: band.p10,
                            nearby_median: band.p50, nearby_p90: band.p90 }
                        : { t: point.t, ...value };
                }),
            };
            const reference = document_.reference_time;
            const wet = sampled.filter(
                (point) => point.t >= reference && point.mmh >= WET_THRESHOLD_MM_H
            );
            document_.summary = {
                text: wet.length
                    ? `Rain expected here, peaking at ${Math.max(...wet.map((p) => p.mmh)).toFixed(1)} mm/h.`
                    : 'No rain expected here in the next six hours.',
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
