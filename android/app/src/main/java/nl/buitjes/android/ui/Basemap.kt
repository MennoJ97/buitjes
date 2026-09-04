package nl.buitjes.android.ui

import org.maplibre.android.maps.Style
import org.maplibre.android.style.expressions.Expression
import org.maplibre.android.style.layers.BackgroundLayer
import org.maplibre.android.style.layers.FillLayer
import org.maplibre.android.style.layers.LineLayer
import org.maplibre.android.style.layers.PropertyFactory
import org.maplibre.android.style.layers.SymbolLayer

/**
 * The repaint that turns OpenFreeMap's Dark into a map you can read rain over.
 *
 * Ported from the web app's `HIGH_CONTRAST_DARK`, colour for colour, because
 * this is the basemap that page opens on and it did not get chosen for taste.
 * The bottom of the rain ramp is #c2e6ff — far brighter than any of the ground
 * below — so on the untouched style a place name under a heavy cell simply
 * loses, and the map stops being able to tell you *where* the shower is at the
 * moment you most want to know.
 *
 * Only the colours that matter are overridden. The cartography itself stays
 * upstream's and stays maintained.
 */
private val GROUND = mapOf(
    "background" to "#0e1116",
    "water" to "#1b2430",
    "waterway" to "#1b2430",
    "landcover_wood" to "#131a22",
    "landuse_residential" to "#151b24",
    "landuse_park" to "#141d1a",
    "building" to "#181f29",
    "highway_minor" to "#1e242c",
    "highway_major_casing" to "#161c23",
    "highway_major_inner" to "#333c47",
    "highway_major_subtle" to "#333c47",
    "highway_motorway_casing" to "#1b222b",
    // Borders are the one line work worth seeing through rain: at the default
    // zoom they are what tells you which country you are looking at.
    "boundary_state" to "#4d5665",
    "boundary_country_z0-4" to "#92a1b3",
    "boundary_country_z5-" to "#92a1b3",
)

/**
 * Lit where a motorway is a landmark, quiet where it is not.
 *
 * Zoomed out, the borders and city names do the placing and the motorway
 * network is texture over four countries; zoomed to a single shower it is the
 * only thing on the map saying which side of town the rain is on. One colour
 * cannot be both, and zoom is what separates them.
 */
private val MOTORWAY = listOf("highway_motorway_inner", "highway_motorway_subtle")

private val MOTORWAY_COLOUR: Expression = Expression.interpolate(
    Expression.linear(),
    Expression.zoom(),
    Expression.stop(7, Expression.color(0xFF46525F.toInt())),
    Expression.stop(10, Expression.color(0xFF74828F.toInt())),
)

/**
 * Place names go to pure white on a near-opaque halo, and the halo does the
 * work: white text alone disappears into drizzle exactly where grey text
 * disappears into a downpour, and the halo is what keeps a city readable
 * whichever end of the ramp is sitting on top of it.
 */
private const val PLACE_TEXT = "#ffffff"
private const val PLACE_HALO = "rgba(6,9,13,0.92)"
private const val WATER_TEXT = "#8496ab"
private const val WATER_HALO = "rgba(6,9,13,0.9)"

/** Repaint a freshly loaded dark style in place. */
fun applyHighContrastDark(style: Style) {
    for (layer in style.layers) {
        val id = layer.id
        when {
            id in MOTORWAY -> (layer as? LineLayer)
                ?.setProperties(PropertyFactory.lineColor(MOTORWAY_COLOUR))

            GROUND.containsKey(id) -> {
                val colour = GROUND.getValue(id)
                when (layer) {
                    // Which property carries a layer's colour depends on what
                    // kind of layer it is, which is why this cannot be one call.
                    is BackgroundLayer -> layer.setProperties(PropertyFactory.backgroundColor(colour))
                    is FillLayer -> layer.setProperties(PropertyFactory.fillColor(colour))
                    is LineLayer -> layer.setProperties(PropertyFactory.lineColor(colour))
                    else -> Unit
                }
            }

            id.startsWith("place_") -> (layer as? SymbolLayer)?.setProperties(
                PropertyFactory.textColor(PLACE_TEXT),
                PropertyFactory.textHaloColor(PLACE_HALO),
                PropertyFactory.textHaloWidth(1.8f),
                PropertyFactory.textHaloBlur(0.2f),
            )

            id == "water_name" -> (layer as? SymbolLayer)?.setProperties(
                PropertyFactory.textColor(WATER_TEXT),
                PropertyFactory.textHaloColor(WATER_HALO),
                PropertyFactory.textHaloWidth(1.4f),
            )
        }
    }
}
