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
import { apiFetch, hasApiKey } from './key.js';
import { fetchHealth, readHealth, describeAge, HEALTH_POLL_MS } from './health.js';
import { formatClock } from './time.js';
import { createRadarMinimap } from './minimap.js';

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

/**
 * What the page is showing, and enough to ask for it again.
 *
 * The dropdown cannot answer that second part: a coordinate arriving from the
 * map has no name, so anything re-deriving the target from `select.value` asks
 * the server for a location called "". Retry and the periodic refresh both did.
 */
let currentRequest = null;
let currentDocument = null;

/** See app.js: whoever raised the banner owns it, and an error outranks age. */
let healthBannerUp = false;

function showBanner(message, retryable, tone = 'warn') {
    $('banner-text').textContent = message;
    $('banner-retry').hidden = !retryable;
    $('banner').classList.toggle('banner--info', tone === 'info');
    $('banner').classList.toggle('banner--stale', tone === 'stale');
    $('banner').hidden = false;
    healthBannerUp = tone === 'stale';
}

const hideBanner = () => {
    $('banner').hidden = true;
    $('banner').classList.remove('banner--info', 'banner--stale');
    healthBannerUp = false;
};

/**
 * The same freshness check the map page runs, for the same reason: these charts
 * are drawn from a document that stops being replaced when the feed stops, and
 * nothing else on the page would say so.
 */
async function updateHealth() {
    const { stale, age, detail } = readHealth(await fetchHealth());

    $('ref-time-wrap').classList.toggle('is-stale', stale);
    $('ref-time-age').textContent = stale ? `\u00b7 ${describeAge(age)} old` : '';
    $('ref-time-wrap').title = stale ? detail : '';

    if (!stale) {
        if (healthBannerUp) hideBanner();
        return;
    }
    if ($('banner').hidden || healthBannerUp) {
        showBanner(
            `No new forecast for ${describeAge(age)} — KNMI publishes every 5 minutes.`
            + ' Showing the last one that arrived.',
            false,
            'stale'
        );
    }
}

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
    const response = await apiFetch('/api/config', { cache: 'no-store' });
    if (!response.ok) return [];
    const manifest = await response.json();
    // Older manifests published bare names; accept both.
    return (manifest.points ?? []).map((point) =>
        typeof point === 'string' ? { name: point, lat: null, lon: null } : point
    );
}

let minimap = null;

/**
 * The radar card follows whatever the page is showing — a named location or a
 * clicked coordinate — so it is pointed from render(), which is the one place
 * that knows which document actually came back.
 *
 * Failures are swallowed on purpose. The charts are the page; a radar loop that
 * cannot load should not take them down with it, and it says so in its own
 * corner rather than in the page banner.
 */
function showRadar(location) {
    if (!minimap) {
        minimap = createRadarMinimap({
            mapEl: $('mini-map'),
            canvasEl: $('mini-canvas'),
            timeEl: $('mini-time'),
            playBtn: $('mini-play'),
            statusEl: $('mini-status'),
            scrubEl: $('mini-scrub'),
            nowEl: $('mini-now'),
        });
    }
    minimap.show(location).catch((error) => {
        $('mini-status').textContent = `unavailable — ${error.message}`;
    });
}

function render(document_) {
    currentDocument = document_;
    const reference = document_.reference_time;

    $('ref-time').textContent = formatClock(reference);
    $('summary-text').textContent = document_.summary?.text ?? '';
    $('location-coords').textContent =
        `${document_.location.lat.toFixed(4)}, ${document_.location.lon.toFixed(4)}`;
    // Three notes, because the three cases are genuinely different questions:
    // members at this square kilometre, the published field with a band taken
    // around each pixel, or the field on its own.
    const rain = document_.precipitation;
    const radius = rain?.band_radius_km;
    $('rain-note').textContent = !rain?.frame_only && radius
        ? `KNMI ensemble, probability-matched mean · 5-minute steps · band within ${Math.round(radius)} km`
        : rain?.frame_only
            ? 'KNMI ensemble, probability-matched mean · 5-minute steps · no spread away from a sampled location'
            : 'KNMI ensemble · 5-minute steps';

    renderSummaryStats(document_);
    showRadar(document_.location);

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
            ...centreOf(block),
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

/**
 * Which entry field carries a block's line, and what to call it.
 *
 * Precipitation is drawn as the field the map draws, so the two views of the
 * same place stop disagreeing — a point's own median is dry unless half the
 * members rain on its exact square kilometre, which for showers is most of the
 * time. Everything else really is a median of its members.
 */
function centreOf(block) {
    const drawn = block?.field_product && block.series?.some((e) => e.field !== undefined);
    return drawn
        ? { centreKey: 'field', centreLabel: block.field_product }
        : { centreKey: 'median', centreLabel: '' };
}

/** Min/max of the line, which is what a glance at a card wants. */
function describeRange(block) {
    const { centreKey } = centreOf(block);
    const middle = block.series.map((entry) => entry[centreKey]);
    const low = Math.min(...middle);
    const high = Math.max(...middle);
    const round = (value) => (Math.abs(value) >= 10 ? Math.round(value) : Math.round(value * 10) / 10);
    return `${round(low)}–${round(high)} ${block.unit}`;
}

function renderSummaryStats(document_) {
    const stats = $('summary-stats');
    stats.innerHTML = '';

    // Every series here starts before now — the rain chart carries the last
    // hour of radar, the conditions charts a few hours of history — and these
    // stats are a forecast, so they count from the reference time forward.
    // They used to span the whole series, which put "Peak rate 1.9 mm/h" under
    // a sentence saying "peaking at 0.7 mm/h": both true, one describing a
    // shower that had already passed.
    const reference = document_.reference_time;
    const ahead = (block) => (block?.series ?? []).filter((entry) => entry.t >= reference);

    const rain = ahead(document_.precipitation);
    const rainCentre = centreOf(document_.precipitation).centreKey;
    const peak = rain.length ? Math.max(...rain.map((entry) => entry[rainCentre])) : 0;
    // 5-minute steps, so a rate in mm/h contributes a twelfth of an hour.
    const total = rain.reduce((sum, entry) => sum + entry[rainCentre] / 12, 0);
    const items = [
        ['Peak rate', `${peak.toFixed(1)} mm/h`],
        ['Total expected', `${total.toFixed(1)} mm`],
    ];
    for (const key of ['temperature', 'wind', 'solar']) {
        const medians = ahead(document_[key]).map((entry) => entry.median);
        if (!medians.length) continue;
        items.push([
            key === 'solar' ? 'Peak solar' : key === 'wind' ? 'Peak wind' : 'Max temp',
            `${Math.max(...medians)} ${document_[key].unit}`,
        ]);
    }

    // Each pair goes in its own wrapper — a <div> inside a <dl> is valid and
    // keeps the dt/dd association. Without it the two are separate flex items
    // that merely happen to sit next to each other, and the first line that
    // wraps leaves a label on one row and its number on the next.
    for (const [label, value] of items) {
        const cell = document.createElement('div');
        cell.className = 'summary-stat';
        const term = document.createElement('dt');
        term.textContent = label;
        const definition = document.createElement('dd');
        definition.textContent = value;
        cell.append(term, definition);
        stats.append(cell);
    }
}

/**
 * Load one request — `{name}` or `{coords}` — and show it.
 *
 * Recorded before the fetch rather than after, so a failure leaves Retry aimed
 * at what the reader actually asked for instead of at whatever the dropdown
 * happens to say.
 *
 * `explicit` means the reader chose this from the dropdown, so the URL is
 * rewritten to match. Arriving from a map click leaves `?lat=&lon=` alone: the
 * URL keeps saying which point was of interest, and the page says which one it
 * resolved to. Carrying both would be redundant, with the name silently winning.
 */
async function load(request, { explicit = false } = {}) {
    currentRequest = request;
    try {
        render(request.coords
            ? await pointForCoordinates(request.coords)
            : await pointForName(request.name));
        hideBanner();
        if (explicit) rewriteUrl(request);
        // After hideBanner(), so a stale reading can claim the banner the
        // successful load just cleared.
        await updateHealth();
    } catch (error) {
        showBanner(`Could not load the forecast — ${error.message}`, true);
    }
}

function rewriteUrl(request) {
    const url = new URL(window.location.href);
    if (request.coords) {
        url.searchParams.set('lat', request.coords.lat);
        url.searchParams.set('lon', request.coords.lon);
        url.searchParams.delete('location');
    } else {
        url.searchParams.set('location', request.name);
        url.searchParams.delete('lat');
        url.searchParams.delete('lon');
    }
    history.replaceState(null, '', url);
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
        // Two different situations, and telling them apart is the difference
        // between "fix your config" and "this list is not yours to see".
        if (request.coords) {
            select.hidden = true;
            await load({ coords: request.coords });
            return;
        }
        // An empty picker is worse than no picker: it looks like a broken
        // control rather than an absent one.
        select.hidden = true;
        showBanner(
            hasApiKey()
                ? 'No point forecasts are published. Set WIDGET_LOCATIONS in the server .env to add one.'
                : 'Saved locations are private to this server. Open this page once with ?key=… to see them.',
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
    // Shown whenever there is anything to choose. It used to need two entries,
    // which meant the picker vanished for the single-location setup that is the
    // normal case — the one reader who has a saved location could not select it.
    select.hidden = select.options.length === 0;

    if (!byName && request.coords) {
        // Honour the coordinate itself rather than snapping to a sampled point.
        select.value = '';
        await load({ coords: request.coords });
    } else {
        // An unknown ?location= falls back to the first published point rather
        // than erroring, but then the URL has to say so - otherwise it goes on
        // naming a location the page is not showing.
        const chosen = byName ?? points[0];
        select.value = chosen.name;
        await load({ name: chosen.name }, { explicit: !byName });
    }

    // The empty value is the ad-hoc "clicked point" option, which only exists
    // when a coordinate brought us here.
    select.addEventListener('change', () => {
        const name = select.value;
        if (!name && !request.coords) return;
        load(name ? { name } : { coords: request.coords }, { explicit: true });
    });
}

// Retry re-runs the request that failed. With nothing loaded at all — the
// manifest was unreachable, so there is no request yet — it starts over.
$('banner-retry').addEventListener('click', () => {
    hideBanner();
    if (currentRequest) load(currentRequest); else boot();
});

// Re-render on resize: the charts are laid out in pixels for the current width.
let resizeHandle = null;
window.addEventListener('resize', () => {
    clearTimeout(resizeHandle);
    resizeHandle = setTimeout(() => currentDocument && render(currentDocument), 150);
});

setInterval(() => {
    if (currentRequest) load(currentRequest);
}, REFRESH_INTERVAL_MS);

// More often than the reload: the document only changes every five minutes,
// but its age changes every minute and that is the part worth watching.
setInterval(updateHealth, HEALTH_POLL_MS);

boot();
