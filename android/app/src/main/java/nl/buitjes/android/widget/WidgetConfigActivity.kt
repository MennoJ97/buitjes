package nl.buitjes.android.widget

import android.app.Activity
import android.appwidget.AppWidgetManager
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.appwidget.updateAll
import androidx.glance.state.PreferencesGlanceStateDefinition
import kotlinx.coroutines.launch
import androidx.lifecycle.lifecycleScope
import nl.buitjes.android.MainActivity
import nl.buitjes.android.data.ForecastRepository
import nl.buitjes.android.data.NamedPoint
import nl.buitjes.android.data.Settings
import nl.buitjes.android.data.WidgetTarget
import nl.buitjes.android.ui.BuitjesTheme
import nl.buitjes.android.work.RefreshWorker

/**
 * Shown when a widget is dropped on the home screen: which place is this one for?
 *
 * The list comes from `/api/config`, so the app never keeps its own copy of the
 * server's locations — add one on the server and it appears here. "Follow my
 * location" is offered first because it is the thing the web app cannot do and
 * therefore the reason this app exists, but a named location is what works with
 * location permission denied outright, which is a perfectly reasonable way to
 * use this.
 */
class WidgetConfigActivity : ComponentActivity() {

    private var appWidgetId: Int = AppWidgetManager.INVALID_APPWIDGET_ID

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        appWidgetId = intent?.extras?.getInt(
            AppWidgetManager.EXTRA_APPWIDGET_ID,
            AppWidgetManager.INVALID_APPWIDGET_ID,
        ) ?: AppWidgetManager.INVALID_APPWIDGET_ID

        // Set before anything else can finish this activity. A configuration
        // activity that returns without RESULT_OK tells the launcher to drop the
        // pending widget — which is exactly what should happen if somebody backs
        // out, and exactly what must not happen silently if the process is
        // killed halfway through.
        setResult(Activity.RESULT_CANCELED, resultIntent())

        if (appWidgetId == AppWidgetManager.INVALID_APPWIDGET_ID) {
            finish()
            return
        }

        setContent {
            BuitjesTheme {
                ConfigScreen(
                    loadLocations = {
                        ForecastRepository(this@WidgetConfigActivity).locations()
                    },
                    isConfigured = {
                        Settings.current(this@WidgetConfigActivity).configured
                    },
                    onPick = ::choose,
                    onOpenApp = {
                        startActivity(
                            Intent(this@WidgetConfigActivity, MainActivity::class.java)
                        )
                    },
                )
            }
        }
    }

    private fun choose(target: WidgetTarget) {
        lifecycleScope.launch {
            val glanceId = GlanceAppWidgetManager(this@WidgetConfigActivity)
                .getGlanceIdBy(appWidgetId)

            updateAppWidgetState(this@WidgetConfigActivity, PreferencesGlanceStateDefinition, glanceId) {
                it.toMutablePreferences().apply {
                    this[BuitjesWidget.TARGET_KEY] = target.storageKey
                }
            }
            BuitjesWidget().updateAll(this@WidgetConfigActivity)

            // The widget will render "no data yet" until this lands, which is
            // the correct thing for it to say in the meantime and better than
            // holding this screen open on a spinner while a fetch happens.
            RefreshWorker.schedule(this@WidgetConfigActivity)
            RefreshWorker.refreshNow(this@WidgetConfigActivity)

            setResult(Activity.RESULT_OK, resultIntent())
            finish()
        }
    }

    private fun resultIntent() =
        Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, appWidgetId)
}

@Composable
private fun ConfigScreen(
    loadLocations: suspend () -> List<NamedPoint>,
    isConfigured: suspend () -> Boolean,
    onPick: (WidgetTarget) -> Unit,
    onOpenApp: () -> Unit,
) {
    var locations by remember { mutableStateOf<List<NamedPoint>?>(null) }
    var configured by remember { mutableStateOf(true) }

    LaunchedEffect(Unit) {
        configured = isConfigured()
        locations = if (configured) loadLocations() else emptyList()
    }

    Scaffold { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("What should this widget show?", style = MaterialTheme.typography.titleLarge)

            when {
                !configured -> {
                    Text(
                        "The app has not been pointed at a server yet. Set that up first and " +
                            "then add the widget again.",
                        style = MaterialTheme.typography.bodyMedium,
                    )
                    Choice("Open Buitjes", "Set up the server", onOpenApp)
                }

                locations == null -> CircularProgressIndicator(
                    modifier = Modifier.align(Alignment.CenterHorizontally),
                )

                else -> LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    item {
                        Choice(
                            title = "Follow my location",
                            subtitle = "Rain for wherever the phone is, to about a kilometre",
                        ) { onPick(WidgetTarget.Here) }
                    }
                    items(locations.orEmpty()) { point ->
                        Choice(
                            title = point.name.replaceFirstChar(Char::uppercaseChar),
                            subtitle = "Configured on the server",
                        ) { onPick(WidgetTarget.Named(point.name)) }
                    }
                    if (locations.orEmpty().isEmpty()) {
                        item {
                            // Both readings of an empty list, because the server
                            // omits `points` entirely for an unrecognised key and
                            // the two are indistinguishable from here.
                            Text(
                                "The server did not offer any named locations. Either none are " +
                                    "configured, or the API key is not valid for them.",
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun Choice(title: String, subtitle: String, onClick: () -> Unit) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick),
    ) {
        Column(Modifier.padding(16.dp)) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
