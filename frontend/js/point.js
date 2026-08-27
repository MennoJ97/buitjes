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
                series: sampled.map((point) => {
                    const band = bands?.get(point.t);
                    return band
                        ? { t: point.t, p10: band.p10, field: point.mmh, p90: band.p90 }
                        : { t: point.t, field: point.mmh };
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
