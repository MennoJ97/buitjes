/**
 * The basemaps, and everything needed to put one on a MapLibre map.
 *
 * Shared by the full map and the small radar view on the forecast page. It was
 * all inside app.js until the second map needed it, and a copy of a table like
 * BASEMAPS is how the two ends up differing from each other.
 *
 * The functions that touch a live map take it as an argument rather than
 * closing over one, which is the only real change from when this lived in
 * app.js: there are two maps now.
 */

// OpenFreeMap serves OpenStreetMap vector tiles without a key or a quota, and
// its TileJSON carries its own attribution, so nothing needs crediting here.
//
// Three of the five basemaps were CARTO's raster tiles until CARTO began
// requiring an API key for that endpoint and watermarking every request without
// one - "API KEY REQUIRED", diagonally, across every tile. A free key exists,
// but it would be a key shipped in a public page, for a raster service CARTO is
// retiring in favour of vector, and it would cost this app the one property
// worth having here: that a reader can run it with nothing but a KNMI key.
const ofm = (name) => `https://tiles.openfreemap.org/styles/${name}`;
const OSM_TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';

const OSM_CREDIT =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// The map's attribution control is the conventional home for credits, so the
// inspiration is acknowledged there as well as in the About dialog.
// MapLibre inserts its own separator between this and the basemap's own credit.
// What stays put whichever basemap is showing; the tile credits come and go
// with the style. KNMI is CC BY 4.0 and has to be named wherever its data is
// shown. The nod to Nimbus is a courtesy rather than an obligation — kept here
// as well as in the About dialog because a credit nobody opens a dialog to find
// is not much of a credit. It costs nothing now the chip starts collapsed.
export const OWN_CREDIT =
    'data &copy; <a href="https://dataplatform.knmi.nl/">KNMI</a>' +
    ' | inspired by <a href="https://nimbus.yannick.cloud">Nimbus</a>';

/**
 * The recolouring that turns OpenFreeMap's Dark into a style you can read rain
 * over, keyed by upstream layer id. Only the colours that matter are overridden,
 * so the cartography itself stays maintained upstream.
 *
 * This replaces a `raster-contrast` boost applied to the CARTO dark tiles, which
 * never worked and could not have. Measured at z10 over Utrecht (luminance
 * 0-255), that ground sits at a median of 9 and its labels at 89. `raster-contrast`
 * pivots around mid-grey, so on a ground that dark it pushes *down* and clips;
 * raising `raster-brightness-min` to compensate then lifts the whole image
 * uniformly, moving the floor without ever widening the gap. The tiles were the
 * ceiling, not the tuning. A vector style has no such ceiling — every colour
 * below is simply ours to choose.
 *
 * Place names go to pure white on a near-opaque halo, and the halo is what does
 * the work. The bottom of the rain ramp (#c2e6ff, luminance 220) is far brighter
 * than the map under it, so white text alone disappears into drizzle exactly
 * where the old grey text disappeared into downpours; the halo is what keeps a
 * city readable whichever end of the ramp happens to be sitting on top of it.
 *
 * Roads stay deliberately quiet. They are here to place the rain, not to be
 * read — a first pass lifted the motorway network to luminance 103 and produced
 * a road atlas with some weather on it.
 */
const HIGH_CONTRAST_DARK = {
    ground: {
        background: '#0e1116',
        water: '#1b2430',
        waterway: '#1b2430',
        landcover_wood: '#131a22',
        landuse_residential: '#151b24',
        landuse_park: '#141d1a',
        building: '#181f29',
        highway_minor: '#222932',
        highway_major_casing: '#161c23',
        highway_major_inner: '#333c47',
        highway_major_subtle: '#333c47',
        highway_motorway_casing: '#1b222b',
        highway_motorway_inner: '#46525f',
        highway_motorway_subtle: '#46525f',
        // Borders are the one line work worth seeing through rain: at the
        // default zoom they are what tells you which country you are looking at.
        boundary_state: '#4d5665',
        'boundary_country_z0-4': '#92a1b3',
        'boundary_country_z5-': '#92a1b3',
    },
    place: {
        'text-color': '#ffffff',
        'text-halo-color': 'rgba(6,9,13,0.92)',
        'text-halo-width': 1.8,
        'text-halo-blur': 0.2,
    },
    water: {
        'text-color': '#8496ab',
        'text-halo-color': 'rgba(6,9,13,0.9)',
        'text-halo-width': 1.4,
    },
};

/**
 * A `styleUrl` entry is a vector style: the whole style is JSON we can repaint,
 * and MapLibre draws the place names as real text on top of the radar. That
 * last part is what matters — rain at 70% opacity over a basemap with its names
 * baked in swallows exactly the city names you want to locate it by. See
 * HIGH_CONTRAST_DARK for how far the repainting is worth taking.
 *
 * Three of these are the same OpenFreeMap style wearing different clothes,
 * which is the point: one cartography, tuned three ways. Plain for a reader who
 * wants the map to look like a map, repainted for one who needs to read rain
 * over it, and stripped of its names for one who wants only the rain.
 *
 * A `ground` entry is a raster sandwich instead, and OpenStreetMap's standard
 * tiles are the reason that path still exists: one image with the names baked
 * in, so they cannot be split and necessarily sit *under* the radar. That is
 * the trade-off for the familiar look, and the opacity slider is the remedy.
 */
export const BASEMAPS = {
    dark: {
        label: 'Dark',
        styleUrl: ofm('dark'),
    },
    contrast: {
        label: 'Dark, high contrast',
        styleUrl: ofm('dark'),
        paint: HIGH_CONTRAST_DARK,
    },
    light: {
        label: 'Light',
        styleUrl: ofm('positron'),
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
        styleUrl: ofm('dark'),
        hideLabels: true,
    },
};

// The repainted one, not the plain one. Both are now the same OpenFreeMap
// cartography, so the only thing separating them is legibility under rain - and
// on the plain style a place name sitting under a heavy cell loses. A first
// visit should get the one that was tuned for the job.
const DEFAULT_BASEMAP = 'contrast';
export const BASEMAP_STORAGE_KEY = 'buitjes.basemap';
const LEGACY_BASEMAP_KEY = 'stratus.basemap';

/** The style chosen last time. Picking a legible map should not be a per-visit chore. */
export function storedBasemap() {
    try {
        const name = localStorage.getItem(BASEMAP_STORAGE_KEY)
            || localStorage.getItem(LEGACY_BASEMAP_KEY);  // named Stratus once
        return name && BASEMAPS[name] ? name : DEFAULT_BASEMAP;
    } catch {
        // Private browsing, or storage disabled entirely.
        return DEFAULT_BASEMAP;
    }
}

/**
 * The style spec for a basemap: either the raster sandwich we assemble here, or
 * a vector style URL for MapLibre to fetch. A vector style brings its own
 * attribution along in its TileJSON, which is why only the raster entries carry
 * a `credit`.
 */
export function styleFor(config) {
    if (config.styleUrl) return config.styleUrl;
    const style = {
        version: 8,
        sources: {
            basemap: {
                type: 'raster',
                tiles: [config.ground],
                tileSize: 256,
                attribution: config.credit,
            },
        },
        layers: [{ id: 'basemap', type: 'raster', source: 'basemap' }],
    };
    if (config.labels) {
        style.sources.labels = { type: 'raster', tiles: [config.labels], tileSize: 256 };
        style.layers.push({ id: 'labels', type: 'raster', source: 'labels' });
    }
    return style;
}

/**
 * Where the radar belongs: under the place names, over the ground. Raster styles
 * name their label layer; a vector style's names are the first symbol layer.
 */
export const labelLayerId = (layers) =>
    layers.find((layer) => layer.id === 'labels' || layer.type === 'symbol')?.id;

/** The ground layers any basemap repaints, so the others can put them back. */
const GROUND_LAYERS = Object.assign({}, ...Object.values(BASEMAPS)
    .filter((config) => config.paint)
    .map((config) => config.paint.ground));

const colourKey = (type) =>
    type === 'background' ? 'background-color' : type === 'fill' ? 'fill-color' : 'line-color';

/** Style JSON by URL, so three basemaps sharing one style cost a single fetch. */
const styleCache = new Map();

/**
 * A vector basemap's stylesheet as its author wrote it, fetched once and kept.
 *
 * Kept because it is the only record of what these layers look like untouched,
 * and switching *away* from a repainted basemap needs that: nothing else can
 * say what colour a layer is supposed to go back to.
 */
export async function pristineStyle(config) {
    if (!config.styleUrl) return null;
    if (!styleCache.has(config.styleUrl)) {
        const response = await fetch(config.styleUrl);
        if (!response.ok) throw new Error(`basemap style ${response.status}`);
        styleCache.set(config.styleUrl, await response.json());
    }
    return styleCache.get(config.styleUrl);
}

/** Every paint property any basemap repaints, so the others can put it back. */
function repaintedKeys() {
    const keys = new Set();
    for (const config of Object.values(BASEMAPS)) {
        if (!config.paint) continue;
        for (const key of Object.keys(config.paint.place)) keys.add(key);
        for (const key of Object.keys(config.paint.water)) keys.add(key);
    }
    return keys;
}

/**
 * Fix up a loaded vector style: hide its names, repaint them, or put both back.
 *
 * Every property is resolved against `pristine` and then set, whether this
 * basemap changes it or not. That symmetry is the whole design. Applying only
 * what a basemap overrides works exactly once - the first time - and after that
 * leaves the last basemap's repaint sitting under the new one's name, because
 * three of these are one OpenFreeMap stylesheet and switching between them
 * gives MapLibre nothing to reload. Setting the pristine value *is* the undo.
 *
 * Done against the live style rather than by handing setStyle a rewritten one:
 * a stylesheet that differs from the showing one only in paint is a diff
 * MapLibre may decide is not worth applying, and it silently was not.
 *
 * `hideLabels` hides every symbol layer rather than only the place names,
 * because the ones left behind are the giveaway: road shields and the little
 * one-way arrows are not places, but a map that has dropped its city names and
 * kept its motorway numbers looks broken rather than deliberate.
 */
export function applyStyleOverrides(map, config, pristine) {
    if (!pristine) return;
    const keys = repaintedKeys();
    for (const layer of pristine.layers) {
        if (layer.type === 'symbol') {
            map.setLayoutProperty(layer.id, 'visibility',
                config.hideLabels ? 'none' : layer.layout?.visibility ?? 'visible');
        }

        const wanted = config.paint;
        if (layer.id in GROUND_LAYERS) {
            const key = colourKey(layer.type);
            map.setPaintProperty(layer.id, key,
                wanted?.ground[layer.id] ?? layer.paint?.[key]);
        } else if (layer.id.startsWith('place_') || layer.id === 'water_name') {
            const source = layer.id === 'water_name' ? wanted?.water : wanted?.place;
            for (const key of keys) {
                map.setPaintProperty(layer.id, key, source?.[key] ?? layer.paint?.[key]);
            }
        }
    }
}

