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
import kotlinx.coroutines.launch
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

        val bitmap = remember(snapshot, widthPx, heightPx, night, stale) {
            ChartRenderer.render(
                forecast = snapshot?.forecast,
                widthPx = widthPx,
                heightPx = heightPx,
                density = density,
                palette = palette,
            )
        }

        Image(
            bitmap = bitmap.asImageBitmap(),
            contentDescription = snapshot?.forecast?.summary?.text,
            contentScale = ContentScale.FillBounds,
            modifier = Modifier.fillMaxWidth().height(maxHeight),
        )
    }
}

/**
 * The sentence, the age, and what kind of forecast this actually is.
 *
 * The last of those is the part worth arguing for. A named location carries
 * real KNMI ensemble spread at five-minute resolution; a coordinate is sampled
 * from median frames after the members have been averaged away, and has none.
 * Both draw a chart that looks equally confident, so the difference has to be
 * written down — the server marks it as `median_only` precisely so a client can
 * say so rather than quietly present one as the other.
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

        val kind = when {
            forecast == null -> null
            forecast.outOfCoverage ->
                "Outside the radar domain — there is no rain forecast for this point at all."

            forecast.medianOnly ->
                "Sampled from the published median frames, so there is no ensemble spread " +
                    "for this point — the chart shows the middle of the forecast and " +
                    "nothing about how much the members disagree."

            else ->
                "Full KNMI ensemble: the band is the 10th to 90th percentile of the members."
        }
        kind?.let {
            Text(
                it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
