package nl.buitjes.android.ui

import androidx.compose.foundation.isSystemInDarkTheme
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import nl.buitjes.android.data.Fix
import nl.buitjes.android.data.LocationSource
import nl.buitjes.android.data.RadarCycle
import nl.buitjes.android.data.Settings
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
    }

    val live = cycle
    val frames = live?.frames.orEmpty()

    // Decode ahead of the playhead rather than at it, so the animation is not
    // waiting on a network round trip every step. One frame ahead is enough at
    // this cadence and keeps the cache small.
    LaunchedEffect(live, index) {
        val current = live ?: return@LaunchedEffect
        frames.getOrNull(index)?.let { current.bitmapFor(it) }
        frames.getOrNull(index + 1)?.let { current.bitmapFor(it) }
        generation++
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

                if (frames.getOrNull(index)?.let { live.cached(it) } == null) {
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                }
            }
        }
    }
}
