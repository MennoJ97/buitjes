package nl.buitjes.android

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import nl.buitjes.android.data.WidgetTarget
import nl.buitjes.android.ui.AlertsScreen
import nl.buitjes.android.ui.BuitjesTheme
import nl.buitjes.android.ui.ForecastScreen
import nl.buitjes.android.ui.SetupScreen
import nl.buitjes.android.work.RefreshWorker

/**
 * The whole app, which is three screens and a bar.
 *
 * There is no navigation library and no ViewModel here, and that is a
 * deliberate floor rather than an oversight. Three destinations with no
 * back stack between them, no deep link beyond "show this location", and no
 * state worth surviving a process death that is not already in DataStore — a
 * nav graph would be more code describing the app than the app contains.
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Opening the app is as good a moment as any to make sure the schedule
        // exists, and it costs one idempotent write.
        RefreshWorker.schedule(applicationContext)

        setContent {
            BuitjesTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    App(initialTarget = WidgetTarget.parse(intent?.getStringExtra(EXTRA_TARGET)))
                }
            }
        }
    }

    companion object {
        /**
         * A widget or a notification saying which place it was about.
         *
         * Carried as the target's `storageKey` rather than as a pair of
         * coordinates, because "here" means "wherever the phone is when the
         * screen opens" — a coordinate baked in when the alert fired would show
         * somebody the forecast for the tram stop they have since left.
         */
        const val EXTRA_TARGET = "nl.buitjes.android.target"
    }
}

private enum class Destination(val label: String) {
    Forecast("Forecast"),
    Alerts("Alerts"),
    Setup("Server"),
}

@Composable
private fun App(initialTarget: WidgetTarget?) {
    var destination by rememberSaveable { mutableStateOf(Destination.Forecast) }

    Scaffold(
        bottomBar = {
            NavigationBar {
                Destination.values().forEach { entry ->
                    NavigationBarItem(
                        selected = destination == entry,
                        onClick = { destination = entry },
                        // No icons: three text labels are unambiguous, and a
                        // guessed-at icon per destination would need the
                        // material-icons artifact for three glyphs.
                        icon = {},
                        label = { Text(entry.label) },
                    )
                }
            }
        },
    ) { padding ->
        val modifier = Modifier.fillMaxSize().padding(padding)
        when (destination) {
            Destination.Forecast -> ForecastScreen(initialTarget, modifier)
            Destination.Alerts -> AlertsScreen(modifier)
            Destination.Setup -> SetupScreen(modifier)
        }
    }
}
