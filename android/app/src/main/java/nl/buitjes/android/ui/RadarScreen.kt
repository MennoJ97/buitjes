package nl.buitjes.android.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import nl.buitjes.android.data.Fix
import nl.buitjes.android.data.ForecastRepository
import nl.buitjes.android.data.Snapshot
import nl.buitjes.android.data.WidgetTarget
import nl.buitjes.android.data.LocationSource
import nl.buitjes.android.data.RadarCycle
import nl.buitjes.android.data.Settings
import nl.buitjes.core.Forecast
import nl.buitjes.android.data.buitjesClient
import org.maplibre.android.geometry.LatLng

/**
 * The radar, full screen, with the timeline the web page has.
 *
 * The frames are the same ones the map on the web draws, and the reason this
 * exists in the app at all is that the two questions a rain app is asked are
 * different questions. The chart answers "will it rain on me, and when"; only a
 * map answers "where is it now, and is it coming this way" — which is the one
 * you ask before deciding whether to wait ten minutes.
 */
@Composable
fun RadarScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val night = isSystemInDarkTheme()

    var cycle by remember { mutableStateOf<RadarCycle?>(null) }
    var index by remember { mutableIntStateOf(0) }
    var playing by remember { mutableStateOf(true) }
    var here by remember { mutableStateOf<LatLng?>(null) }
    var problem by remember { mutableStateOf<String?>(null) }
    var generation by remember { mutableIntStateOf(0) }
    var loaded by remember { mutableIntStateOf(0) }
    var total by remember { mutableIntStateOf(0) }
    var snapshot by remember { mutableStateOf<Snapshot?>(null) }

    val live = cycle
    val frames = live?.frames.orEmpty()

    LaunchedEffect(Unit) {
        val prefs = Settings.current(context)
        if (!prefs.configured) {
            problem = "No server set up yet"
            return@LaunchedEffect
        }
        val loaded = RadarCycle.forServer(prefs)
        problem = if (loaded.refresh()) null else "Could not reach the server"
        if (loaded.ready) {
            cycle = loaded
            index = loaded.indexOfNow()
            generation++
        }

        // Where to point it. A radar centred on the middle of the domain is a
        // picture of the Netherlands; centred on the phone it is an answer.
        (LocationSource.current(context) as? Fix.Known)?.let {
            here = LatLng(it.lat, it.lon)
        }

        // The chart under the map is for the place the map is centred on, and
        // the repository has usually just fetched it for the forecast screen.
        snapshot = ForecastRepository(context).cached(WidgetTarget.Here)
    }

    // The whole cycle, outward from now, so the timeline can be dragged rather
    // than waited on. Cancelled by leaving the screen.
    LaunchedEffect(live) {
        val current = live ?: return@LaunchedEffect
        total = current.frames.size
        current.prefetch(current.indexOfNow()) { done, count ->
            loaded = done
            total = count
            generation++
        }
    }

    LaunchedEffect(live, playing) {
        if (live == null || !playing) return@LaunchedEffect
        while (true) {
            delay(320)
            index = if (index >= frames.lastIndex) 0 else index + 1
        }
    }

    Column(modifier = modifier.fillMaxSize()) {
        Box(modifier = Modifier.weight(1f).fillMaxWidth()) {
            val frame = frames.getOrNull(index)
            // `generation` is read here so that a frame finishing its decode
            // recomposes this: the bitmap arrives inside a cache the composition
            // cannot otherwise see changing.
            val bitmap = remember(live, index, generation) {
                frame?.let { live?.cached(it) }
            }

            RadarMap(
                manifest = live?.manifest,
                frame = bitmap,
                // The phone if it will say, the middle of the domain otherwise.
                // Location is optional for the radar — it is a map of where the
                // rain is, and that is worth seeing whether or not the app has
                // been told where you are.
                centre = here ?: live?.manifest?.centre(),
                // Only where the phone actually said it was. No fix, no dot.
                marker = here,
                zoom = if (here != null) 7.6 else 6.2,
                night = night,
                interactive = true,
                modifier = Modifier.fillMaxSize(),
            )

            if (live == null) {
                Text(
                    problem ?: "Loading the radar…",
                    modifier = Modifier.align(Alignment.Center),
                    style = MaterialTheme.typography.bodyLarge,
                )
            }
        }

        // The rain where you are, under the map of where it is.
        //
        // The two answer halves of one question and were on separate screens:
        // the map says a shower is twenty kilometres west, the chart says
        // whether it arrives. Sharing the timeline's playhead is what joins
        // them — drag the slider and the marker moves with it, so "this blob,
        // at this time" is one gesture instead of two screens and a memory.
        val current = snapshot?.forecast
        if (current != null && current.hasRainSeries) {
            LocalRainStrip(
                forecast = current,
                atTime = frames.getOrNull(index)?.t,
                modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp),
            )
        }

        if (live != null && frames.isNotEmpty()) {
            Column(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = { playing = !playing }) {
                        Text(if (playing) "Pause" else "Play")
                    }
                    val step = frames.getOrNull(index)
                    Text(
                        buildString {
                            append(step?.t?.let(::formatClock) ?: "")
                            // Which part of the cycle this is. The whole app is
                            // built around not presenting extrapolation as
                            // measurement, and a timeline is where that
                            // distinction is easiest to lose.
                            when (step?.kind) {
                                "observed" -> append(" · measured")
                                "nowcast" -> append(" · nowcast")
                                "forecast" -> append(" · forecast")
                            }
                            if (step?.estimated == true) append(" · stood in for")
                        },
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }

                Slider(
                    value = index.toFloat(),
                    onValueChange = {
                        playing = false
                        index = it.toInt().coerceIn(0, frames.lastIndex)
                    },
                    valueRange = 0f..frames.lastIndex.toFloat().coerceAtLeast(0f),
                )

                // One line for the whole loop, not one per frame.
                //
                // It used to show an indeterminate bar whenever the frame under
                // the playhead was not decoded yet, which — with only the next
                // frame prefetched — was nearly every position on the slider.
                // Dragging it produced a strobe of loading bars that said
                // nothing except that the app was busy. This says how much of
                // the loop is in hand, once, and goes away when it is all here.
                if (total > 0 && loaded < total) {
                    LinearProgressIndicator(
                        progress = { loaded.toFloat() / total },
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            }
        }
    }
}


/**
 * A short rain chart for the place the map is centred on, with a marker at the
 * frame being shown above it.
 *
 * The same renderer the forecast screen uses, at a third of the height and
 * without the scrubbing: this one's playhead belongs to the timeline below it,
 * not to a finger. It is deliberately not a second copy of the forecast card —
 * no summary, no provenance, no bands to interrogate. Those are one tab away,
 * and repeating them here would make this screen an argument about which chart
 * is the real one.
 */
@Composable
private fun LocalRainStrip(
    forecast: Forecast,
    atTime: Long?,
    modifier: Modifier = Modifier,
) {
    val night = isSystemInDarkTheme()
    val density = LocalDensity.current.density

    BoxWithConstraints(modifier = modifier.height(96.dp)) {
        val widthPx = with(LocalDensity.current) { maxWidth.roundToPx() }
        val heightPx = with(LocalDensity.current) { maxHeight.roundToPx() }
        val palette = ChartPalette.of(night)

        val rendered = remember(forecast, widthPx, heightPx, night) {
            ChartRenderer.render(
                forecast = forecast,
                widthPx = widthPx,
                heightPx = heightPx,
                density = density,
                palette = palette,
            )
        }

        Image(
            bitmap = rendered.bitmap.asImageBitmap(),
            contentDescription = forecast.summary.text,
            contentScale = ContentScale.FillBounds,
            modifier = Modifier.fillMaxWidth().height(maxHeight),
        )

        val geometry = rendered.geometry
        if (geometry != null && atTime != null && atTime in geometry.firstT..geometry.lastT) {
            Box(
                modifier = Modifier
                    .offset(x = maxWidth * geometry.fractionOf(atTime) - 1.dp, y = maxHeight * geometry.top)
                    .width(2.dp)
                    .height(maxHeight * (geometry.bottom - geometry.top))
                    .background(MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f)),
            )
        }
    }
}
