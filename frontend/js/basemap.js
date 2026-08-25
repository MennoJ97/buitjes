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

const carto = (name) => `https://a.basemaps.cartocdn.com/${name}/{z}/{x}/{y}.png`;
const OSM_TILES = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
// OpenFreeMap serves OpenStreetMap vector tiles without a key or a quota, and
// its TileJSON carries its own attribution, so nothing needs crediting here.
const OFM_DARK_STYLE = 'https://tiles.openfreemap.org/styles/dark';

const CARTO_CREDIT =
    '&copy; <a href="https://carto.com/attributions">CARTO</a> &copy; OpenStreetMap contributors';
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
 * Raster basemaps are split into a label-free ground layer and a separate label
 * layer, so place names can be drawn back on top of the radar. Rain at 70%
 * opacity over an all-in-one basemap swallows exactly the city names you want
 * to locate it by.
 *
 * A `styleUrl` entry is a vector style instead: the whole style is JSON we can
 * repaint, and MapLibre draws the place names as real text on top of the radar
 * rather than as a second image. See HIGH_CONTRAST_DARK for why the one style
 * that has to be legible is built that way.
 *
 * OpenStreetMap's standard tiles are one image with the names baked in, so they
 * cannot be split — its labels necessarily sit *under* the radar. That is the
 * trade-off for the familiar look, and the radar opacity slider is the remedy.
 */
export const BASEMAPS = {
    dark: {
        label: 'Dark',
        ground: carto('dark_nolabels'),
        labels: carto('dark_only_labels'),
        credit: CARTO_CREDIT,
    },
    contrast: {
        label: 'Dark, high contrast',
        styleUrl: OFM_DARK_STYLE,
        paint: HIGH_CONTRAST_DARK,
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

const colourKey = (type) =>
    type === 'background' ? 'background-color' : type === 'fill' ? 'fill-color' : 'line-color';

const setPaint = (map, id, paint) => {
    for (const [property, value] of Object.entries(paint)) {
        map.setPaintProperty(id, property, value);
    }
};

/**
 * Repaint a freshly loaded vector style. Done with setPaintProperty against the
 * live style rather than by rewriting the JSON on its way in, so the same call
 * covers the initial load and every later switch.
 */
export function applyPaintOverrides(map, config) {
    if (!config.paint) return;
    for (const layer of map.getStyle().layers) {
        const ground = config.paint.ground[layer.id];
        if (ground) {
            map.setPaintProperty(layer.id, colourKey(layer.type), ground);
        } else if (layer.id.startsWith('place_')) {
            setPaint(map, layer.id, config.paint.place);
        } else if (layer.id === 'water_name') {
            setPaint(map, layer.id, config.paint.water);
        }
    }
}

