package nl.buitjes.android.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.dp
import kotlin.math.roundToInt
import kotlinx.coroutines.launch
import nl.buitjes.core.Centre
import nl.buitjes.core.CentreKey
import nl.buitjes.core.Forecast
import nl.buitjes.core.Series
import nl.buitjes.android.data.ForecastRepository
import nl.buitjes.android.data.NamedPoint
import nl.buitjes.android.data.Snapshot
import nl.buitjes.android.data.WidgetTarget

/**
 * One location's next six hours.
 *
 * The chart is the same bitmap the widget draws, at a different size. That is
 * not laziness about Compose — it is the only way the two surfaces can be
 * guaranteed to agree, and a chart that disagrees with the widget beside it on
 * the home screen would undermine both.
 */
@Composable
fun ForecastScreen(initialTarget: WidgetTarget?, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val repository = remember { ForecastRepository(context) }

    var target by remember { mutableStateOf(initialTarget ?: WidgetTarget.Here) }
    var locations by remember { mutableStateOf<List<NamedPoint>>(emptyList()) }
    var snapshot by remember { mutableStateOf<Snapshot?>(null) }
    var loading by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        locations = repository.locations()
    }

    // Keyed on the target, so switching location shows what is cached for the
    // new one immediately and then refreshes it. Showing the previous
    // location's chart under the new location's name for two seconds is the
    // sort of thing nobody reports and everybody distrusts.
    LaunchedEffect(target) {
        snapshot = repository.cached(target)
        loading = true
        snapshot = repository.refresh(target)
        loading = false
    }

    Column(
        modifier = modifier
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(
            modifier = Modifier.horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            FilterChip(
                selected = target == WidgetTarget.Here,
                onClick = { target = WidgetTarget.Here },
                label = { Text("Here") },
            )
            locations.forEach { point ->
                val named = WidgetTarget.Named(point.name)
                FilterChip(
                    selected = target == named,
                    onClick = { target = named },
                    label = { Text(named.label) },
                )
            }
        }

        if (loading) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
        }

        val current = snapshot
        Card(modifier = Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier.padding(12.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text(target.label, style = MaterialTheme.typography.titleLarge)
                Chart(current)
                Provenance(current)
            }
        }

        ConditionCards(current)

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.End,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TextButton(
                onClick = {
                    scope.launch {
                        loading = true
                        snapshot = repository.refresh(target)
                        loading = false
                    }
                },
                enabled = !loading,
            ) {
                Text("Refresh")
            }
        }
    }
}

@Composable
private fun Chart(snapshot: Snapshot?) {
    val night = isSystemInDarkTheme()
    val density = LocalDensity.current.density
    val now = System.currentTimeMillis() / 1000
    val stale = snapshot?.isStale(now) ?: false

    // BoxWithConstraints because the bitmap needs a pixel width before it can
    // exist, and a Compose layout does not know its own width until it is
    // measured. This is the one place the widget's sizing problem shows up
    // in-app too.
    BoxWithConstraints(modifier = Modifier.fillMaxWidth().height(200.dp)) {
        val widthPx = with(LocalDensity.current) { maxWidth.roundToPx() }
        val heightPx = with(LocalDensity.current) { maxHeight.roundToPx() }
        val palette = ChartPalette.of(night).let { if (stale) it.muted() else it }

        val rendered = remember(snapshot, widthPx, heightPx, night, stale) {
            ChartRenderer.render(
                forecast = snapshot?.forecast,
                widthPx = widthPx,
                heightPx = heightPx,
                density = density,
                palette = palette,
            )
        }

        val forecast = snapshot?.forecast
        ScrubbableChart(
            rendered = rendered,
            series = forecast?.rainSteps.orEmpty(),
            centre = forecast?.rain ?: Centre(emptyList(), emptyList()),
            unit = forecast?.precipitation?.unit ?: "mm/h",
            height = maxHeight,
            contentDescription = forecast?.summary?.text,
        )
    }
}

/**
 * The sentence, the age, and what the chart above it is actually of.
 *
 * The last of those is the part worth arguing for. Two documents can draw the
 * same-looking chart from quite different numbers: a configured location's band
 * is its own twenty members at its own square kilometre, while a coordinate's
 * is the ensemble within a few kilometres of it, read back off the published
 * frames and with no count of members behind it. Both are real uncertainty and
 * they are not the same claim, so the labels come from the same place the chart
 * takes its keys — if the line changes, the sentence describing it changes with
 * it rather than going quietly out of date.
 */
@Composable
private fun Provenance(snapshot: Snapshot?) {
    val now = System.currentTimeMillis() / 1000
    val forecast = snapshot?.forecast

    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(
            forecast?.summary?.text ?: snapshot?.problem?.text ?: "Nothing fetched yet",
            style = MaterialTheme.typography.bodyLarge,
        )

        val age = snapshot
            ?.takeIf { it.fetchedAt > 0 }
            ?.let { formatAge(it.ageSeconds(now)) }
        val staleness = when {
            snapshot == null -> null
            // Never fetched at all. `isStale` says true here — deliberately, so
            // that alerting refuses to evaluate — but "last checked a while ago"
            // would be claiming a check that never happened. The line above
            // already says why there is nothing ("No server set up yet").
            snapshot.fetchedAt <= 0L -> null
            snapshot.isStale(now) -> "Stale — last checked ${age ?: "a while ago"}"
            snapshot.problem != null -> "${snapshot.problem.text}; showing data from $age"
            else -> "Updated $age"
        }
        staleness?.let {
            Text(
                it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        val provenance: List<String> = when {
            forecast == null -> emptyList()
            forecast.outOfCoverage -> listOf(
                "Outside the radar domain — there is no rain forecast for this point at all."
            )

            else -> buildList {
                val centre = forecast.rain
                val band = centre.bands.firstOrNull()
                add(
                    if (band != null) {
                        "Bars are the ${centre.label}; the shaded band covers ${band.label}."
                    } else {
                        // An older server serves a coordinate as one number
                        // copied across the percentiles. Saying nothing here
                        // would leave a confident-looking chart unqualified.
                        "Bars are the ${centre.label}. This forecast carries no band, so " +
                            "nothing on the chart says how much the members disagreed."
                    }
                )
                if (CentreKey.Measured in centre.keys) {
                    add(
                        "The first steps are measured by radar, and carry no band — a " +
                            "measurement has no ensemble behind it to disagree."
                    )
                }
                if (forecast.location.adHoc) {
                    add(
                        "Read back off the published frames rather than from the members " +
                            "themselves, so the band answers \"near here\" rather than " +
                            "\"exactly here\", and there is no share-of-members behind it."
                    )
                }
                // A licence obligation as much as a courtesy: this draws two
                // organisations' data and neither is named anywhere else in the
                // app.
                listOfNotNull(forecast.source?.text, forecast.conditionsSource?.text)
                    .filter { it.isNotBlank() }
                    .forEach { add(it) }
            }
        }
        provenance.forEach { line ->
            Text(
                line,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/**
 * The hourly blocks, one card each.
 *
 * These are the web forecast page's other charts, and they are here for the
 * reason that page has them: the rain chart answers "will I get wet in the next
 * six hours" and nothing else, while a phone is also asked "is it worth a coat"
 * and "will it still be raining tomorrow". They come free — every point
 * document already carries them, and the app was parsing and discarding them.
 *
 * They are drawn as lines inside their bands rather than as bars, because they
 * are levels rather than amounts, and they are visibly a different kind of
 * chart from the one above so that nobody reads the two as the same
 * measurement at different resolutions. Different model, hourly, and out to two
 * days.
 */
private enum class Conditions(
    val label: String,
    /** Whether the axis must start at zero. Wrong for temperature. */
    val zeroFloor: Boolean,
    /** The smallest range worth drawing, in the series' own units. */
    val minSpan: Double,
    val light: Int,
    val dark: Int,
) {
    Temperature("Temperature", zeroFloor = false, minSpan = 4.0, light = 0xFFEA580C.toInt(), dark = 0xFFF97316.toInt()),
    Wind("Wind", zeroFloor = true, minSpan = 4.0, light = 0xFF16A34A.toInt(), dark = 0xFF22C55E.toInt()),
    Solar("Sunlight", zeroFloor = true, minSpan = 100.0, light = 0xFFCA8A04.toInt(), dark = 0xFFEAB308.toInt()),
    RainOutlook("Rain outlook", zeroFloor = true, minSpan = 1.0, light = 0xFF6366F1.toInt(), dark = 0xFF818CF8.toInt());

    fun blockOf(forecast: Forecast): Series? = when (this) {
        Temperature -> forecast.temperature
        Wind -> forecast.wind
        Solar -> forecast.solar
        RainOutlook -> forecast.precipitationOutlook
    }?.takeIf { it.series.size >= 2 }

    fun accent(night: Boolean): Int = if (night) dark else light

    /**
     * Axis labels. Deliberately coarser than the values behind them: a
     * temperature axis reading 15.0, 17.5, 20.0 spends its width on decimals
     * nobody reads off a gridline.
     */
    fun format(value: Double): String = when (this) {
        RainOutlook -> formatRate(value)
        else -> value.roundToInt().toString()
    }

    /** What this block is, in the one line there is room for under it. */
    fun caption(forecast: Forecast): String = when (this) {
        RainOutlook -> {
            val ends = forecast.rainEndsAt
            if (ends != null) {
                "A different model from the chart above, hourly, picking up at " +
                    "${formatClock(ends)} where the radar forecast stops."
            } else {
                "Hourly, and the only rain forecast there is for this point — " +
                    "the radar does not reach here."
            }
        }

        else -> "Hourly, from the same ensemble as the outlook below."
    }
}

@Composable
private fun ConditionCards(snapshot: Snapshot?) {
    val forecast = snapshot?.forecast ?: return
    val night = isSystemInDarkTheme()
    val density = LocalDensity.current.density
    val now = System.currentTimeMillis() / 1000
    val stale = snapshot.isStale(now)

    Conditions.values().forEach { kind ->
        val block = kind.blockOf(forecast) ?: return@forEach
        val centre = Centre.of(block)
        // Nothing to draw a line from. Not expected — these blocks are medians
        // of real members — but a card with an empty chart in it is worse than
        // no card, and the server is allowed to change its mind.
        if (centre.keys.none { key -> block.series.any { it.value(key) != null } }) return@forEach

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier.padding(12.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text(kind.label, style = MaterialTheme.typography.titleMedium)

                BoxWithConstraints(modifier = Modifier.fillMaxWidth().height(150.dp)) {
                    val widthPx = with(LocalDensity.current) { maxWidth.roundToPx() }
                    val heightPx = with(LocalDensity.current) { maxHeight.roundToPx() }
                    val palette = ChartPalette.of(night)
                        .accented(kind.accent(night))
                        .let { if (stale) it.muted() else it }

                    val rendered = remember(block, widthPx, heightPx, night, stale) {
                        ChartRenderer.renderBand(
                            block = block,
                            centre = centre,
                            referenceTime = forecast.referenceTime,
                            widthPx = widthPx,
                            heightPx = heightPx,
                            density = density,
                            palette = palette,
                            zeroFloor = kind.zeroFloor,
                            minSpan = kind.minSpan,
                            format = kind::format,
                        )
                    }

                    ScrubbableChart(
                        rendered = rendered,
                        series = block.series,
                        centre = centre,
                        unit = block.unit,
                        height = maxHeight,
                        contentDescription = "${kind.label} forecast",
                    )
                }

                Text(
                    kind.caption(forecast),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}
