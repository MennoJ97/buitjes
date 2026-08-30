package nl.buitjes.android.ui

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings as SystemSettings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Slider
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import nl.buitjes.android.data.ForecastRepository
import nl.buitjes.android.data.LocationSource
import nl.buitjes.android.data.NamedPoint
import nl.buitjes.android.data.Prefs
import nl.buitjes.android.data.Settings
import nl.buitjes.android.work.RefreshWorker
import kotlin.math.roundToInt

/**
 * The rule editor, which is one rule applied to several places.
 *
 * One shared threshold and lead time rather than a rule per location, because
 * the question "how much rain, how far ahead, before you want to know" is a
 * property of the person, not of the place. Somebody who wants half a
 * millimetre an hour with an hour's warning wants it for home and for wherever
 * they are standing, and per-place thresholds would be four sliders to keep in
 * agreement.
 */
@Composable
fun AlertsScreen(modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val repository = remember { ForecastRepository(context) }

    var prefs by remember { mutableStateOf(Prefs()) }
    var locations by remember { mutableStateOf<List<NamedPoint>>(emptyList()) }
    var hasLocation by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        prefs = Settings.current(context)
        locations = repository.locations()
        hasLocation = LocationSource.hasPermission(context)
    }

    fun edit(transform: (Prefs) -> Prefs) {
        prefs = transform(prefs)
        scope.launch {
            Settings.update(context, transform)
            // A changed rule is a new question, so the answer should not wait
            // fifteen minutes. It also re-arms the latch, since the rule's key
            // includes its thresholds — which is why turning the threshold down
            // during a shower will fire once and then settle.
            RefreshWorker.schedule(context)
            RefreshWorker.refreshNow(context)
        }
    }

    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        hasLocation = granted
        if (granted) edit { it.copy(alertHere = true) }
    }

    Column(
        modifier = modifier
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            Column(Modifier.weight(1f)) {
                Text("Rain alerts", style = MaterialTheme.typography.titleLarge)
                Text(
                    "Checked every 15 minutes or so. Android decides exactly when, and " +
                        "stretches it while the phone is asleep.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(
                checked = prefs.alertsEnabled,
                onCheckedChange = { on -> edit { it.copy(alertsEnabled = on) } },
            )
        }

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text("Watch", style = MaterialTheme.typography.titleMedium)

                CheckRow(
                    checked = prefs.alertHere,
                    label = "Wherever I am",
                    detail = if (hasLocation) {
                        "Coarse location, to about a kilometre."
                    } else {
                        "Needs location permission."
                    },
                ) { on ->
                    if (on && !hasLocation) {
                        permissionLauncher.launch(Manifest.permission.ACCESS_COARSE_LOCATION)
                    } else {
                        edit { it.copy(alertHere = on) }
                    }
                }

                locations.forEach { point ->
                    CheckRow(
                        checked = point.name in prefs.alertLocations,
                        label = point.name.replaceFirstChar(Char::uppercaseChar),
                        detail = "Configured on the server, with full ensemble spread.",
                    ) { on ->
                        edit {
                            it.copy(
                                alertLocations = if (on) {
                                    it.alertLocations + point.name
                                } else {
                                    it.alertLocations - point.name
                                },
                            )
                        }
                    }
                }
            }
        }

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text("When to speak up", style = MaterialTheme.typography.titleMedium)

                SettingSlider(
                    label = "At least ${formatRate(prefs.thresholdMmH)} mm/h",
                    detail = "Anything lighter than this is drizzle you would not turn back for.",
                    value = prefs.thresholdMmH.toFloat(),
                    range = 0.1f..5f,
                    steps = 48,
                ) { value ->
                    // Rounded to a tenth, because the rule's identity is its
                    // numbers: an un-rounded 0.5000000074 from a slider would
                    // key a different rule from the 0.5 already stored, and the
                    // latch would reset on every drag.
                    edit { it.copy(thresholdMmH = (value * 10).roundToInt() / 10.0) }
                }

                SettingSlider(
                    label = "Looking ${prefs.withinMinutes} minutes ahead",
                    detail = "How much warning you want. Longer means more false alarms, " +
                        "because the forecast is less sure that far out.",
                    value = prefs.withinMinutes.toFloat(),
                    range = 15f..180f,
                    steps = 10,
                ) { value ->
                    edit { it.copy(withinMinutes = (value / 15f).roundToInt() * 15) }
                }

                SettingSlider(
                    label = "Quiet for ${prefs.quietMinutes} minutes after",
                    detail = "A floor on how often one rule may speak, whatever the weather " +
                        "does.",
                    value = prefs.quietMinutes.toFloat(),
                    range = 15f..240f,
                    steps = 14,
                ) { value ->
                    edit { it.copy(quietMinutes = (value / 15f).roundToInt() * 15) }
                }

                Text(
                    "An alert fires when rain appears in that window having not been there " +
                        "before, and then stays quiet until the window has clearly dried out. " +
                        "The same rules the server uses for its own webhook alerts.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        if (prefs.alertHere) {
            BackgroundLocationNote()
        }
    }
}

/**
 * The explanation Android will not give on your behalf.
 *
 * "Allow all the time" cannot be requested from a dialog on API 30 and above —
 * the system requires a trip to Settings, and a permission request fired for it
 * returns denied without showing anything at all. So the only workable flow is
 * to explain first and then open the right page. Worth doing properly: from the
 * Settings screen the option is called "Allow all the time" with no mention of
 * rain, and somebody who has not been told what they are looking for will not
 * find it.
 */
@Composable
private fun BackgroundLocationNote() {
    val context = LocalContext.current

    Card(modifier = Modifier.fillMaxWidth()) {
        Column(
            modifier = Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text("Checking while the app is closed", style = MaterialTheme.typography.titleMedium)
            Text(
                "To warn you about rain where you are, the app has to check your location " +
                    "every so often while it is not open. Android only allows that if " +
                    "location access is set to \"Allow all the time\", and only from its own " +
                    "settings screen — it cannot be asked for here.\n\n" +
                    "Without it, alerts for named locations still work, and so does every " +
                    "widget. Only \"wherever I am\" needs it.",
                style = MaterialTheme.typography.bodyMedium,
            )
            OutlinedButton(
                onClick = {
                    // The app's own details page rather than the global location
                    // settings: it is the one screen where the choice actually
                    // is, and it needs no package-visibility declaration.
                    context.startActivity(
                        Intent(
                            SystemSettings.ACTION_APPLICATION_DETAILS_SETTINGS,
                            Uri.fromParts("package", context.packageName, null),
                        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                    )
                },
            ) {
                Text(
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                        "Open location settings"
                    } else {
                        "Open app settings"
                    }
                )
            }
        }
    }
}

@Composable
private fun CheckRow(
    checked: Boolean,
    label: String,
    detail: String,
    onChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Checkbox(checked = checked, onCheckedChange = onChange)
        Column(Modifier.padding(start = 4.dp)) {
            Text(label, style = MaterialTheme.typography.bodyLarge)
            Text(
                detail,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun SettingSlider(
    label: String,
    detail: String,
    value: Float,
    range: ClosedFloatingPointRange<Float>,
    steps: Int,
    onChange: (Float) -> Unit,
) {
    Column(modifier = Modifier.padding(top = 8.dp)) {
        Text(label, style = MaterialTheme.typography.bodyLarge)
        Text(
            detail,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Slider(
            value = value.coerceIn(range.start, range.endInclusive),
            onValueChange = onChange,
            valueRange = range,
            steps = steps,
        )
    }
}
