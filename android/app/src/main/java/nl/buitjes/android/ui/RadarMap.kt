package nl.buitjes.android.ui

import android.graphics.Bitmap
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import nl.buitjes.android.data.Manifest
import org.maplibre.android.MapLibre
import org.maplibre.android.camera.CameraPosition
import org.maplibre.android.geometry.LatLng
import org.maplibre.android.geometry.LatLngQuad
import org.maplibre.android.maps.MapView
import org.maplibre.android.maps.Style
import org.maplibre.android.style.layers.CircleLayer
import org.maplibre.android.style.layers.PropertyFactory
import org.maplibre.android.style.layers.RasterLayer
import org.maplibre.android.style.sources.GeoJsonSource
import org.maplibre.android.style.sources.ImageSource
import org.maplibre.geojson.Point

/**
 * The radar, on a map, drawn the way the web app draws it.
 *
 * MapLibre both ends, and the same frames: a published frame is an image
 * stretched across four corners the manifest names, which is precisely what an
 * image source is for. That correspondence is the reason for taking on a map
 * library at all — the alternative was hand-rolling tile fetching and mercator
 * placement, to arrive at a worse version of something the web half already
 * does correctly.
 *
 * The basemap is OpenFreeMap's, again the same as the web, and it wants no key
 * and no quota. That keeps the one property the module layout has been
 * protecting all along: this app runs on a fresh install with nothing but a
 * server address.
 */
// The same two the web app uses, and `positron` rather than `bright` for the
// light one: bright is a colourful general-purpose style, and rain drawn over
// it competes with the cartography instead of sitting on top of it.
private const val STYLE_DARK = "https://tiles.openfreemap.org/styles/dark"
private const val STYLE_LIGHT = "https://tiles.openfreemap.org/styles/positron"

private const val RADAR_SOURCE = "buitjes-radar"
private const val RADAR_LAYER = "buitjes-radar-layer"
private const val HERE_SOURCE = "buitjes-here"
private const val HERE_HALO = "buitjes-here-halo"
private const val HERE_DOT = "buitjes-here-dot"

/**
 * What the map is showing, kept outside the composable so a recomposition
 * cannot cost a style reload.
 *
 * `MapView` is a heavyweight Android view with a GL surface behind it and a
 * lifecycle of its own, none of which Compose knows about — so it is created
 * once, driven by the host's lifecycle through a `DisposableEffect`, and
 * updated in place. Recreating it per frame of an animation would be a new GL
 * context sixty times a minute.
 */
@Composable
fun RadarMap(
    manifest: Manifest?,
    frame: Bitmap?,
    centre: LatLng?,
    /**
     * The place this map is about, marked. Distinct from [centre], which is
     * only where the camera starts: an interactive map keeps its marker while
     * the reader pans away from it, and a radar with no location fix is centred
     * on the domain and marks nothing, rather than putting a dot in the North
     * Sea and calling it you.
     */
    marker: LatLng?,
    zoom: Double,
    night: Boolean,
    interactive: Boolean,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current

    // Once per process, and before any MapView exists. Idempotent, so calling
    // it from every map that appears is cheaper than tracking whether it has
    // happened.
    remember { MapLibre.getInstance(context) }

    val mapView = remember { MapView(context) }
    val state = remember { RadarMapState() }

    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_CREATE -> mapView.onCreate(null)
                Lifecycle.Event.ON_START -> mapView.onStart()
                Lifecycle.Event.ON_RESUME -> mapView.onResume()
                Lifecycle.Event.ON_PAUSE -> mapView.onPause()
                Lifecycle.Event.ON_STOP -> mapView.onStop()
                Lifecycle.Event.ON_DESTROY -> mapView.onDestroy()
                else -> Unit
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
            // The view outlives the composition when the screen is merely
            // scrolled off, but not when it is left for good.
            mapView.onStop()
            mapView.onDestroy()
        }
    }

    AndroidView(
        factory = {
            mapView.onCreate(null)
            mapView.getMapAsync { map ->
                state.map = map
                map.uiSettings.apply {
                    isAttributionEnabled = true
                    isLogoEnabled = true
                    // A map inside a scrolling column must not swallow the
                    // scroll. The card version is a picture you glance at; the
                    // full screen one is a map you drag.
                    setAllGesturesEnabled(interactive)
                }
                map.setStyle(if (night) STYLE_DARK else STYLE_LIGHT) { style ->
                    state.style = style
                    // Dark is repainted; light is not, exactly as on the web —
                    // Positron's ground is already pale enough that rain reads
                    // over it without help.
                    if (night) applyHighContrastDark(style)
                    state.applyFrame(manifest, frame)
                    state.applyMarker(marker)
                }
                state.point(map, centre, zoom, interactive)
            }
            mapView
        },
        update = {
            state.applyFrame(manifest, frame)
            state.applyMarker(marker)
            state.map?.let { state.point(it, centre, zoom, interactive) }
        },
        modifier = modifier,
    )
}

/**
 * The live map objects, and the one thing worth being careful about: an image
 * source can only be created once per style, and setting its image afterwards
 * is what makes an animation cheap.
 */
private class RadarMapState {
    var map: org.maplibre.android.maps.MapLibreMap? = null
    var style: Style? = null
    private var sourceAdded = false
    private var markerAdded = false
    private var placed = false

    /**
     * Put the camera somewhere useful, once.
     *
     * The "once" is the point. A location fix arrives some seconds after the
     * map does, so a camera set only in the factory is set before there is
     * anywhere to set it to — which is how the radar came up showing the whole
     * planet, with the Netherlands four pixels across. But a map somebody is
     * dragging must not be yanked back under their finger every time a frame
     * decodes, so an interactive map accepts a target exactly once and then
     * belongs to the reader.
     *
     * A card is not dragged and does follow, because its whole job is to be
     * about the place the screen is about.
     */
    fun point(
        map: org.maplibre.android.maps.MapLibreMap,
        centre: LatLng?,
        zoom: Double,
        interactive: Boolean,
    ) {
        if (centre == null) return
        if (interactive && placed) return
        map.cameraPosition = CameraPosition.Builder().target(centre).zoom(zoom).build()
        placed = true
    }

    /**
     * A dot where the forecast is for.
     *
     * Black inside a white ring, and neither colour is in the rain ramp on
     * purpose. A marker tinted anywhere near the ramp disappears exactly when
     * it matters — a blue dot under blue rain — and the two-tone version reads
     * on the dark basemap (the ring) and on pale drizzle over it (the dot).
     *
     * Added after the radar layer, so it sits on top of the weather rather than
     * under it.
     */
    fun applyMarker(marker: LatLng?) {
        val style = style ?: return
        if (marker == null) return

        val point = Point.fromLngLat(marker.longitude, marker.latitude)
        val existing = style.getSourceAs<GeoJsonSource>(HERE_SOURCE)
        if (existing != null) {
            existing.setGeoJson(point)
            return
        }
        if (markerAdded) return

        style.addSource(GeoJsonSource(HERE_SOURCE, point))
        style.addLayer(
            CircleLayer(HERE_HALO, HERE_SOURCE).withProperties(
                PropertyFactory.circleRadius(7f),
                PropertyFactory.circleColor("#FFFFFF"),
                PropertyFactory.circleOpacity(0.9f),
            ),
        )
        style.addLayer(
            CircleLayer(HERE_DOT, HERE_SOURCE).withProperties(
                PropertyFactory.circleRadius(3.5f),
                PropertyFactory.circleColor("#111318"),
            ),
        )
        markerAdded = true
    }

    fun applyFrame(manifest: Manifest?, frame: Bitmap?) {
        val style = style ?: return
        if (manifest == null || !manifest.drawable || frame == null) return

        val quad = manifest.quad() ?: return
        val existing = style.getSourceAs<ImageSource>(RADAR_SOURCE)
        if (existing != null) {
            existing.setImage(frame)
            return
        }
        if (sourceAdded) return

        style.addSource(ImageSource(RADAR_SOURCE, quad, frame))
        style.addLayer(
            RasterLayer(RADAR_LAYER, RADAR_SOURCE).withProperties(
                // Enough to read the map through the rain, which is the whole
                // point of putting it on a map rather than beside one.
                PropertyFactory.rasterOpacity(0.78f),
                // Nearest rather than smoothed: a frame is a kilometre grid,
                // and interpolating it invents a gradient the radar never
                // measured. The web makes the same choice.
                PropertyFactory.rasterResampling("nearest"),
            ),
        )
        sourceAdded = true
    }
}

/**
 * The middle of the published domain, for a map with nowhere better to look.
 *
 * Without this the radar opens on whatever MapLibre defaults to — the whole
 * globe — while it waits for a location fix that may never come, because
 * location permission is optional in this app and the radar does not need it.
 * The domain's centre is always the right second choice: it is the only part of
 * the world these frames say anything about.
 */
fun Manifest.centre(): LatLng? {
    if (bounds.size != 4) return null
    val lats = bounds.mapNotNull { it.getOrNull(1) }
    val lons = bounds.mapNotNull { it.getOrNull(0) }
    if (lats.size != 4 || lons.size != 4) return null
    return LatLng((lats.min() + lats.max()) / 2, (lons.min() + lons.max()) / 2)
}

/** The manifest's four corners, in the order an image source expects them. */
private fun Manifest.quad(): LatLngQuad? {
    if (bounds.size != 4) return null
    val corners = bounds.map { pair ->
        if (pair.size < 2) return null else LatLng(pair[1], pair[0])
    }
    return LatLngQuad(corners[0], corners[1], corners[2], corners[3])
}
