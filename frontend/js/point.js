/**
 * Building a point forecast, for a named location or for any coordinate.
 *
 * Shared by the map's trend panel and the standalone page so a click gives the
 * same answer in both. A named location is served precomputed; a coordinate is
 * assembled here from the on-demand conditions proxy plus the radar frames.
 *
 * The difference between the two is not cosmetic and callers should surface it:
 * KNMI's 20 members exist only while the ingestor holds a timestep in memory,
 * so a coordinate gets the rain *median* but no band and no probability.
 */

import { conditionsFromEnsemble } from './ensemble.js';
import { FrameStore } from './radar.js';

const WET_THRESHOLD_MM_H = 0.1;

export async function pointForName(name) {
    const response = await fetch(`/api/point/${encodeURIComponent(name)}`, { cache: 'no-store' });
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
            ?? fetch('/api/config', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null)),
        fetch(`/api/conditions?lat=${lat}&lon=${lon}`, { cache: 'no-store' })
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
        const sampled = frames.series(lon, lat).filter((point) => point.mmh !== null);
        if (sampled.length) {
            // Median only: no members here, so no band and no probability. The
            // flat percentiles keep the chart's shape without implying spread.
            document_.precipitation = {
                unit: 'mm/h',
                median_only: true,
                series: sampled.map((point) => ({
                    t: point.t,
                    p10: point.mmh, p25: point.mmh, median: point.mmh,
                    p75: point.mmh, p90: point.mmh,
                })),
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
