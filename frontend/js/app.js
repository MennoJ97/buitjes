import { RadarRenderer, FrameStore } from './radar.js';
import { renderLegend, colorForRate, rampPosition, formatRate } from './ramp.js';
import { renderBandChart } from './chart.js';
import { centreOf, pointForName, pointForCoordinates, summariseFrames } from './point.js';
import { apiFetch } from './key.js';
import { fetchHealth, readHealth, describeAge, HEALTH_POLL_MS } from './health.js';
import { formatClock } from './time.js';
import {
    BASEMAPS, BASEMAP_STORAGE_KEY, OWN_CREDIT,
    applyStyleOverrides, labelLayerId, storedBasemap, styleFor,
} from './basemap.js';

/** How each timeline zone is described in the UI. */
const ZONES = {
    observed: { label: 'Observed', hint: 'Radar measurements' },
    nowcast: { label: 'Nowcast', hint: 'Mostly radar extrapolation' },
    forecast: { label: 'Forecast', hint: 'Increasingly weather-model driven' },
};

/**
 * setStyle discards the whole style, radar included. transformStyle hands us the
 * replacement just before it is applied, which is the one moment the radar can
 * be put back without a frame of bare basemap flashing in between.
 *
 * Nothing to carry on the very first switch after a cold start, when the radar
 * has not been added yet — setupRadarLayer adds it once the data arrives.
 */
function carryRadarOver(previous, next) {
    const source = previous.sources?.radar;
    const layer = previous.layers?.find((candidate) => candidate.id === 'radar-layer');
    if (!source || !layer) return next;

    const labels = labelLayerId(next.layers);
    const at = labels ? next.layers.findIndex((candidate) => candidate.id === labels) : next.layers.length;
    return {
        ...next,
        sources: { ...next.sources, radar: source },
        layers: [...next.layers.slice(0, at), layer, ...next.layers.slice(at)],
    };
}

/** Run `fn` once the current style is loaded, whether or not it already is. */
function onStyleReady(fn) {
    if (map.isStyleLoaded()) fn();
    else map.once('styledata', fn);
}

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
    refTimeWrap: $('ref-time-wrap'),
    refTimeAge: $('ref-time-age'),
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
    hoverBand: $('hover-band'),
    scaleTitle: $('scale-title'),
    pctSwitch: $('pct-switch'),
    aboutSpreadRadius: $('about-spread-radius'),
};

const store = new FrameStore();
// A spread frame lands after the rate it belongs to: redraw so the band under
// the cursor, or the field itself when an end of the ensemble is being shown,
// appears as soon as it can rather than on the next mouse move.
store.onSpreadLoaded = (frame) => {
    if (store.frames[currentIndex] === frame) scheduleRender();
};
let renderer;
let currentIndex = 0;
let isPlaying = false;
let playTimer = null;
let inspectMarker = null;
let inspectPopup = null;
let inspectPoint = null;
let inspectSeries = null;
/** Elements inside the open popup, so a frame change can write to them
 *  instead of replacing them. Null whenever the body is not the graph. */
let inspectDom = null;
/** Which body is currently rendered: 'graph', 'loading', 'failed' or
 *  'uncovered'. Only a change of mode justifies rebuilding the DOM. */
let inspectMode = null;
/** Which of the ensemble the map paints: 'mid', 'p10' or 'p90'. */
let currentBand = 'mid';
let refreshTimer = null;
let warmupTimer = null;
let healthTimer = null;
/** Whether the banner currently on screen is ours, so we know we may replace
 *  or clear it. A real error outranks "the data is old" and must not be
 *  overwritten by the next health poll sixty seconds later. */
let healthBannerUp = false;

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
    style: styleFor(initialConfig),
    center: [5.2913, 52.1326],
    zoom: 6.4,
    // Added by hand below instead: on MapLibre 3 this option is only read as a
    // boolean, so the `{ compact: true }` it used to be given did nothing.
    attributionControl: false,
});
// Our own credits are the same whichever basemap is showing, so they belong on
// the control rather than being pasted onto every provider's line — which also
// means they survive a vector style, whose sources we do not build.
map.addControl(
    new maplibregl.AttributionControl({ compact: true, customAttribution: OWN_CREDIT })
);
el.basemapSelect.value = initialBasemap;
document.body.classList.toggle('theme-light', !!initialConfig.lightUi);
onStyleReady(() => applyStyleOverrides(map, initialConfig));
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

// ---------------------------------------------------------------- boot

async function boot() {
    try {
        renderer = new RadarRenderer(el.glcanvas);
    } catch (err) {
        showBanner(err.message, false);
        return;
    }
    // Started before the first load, not after it. A first load that threw used
    // to take the periodic refresh down with it: the interval was only ever
    // created on the line after the await, so a page that failed once would
    // sit there for the rest of the session — and even a successful Retry
    // would not bring the refresh back, because that line had been skipped.
    if (!refreshTimer) {
        refreshTimer = setInterval(() => load({ initial: false }).catch(() => {}), REFRESH_INTERVAL_MS);
    }
    // Polled separately from the manifest, and more often: the reload is a
    // heavy thing to do every minute, but "how old is this" changes by the
    // minute and is the one question a frozen map cannot answer for itself.
    if (!healthTimer) {
        healthTimer = setInterval(updateHealth, HEALTH_POLL_MS);
    }
    await load({ initial: true });
}

/**
 * Say out loud when the forecast has stopped arriving.
 *
 * Without this the map degrades silently: the last cycle keeps being drawn,
 * the timeline still animates, and the only clue is a clock in the corner that
 * the reader has to notice and subtract from the current time themselves.
 */
async function updateHealth() {
    const { stale, age, detail } = readHealth(await fetchHealth());

    el.refTimeWrap.classList.toggle('is-stale', stale);
    el.refTimeAge.textContent = stale ? `\u00b7 ${describeAge(age)} old` : '';
    // The server's own wording, for anyone who wants the precise complaint.
    el.refTimeWrap.title = stale ? detail : '';

    if (!stale) {
        if (healthBannerUp) hideBanner();
        return;
    }
    // Only claim the banner if nothing more urgent is using it.
    if (el.banner.hidden || healthBannerUp) {
        showBanner(
            `No new forecast for ${describeAge(age)} — KNMI publishes every 5 minutes.`
            + ' Showing the last one that arrived.',
            false,
            'stale'
        );
    }
}

async function load({ initial }) {
    try {
        const response = await apiFetch('/api/config', { cache: 'no-store' });
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
        setupBandSwitch();
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

        // A cycle the reader is already looking at through one end of the
        // ensemble has to keep those frames coming, or the map would go blank
        // on the new manifest and stay blank.
        if (currentBand !== 'mid') await store.prefetchSpread();

        // The frame list just changed, so any cached point series is stale —
        // and with it the summary and the bars, which are built once per series.
        if (inspectPoint) {
            inspectSeries = store.series(inspectPoint.lng, inspectPoint.lat);
            inspectMode = null;
        }

        showFrame(currentIndex);
        // After hideBanner() above, so a stale reading can claim the banner
        // that the successful load just cleared.
        await updateHealth();
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
    // 'styledata' rather than 'load', because a basemap switch replaces the
    // style without the map ever loading a second time.
    if (!map.isStyleLoaded()) {
        map.once('styledata', setupRadarLayer);
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
        labelLayerId(map.getStyle().layers)
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
    describeFrame(frame);

    scheduleRender();
}

/**
 * The badge beside the clock: which kind of frame this is, or that it is not
 * really a frame at all.
 *
 * KNMI publishes a timestep with no ensemble behind it about once a cycle, and
 * the ingestor stands in for it with the average of the steps five minutes
 * either side rather than let it read as five minutes of nationwide dry. That
 * is worth saying, and it takes the badge rather than sitting next to it: the
 * readout is a fixed width so that scrubbing does not shuffle the panel about,
 * and a badge that appears and disappears per frame is exactly the jitter that
 * width exists to prevent. The zone bar under the slider still says which part
 * of the timeline this is, so the kind is not lost — only demoted, for one
 * frame, under the more important fact that nobody forecast it.
 */
function describeFrame(frame) {
    const zone = ZONES[frame.kind];
    const kind = zone?.label ?? frame.kind;
    el.kindBadge.textContent = frame.estimated ? 'Estimated' : kind;
    el.kindBadge.className = `kind-badge kind-badge--${frame.estimated ? 'estimated' : frame.kind}`;
    el.kindBadge.title = frame.estimated
        ? `${kind}, estimated: KNMI published no ensemble for this step, so this `
          + 'is the average of the steps five minutes either side.'
        : zone?.hint ?? '';
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

    // Which of the twenty runs to paint. "Expected" is the rain frame the map
    // has always drawn; the two ends come out of the spread frame instead, a
    // different image and a different encoding, so the shader is told both.
    // `frame.spread` and not merely `spread`: the observed hour has no companion
    // frame, because a radar measurement has no ensemble to take percentiles
    // of. Without that check, scrubbing back into the measured past while Low
    // or High is selected asked for a file that does not exist and blanked the
    // map. There the measurement is the only answer, so it is the one drawn.
    const spread = store.spreadInfo;
    const wantsBand = currentBand !== 'mid' && spread && frame.spread;
    const image = wantsBand ? store.requestSpread(frame) : store.imageFor(frame);
    updateScaleTitle(wantsBand);

    if (image) {
        if (wantsBand) {
            renderer.readSpreadChannel(spread.percentiles.indexOf(bandPercentile()), spread);
        } else {
            renderer.readRainFrames();
        }
        renderer.draw(image);
    } else {
        // Nothing to show yet: a spread frame still in flight, or a rain frame
        // that never arrived. Either way an empty map beats a stale one.
        renderer.clear();
    }

    if (inspectPoint) updateInspectPopup();
    // The cursor has not moved, but the rain under it has.
    if (hoverPixel) updateHoverReadout();
}

/** Which percentile the switch is asking for, as a number. */
function bandPercentile() {
    return currentBand === 'p10' ? 10 : 90;
}

const BAND_TITLES = {
    mid: 'Rainfall rate',
    p10: 'Rainfall rate — low',
    p90: 'Rainfall rate — high',
};

/**
 * The legend names what is on screen, not what the switch is set to.
 *
 * They part company over the measured hour, which has no ensemble and so is
 * drawn as itself whatever the switch says. A bar labelled "high" above a radar
 * measurement would be claiming a percentile of a single number.
 */
function updateScaleTitle(showingBand) {
    el.scaleTitle.textContent = showingBand ? BAND_TITLES[currentBand] : BAND_TITLES.mid;
}

/**
 * Switch which of the ensemble the map paints.
 *
 * Playback needs every step's spread frame, not just the one on screen, so
 * moving off "Expected" prefetches them — the one moment the reader has said
 * they want this layer. Moving back never fetches anything.
 */
async function setBand(band) {
    if (band === currentBand) return;
    currentBand = band;
    for (const option of el.pctSwitch.querySelectorAll('.pct-option')) {
        const active = option.dataset.band === band;
        option.classList.toggle('is-active', active);
        option.setAttribute('aria-pressed', String(active));
    }
    if (band !== 'mid') {
        el.loading.hidden = false;
        await store.prefetchSpread((progress) => {
            el.loading.style.setProperty('--progress', `${Math.round(progress * 100)}%`);
        });
        el.loading.hidden = true;
    }
    scheduleRender();
}

/** Show the switch only where the server publishes a spread layer at all. */
function setupBandSwitch() {
    const spread = store.spreadInfo;
    el.pctSwitch.hidden = !spread;
    if (!spread) return;

    const radius = Math.round(spread.radius_km);
    el.pctSwitch.title =
        `What the twenty ensemble members say, within ${radius} km: `
        + `Low is p${spread.percentiles[0]}, High is p${spread.percentiles[2]}. `
        + 'Expected is the field the map normally draws. The measured hour has '
        + 'no ensemble, so it is shown as itself whichever you pick.';
    // A span of its own rather than a search-and-replace across the paragraph:
    // the prose wraps, so the phrase is not contiguous in textContent, and
    // rewriting the paragraph would flatten the emphasis inside it.
    if (el.aboutSpreadRadius) el.aboutSpreadRadius.textContent = `${radius} km`;
    if (el.pctSwitch.dataset.wired) return;
    el.pctSwitch.dataset.wired = '1';
    for (const option of el.pctSwitch.querySelectorAll('.pct-option')) {
        option.addEventListener('click', () => setBand(option.dataset.band));
    }
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
    let band = '';
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
        if (mmh !== null) band = describeBand(frame, lng, lat);
    }

    el.hoverValue.textContent = text;
    el.hoverBand.textContent = band;
    if (band && store.spreadInfo) {
        el.hoverReadout.title =
            `The ensemble's 10th to 90th percentile within ${Math.round(store.spreadInfo.radius_km)}`
            + ' km, each member taken at its wettest inside that radius.';
    }
    el.hoverBand.hidden = !band;
    el.hoverSwatch.hidden = colour === null;
    if (colour) el.hoverSwatch.style.background = colour;
    el.hoverReadout.classList.toggle('hover-readout--muted', colour === null);
    el.hoverReadout.hidden = false;

    positionHoverReadout();
}

/**
 * The ensemble's range under the cursor, as "0.1–2.4".
 *
 * Asks for the spread frame as a side effect: the first hover on a step fetches
 * it, and `onSpreadLoaded` redraws the readout when it lands, so the band
 * appears a moment after the rate rather than holding it up. A reader who never
 * hovers never downloads any of this.
 *
 * Empty when the band is entirely dry — "0.3 mm/h · 0–0" is noise, and a point
 * every member agrees is dry has no disagreement worth reporting.
 *
 * Prefixed "nearby", and that word is doing real work. Each member is taken at
 * its wettest within the published radius before the percentiles are read, so
 * the band sits *above* the rate under the cursor as often as around it — 3.9
 * mm/h against 4.8–7.6 is not a contradiction, it is a pixel with heavier rain
 * a couple of kilometres away. Without the word it reads as a bug.
 */
function describeBand(frame, lng, lat) {
    if (!store.spreadInfo) return '';
    // The measured hour has no band and never will: radar is one number, not
    // twenty. Saying so beats saying nothing, which reads as a missing feature
    // — the app opens on the newest observed frame, so an empty readout there
    // is the first thing anyone sees.
    if (frame.kind === 'observed') return 'measured';
    store.requestSpread(frame);
    const band = store.sampleSpread(frame, lng, lat);
    if (!band) return '';
    const low = band.p10 ?? 0;
    const high = band.p90 ?? 0;
    if (high < WET_THRESHOLD_MM_H) return '';
    return `nearby ${formatRate(low)}–${formatRate(high)}`;
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
            inspectDom = null;
            inspectMode = null;
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
    // The summary and the bars belong to the series, not the frame, so a new
    // point is the one case that genuinely has to rebuild the body.
    inspectMode = null;
    updateInspectPopup();
}

/**
 * Reflect the current frame in the open popup.
 *
 * Called on every frame change, which during a timeline drag is dozens of times
 * a second — so this must not rebuild the popup. It used to: one `setHTML` per
 * frame, throwing away the body and building a fresh `<canvas>` each time, with
 * the redraw deferred to `requestAnimationFrame`. That leaves at least one
 * painted frame where the canvas is in the DOM but has not been drawn yet, and
 * a `<canvas>` with no width attribute defaults to 300x150 — so the sparkline
 * blanked and re-appeared on every step. Scrubbing strobed instead of moving.
 *
 * Now the body is built once and only the four things that actually depend on
 * the frame are written: the swatch, the rate, the clock, and the marker.
 */
function updateInspectPopup() {
    if (!inspectPoint || !inspectPopup || !inspectSeries) return;

    const frame = store.frames[currentIndex];
    const current = inspectSeries[currentIndex];

    // "No value" has two causes and they are not the same thing: a frame that
    // has not downloaded yet, and a point genuinely outside radar coverage.
    // Reporting the first as the second is how Rotterdam came to look like open
    // ocean while the prefetch was still running.
    const mode = !store.isLoaded(frame)
        ? (store.hasFailed(frame) ? 'failed' : 'loading')
        : current?.mmh === null ? 'uncovered' : 'graph';

    if (mode !== inspectMode) {
        buildInspectBody(mode);
        inspectMode = mode;
    }
    if (mode !== 'graph') return;

    const rate = current?.mmh ?? 0;
    inspectDom.swatch.style.background = colorForRate(Math.max(rate, 0.1));
    inspectDom.rate.textContent =
        rate < WET_THRESHOLD_MM_H ? 'Dry' : `${rate.toFixed(1)} mm/h`;
    inspectDom.at.textContent = `at ${formatClock(current.t)}`;
    drawSparkline(inspectDom.spark, inspectSeries);
}

const INSPECT_MESSAGES = {
    loading: 'Loading this frame…',
    failed: 'This frame could not be loaded.',
    uncovered: 'Outside radar coverage',
};

/**
 * Replace the popup body. Only on a mode change or a new point.
 *
 * The elements are looked up straight after `setHTML` rather than in a
 * `requestAnimationFrame`: MapLibre inserts the markup synchronously, so they
 * are already there, and drawing now means the sparkline is never painted
 * blank — not even on the first frame after a click.
 */
function buildInspectBody(mode) {
    if (mode !== 'graph') {
        inspectDom = null;
        inspectPopup.setHTML(
            `<div class="popup-body"><p class="popup-empty">${INSPECT_MESSAGES[mode]}</p></div>`
        );
        return;
    }

    const reference = store.manifest.reference_time ?? store.frames[0].t;
    inspectPopup.setHTML(`
        <div class="popup-body">
            <div class="popup-rate">
                <span class="popup-swatch" id="popup-swatch"></span>
                <strong id="popup-rate-value"></strong>
                <span class="popup-at" id="popup-at"></span>
            </div>
            <canvas class="popup-spark" id="popup-spark" height="44"></canvas>
            <p class="popup-summary">${summariseFrames(store, inspectPoint.lng, inspectPoint.lat, reference)}</p>
            ${trendLinkHtml()}
        </div>
    `);

    const element = inspectPopup.getElement();
    inspectDom = {
        swatch: element.querySelector('#popup-swatch'),
        rate: element.querySelector('#popup-rate-value'),
        at: element.querySelector('#popup-at'),
        spark: element.querySelector('#popup-spark'),
    };
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
    // Assigning width or height resets the backing store even when the value
    // is unchanged, and this now runs on every frame of a drag rather than
    // once per popup. Only pay for it when the size has actually changed.
    const backingWidth = Math.round(width * dpr);
    const backingHeight = Math.round(height * dpr);
    if (canvas.width !== backingWidth) canvas.width = backingWidth;
    if (canvas.height !== backingHeight) canvas.height = backingHeight;
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
            // The same choice of line and band the standalone page makes: a
            // clicked point's series carries no `median` at all, so defaulting
            // to one drew nothing here while the full page drew the forecast.
            // No fallback label — `config.label` is this card's heading.
            ...centreOf(block),
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
    el.banner.classList.toggle('banner--stale', tone === 'stale');
    el.banner.hidden = false;
    // Whoever put the banner up owns it. An error raised while a stale notice
    // was showing must not be replaced by the next health poll a minute later.
    healthBannerUp = tone === 'stale';
}

function hideBanner() {
    el.banner.hidden = true;
    el.banner.classList.remove('banner--info', 'banner--stale');
    healthBannerUp = false;
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

/** The basemap whose style is loading, and the one chosen while it loaded. */
let loadingBasemap = null;
let queuedBasemap = null;

/**
 * Swap the basemap. Replacing the whole style is what lets a vector basemap sit
 * in the same list as the raster ones — its ground is dozens of layers, not the
 * one that swapping tile URLs in place could reach.
 *
 * Starting a second setStyle before the first has loaded leaves MapLibre unable
 * to diff the two and, in practice, leaves the map blank — which is exactly what
 * arrowing down the picker does. So a choice made mid-load is queued rather than
 * applied, and only the last one is; the UI has already moved on regardless.
 */
function setBasemap(name) {
    const config = BASEMAPS[name];
    if (!config) return;

    document.body.classList.toggle('theme-light', !!config.lightUi);
    try {
        localStorage.setItem(BASEMAP_STORAGE_KEY, name);
    } catch {
        // Not being able to remember the choice is not worth an error.
    }

    if (loadingBasemap) queuedBasemap = name;
    else applyBasemap(name);
}

function applyBasemap(name) {
    const config = BASEMAPS[name];
    loadingBasemap = name;
    map.setStyle(styleFor(config), { transformStyle: carryRadarOver });
    // Not onStyleReady: right after setStyle the *old* style can still report
    // itself loaded, which would repaint layers that are about to be replaced.
    map.once('styledata', () => {
        loadingBasemap = null;
        applyStyleOverrides(map, config);
        // A new style brings a new credits line, opened by MapLibre again.
        collapseAttribution();
        const queued = queuedBasemap;
        queuedBasemap = null;
        if (queued && queued !== name) applyBasemap(queued);
    });
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

/**
 * Keep MapLibre's bottom controls clear of the playback panel.
 *
 * The panel is centred and up to 960px wide, so on a wide screen the bottom
 * corners are free and the controls belong in them. It is only once the panel
 * grows towards the full width that it starts covering the zoom buttons and the
 * credits.
 *
 * Two ways out, and the cheap one is preferred. Expanded, the credits want
 * about 420px on one line, which fits beside the panel only on a very wide
 * window — but they wrap perfectly well, exactly as they already do on a phone
 * where the viewport forces it. So the first move is to cap their width to the
 * gap beside the panel and let them use two or three lines. Nothing moves;
 * the corner stays a corner.
 *
 * Lifting the whole corner is the fallback, for when that gap is too narrow to
 * wrap into — a phone, where the panel really does span the window. Lifting
 * unconditionally was the old behaviour and it is the worse one: at 1280 the
 * credits jumped 176px up the side of the map to buy a line of text they did
 * not need.
 *
 * Measured rather than set by breakpoint, because the panel changes height when
 * it collapses or the legend rewraps, and the credits change width when a
 * vector style's TileJSON finally loads. No breakpoint tracks either.
 */

/** Below this a wrapped credit line is more hyphen than word, so lift instead. */
const MIN_ATTRIB_WIDTH = 140;

function updateControlClearance() {
    const panel = el.panel.getBoundingClientRect();
    const clearance = Math.round(window.innerHeight - panel.top + 8);

    for (const corner of ['left', 'right']) {
        const node = document.querySelector(`.maplibregl-ctrl-bottom-${corner}`);
        if (!node) continue;

        // Reset both before measuring, so the measurement never depends on the
        // answer it is about to produce.
        node.style.setProperty('--corner-lift', '0px');
        node.style.removeProperty('--attrib-max-width');

        const box = node.getBoundingClientRect();
        const collides = box.width > 0 && panel.right > box.left && panel.left < box.right;
        if (!collides) continue;

        // MapLibre gives its control containers a 10px margin, which is inside
        // the gap but outside the chip's own box.
        const gap = Math.round(
            corner === 'right' ? window.innerWidth - panel.right : panel.left
        ) - 16;
        if (gap >= MIN_ATTRIB_WIDTH) {
            node.style.setProperty('--attrib-max-width', `${gap}px`);
        } else {
            node.style.setProperty('--corner-lift', `${clearance}px`);
        }
    }
}

/**
 * Start the credits collapsed to their ⓘ.
 *
 * MapLibre's `compact` attribution is a <details> that it opens on load, so the
 * "compact" control is a 400-500px bar across the bottom of the map — wide
 * enough to run under the panel on any normal window. Collapsed it is a single
 * button, and one click still shows every credit in full, which is what the
 * licences ask for.
 *
 * Re-applied on styledata because switching basemaps rebuilds the control.
 *
 * Both halves of MapLibre's open/closed state have to go. It keeps the state
 * twice — the `open` attribute, which decides whether the credits render, and
 * the `compact-show` class, which its CSS hangs the expanded padding on — and
 * its click handler branches on the class alone. Clearing only `open` left the
 * class saying "expanded", so the chip sat there as an empty pill, and the
 * first click read the stale class, took the collapse branch, and cancelled
 * out the browser's own <details> toggle. It took two clicks to open.
 */
function collapseAttribution() {
    for (const node of document.querySelectorAll('details.maplibregl-ctrl-attrib')) {
        node.removeAttribute('open');
        node.classList.remove('maplibregl-compact-show');
    }
}

new ResizeObserver(updateControlClearance).observe(el.panel);
window.addEventListener('resize', updateControlClearance);

/**
 * Watch the corner containers themselves, not just the panel.
 *
 * A vector style's credits are assembled from its TileJSON once the source
 * loads, which is long after the page settles and never resizes the panel — so
 * a one-off measurement decided "no collision" against a chip that had not yet
 * grown to its full width. Observing the corners catches that, and catches the
 * reader expanding the credits by hand.
 */
const clearanceObserver = new ResizeObserver(updateControlClearance);
for (const corner of ['left', 'right']) {
    const node = document.querySelector(`.maplibregl-ctrl-bottom-${corner}`);
    if (node) clearanceObserver.observe(node);
}

// Deliberately not on `map.on('styledata')`: that fires repeatedly as sources
// settle, so collapsing there would snap the credits shut under a reader who
// had just opened them. Collapsing belongs to the moment a style is *applied* —
// see applyBasemap — and to first load, below.
map.on('styledata', updateControlClearance);

// The controls are created by MapLibre, so wait until they exist.
requestAnimationFrame(() => {
    collapseAttribution();
    updateControlClearance();
});

updateSliderFill(el.speedSlider);
updateSliderFill(el.opacitySlider);
redrawLegend();
boot().catch(() => {});
