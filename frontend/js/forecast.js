/**
 * The standalone forecast page: every published series for one location, drawn
 * with its ensemble spread.
 *
 * Reads `/api/point/<name>` — one document holding precipitation and the
 * Open-Meteo conditions — plus `/api/config` for the list of locations the
 * ingestor publishes.
 */

import { renderBandChart } from './chart.js';
import { colorForRate } from './ramp.js';
import { conditionsFromEnsemble } from './ensemble.js';
import { FrameStore } from './radar.js';

const $ = (id) => document.getElementById(id);
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;
const WET_THRESHOLD_MM_H = 0.1;

/** Which document key goes in which card, and how it should look. */
const CHARTS = [
    {
        key: 'precipitation', container: 'chart-rain', meta: 'rain-meta',
        colour: '#3b82f6', zeroFloor: true, minSpan: 1,
        format: (value) => (value >= 10 ? value.toFixed(0) : value.toFixed(1)),
    },
    {
        key: 'temperature', container: 'chart-temperature', meta: 'temp-meta',
        colour: '#f97316', zeroFloor: false, minSpan: 4, format: (value) => value.toFixed(0),
    },
    {
        key: 'wind', container: 'chart-wind', meta: 'wind-meta',
        colour: '#22c55e', zeroFloor: true, minSpan: 4, format: (value) => value.toFixed(0),
    },
    {
        key: 'solar', container: 'chart-solar', meta: 'solar-meta',
        colour: '#eab308', zeroFloor: true, minSpan: 100, format: (value) => value.toFixed(0),
    },
    {
        key: 'precipitation_outlook', container: 'chart-outlook', meta: 'outlook-meta',
        colour: '#818cf8', zeroFloor: true, minSpan: 1,
        format: (value) => (value >= 10 ? value.toFixed(0) : value.toFixed(1)),
    },
];

let currentDocument = null;
const frames = new FrameStore();

/**
 * Build a point document for a coordinate nobody configured.
 *
 * Conditions come from the server's on-demand proxy, and the high-resolution
 * KNMI rain is sampled straight out of the frames the map already publishes.
 * What a coordinate cannot have is KNMI's *spread*: those 20 members exist only
 * while the ingestor holds a timestep in memory. So the rain line is drawn
 * without a band, and the page says why rather than implying certainty.
 */
async function buildForCoordinates({ lat, lon }) {
    const [manifest, ensemble] = await Promise.all([
        fetch('/api/config', { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null)),
        fetch(`/api/conditions?lat=${lat}&lon=${lon}`, { cache: 'no-store' })
            .then((r) => (r.ok ? r.json() : null))
            .catch(() => null),
    ]);

    const document_ = {
        generated_at: Math.floor(Date.now() / 1000),
        reference_time: manifest?.reference_time ?? Math.floor(Date.now() / 1000),
        location: { name: 'this point', lat, lon },
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

    if (manifest?.frames?.length) {
        frames.setManifest(manifest);
        await frames.prefetch();
        const sampled = frames.series(lon, lat).filter((point) => point.mmh !== null);
        if (sampled.length) {
            // Median only: no members, so no band and no probability.
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
            const wet = sampled.filter((p) => p.t >= reference && p.mmh >= WET_THRESHOLD_MM_H);
            document_.summary = {
                text: wet.length
                    ? `Rain expected at this point, peaking at ${Math.max(...wet.map((p) => p.mmh)).toFixed(1)} mm/h.`
                    : 'No rain expected at this point in the next six hours.',
            };
        }
    }

    // The outlook only makes sense past where KNMI stops.
    const knmiEnds = document_.precipitation?.series?.slice(-1)[0]?.t;
    if (knmiEnds && document_.precipitation_outlook) {
        document_.precipitation_outlook.series =
            document_.precipitation_outlook.series.filter((entry) => entry.t > knmiEnds);
    }
    return document_;
}

function showBanner(message, retryable, tone = 'warn') {
    $('banner-text').textContent = message;
    $('banner-retry').hidden = !retryable;
    $('banner').classList.toggle('banner--info', tone === 'info');
    $('banner').hidden = false;
}

const hideBanner = () => { $('banner').hidden = true; };

function requestFromUrl() {
    const params = new URLSearchParams(location.search);
    const lat = Number(params.get('lat'));
    const lon = Number(params.get('lon'));
    return {
        name: params.get('location'),
        coords: Number.isFinite(lat) && Number.isFinite(lon) && params.has('lat')
            ? { lat, lon }
            : null,
    };
}

async function loadLocations() {
    const response = await fetch('/api/config', { cache: 'no-store' });
    if (!response.ok) return [];
    const manifest = await response.json();
    // Older manifests published bare names; accept both.
    return (manifest.points ?? []).map((point) =>
        typeof point === 'string' ? { name: point, lat: null, lon: null } : point
    );
}

async function loadPoint(name) {
    const response = await fetch(`/api/point/${encodeURIComponent(name)}`, { cache: 'no-store' });
    if (response.status === 401) throw new Error('this server requires an API key for point forecasts');
    if (response.status === 404) throw new Error(`no forecast is published for "${name}"`);
    if (!response.ok) throw new Error(`server returned ${response.status}`);
    return response.json();
}

function render(document_) {
    currentDocument = document_;
    const reference = document_.reference_time;

    $('ref-time').textContent = new Date(reference * 1000)
        .toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    $('summary-text').textContent = document_.summary?.text ?? '';
    $('location-coords').textContent =
        `${document_.location.lat.toFixed(4)}, ${document_.location.lon.toFixed(4)}`;
    $('rain-note').textContent = document_.precipitation?.median_only
        ? 'KNMI ensemble median · 5-minute steps · no spread away from a sampled location'
        : 'KNMI ensemble · 5-minute steps';

    renderSummaryStats(document_);

    for (const config of CHARTS) {
        const block = document_[config.key];
        const container = $(config.container);
        const meta = $(config.meta);
        if (!block) {
            container.innerHTML = '<p class="chart-empty">Not published for this location.</p>';
            meta.textContent = '';
            continue;
        }
        meta.textContent = describeRange(block);
        renderBandChart(container, block.series, {
            unit: block.unit,
            colour: config.colour,
            zeroFloor: config.zeroFloor,
            minSpan: config.minSpan,
            formatValue: config.format,
            now: reference,
        });
    }

    renderProbability(document_, reference);

    const source = document_.source ?? {};
    const conditions = document_.conditions_source ?? {};
    $('forecast-foot').textContent = [
        `Precipitation: ${source.attribution ?? 'KNMI'} — ${source.dataset ?? ''}`,
        conditions.model ? `Conditions: ${conditions.attribution} — ${conditions.model}` : null,
    ].filter(Boolean).join(' · ');
}

/** Min/max of the median line, which is what a glance at a card wants. */
function describeRange(block) {
    const medians = block.series.map((entry) => entry.median);
    const low = Math.min(...medians);
    const high = Math.max(...medians);
    const round = (value) => (Math.abs(value) >= 10 ? Math.round(value) : Math.round(value * 10) / 10);
    return `${round(low)}–${round(high)} ${block.unit}`;
}

function renderSummaryStats(document_) {
    const stats = $('summary-stats');
    stats.innerHTML = '';

    const rain = document_.precipitation?.series ?? [];
    const peak = rain.length ? Math.max(...rain.map((entry) => entry.median)) : 0;
    // 5-minute steps, so a rate in mm/h contributes a twelfth of an hour.
    const total = rain.reduce((sum, entry) => sum + entry.median / 12, 0);
    const wettest = rain.reduce(
        (best, entry) =>
            (entry.probability ?? -1) > (best?.probability ?? -1) ? entry : best,
        null
    );

    const items = [
        ['Peak rate', `${peak.toFixed(1)} mm/h`],
        ['Total expected', `${total.toFixed(1)} mm`],
        ['Highest chance',
         wettest?.probability !== undefined ? `${Math.round(wettest.probability * 100)}%` : '—'],
    ];
    for (const key of ['temperature', 'wind', 'solar']) {
        const block = document_[key];
        if (!block?.series?.length) continue;
        const medians = block.series.map((entry) => entry.median);
        items.push([
            key === 'solar' ? 'Peak solar' : key === 'wind' ? 'Peak wind' : 'Max temp',
            `${Math.max(...medians)} ${block.unit}`,
        ]);
    }

    for (const [label, value] of items) {
        const term = document.createElement('dt');
        term.textContent = label;
        const definition = document.createElement('dd');
        definition.textContent = value;
        stats.append(term, definition);
    }
}

/**
 * Probability has no percentiles — it is already a summary across members — so
 * it is drawn as bars coloured by the rate they accompany rather than a band.
 */
function renderProbability(document_, reference) {
    const container = $('chart-probability');
    const series = document_.precipitation?.series ?? [];
    container.innerHTML = '';

    // Probability is a count across members, so it exists only where the
    // ingestor sampled them. A coordinate gets the median line and an honest
    // note instead of a chart of NaN.
    const hasProbability = series.some((entry) => entry.probability !== undefined);
    if (!series.length || !hasProbability) {
        $('prob-meta').textContent = '';
        container.innerHTML =
            '<p class="chart-empty">Only available at a sampled location — it counts how ' +
            'many of KNMI\'s 20 members see rain, and those members are not kept for ' +
            'arbitrary points.</p>';
        return;
    }

    const peak = Math.max(...series.map((entry) => entry.probability));
    $('prob-meta').textContent = `peaks at ${Math.round(peak * 100)}%`;

    const height = 150;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const width = Math.max(240, container.clientWidth || 480);
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('width', '100%');
    svg.setAttribute('height', String(height));

    const pad = { top: 10, right: 8, bottom: 22, left: 42 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;
    const barWidth = plotWidth / series.length;

    for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
        const y = pad.top + plotHeight - fraction * plotHeight;
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', pad.left); line.setAttribute('x2', width - pad.right);
        line.setAttribute('y1', y); line.setAttribute('y2', y);
        line.setAttribute('class', 'chart-grid');
        svg.appendChild(line);
        const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        label.setAttribute('x', pad.left - 6); label.setAttribute('y', y + 3);
        label.setAttribute('class', 'chart-axis');
        label.textContent = `${Math.round(fraction * 100)}%`;
        svg.appendChild(label);
    }

    series.forEach((entry, index) => {
        if (entry.probability <= 0) return;
        const barHeight = entry.probability * plotHeight;
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', pad.left + index * barWidth);
        rect.setAttribute('y', pad.top + plotHeight - barHeight);
        rect.setAttribute('width', Math.max(1, barWidth - 1));
        rect.setAttribute('height', barHeight);
        // Colour by the rate those members are predicting, so a high chance of
        // drizzle does not look like a high chance of a downpour.
        rect.setAttribute('fill', colorForRate(Math.max(entry.median, WET_THRESHOLD_MM_H)));
        svg.appendChild(rect);
    });

    if (reference > series[0].t && reference < series[series.length - 1].t) {
        const span = series[series.length - 1].t - series[0].t;
        const x = pad.left + ((reference - series[0].t) / span) * plotWidth;
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x); line.setAttribute('x2', x);
        line.setAttribute('y1', pad.top); line.setAttribute('y2', pad.top + plotHeight);
        line.setAttribute('class', 'chart-now');
        svg.appendChild(line);
    }

    container.appendChild(svg);
}

/**
 * `explicit` means the reader chose this location from the dropdown, so the URL
 * becomes a request for that location and the coordinate that led here is
 * dropped. Arriving from a map click leaves `?lat=&lon=` alone: the URL keeps
 * saying which point was of interest, and the page says which one it resolved
 * to. Carrying both would be redundant, with the name silently winning.
 */
async function selectCoordinates(coords) {
    try {
        render(await buildForCoordinates(coords));
        hideBanner();
    } catch (error) {
        showBanner(`Could not load the forecast — ${error.message}`, true);
    }
}

async function selectLocation(name, { explicit = false } = {}) {
    try {
        render(await loadPoint(name));
        hideBanner();
        if (explicit) {
            const url = new URL(window.location.href);
            url.searchParams.set('location', name);
            url.searchParams.delete('lat');
            url.searchParams.delete('lon');
            history.replaceState(null, '', url);
        }
    } catch (error) {
        showBanner(`Could not load the forecast — ${error.message}`, true);
    }
}

const byNameEarly = (points, name) => points.find((point) => point.name === name);

async function boot() {
    const select = $('location-select');
    let points = [];
    try {
        points = await loadLocations();
    } catch {
        // Fall through: an explicit ?location= may still work.
    }

    const request = requestFromUrl();
    if (!points.length && request.name) points = [{ name: request.name, lat: null, lon: null }];

    if (!points.length) {
        showBanner(
            'No point forecasts are published. Set WIDGET_LOCATIONS in the server .env to add one.',
            false
        );
        return;
    }

    select.innerHTML = '';
    if (request.coords && !byNameEarly(points, request.name)) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'clicked point';
        select.appendChild(option);
    }
    for (const point of points) {
        const option = document.createElement('option');
        option.value = point.name;
        option.textContent = point.name;
        select.appendChild(option);
    }

    const byName = points.find((point) => point.name === request.name);
    select.hidden = points.length < 2;

    if (!byName && request.coords) {
        // Honour the coordinate itself rather than snapping to a sampled point.
        select.value = '';
        await selectCoordinates(request.coords);
    } else {
        const chosen = byName ?? points[0];
        select.value = chosen.name;
        await selectLocation(chosen.name);
    }

    select.addEventListener('change', () => selectLocation(select.value, { explicit: true }));
}

$('banner-retry').addEventListener('click', () => {
    hideBanner();
    selectLocation($('location-select').value);
});

// Re-render on resize: the charts are laid out in pixels for the current width.
let resizeHandle = null;
window.addEventListener('resize', () => {
    clearTimeout(resizeHandle);
    resizeHandle = setTimeout(() => currentDocument && render(currentDocument), 150);
});

setInterval(() => {
    const name = $('location-select').value;
    if (name) selectLocation(name);
}, REFRESH_INTERVAL_MS);

boot();
