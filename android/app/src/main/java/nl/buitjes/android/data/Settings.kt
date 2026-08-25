package nl.buitjes.android.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.doublePreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.core.stringSetPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map
import nl.buitjes.core.AlertRule
import nl.buitjes.core.AlertTargetKind

/**
 * The one DataStore this app has.
 *
 * One file, not three, and it is not a stylistic preference: DataStore throws
 * if two instances are created over the same file in a process, and this
 * process contains the UI, the Glance widget's rendering pass and the refresh
 * worker, all of which want to read settings. A single top-level delegate is
 * the only shape that cannot be constructed twice by accident.
 *
 * Three unrelated things therefore share it, separated by key prefix rather
 * than by file: settings (below), the cached forecast documents
 * (`ForecastRepository`) and the alert latches (`AlertStore`). They are written
 * from different places but never the same key, and DataStore serialises the
 * writes for us.
 */
val Context.buitjesStore: DataStore<Preferences> by preferencesDataStore(name = "buitjes")

/**
 * Everything the app needs to know before it can ask a question.
 *
 * The defaults for threshold, lead time and quiet period are the ingestor's
 * (`alerts.py`: 0.5 mm/h, 60 minutes, 60 minutes). Matching them is the point —
 * an alert should mean the same thing whether it came from the server's webhook
 * or from this phone, and someone comparing the two should not have to discover
 * that the phone is quietly twice as sensitive.
 */
data class Prefs(
    val baseUrl: String = "",
    val apiKey: String = "",
    val alertsEnabled: Boolean = false,
    val thresholdMmH: Double = 0.5,
    val withinMinutes: Int = 60,
    val quietMinutes: Int = 60,
    val alertHere: Boolean = false,
    val alertLocations: Set<String> = emptySet(),
) {
    val configured: Boolean get() = baseUrl.isNotBlank()

    /**
     * The rules to evaluate this pass, one per watched place.
     *
     * `probability` is pinned at zero for every rule, which in the state
     * machine's terms means "the median alone decides". That is not a
     * simplification for the sake of a smaller settings screen; it is the only
     * value that means the same thing on both endpoints. `/api/point/<name>`
     * carries a real per-step probability from the KNMI members, but
     * `/api/point?lat&lon` is sampled from median frames and has none — so a
     * rule asking for 40% agreement would fire for `home` and be silently
     * incapable of ever firing for wherever the phone actually is. Silent
     * asymmetry in an alarm is worse than a blunter alarm.
     */
    fun rules(): List<AlertRule> {
        if (!alertsEnabled) return emptyList()
        val here = if (alertHere) {
            listOf(
                AlertRule(
                    targetKind = AlertTargetKind.CURRENT_LOCATION,
                    locationName = null,
                    thresholdMmH = thresholdMmH,
                    withinSeconds = withinMinutes * 60,
                    probability = 0.0,
                    quietSeconds = quietMinutes * 60,
                )
            )
        } else {
            emptyList()
        }
        val named = alertLocations.sorted().map { name ->
            AlertRule(
                targetKind = AlertTargetKind.NAMED,
                locationName = name,
                thresholdMmH = thresholdMmH,
                withinSeconds = withinMinutes * 60,
                probability = 0.0,
                quietSeconds = quietMinutes * 60,
            )
        }
        return here + named
    }
}

/** Which forecast a rule is about. The rule knows the place; this knows the URL. */
fun AlertRule.target(): WidgetTarget = when (targetKind) {
    AlertTargetKind.CURRENT_LOCATION -> WidgetTarget.Here
    AlertTargetKind.NAMED -> WidgetTarget.Named(locationName.orEmpty())
}

/** How a rule is named to a human — the second half of `AlertEngine.describe`. */
fun AlertRule.where(): String = when (targetKind) {
    AlertTargetKind.CURRENT_LOCATION -> "your location"
    AlertTargetKind.NAMED -> locationName.orEmpty()
}

object Settings {

    private val BASE_URL = stringPreferencesKey("settings:base-url")
    private val API_KEY = stringPreferencesKey("settings:api-key")
    private val ALERTS_ENABLED = booleanPreferencesKey("settings:alerts-enabled")
    private val THRESHOLD = doublePreferencesKey("settings:threshold-mm-h")
    private val WITHIN = intPreferencesKey("settings:within-minutes")
    private val QUIET = intPreferencesKey("settings:quiet-minutes")
    private val ALERT_HERE = booleanPreferencesKey("settings:alert-here")
    private val ALERT_LOCATIONS = stringSetPreferencesKey("settings:alert-locations")

    fun flow(context: Context): Flow<Prefs> = context.buitjesStore.data.map(::read)

    suspend fun current(context: Context): Prefs = read(context.buitjesStore.data.first())

    /**
     * Read-modify-write in one transaction.
     *
     * The alerts screen has half a dozen controls that all mutate the same
     * record, and every one of them would otherwise need its own setter with
     * its own key. `transform` runs inside `edit`, so two controls touched in
     * quick succession cannot lose each other's change.
     */
    suspend fun update(context: Context, transform: (Prefs) -> Prefs) {
        context.buitjesStore.edit { store ->
            val next = transform(read(store))
            store[BASE_URL] = next.baseUrl
            store[API_KEY] = next.apiKey
            store[ALERTS_ENABLED] = next.alertsEnabled
            store[THRESHOLD] = next.thresholdMmH
            store[WITHIN] = next.withinMinutes
            store[QUIET] = next.quietMinutes
            store[ALERT_HERE] = next.alertHere
            store[ALERT_LOCATIONS] = next.alertLocations
        }
    }

    suspend fun setServer(context: Context, baseUrl: String, apiKey: String) {
        update(context) { it.copy(baseUrl = normaliseUrl(baseUrl), apiKey = apiKey.trim()) }
    }

    /**
     * Turn what somebody typed into something OkHttp will accept.
     *
     * People paste `buitjes.example.com`, or the URL of the page they were
     * looking at, complete with a trailing slash and sometimes `/forecast.html`.
     * A bare host with no scheme is not a URL at all and fails inside the HTTP
     * client with a message about protocols, which reads as a server problem
     * rather than a typing one — so the scheme is supplied here, and https is
     * the assumption because a LAN address is the case where somebody knows
     * enough to type `http://` themselves.
     *
     * The trailing slash goes because every request appends its own path
     * segments; leaving it would produce `//api/config` on some servers and a
     * 404 that looks like a missing endpoint.
     */
    fun normaliseUrl(raw: String): String {
        val trimmed = raw.trim().trimEnd('/')
        if (trimmed.isEmpty()) return ""
        val withScheme = if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
            trimmed
        } else {
            "https://$trimmed"
        }
        return withScheme.removeSuffix("/index.html").removeSuffix("/forecast.html").trimEnd('/')
    }

    private fun read(store: Preferences) = Prefs(
        baseUrl = store[BASE_URL].orEmpty(),
        apiKey = store[API_KEY].orEmpty(),
        alertsEnabled = store[ALERTS_ENABLED] ?: false,
        thresholdMmH = store[THRESHOLD] ?: 0.5,
        withinMinutes = store[WITHIN] ?: 60,
        quietMinutes = store[QUIET] ?: 60,
        alertHere = store[ALERT_HERE] ?: false,
        alertLocations = store[ALERT_LOCATIONS] ?: emptySet(),
    )
}
