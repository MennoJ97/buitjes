package nl.buitjes.android.data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import kotlinx.coroutines.flow.first
import kotlinx.serialization.json.Json
import nl.buitjes.core.Forecast
import nl.buitjes.core.ForecastJson

/**
 * Which forecast a widget, a screen or a rule is about.
 *
 * A closed set of two, because there are only two kinds of question the server
 * can answer: one of the locations somebody configured on the server, or
 * wherever the phone happens to be. `storageKey` is the string form used for
 * DataStore keys and for the per-widget Glance state — stable, because a widget
 * placed today has to still mean the same place after an upgrade.
 */
sealed interface WidgetTarget {
    val storageKey: String
    val label: String

    data class Named(val name: String) : WidgetTarget {
        override val storageKey: String get() = "named:$name"
        // Server-side names are lower-case slugs (`home`, `work`, `oma`); the
        // first letter is raised for display and nothing else is touched,
        // because a slug someone chose is the closest thing to a label there is.
        override val label: String get() = name.replaceFirstChar(Char::uppercaseChar)
    }

    data object Here : WidgetTarget {
        override val storageKey: String get() = "here"
        override val label: String get() = "Here"
    }

    companion object {
        fun parse(key: String?): WidgetTarget? = when {
            key.isNullOrBlank() -> null
            key == Here.storageKey -> Here
            key.startsWith("named:") -> key.removePrefix("named:")
                .takeIf { it.isNotBlank() }
                ?.let(::Named)

            else -> null
        }
    }
}

/** Why the last attempt did not produce a fresh answer. */
sealed interface Problem {
    val text: String

    data object NotConfigured : Problem {
        override val text: String get() = "No server set up yet"
    }

    data object Offline : Problem {
        override val text: String get() = "Could not reach the server"
    }

    data object Unauthorized : Problem {
        override val text: String get() = "The server rejected the API key"
    }

    data object WarmingUp : Problem {
        override val text: String get() = "The server has not published a forecast yet"
    }

    data object NoLocation : Problem {
        override val text: String get() = "Location permission is off"
    }

    data object NoProvider : Problem {
        override val text: String get() = "Location is switched off"
    }

    data object NoFix : Problem {
        override val text: String get() = "Could not get a location fix"
    }

    data class Server(val reason: String) : Problem {
        override val text: String get() = reason
    }
}

/**
 * What is known about one target right now: the best forecast available, when
 * it was actually fetched, and what went wrong if anything did.
 *
 * `forecast` and `problem` are both nullable and are not mutually exclusive.
 * That combination is the whole point — a cached document plus a `problem` is
 * the ordinary offline case, and it is the state the widget renders greyed. A
 * shape that forced a choice between "data" and "error" would make that state
 * unrepresentable, and the widget would have to choose between lying and going
 * blank.
 */
data class Snapshot(
    val target: WidgetTarget,
    val forecast: Forecast?,
    val fetchedAt: Long,
    val problem: Problem?,
) {
    val label: String get() = target.label

    fun ageSeconds(now: Long): Long =
        if (fetchedAt <= 0L) Long.MAX_VALUE else (now - fetchedAt).coerceAtLeast(0L)

    /**
     * Old enough that showing it as current would be a lie.
     *
     * Thirty minutes is chosen against what the data is, not against how often
     * the app refreshes. A KNMI cycle is republished every five minutes and the
     * forecast reaches six hours out, so a half-hour-old document is still
     * broadly right about the next few hours and worth showing — it is just no
     * longer worth trusting about the next twenty minutes, which is the question
     * a rain widget is usually being asked.
     */
    fun isStale(now: Long): Boolean = ageSeconds(now) > STALE_AFTER_SECONDS

    companion object {
        const val STALE_AFTER_SECONDS = 30L * 60L
    }
}

/**
 * Resolves a target to a forecast, and remembers the last good answer.
 *
 * The cache exists for the widget, not for politeness to the server. A home
 * screen is looked at in lifts, on trains and in the seconds after a phone
 * unlocks in a basement, and a widget that blanks whenever the network does is
 * a widget that is empty exactly when someone glances at it. So the last good
 * document is kept per target and rendered with its age on it.
 */
class ForecastRepository(private val context: Context) {

    /**
     * Fetch, falling back to whatever was last stored for this target.
     *
     * Note the order: the cache is only read after a fetch has failed. Reading
     * it first and returning early would make the widget fast and permanently
     * one cycle behind.
     */
    suspend fun refresh(target: WidgetTarget, prefs: Prefs? = null): Snapshot {
        val settings = prefs ?: Settings.current(context)
        if (!settings.configured) return cached(target, Problem.NotConfigured)

        val client = BuitjesClient(settings.baseUrl, settings.apiKey)
        val result = when (target) {
            is WidgetTarget.Named -> client.pointForName(target.name)
            WidgetTarget.Here -> when (val fix = LocationSource.current(context)) {
                is Fix.Known -> client.pointAt(fix.lat, fix.lon)
                // A location failure is not a fetch failure, and is reported as
                // itself: "location is off" is something the reader can act on
                // in ten seconds, where "could not reach the server" would send
                // them to check a server that is fine.
                Fix.PermissionMissing -> return cached(target, Problem.NoLocation)
                Fix.NoProvider -> return cached(target, Problem.NoProvider)
                Fix.Unavailable -> return cached(target, Problem.NoFix)
            }
        }

        return when (result) {
            is FetchResult.Success -> {
                store(target, result.body)
                Snapshot(target, result.value, nowSeconds(), problem = null)
            }

            FetchResult.Offline -> cached(target, Problem.Offline)
            FetchResult.Unauthorized -> cached(target, Problem.Unauthorized)
            FetchResult.WarmingUp -> cached(target, Problem.WarmingUp)
            is FetchResult.Failed -> cached(target, Problem.Server(result.reason))
        }
    }

    /** The stored answer and nothing else — what the widget renders from. */
    suspend fun cached(target: WidgetTarget, problem: Problem? = null): Snapshot {
        val store = context.buitjesStore.data.first()
        val body = store[documentKey(target)]
        val at = store[fetchedAtKey(target)] ?: 0L
        // A cached document that no longer parses is dropped rather than
        // reported: it means the app was downgraded, or :core changed its mind
        // about the schema, and neither is something the reader can do anything
        // about. Behaving as though there were simply no cache yet is the
        // recovery, and the next successful fetch overwrites it.
        val forecast = body?.let { runCatching { ForecastJson.parse(it) }.getOrNull() }
        return Snapshot(target, forecast, if (forecast == null) 0L else at, problem)
    }

    private suspend fun store(target: WidgetTarget, body: String) {
        context.buitjesStore.edit { store ->
            store[documentKey(target)] = body
            store[fetchedAtKey(target)] = nowSeconds()
        }
    }

    /**
     * Drop cached documents for targets nothing refers to any more.
     *
     * Done here, on the refresh pass, rather than in the widget receiver's
     * `onDeleted`. That callback hands over app-widget ids, and an id is not the
     * cache key — two widgets can watch `home`, so removing one of them must not
     * evict the document the other is still rendering. Reconciling against the
     * live set of targets is the only version of this that is correct, and the
     * refresh pass is where that set is already known.
     */
    suspend fun prune(live: Set<WidgetTarget>) {
        val keep = live.map { it.storageKey }.toSet()
        context.buitjesStore.edit { store ->
            // Snapshotted before the loop, because removing from `store` while
            // walking its own key set is asking for trouble. A storage key can
            // itself contain a colon (`named:home`), so what follows the prefix
            // is taken whole rather than split on the last separator.
            val names = store.asMap().keys.map { it.name }
            for (name in names) {
                when {
                    name.startsWith(DOCUMENT_PREFIX) ->
                        if (name.removePrefix(DOCUMENT_PREFIX) !in keep) {
                            store.remove(stringPreferencesKey(name))
                        }

                    name.startsWith(FETCHED_AT_PREFIX) ->
                        if (name.removePrefix(FETCHED_AT_PREFIX) !in keep) {
                            store.remove(longPreferencesKey(name))
                        }
                }
            }
        }
    }

    /**
     * The configured locations, from the server if it answers and from the last
     * successful answer if it does not.
     *
     * Cached for the same reason the forecasts are: the widget configuration
     * screen appears when somebody drags a widget onto the home screen, which is
     * not a moment they chose for its connectivity.
     */
    suspend fun locations(prefs: Prefs? = null): List<NamedPoint> {
        val settings = prefs ?: Settings.current(context)
        if (!settings.configured) return cachedLocations()

        val result = BuitjesClient(settings.baseUrl, settings.apiKey).manifest()
        if (result !is FetchResult.Success) return cachedLocations()

        val points = result.value.points
        // An empty list is not written over a good one. The server omits
        // `points` entirely when the presented key is not valid, so "no
        // locations" and "not allowed to see the locations" arrive identically —
        // and forgetting the list because a key expired would leave the widget
        // configuration screen empty with nothing to explain itself.
        if (points.isEmpty()) return cachedLocations()

        context.buitjesStore.edit { store ->
            store[LOCATIONS] = json.encodeToString(points)
        }
        return points
    }

    private suspend fun cachedLocations(): List<NamedPoint> {
        val stored = context.buitjesStore.data.first()[LOCATIONS] ?: return emptyList()
        return runCatching { json.decodeFromString<List<NamedPoint>>(stored) }.getOrDefault(emptyList())
    }

    private companion object {
        const val DOCUMENT_PREFIX = "cache:doc:"
        const val FETCHED_AT_PREFIX = "cache:at:"

        val LOCATIONS = stringPreferencesKey("cache:locations")
        val json = Json { ignoreUnknownKeys = true }

        fun documentKey(target: WidgetTarget) =
            stringPreferencesKey("$DOCUMENT_PREFIX${target.storageKey}")

        fun fetchedAtKey(target: WidgetTarget) =
            longPreferencesKey("$FETCHED_AT_PREFIX${target.storageKey}")

        fun nowSeconds() = System.currentTimeMillis() / 1000
    }
}
