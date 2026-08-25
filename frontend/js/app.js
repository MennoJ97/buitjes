import { RadarRenderer, FrameStore } from './radar.js';
import { renderLegend, colorForRate, rampPosition, formatRate } from './ramp.js';
import { renderBandChart } from './chart.js';
import { pointForName, pointForCoordinates } from './point.js';

/** How each timeline zone is described in the UI. */
const ZONES = {
    observed: { label: 'Observed', hint: 'Radar measurements' },
    nowcast: { label: 'Nowcast', hint: 'Mostly radar extrapolation' },
    forecast: { label: 'Forecast', hint: 'Increasingly weather-model driven' },
};

const carto = (name) => `https://a.basemaps.cartocdn.com/${name}/{z}/{x}/{y}.png`;
const OSM_TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';

const CARTO_CREDIT =
    '&copy; <a href="https://carto.com/attributions">CARTO</a> &copy; OpenStreetMap contributors';
const OSM_CREDIT =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// The map's attribution control is the conventional home for credits, so the
// inspiration is acknowledged there as well as in the About dialog.
const OWN_CREDIT =
    ' | data &copy; <a href="https://dataplatform.knmi.nl/">KNMI</a>' +
    ' | inspired by <a href="https://nimbus.yannick.cloud">Nimbus</a>';

/**
 * Basemaps are split into a label-free ground layer and a separate label layer,
 * so place names can be drawn back on top of the radar. Rain at 70% opacity over
 * an all-in-one basemap swallows exactly the city names you want to locate it by.
 *
 * `groundPaint` and `labelPaint` make a style more legible without a different
 * tile provider — the alternatives with genuinely higher-contrast dark
 * cartography (Stadia's Alidade Smooth Dark, Stamen Toner) all want an API key
 * and a registered domain.
 *
 * The numbers are measured, not guessed, because the obvious ones are wrong:
 * `raster-contrast` pivots around mid-grey, and CARTO's dark ground lives at
 * 3-15% luminance, so simply turning contrast up drives the whole map below
 * zero and clips it to black. At contrast 0.35 alone, 100% of the ground
 * clipped. Raising `raster-brightness-min` in step lifts the range back into
 * view: at contrast 0.3 with a floor of 0.18 nothing clips, land reads 45/255
 * against water at 11 (from 38 against 8), and the labels go from 102 to 148.
 *
 * That is a real improvement but a bounded one. Brightness can only compress
 * the range and contrast can only pivot at mid-grey, so these tiles cannot be
 * made dramatically punchier from the client side; the Light and OpenStreetMap
 * styles are the genuinely higher-contrast options here.
 *
 * OpenStreetMap's standard tiles are one image with the names baked in, so they
 * cannot be split — its labels necessarily sit *under* the radar. That is the
 * trade-off for the familiar look, and the radar opacity slider is the remedy.
 */
const BASEMAPS = {
    dark: {
        label: 'Dark',
        ground: carto('dark_nolabels'),
        labels: carto('dark_only_labels'),
        credit: CARTO_CREDIT,
    },
    contrast: {
        label: 'Dark, high contrast',
        ground: carto('dark_nolabels'),
        labels: carto('dark_only_labels'),
        credit: CARTO_CREDIT,
        groundPaint: { 'raster-contrast': 0.3, 'raster-brightness-min': 0.18 },
        labelPaint: { 'raster-brightness-min': 0.3 },
    },
    light: {
        label: 'Light',
        ground: carto('light_nolabels'),
        labels: carto('light_only_labels'),
        credit: CARTO_CREDIT,
        lightUi: true,
    },
    osm: {
        label: 'OpenStreetMap',
        ground: OSM_TILES,
        labels: null,
        credit: OSM_CREDIT,
        lightUi: true,
    },
    minimal: {
        label: 'Dark, no labels',
        ground: carto('dark_nolabels'),
        labels: null,
        credit: CARTO_CREDIT,
    },
};

const DEFAULT_BASEMAP = 'dark';
const BASEMAP_STORAGE_KEY = 'stratus.basemap';

/** The style chosen last time. Picking a legible map should not be a per-visit chore. */
function storedBasemap() {
    try {
        const name = localStorage.getItem(BASEMAP_STORAGE_KEY);
        return name && BASEMAPS[name] ? name : DEFAULT_BASEMAP;
    } catch {
        // Private browsing, or storage disabled entirely.
        return DEFAULT_BASEMAP;
    }
}

const attributionFor = (config) => config.credit + OWN_CREDIT;

/** Below this rate we call it dry, matching the bottom of the colour ramp. */
const WET_THRESHOLD_MM_H = 0.1;
const REFRESH_INTERVAL_MS = 5 * 60 * 1000;
/** How often to re-check while the ingestor is still fetching its first cycle. */
const WARMUP_RETRY_MS = 10 * 1000;

const $ = (id) => document.getElementById(id);

const el = {
    banner: $('banner'),
    bannerText: $('banner-text'),
    bannerRetry: $('banner-retry'),
    panel: $('panel'),
    playBtn: $('play-pause-btn'),
    stepBack: $('step-back'),
    stepForward: $('step-fwd'),
    slider: $('timeline-slider'),
    zones: $('tl-zones'),
    ticks: $('tl-ticks'),
    loading: $('tl-loading'),
    nowMarker: $('tl-now'),
    currentTime: $('current-time'),
    stepLabel: $('step-label'),
    kindBadge: $('kind-badge'),
    refTime: $('ref-time'),
    zoneKey: $('zone-key'),
    legendCanvas: $('legend-canvas'),
    legendLabels: $('scale-labels'),
    settingsBtn: $('settings-btn'),
    settingsPopover: $('settings-popover'),
    infoBtn: $('info-btn'),
    aboutModal: $('about-modal'),
    aboutBackdrop: $('about-backdrop'),
    aboutClose: $('about-close'),
    aboutDatasets: $('about-datasets'),
    collapseBtn: $('collapse-btn'),
    locateBtn: $('locate-btn'),
    speedSlider: $('speed-slider'),
    speedLabel: $('speed-label'),
    opacitySlider: $('opacity-slider'),
    opacityLabel: $('opacity-label'),
    basemapSelect: $('basemap-select'),
    aboutSource: $('about-source'),
    trendPanel: $('trend-panel'),
    trendToggle: $('trend-toggle'),
    trendClose: $('trend-close'),
    trendBody: $('trend-body'),
    trendTitle: $('trend-title'),
    trendSub: $('trend-sub'),
    trendLink: $('trend-link'),
    glcanvas: $('glcanvas'),
    hoverReadout: $('hover-readout'),
    hoverSwatch: $('hover-swatch'),
    hoverValue: $('hover-value'),
};

const store = new FrameStore();
let renderer;
let currentIndex = 0;
let isPlaying = false;
let playTimer = null;
let inspectMarker = null;
let inspectPopup = null;
let inspectPoint = null;
let inspectSeries = null;
let refreshTimer = null;
let warmupTimer = null;

/**
 * The server is reachable but the ingestor has not published a cycle yet.
 * Normal for the first minute or so after `docker compose up`, and while KNMI
 * is rate-limiting the poller — so it is a waiting state, not an error.
 */
class DataNotReady extends Error {}

// Built from the remembered style rather than always starting dark and swapping:
// rebuilding the sources a moment after load makes the map visibly flash, and on
// a slow connection it downloads a set of tiles nobody asked to see.
const initialBasemap = storedBasemap();
const initialConfig = BASEMAPS[initialBasemap];

const map = new maplibregl.Map({
    container: 'map',
    style: {
        version: 8,
        sources: {
            basemap: {
                type: 'raster',
                tiles: [initialConfig.ground],
                tileSize: 256,
                attribution: attributionFor(initialConfig),
            },
            ...(initialConfig.labels
                ? { labels: { type: 'raster', tiles: [initialConfig.labels], tileSize: 256 } }
                : {}),
        },
        layers: [
            {
                id: 'basemap', type: 'raster', source: 'basemap',
                ...(initialConfig.groundPaint ? { paint: initialConfig.groundPaint } : {}),
            },
            ...(initialConfig.labels
                ? [{
                    id: 'labels', type: 'raster', source: 'labels',
                    ...(initialConfig.labelPaint ? { paint: initialConfig.labelPaint } : {}),
                }]
                : []),
        ],
    },
    center: [5.2913, 52.1326],
    zoom: 6.4,
    attributionControl: { compact: true },
});
el.basemapSelect.value = initialBasemap;
document.body.classList.toggle('theme-light', !!initialConfig.lightUi);
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

// ---------------------------------------------------------------- boot

async function boot() {
    try {
        renderer = new RadarRenderer(el.glcanvas);
    } catch (err) {
        showBanner(err.message, false);
        return;
    }
    await load({ initial: true });
    if (!refreshTimer) {
        refreshTimer = setInterval(() => load({ initial: false }).catch(() => {}), REFRESH_INTERVAL_MS);
    }
}

async function load({ initial }) {
    try {
        const response = await fetch('/api/config', { cache: 'no-store' });
        if (response.status === 503) throw new DataNotReady();
        if (!response.ok) throw new Error(`Server returned ${response.status}`);
        const manifest = await response.json();
        if (!manifest.frames?.length) throw new Error('No radar frames are available.');

        const previousTime = store.frames[currentIndex]?.t;
        const wasAtLatest = !initial && currentIndex === store.frames.length - 1;

        store.setManifest(manifest);
        renderer.resize(store.width, store.height);
        renderer.setMaxPrecip(store.maxPrecip);

        buildTimeline();
        el.refTime.textContent = formatClock(manifest.reference_time ?? manifest.generated_at);
        describeSources(manifest);

        if (initial) {
            // Open on the frame nearest to now, not the end of the forecast.
            currentIndex = nearestIndexForTime(manifest.reference_time) ?? 0;
        } else if (wasAtLatest) {
            currentIndex = store.frames.length - 1;
        } else {
            // Hold the wall-clock time the user was looking at. A frame that has
            // aged off the front of the timeline falls back to the nearest one.
            currentIndex =
                indexOfTime(previousTime) ?? nearestIndexForTime(previousTime) ?? clampIndex(currentIndex);
        }

        setupRadarLayer();
        hideBanner();
        clearWarmupRetry();

        el.loading.hidden = false;
        await store.prefetch((progress) => {
            el.loading.style.setProperty('--progress', `${Math.round(progress * 100)}%`);
        });
        el.loading.hidden = true;

        // The frame list just changed, so any cached point series is stale.
        if (inspectPoint) inspectSeries = store.series(inspectPoint.lng, inspectPoint.lat);

        showFrame(currentIndex);
    } catch (err) {
        el.loading.hidden = true;
        if (err instanceof DataNotReady) {
            showBanner(
                'Waiting for the first forecast from KNMI — this usually takes a minute after startup.',
                false,
                'info'
            );
            scheduleWarmupRetry(initial);
            return;
        }
        showBanner(`Could not load radar data — ${err.message}`, true);
        throw err;
    }
}

function scheduleWarmupRetry(initial) {
    if (warmupTimer) return;
    warmupTimer = setTimeout(() => {
        warmupTimer = null;
        load({ initial }).catch(() => {});
    }, WARMUP_RETRY_MS);
}

function clearWarmupRetry() {
    clearTimeout(warmupTimer);
    warmupTimer = null;
}

function setupRadarLayer() {
    if (!map.isStyleLoaded()) {
        map.once('load', setupRadarLayer);
        return;
    }
    if (map.getSource('radar')) {
        map.getSource('radar').setCoordinates(store.coordinates);
        return;
    }
    map.addSource('radar', {
        type: 'canvas',
        canvas: 'glcanvas',
        coordinates: store.coordinates,
        animate: true,
    });
    map.addLayer(
        {
            id: 'radar-layer',
            type: 'raster',
            source: 'radar',
            paint: {
                'raster-opacity': Number(el.opacitySlider.value) / 100,
                'raster-fade-duration': 0,
            },
        },
        // Under the place names, over the ground.
        map.getLayer('labels') ? 'labels' : undefined
    );
}

// ---------------------------------------------------------------- timeline

function buildTimeline() {
    const frames = store.frames;
    el.slider.max = String(Math.max(0, frames.length - 1));
    el.slider.step = '1';

    // Zones are sized by frame count, not by elapsed time: the slider steps
    // frame by frame, so equal-width frames keep scrubbing predictable. The
    // labels underneath carry the change in step size.
    el.zones.innerHTML = '';
    for (const group of groupByKind(frames)) {
        const zone = document.createElement('div');
        zone.className = `tl-zone tl-zone--${group.kind}`;
        zone.style.flexGrow = String(group.count);
        zone.title = ZONES[group.kind]?.hint ?? group.kind;
        el.zones.appendChild(zone);
    }

    el.ticks.innerHTML = '';
    const span = Math.max(1, frames.length - 1);
    frames.forEach((frame, i) => {
        const tick = document.createElement('span');
        tick.className = 'tl-tick';
        if (isHourBoundary(frame.t)) tick.classList.add('tl-tick--hour');
        tick.style.left = `${(i / span) * 100}%`;
        el.ticks.appendChild(tick);
    });

    // "Now" rarely lands exactly on a frame — the blend starts at +5 min — so
    // interpolate its position. Hide it when the whole timeline is in the
    // future, where a marker pinned to the left edge would just be decoration.
    const nowIndex = fractionalIndexForTime(store.manifest.reference_time);
    const nowInside = nowIndex !== null && nowIndex > 0 && nowIndex < span;
    el.nowMarker.hidden = !nowInside;
    if (nowInside) el.nowMarker.style.left = `${(nowIndex / span) * 100}%`;

    el.zoneKey.innerHTML = '';
    for (const group of groupByKind(frames)) {
        const item = document.createElement('span');
        item.className = 'zone-key-item';
        item.innerHTML = `<i class="zone-swatch zone-swatch--${group.kind}"></i>${
            ZONES[group.kind]?.label ?? group.kind
        } <small>${describeSpan(group)}</small>`;
        el.zoneKey.appendChild(item);
    }
}

function groupByKind(frames) {
    const groups = [];
    for (const frame of frames) {
        const last = groups[groups.length - 1];
        if (last && last.kind === frame.kind) {
            last.count++;
            last.end = frame.t;
        } else {
            groups.push({ kind: frame.kind, count: 1, start: frame.t, end: frame.t });
        }
    }
    return groups;
}

function describeSpan(group) {
    const reference = store.manifest.reference_time ?? store.frames[0].t;
    return `${formatOffset(group.start - reference)} … ${formatOffset(group.end - reference)}`;
}

// ---------------------------------------------------------------- frames

/**
 * Show a frame.
 *
 * The text updates run now, but the expensive part — a 780x780 texture upload,
 * and rebuilding the popup — is deferred to the next animation frame, keeping
 * at most one render in flight. A drag fires `input` at pointer rate, which is
 * faster than the display and far faster than a frame upload; doing the work
 * synchronously saturated the main thread until pointer events were coalesced
 * and the thumb visibly came away from the cursor.
 *
 * `fromSlider` suppresses writing the value back into the input it came from,
 * which is the other way a native range thumb detaches mid-drag.
 */
function showFrame(index, { fromSlider = false } = {}) {
    const frames = store.frames;
    if (!frames.length) return;
    currentIndex = clampIndex(index);
    const frame = frames[currentIndex];

    if (!fromSlider) el.slider.value = String(currentIndex);
    el.currentTime.textContent = formatClock(frame.t);
    el.currentTime.dateTime = new Date(frame.t * 1000).toISOString();

    const reference = store.manifest.reference_time ?? frames[0].t;
    el.stepLabel.textContent = formatOffset(frame.t - reference);
    el.kindBadge.textContent = ZONES[frame.kind]?.label ?? frame.kind;
    el.kindBadge.className = `kind-badge kind-badge--${frame.kind}`;

    scheduleRender();
}

let renderHandle = null;

function scheduleRender() {
    if (renderHandle !== null) return;
    renderHandle = requestAnimationFrame(() => {
        renderHandle = null;
        renderCurrentFrame();
    });
}

function renderCurrentFrame() {
    const frame = store.frames[currentIndex];
    if (!frame) return;

    const image = store.imageFor(frame);
    if (image) {
        renderer.draw(image);
    } else {
        renderer.clear();
    }

    if (inspectPoint) updateInspectPopup();
    // The cursor has not moved, but the rain under it has.
    if (hoverPixel) updateHoverReadout();
}

function clampIndex(index) {
    return Math.max(0, Math.min(store.frames.length - 1, index));
}

function indexOfTime(t) {
    if (t == null) return null;
    const index = store.frames.findIndex((frame) => frame.t === t);
    return index === -1 ? null : index;
}

/** Where a timestamp sits on the frame axis, interpolated between frames. */
function fractionalIndexForTime(t) {
    const frames = store.frames;
    if (t == null || frames.length < 2) return null;
    if (t <= frames[0].t) return 0;
    if (t >= frames[frames.length - 1].t) return frames.length - 1;
    const next = frames.findIndex((frame) => frame.t >= t);
    const before = frames[next - 1];
    const after = frames[next];
    return next - 1 + (t - before.t) / (after.t - before.t);
}

/** The frame closest in time to a timestamp — the one to open on. */
function nearestIndexForTime(t) {
    const frames = store.frames;
    if (t == null || !frames.length) return null;
    let best = 0;
    for (let i = 1; i < frames.length; i++) {
        if (Math.abs(frames[i].t - t) < Math.abs(frames[best].t - t)) best = i;
    }
    return best;
}

function step(delta) {
    showFrame(currentIndex + delta);
}

// ---------------------------------------------------------------- playback

function setPlaying(playing) {
    isPlaying = playing;
    clearInterval(playTimer);
    playTimer = null;

    el.playBtn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
    el.playBtn.classList.toggle('is-playing', playing);
    el.playBtn.querySelector('use').setAttribute('href', playing ? '#icon-pause' : '#icon-play');

    if (!playing) return;
    const fps = Number(el.speedSlider.value);
    playTimer = setInterval(() => {
        showFrame((currentIndex + 1) % store.frames.length);
    }, 1000 / fps);
}

// ---------------------------------------------------------- hover readout

/**
 * The rain rate under the cursor, without having to click.
 *
 * Clicking pins a point and opens the popup with its whole time series; this is
 * the cheap continuous version — one pixel of the frame on screen, so the map
 * can be read by sweeping across it. The popup answers "what happens here?",
 * the readout answers "where is the rain right now?".
 *
 * The pointer position is kept in *screen* pixels rather than as a coordinate,
 * because the ground moves under a stationary cursor: zooming, or a frame
 * changing during playback, both have to re-read the same pixel.
 */
const CAN_HOVER = window.matchMedia('(hover: hover)').matches;

let hoverPixel = null;
let hoverHandle = null;
let isPanning = false;

function setupHoverReadout() {
    if (!CAN_HOVER) return;
    map.on('mousemove', (event) => {
        hoverPixel = event.point;
        scheduleHoverUpdate();
    });
    map.on('mouseout', hideHoverReadout);
    // While panning, the value under the cursor is whatever is being dragged
    // past it — briefly true and not worth reading, so stay out of the way.
    map.on('dragstart', () => { isPanning = true; hideHoverReadout(); });
    map.on('dragend', () => { isPanning = false; scheduleHoverUpdate(); });
    // Zooming keeps the cursor still and moves the ground beneath it.
    map.on('move', () => { if (hoverPixel) scheduleHoverUpdate(); });
}

function scheduleHoverUpdate() {
    if (hoverHandle !== null) return;
    hoverHandle = requestAnimationFrame(() => {
        hoverHandle = null;
        updateHoverReadout();
    });
}

function hideHoverReadout() {
    hoverPixel = null;
    el.hoverReadout.hidden = true;
}

function updateHoverReadout() {
    if (!hoverPixel || isPanning || !store.frames.length) {
        el.hoverReadout.hidden = true;
        return;
    }

    const frame = store.frames[currentIndex];
    const { lng, lat } = map.unproject(hoverPixel);

    // The same three states the popup distinguishes, in about three words each:
    // a frame still downloading is not the same as a point with no radar over it.
    let text;
    let colour = null;
    if (!store.isLoaded(frame)) {
        text = store.hasFailed(frame) ? 'unavailable' : 'loading…';
    } else {
        const mmh = store.sample(frame, lng, lat);
        if (mmh === null) {
            text = 'no radar here';
        } else if (mmh < WET_THRESHOLD_MM_H) {
            text = 'dry';
        } else {
            text = `${formatRate(mmh)} mm/h`;
            colour = colorForRate(mmh);
        }
    }

    el.hoverValue.textContent = text;
    el.hoverSwatch.hidden = colour === null;
    if (colour) el.hoverSwatch.style.background = colour;
    el.hoverReadout.classList.toggle('hover-readout--muted', colour === null);
    el.hoverReadout.hidden = false;

    positionHoverReadout();
}

/** Offset from the cursor, flipped near an edge so it stays fully on screen. */
function positionHoverReadout() {
    const rect = map.getContainer().getBoundingClientRect();
    const chip = el.hoverReadout.getBoundingClientRect();
    const gap = 16;

    let left = rect.left + hoverPixel.x + gap;
    let top = rect.top + hoverPixel.y + gap;
    if (left + chip.width > window.innerWidth - 8) {
        left = rect.left + hoverPixel.x - chip.width - gap;
    }
    if (top + chip.height > window.innerHeight - 8) {
        top = rect.top + hoverPixel.y - chip.height - gap;
    }

    el.hoverReadout.style.left = `${Math.max(8, left)}px`;
    el.hoverReadout.style.top = `${Math.max(8, top)}px`;
}

// ---------------------------------------------------------------- inspect

function inspect(lngLat) {
    if (!inspectPopup) {
        inspectPopup = new maplibregl.Popup({
            closeButton: true,
            closeOnClick: false,
            offset: 14,
            className: 'radar-popup',
        });
        inspectPopup.on('close', () => {
            inspectPoint = null;
            inspectSeries = null;
            inspectMarker?.remove();
            inspectMarker = null;
        });
    }

    inspectPopup.setLngLat(lngLat);
    // addTo() on an already-open popup removes it first, which fires 'close' and
    // tears down the state we are in the middle of setting up.
    if (!inspectPopup.isOpen()) inspectPopup.addTo(map);

    if (!inspectMarker) {
        inspectMarker = new maplibregl.Marker({ color: '#f8fafc', scale: 0.7 })
            .setLngLat(lngLat)
            .addTo(map);
    } else {
        inspectMarker.setLngLat(lngLat);
    }

    inspectPoint = lngLat;
    // Sampling every frame is the expensive part, and it only depends on the
    // point - so do it once here rather than on every frame during playback.
    inspectSeries = store.series(lngLat.lng, lngLat.lat);
    updateInspectPopup();
}

function updateInspectPopup() {
    if (!inspectPoint || !inspectPopup || !inspectSeries) return;

    const series = inspectSeries;
    const current = series[currentIndex];

    // "No value" has two causes and they are not the same thing: a frame that
    // has not downloaded yet, and a point genuinely outside radar coverage.
    // Reporting the first as the second is how Rotterdam came to look like open
    // ocean while the prefetch was still running.
    const frame = store.frames[currentIndex];
    if (!store.isLoaded(frame)) {
        const message = store.hasFailed(frame)
            ? 'This frame could not be loaded.'
            : 'Loading this frame…';
        inspectPopup.setHTML(`<div class="popup-body"><p class="popup-empty">${message}</p></div>`);
        return;
    }

    if (current?.mmh === null) {
        inspectPopup.setHTML('<div class="popup-body"><p class="popup-empty">Outside radar coverage</p></div>');
        return;
    }

    const reference = store.manifest.reference_time ?? store.frames[0].t;
    const rate = current?.mmh ?? 0;

    inspectPopup.setHTML(`
        <div class="popup-body">
            <div class="popup-rate">
                <span class="popup-swatch" style="background:${colorForRate(Math.max(rate, 0.1))}"></span>
                <strong>${rate < WET_THRESHOLD_MM_H ? 'Dry' : `${rate.toFixed(1)} mm/h`}</strong>
                <span class="popup-at">at ${formatClock(current.t)}</span>
            </div>
            <canvas class="popup-spark" id="popup-spark" height="44"></canvas>
            <p class="popup-summary">${summarise(series, reference)}</p>
            ${trendLinkHtml()}
        </div>
    `);

    // The popup element only exists after setHTML, so draw the sparkline now.
    requestAnimationFrame(() => {
        const canvas = document.getElementById('popup-spark');
        if (canvas) drawSparkline(canvas, series);
    });
}

/**
 * Link through to the full page for the point that was clicked.
 *
 * The label has to name whatever the link actually opens: it used to say the
 * nearest sampled location while linking to the clicked coordinate, which read
 * as though the graphs would be for somewhere else.
 */
function trendLinkHtml() {
    if (inspectPoint) {
        const query = `lat=${inspectPoint.lat.toFixed(4)}&lon=${inspectPoint.lng.toFixed(4)}`;
        return `<a class="popup-link" href="forecast.html?${query}">` +
               'Full forecast for this point &rarr;</a>';
    }
    const point = nearestPublishedLocation(null);
    if (!point) return '';
    return `<a class="popup-link" href="forecast.html?location=${encodeURIComponent(point.name)}">` +
           `Full forecast for ${point.name} &rarr;</a>`;
}

function drawSparkline(canvas, series) {
    const width = canvas.clientWidth || 240;
    const height = canvas.clientHeight || 44;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const barWidth = width / series.length;
    series.forEach((point, i) => {
        const rate = point.mmh ?? 0;
        const x = i * barWidth;
        if (rate >= WET_THRESHOLD_MM_H) {
            // Height follows the same log scale as the colour ramp, so light
            // rain stays visible instead of collapsing to a flat line.
            const barHeight = Math.max(2, rampPosition(rate) * (height - 10));
            ctx.fillStyle = colorForRate(rate);
            ctx.fillRect(x, height - barHeight, Math.max(1, barWidth - 1), barHeight);
        } else {
            ctx.fillStyle = 'rgba(148, 163, 184, 0.25)';
            ctx.fillRect(x, height - 2, Math.max(1, barWidth - 1), 2);
        }
    });

    // Marker for the frame currently on screen.
    ctx.fillStyle = '#f8fafc';
    ctx.fillRect(currentIndex * barWidth, 0, Math.max(1.5, barWidth - 1), height);
}

/** Plain-language read of the point series — the same logic the widget needs. */
function summarise(series, reference) {
    const future = series.filter((p) => p.t >= reference && p.mmh !== null);
    if (!future.length) return 'No forecast for this point.';

    const rainingNow = (future[0].mmh ?? 0) >= WET_THRESHOLD_MM_H;
    if (rainingNow) {
        const clearing = future.find((p) => (p.mmh ?? 0) < WET_THRESHOLD_MM_H);
        return clearing
            ? `Raining now, easing off around ${formatClock(clearing.t)}.`
            : 'Raining for the rest of the forecast.';
    }

    const onset = future.find((p) => (p.mmh ?? 0) >= WET_THRESHOLD_MM_H);
    if (!onset) return 'Staying dry for the whole forecast.';
    const minutes = Math.round((onset.t - reference) / 60);
    const when = minutes < 90 ? `in ${minutes} min` : `at ${formatClock(onset.t)}`;
    return `Dry now — rain expected ${when} (${formatRate(onset.mmh)} mm/h).`;
}

// ---------------------------------------------------------------- trend panel

/**
 * The same charts as the standalone page, docked over the map.
 *
 * Optional and off by default: it covers a chunk of the map, and the map's own
 * job is the spatial picture. It shows the nearest *published* location rather
 * than wherever you clicked — ensemble spread only exists for the locations the
 * ingestor samples, and inventing a band for an arbitrary pixel would be a lie.
 */
const TREND_CHARTS = [
    { key: 'precipitation', label: 'Rainfall', colour: '#3b82f6', zeroFloor: true, minSpan: 1,
      format: (v) => (v >= 10 ? v.toFixed(0) : v.toFixed(1)) },
    { key: 'temperature', label: 'Temperature', colour: '#f97316', zeroFloor: false, minSpan: 4,
      format: (v) => v.toFixed(0) },
    { key: 'wind', label: 'Wind', colour: '#22c55e', zeroFloor: true, minSpan: 4,
      format: (v) => v.toFixed(0) },
];

let trendPoint = null;
let trendLocation = null;

/**
 * The published location closest to a map position.
 *
 * Ensemble spread only exists where the ingestor samples members, so a click on
 * open water cannot have its own forecast. Picking the nearest published point
 * and saying which one it is beats both a dead link and a fabricated band.
 * Equirectangular distance is ample over a country-sized domain.
 */
function nearestPublishedLocation(lngLat) {
    const points = store.manifest?.points ?? [];
    if (!points.length) return null;
    if (!lngLat) return points[0];

    const latitudeScale = Math.cos((lngLat.lat * Math.PI) / 180);
    let best = points[0];
    let bestDistance = Infinity;
    for (const point of points) {
        const dx = (point.lon - lngLat.lng) * latitudeScale;
        const dy = point.lat - lngLat.lat;
        const distance = dx * dx + dy * dy;
        if (distance < bestDistance) {
            bestDistance = distance;
            best = point;
        }
    }
    return best;
}

function renderTrend(document_) {
    trendPoint = document_;
    const { name, lat, lon, ad_hoc: adHoc } = document_.location;
    el.trendTitle.textContent = adHoc ? 'Clicked point' : name;
    el.trendSub.textContent = document_.summary?.text ?? '';
    el.trendLink.href = adHoc
        ? `forecast.html?lat=${lat.toFixed(4)}&lon=${lon.toFixed(4)}`
        : `forecast.html?location=${encodeURIComponent(name)}`;
    el.trendSub.title = `${lat.toFixed(4)}, ${lon.toFixed(4)}`;

    el.trendBody.innerHTML = '';
    for (const config of TREND_CHARTS) {
        const block = document_[config.key];
        if (!block) continue;
        const wrapper = document.createElement('div');
        wrapper.className = 'trend-chart';
        const heading = document.createElement('h3');
        heading.textContent = `${config.label} (${block.unit})`;
        const chart = document.createElement('div');
        chart.className = 'chart';
        wrapper.append(heading, chart);
        el.trendBody.appendChild(wrapper);
        renderBandChart(chart, block.series, {
            colour: config.colour,
            zeroFloor: config.zeroFloor,
            minSpan: config.minSpan,
            formatValue: config.format,
            height: 120,
            now: document_.reference_time,
        });
    }
}

async function openTrend(lngLat = inspectPoint) {
    // A click means that spot; with no click, fall back to a published location.
    if (lngLat) {
        el.trendPanel.hidden = false;
        const key = `${lngLat.lng.toFixed(4)},${lngLat.lat.toFixed(4)}`;
        if (trendLocation === key && trendPoint) return;
        try {
            trendLocation = key;
            renderTrend(await pointForCoordinates(
                { lat: lngLat.lat, lon: lngLat.lng },
                { frames: store, manifest: store.manifest }
            ));
        } catch (error) {
            trendLocation = null;
            el.trendBody.innerHTML = `<p class="chart-empty">Could not load: ${error.message}</p>`;
        }
        return;
    }

    const point = nearestPublishedLocation(null);
    if (!point) {
        el.trendBody.innerHTML =
            '<p class="chart-empty">No point forecasts are published. ' +
            'Set WIDGET_LOCATIONS on the server to add one.</p>';
        el.trendTitle.textContent = 'Forecast trend';
        el.trendSub.textContent = '';
        el.trendPanel.hidden = false;
        return;
    }
    el.trendPanel.hidden = false;
    if (trendLocation === point.name && trendPoint) return; // already showing it
    try {
        trendLocation = point.name;
        renderTrend(await pointForName(point.name));
    } catch (error) {
        trendLocation = null;
        el.trendBody.innerHTML = `<p class="chart-empty">Could not load: ${error.message}</p>`;
    }
}

function closeTrend() {
    el.trendPanel.hidden = true;
    el.trendToggle.checked = false;
    trendLocation = null;
}

// ---------------------------------------------------------------- about

/** KNMI's dataset pages follow a fixed slug: underscores and dots become dashes. */
function knmiDatasetUrl(dataset, version) {
    const slug = `${dataset}-${version}`.replace(/[_.]/g, '-');
    return `https://dataplatform.knmi.nl/dataset/${slug}`;
}

const DATASET_NOTES = {
    forecast:
        'The seamless ensemble: KNMI runs pySTEPS to blend radar extrapolation with the ' +
        'HARMONIE-AROME ensemble, publishing 20 members every 5 minutes out to 6 hours. ' +
        'The map shows the median of those members.',
    observed:
        'The real-time radar composite, corrected against rain gauges, from Dutch, Belgian ' +
        'and German radars. This is measurement rather than forecast, and it fills the part ' +
        'of the timeline before now.',
};

/**
 * Build the data-source list from the manifest, so the About box describes what
 * is actually being served rather than what was true when it was written.
 */
function describeSources(manifest) {
    const source = manifest.source ?? {};
    if (el.aboutDatasets) {
        const entries = [
            { dataset: source.dataset, version: source.version, kind: 'forecast' },
            { dataset: source.observed, version: '1.0', kind: 'observed' },
        ].filter((entry) => entry.dataset);

        el.aboutDatasets.innerHTML = '';
        for (const entry of entries) {
            const item = document.createElement('li');
            const link = document.createElement('a');
            link.href = knmiDatasetUrl(entry.dataset, entry.version);
            link.target = '_blank';
            link.rel = 'noopener';
            link.textContent = entry.dataset;
            const kind = document.createElement('span');
            kind.className = 'about-kind';
            kind.textContent = entry.kind;
            const note = document.createElement('p');
            note.textContent = DATASET_NOTES[entry.kind] ?? '';
            item.append(link, kind, note);
            el.aboutDatasets.appendChild(item);
        }
    }

    if (el.aboutSource) {
        const parts = [source.attribution, source.product].filter(Boolean);
        el.aboutSource.textContent = parts.length
            ? `${parts.join(' — ')}. Updated every 5 minutes.`
            : '';
    }
}

function openAbout() {
    closePopovers();
    el.aboutBackdrop.hidden = false;
    el.aboutModal.hidden = false;
    el.infoBtn.setAttribute('aria-expanded', 'true');
    el.aboutClose.focus();
}

function closeAbout() {
    el.aboutBackdrop.hidden = true;
    el.aboutModal.hidden = true;
    el.infoBtn.setAttribute('aria-expanded', 'false');
}

const aboutIsOpen = () => !el.aboutModal.hidden;

// ---------------------------------------------------------------- chrome

function showBanner(message, retryable, tone = 'warn') {
    el.bannerText.textContent = message;
    el.bannerRetry.hidden = !retryable;
    el.banner.classList.toggle('banner--info', tone === 'info');
    el.banner.hidden = false;
}

function hideBanner() {
    el.banner.hidden = true;
    el.banner.classList.remove('banner--info');
}

function openPopover(popover, button) {
    closePopovers(popover);
    popover.hidden = false;
    button.setAttribute('aria-expanded', 'true');
}

function closePopovers(except) {
    for (const [popover, button] of [[el.settingsPopover, el.settingsBtn]]) {
        if (popover === except) continue;
        popover.hidden = true;
        button.setAttribute('aria-expanded', 'false');
    }
}

function togglePopover(popover, button) {
    if (popover.hidden) openPopover(popover, button);
    else closePopovers();
}

function updateSliderFill(input) {
    const min = Number(input.min) || 0;
    const max = Number(input.max) || 100;
    const percent = ((Number(input.value) - min) / (max - min)) * 100;
    input.style.setProperty('--progress', `${percent}%`);
}

// ---------------------------------------------------------------- formatting

function formatClock(seconds) {
    return new Date(seconds * 1000).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
    });
}

function formatOffset(diffSeconds) {
    if (diffSeconds === 0) return 'now';
    const sign = diffSeconds > 0 ? '+' : '−';
    const abs = Math.abs(diffSeconds);
    const hours = Math.floor(abs / 3600);
    const minutes = Math.round((abs % 3600) / 60);
    if (hours && minutes) return `${sign}${hours}h ${minutes}m`;
    if (hours) return `${sign}${hours}h`;
    return `${sign}${minutes}m`;
}

function isHourBoundary(t) {
    return t % 3600 === 0;
}

// ---------------------------------------------------------------- events

el.slider.addEventListener('input', (event) => {
    showFrame(Number(event.target.value), { fromSlider: true });
});
el.stepBack.addEventListener('click', () => step(-1));
el.stepForward.addEventListener('click', () => step(1));
el.playBtn.addEventListener('click', () => setPlaying(!isPlaying));
el.bannerRetry.addEventListener('click', () => {
    hideBanner();
    load({ initial: true }).catch(() => {});
});

el.speedSlider.addEventListener('input', (event) => {
    updateSliderFill(event.target);
    el.speedLabel.textContent = `${event.target.value} fps`;
    if (isPlaying) setPlaying(true); // restart the timer at the new interval
});

el.opacitySlider.addEventListener('input', (event) => {
    const value = Number(event.target.value);
    updateSliderFill(event.target);
    el.opacityLabel.textContent = `${value}%`;
    if (map.getLayer('radar-layer')) {
        map.setPaintProperty('radar-layer', 'raster-opacity', value / 100);
    }
});

el.basemapSelect.addEventListener('change', (event) => {
    setBasemap(event.target.value);
});

/**
 * Swap the basemap. RasterTileSource.setTiles only exists from MapLibre 4
 * onwards, so rebuild the ground and label layers around the radar layer, which
 * stays put in the middle of the stack.
 */
function setBasemap(name) {
    const config = BASEMAPS[name];
    if (!config || !map.getSource('basemap')) return;

    for (const id of ['labels', 'basemap']) {
        if (map.getLayer(id)) map.removeLayer(id);
        if (map.getSource(id)) map.removeSource(id);
    }

    map.addSource('basemap', {
        type: 'raster',
        tiles: [config.ground],
        tileSize: 256,
        attribution: attributionFor(config),
    });
    // Ground goes below everything that is left, i.e. below the radar.
    map.addLayer(
        {
            id: 'basemap', type: 'raster', source: 'basemap',
            ...(config.groundPaint ? { paint: config.groundPaint } : {}),
        },
        map.getStyle().layers[0]?.id
    );

    if (config.labels) {
        map.addSource('labels', { type: 'raster', tiles: [config.labels], tileSize: 256 });
        map.addLayer({
            id: 'labels', type: 'raster', source: 'labels',
            ...(config.labelPaint ? { paint: config.labelPaint } : {}),
        });
    }

    document.body.classList.toggle('theme-light', !!config.lightUi);
    try {
        localStorage.setItem(BASEMAP_STORAGE_KEY, name);
    } catch {
        // Not being able to remember the choice is not worth an error.
    }
}

el.settingsBtn.addEventListener('click', () => togglePopover(el.settingsPopover, el.settingsBtn));
el.infoBtn.addEventListener('click', () => (aboutIsOpen() ? closeAbout() : openAbout()));
el.aboutClose.addEventListener('click', closeAbout);
el.trendClose.addEventListener('click', closeTrend);
el.trendToggle.addEventListener('change', () => {
    if (el.trendToggle.checked) openTrend();
    else closeTrend();
});
el.aboutBackdrop.addEventListener('click', closeAbout);

el.collapseBtn.addEventListener('click', () => {
    const collapsed = el.panel.classList.toggle('is-collapsed');
    el.collapseBtn.setAttribute('aria-expanded', String(!collapsed));
    el.collapseBtn.setAttribute('aria-label', collapsed ? 'Expand panel' : 'Collapse panel');
});

el.locateBtn.addEventListener('click', () => {
    if (!navigator.geolocation) {
        showBanner('This browser cannot share a location.', false);
        return;
    }
    el.locateBtn.classList.add('is-busy');
    navigator.geolocation.getCurrentPosition(
        (position) => {
            el.locateBtn.classList.remove('is-busy');
            const lngLat = new maplibregl.LngLat(position.coords.longitude, position.coords.latitude);
            map.easeTo({ center: lngLat, zoom: Math.max(map.getZoom(), 8) });
            inspect(lngLat);
        },
        () => {
            el.locateBtn.classList.remove('is-busy');
            showBanner('Could not get your location.', false);
        },
        { enableHighAccuracy: false, timeout: 8000, maximumAge: 60000 }
    );
});

map.on('click', (event) => {
    closePopovers();
    inspect(event.lngLat);
    if (!el.trendPanel.hidden) openTrend(event.lngLat);
});
map.on('load', () => {
    if (store.manifest) setupRadarLayer();
});
// Registered directly rather than from 'load': nothing here needs the style, and
// 'load' can simply never arrive - it did not in a sandbox where the basemap
// tiles were unreachable, which would have silently cost the readout entirely.
setupHoverReadout();

document.addEventListener('click', (event) => {
    if (event.target.closest('.popover') || event.target.closest('.icon-btn')) return;
    closePopovers();
});

document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
        closeAbout();
        closePopovers();
        return;
    }
    // The map's shortcuts should not fire while the About dialog has focus.
    if (aboutIsOpen()) return;
    // Leave the arrow keys alone while a slider or select has focus.
    const tag = event.target.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;

    switch (event.key) {
        case ' ':
            event.preventDefault();
            setPlaying(!isPlaying);
            break;
        case 'ArrowLeft':
            event.preventDefault();
            setPlaying(false);
            step(-1);
            break;
        case 'ArrowRight':
            event.preventDefault();
            setPlaying(false);
            step(1);
            break;
        case 'Home':
            event.preventDefault();
            showFrame(0);
            break;
        case 'End':
            event.preventDefault();
            showFrame(store.frames.length - 1);
            break;
        default:
            break;
    }
});

const redrawLegend = () => renderLegend(el.legendCanvas, el.legendLabels);
window.addEventListener('resize', redrawLegend);

// The map container can change size without a window resize event (embedded in
// a split pane, panel collapsing, phone rotation), and MapLibre only listens for
// the window one.
new ResizeObserver(() => map.resize()).observe(document.getElementById('map'));
new ResizeObserver(redrawLegend).observe(el.legendCanvas);

updateSliderFill(el.speedSlider);
updateSliderFill(el.opacitySlider);
redrawLegend();
boot().catch(() => {});
