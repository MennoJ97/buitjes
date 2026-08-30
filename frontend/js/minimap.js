/**
 * The small looping radar on the forecast page.
 *
 * The charts answer "how much, and when" for one point. They cannot answer
 * "where is it coming from", which is the question a radar picture is uniquely
 * good at — a shower that will miss you by five kilometres looks identical to a
 * direct hit until you see it move.
 *
 * Deliberately not a second copy of the map page. It reuses the same frame
 * store, the same WebGL renderer and the same basemap table; what it adds is a
 * loop, a marker, and the decision about which frames are worth downloading.
 */

import { RadarRenderer, FrameStore } from './radar.js';
import {
    BASEMAPS, OWN_CREDIT, applyStyleOverrides, labelLayerId, pristineStyle, storedBasemap,
    styleFor,
} from './basemap.js';
import { formatClock } from './time.js';
import { apiFetch } from './key.js';

/**
 * Only the measured past and the extrapolated near future.
 *
 * The full manifest is ~85 frames at about 30 KB each — 2.5 MB, on a page that
 * otherwise weighs 25 KB. The model-driven hours beyond +2h are what the Rain
 * outlook chart below is for, and they are the part a radar loop conveys worst:
 * smooth blobs that say more about the model than the sky. Trimming to
 * observed + nowcast is about a third of that.
 */
const WINDOW_KINDS = new Set(['observed', 'nowcast']);

const FRAME_MS = 260;
/** A beat on the last frame, so the loop reads as a loop and not a stutter. */
const HOLD_LAST_MS = 1100;
const REFRESH_MS = 5 * 60 * 1000;

export function createRadarMinimap({ mapEl, canvasEl, timeEl, playBtn, statusEl, scrubEl, nowEl }) {
    const store = new FrameStore();
    let renderer = null;
    let map = null;
    /** Set on 'style.load'; see addRadarLayer for why not isStyleLoaded(). */
    let styleReady = false;
    let marker = null;
    let index = 0;
    let timer = null;
    let playing = true;
    let point = null;

    const frame = () => store.frames[index];

/** Matches the thumb width in the stylesheet; see .mini-track for why. */
    const THUMB_PX = 11;

    /**
     * Put the "now" mark where the cycle's reference time falls.
     *
     * Taken from the manifest rather than the clock, and nearest-frame rather
     * than interpolated, so it lands on the same instant the map page calls now
     * and always sits exactly on a frame the scrubber can stop at.
     */
    function placeNow() {
        const reference = store.manifest?.reference_time;
        const last = store.frames.length - 1;
        if (!reference || last < 1) return void (nowEl.hidden = true);
        let nearest = 0;
        store.frames.forEach((f, i) => {
            if (Math.abs(f.t - reference) < Math.abs(store.frames[nearest].t - reference)) nearest = i;
        });
        nowEl.hidden = false;
        nowEl.style.left =
            `calc(${THUMB_PX / 2}px + (100% - ${THUMB_PX}px) * ${nearest / last})`;
    }

    function paint() {
        const current = frame();
        if (!current) return;
        const image = store.imageFor(current);
        if (image) renderer.draw(image); else renderer.clear();
        timeEl.textContent = formatClock(current.t);
        scrubEl.value = String(index);
        timeEl.dateTime = new Date(current.t * 1000).toISOString();
        // The observed/nowcast split is the honest part of a radar loop, so say
        // which one is on screen rather than letting them blur together.
        statusEl.textContent = current.kind === 'observed' ? 'measured' : 'extrapolated';
    }

    function step() {
        const last = index === store.frames.length - 1;
        index = last ? 0 : index + 1;
        paint();
        schedule(last ? HOLD_LAST_MS : FRAME_MS);
    }

    function schedule(delay) {
        clearTimeout(timer);
        // setTimeout rather than setInterval: the pause on the last frame means
        // the gap is not constant, and a drifting interval cannot express that.
        if (playing) timer = setTimeout(step, delay);
    }

    function setPlaying(next) {
        playing = next;
        playBtn.setAttribute('aria-label', playing ? 'Pause' : 'Play');
        playBtn.classList.toggle('is-paused', !playing);
        if (playing) schedule(FRAME_MS); else clearTimeout(timer);
    }

    playBtn.addEventListener('click', () => setPlaying(!playing));

    // Scrubbing is taking manual control, so it stops the loop rather than
    // fighting it for the next frame. The button flips to ▶, which is the only
    // hint needed that playback is now yours to restart.
    scrubEl.addEventListener('input', () => {
        if (playing) setPlaying(false);
        index = Number(scrubEl.value);
        paint();
    });

    /** Pause while off screen. A loop nobody is looking at is just battery. */
    const visibility = new IntersectionObserver(([entry]) => {
        if (entry.isIntersecting) schedule(FRAME_MS);
        else clearTimeout(timer);
    }, { threshold: 0.1 });

    /**
     * Anything that goes wrong here says so in the card's own status line.
     *
     * The map is built inside MapLibre callbacks, so a throw would otherwise
     * land nowhere the reader can see it — the card would just sit blank, which
     * is indistinguishable from "no radar on this device" and impossible to
     * report usefully. WebGL and canvas sources are exactly the things that
     * differ between a desktop browser and a phone.
     */
    function fail(where, error) {
        statusEl.textContent = `radar unavailable — ${where}`;
        statusEl.title = String(error?.message ?? error);
        console.error(`minimap: ${where}`, error);
    }

    function buildMap() {
        const name = storedBasemap();
        const config = BASEMAPS[name];
        map = new maplibregl.Map({
            container: mapEl,
            style: styleFor(config),
            center: [point.lon, point.lat],
            zoom: 8,
            attributionControl: false,
            // The page scrolls; a map that eats the wheel inside it does not.
            scrollZoom: false,
            dragRotate: false,
            touchZoomRotate: false,
        });
        map.addControl(
            new maplibregl.AttributionControl({ compact: true, customAttribution: OWN_CREDIT })
        );
        // MapLibre opens the compact attribution on load, which on a card this
        // small is a bar across the whole map. Same reasoning as the full map:
        // collapsed it is one ⓘ, and a click still shows every credit.
        //
        // The `compact-show` class has to go along with `open`: MapLibre holds
        // its open/closed state in both, and its click handler reads the class,
        // so leaving it set costs the reader their first click. See
        // collapseAttribution in app.js.
        requestAnimationFrame(() => {
            const attrib = mapEl.querySelector('details.maplibregl-ctrl-attrib');
            attrib?.removeAttribute('open');
            attrib?.classList.remove('maplibregl-compact-show');
        });
        mapEl.classList.toggle('theme-light', !!config.lightUi);
        map.on('error', (event) => fail('map error', event?.error));
        // 'style.load' rather than 'load': it fires when the stylesheet has been
        // parsed and its layers exist, which is everything the radar layer
        // needs. 'load' additionally waits for the first complete render, so a
        // basemap whose tiles are slow or unreachable would hold the rain
        // hostage to scenery.
        map.once('style.load', () => {
            styleReady = true;
            // The rain goes on first, and without awaiting anything. The
            // repaint below needs the basemap's own stylesheet over the
            // network, and on a cold visit that fetch is long enough for the
            // map to start loading tiles again — which is exactly the state
            // addRadarLayer used to refuse to add a layer in.
            try {
                addRadarLayer();
            } catch (error) {
                fail('could not add the radar layer', error);
            }
            // Its own failure, and a survivable one: losing the recolouring
            // costs legibility, not the radar. It used to share a catch with
            // the line above and report itself as the radar being unavailable.
            pristineStyle(config)
                .then((pristine) => applyStyleOverrides(map, config, pristine))
                .catch((error) => console.error('minimap: could not repaint the basemap', error));
        });
    }

    /**
     * Put the radar canvas on the map, once the map has a style to put it in.
     *
     * The wait is on our own flag rather than on `map.isStyleLoaded()`, which
     * answers a different question: whether every tile, sprite and glyph the
     * basemap wants has *settled*. That goes back to false whenever the map
     * fetches anything, and no event says when it becomes true again —
     * `styledata` fires for changes to the style itself, never for a tile
     * arriving. So `once('styledata')` was a wait that could outlive the last
     * event that would ever end it, and then the card sat there with a basemap
     * and no rain on it for the rest of the visit. Opening the map page first
     * hid it: the tiles were already cached, so the style was still settled by
     * the time this ran.
     *
     * Adding a source and a layer needs the stylesheet parsed and nothing more,
     * which is what 'style.load' says and all this flag records.
     */
    function addRadarLayer() {
        if (!styleReady) return;
        if (map.getSource('radar-mini')) {
            map.getSource('radar-mini').setCoordinates(store.coordinates);
            return;
        }
        map.addSource('radar-mini', {
            type: 'canvas',
            canvas: canvasEl.id,
            coordinates: store.coordinates,
            animate: true,
        });
        map.addLayer(
            { id: 'radar-mini-layer', type: 'raster', source: 'radar-mini',
              paint: { 'raster-opacity': 0.85, 'raster-fade-duration': 0 } },
            labelLayerId(map.getStyle().layers)
        );
    }

    async function loadFrames() {
        const response = await apiFetch('/api/config', { cache: 'no-store' });
        if (!response.ok) throw new Error(`manifest unavailable (${response.status})`);
        const manifest = await response.json();
        const frames = (manifest.frames ?? []).filter((f) => WINDOW_KINDS.has(f.kind));
        if (!frames.length) throw new Error('no recent frames published');

        const previous = frame()?.t;
        store.setManifest({ ...manifest, frames });
        if (!renderer) {
            try {
                renderer = new RadarRenderer(canvasEl);
            } catch (error) {
                fail('no WebGL on this device', error);
                throw error;
            }
        }
        renderer.resize(store.width, store.height);
        renderer.setMaxPrecip(store.maxPrecip);
        // Hold the moment the reader was watching across a refresh rather than
        // snapping back to the start of the window.
        const held = store.frames.findIndex((f) => f.t === previous);
        index = held === -1 ? Math.max(0, store.frames.findIndex((f) => f.kind === 'nowcast') - 1) : held;

        scrubEl.max = String(Math.max(0, store.frames.length - 1));
        placeNow();
        await store.prefetch();
        if (!map) { buildMap(); visibility.observe(mapEl); } else { addRadarLayer(); }
        paint();
        schedule(FRAME_MS);
    }

    return {
        /** Point the loop at a location. Safe to call again when it changes. */
        async show(next) {
            const moved = !point || point.lat !== next.lat || point.lon !== next.lon;
            point = next;
            if (map && moved) {
                map.jumpTo({ center: [point.lon, point.lat], zoom: 8 });
            }
            if (map) {
                marker?.remove();
                marker = new maplibregl.Marker({ color: '#f8fafc', scale: 0.7 })
                    .setLngLat([point.lon, point.lat]).addTo(map);
            }
            await loadFrames();
            if (!marker) {
                marker = new maplibregl.Marker({ color: '#f8fafc', scale: 0.7 })
                    .setLngLat([point.lon, point.lat]).addTo(map);
            }
        },
        refreshTimer: setInterval(() => { loadFrames().catch(() => {}); }, REFRESH_MS),
    };
}
