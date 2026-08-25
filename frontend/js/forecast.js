/**
 * The standalone forecast page: every published series for one location, drawn
 * with its ensemble spread.
 *
 * Reads `/api/point/<name>` — one document holding precipitation and the
 * Open-Meteo conditions — plus `/api/config` for the list of locations the
 * ingestor publishes.
 */

import { renderBandChart } from './chart.js';
import { pointForName, pointForCoordinates } from './point.js';

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
    const items = [
        ['Peak rate', `${peak.toFixed(1)} mm/h`],
        ['Total expected', `${total.toFixed(1)} mm`],
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
 * `explicit` means the reader chose this location from the dropdown, so the URL
 * becomes a request for that location and the coordinate that led here is
 * dropped. Arriving from a map click leaves `?lat=&lon=` alone: the URL keeps
 * saying which point was of interest, and the page says which one it resolved
 * to. Carrying both would be redundant, with the name silently winning.
 */
async function selectCoordinates(coords) {
    try {
        render(await pointForCoordinates(coords));
        hideBanner();
    } catch (error) {
        showBanner(`Could not load the forecast — ${error.message}`, true);
    }
}

async function selectLocation(name, { explicit = false } = {}) {
    try {
        render(await pointForName(name));
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
