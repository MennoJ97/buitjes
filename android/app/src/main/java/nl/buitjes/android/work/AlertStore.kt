package nl.buitjes.android.work

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import kotlinx.coroutines.flow.first
import nl.buitjes.android.data.WidgetTarget
import nl.buitjes.android.data.buitjesStore
import nl.buitjes.core.RuleState

/**
 * Which rules are latched, surviving reboots and app updates.
 *
 * The same job `AlertState` does in the ingestor, and for the same reason:
 * without it, every restart re-announces rain that was announced an hour ago —
 * and on a phone, "restart" includes the system killing a background process
 * because a game wanted the memory, which happens constantly and at no
 * particular time.
 *
 * State is keyed by `AlertRule.key`, which is the rule's whole shape, so
 * editing a threshold re-arms it rather than inheriting the latch of the rule
 * it replaced. An edited rule is a different question and deserves an answer to
 * itself.
 *
 * Crucially the key does *not* include a coordinate, even for the
 * current-location rule. The phone moves; one shower does not become two
 * because somebody walked to the shops during it. Hysteresis on the fetched
 * window is what ends a spell, exactly as it does server-side, and a
 * coordinate-keyed latch would re-arm on every street corner.
 */
object AlertStore {

    suspend fun state(context: Context, ruleKey: String): RuleState {
        val stored = context.buitjesStore.data.first()[stateKey(ruleKey)] ?: return fresh()
        return decode(stored) ?: fresh()
    }

    suspend fun remember(context: Context, ruleKey: String, state: RuleState) {
        context.buitjesStore.edit { store -> store[stateKey(ruleKey)] = encode(state) }
    }

    /** Forget rules that are no longer configured, mirroring `AlertState.prune`. */
    suspend fun prune(context: Context, liveKeys: Set<String>) {
        val keep = liveKeys.map { STATE_PREFIX + it }.toSet()
        context.buitjesStore.edit { store ->
            val names = store.asMap().keys.map { it.name }
            for (name in names) {
                if (name.startsWith(STATE_PREFIX) && name !in keep) {
                    store.remove(stringPreferencesKey(name))
                }
            }
        }
    }

    /**
     * Whether the reader has already been told they are outside radar coverage.
     *
     * A flag rather than a timestamp, cleared the moment coverage returns, so
     * the notice arrives once per trip abroad rather than once every fifteen
     * minutes for a fortnight.
     */
    suspend fun coverageNoticed(context: Context, target: WidgetTarget): Boolean =
        context.buitjesStore.data.first()[coverageKey(target)] ?: false

    suspend fun setCoverageNoticed(context: Context, target: WidgetTarget, noticed: Boolean) {
        context.buitjesStore.edit { store -> store[coverageKey(target)] = noticed }
    }

    /**
     * When the server last answered, and when we last complained that it had
     * not. Both are needed: the first says whether there is a problem, the
     * second stops the problem being reported every quarter of an hour.
     */
    suspend fun lastSuccess(context: Context): Long =
        context.buitjesStore.data.first()[LAST_SUCCESS] ?: 0L

    suspend fun setLastSuccess(context: Context, at: Long) {
        context.buitjesStore.edit { store -> store[LAST_SUCCESS] = at }
    }

    suspend fun lastComplaint(context: Context): Long =
        context.buitjesStore.data.first()[LAST_COMPLAINT] ?: 0L

    suspend fun setLastComplaint(context: Context, at: Long) {
        context.buitjesStore.edit { store -> store[LAST_COMPLAINT] = at }
    }

    // ---------------------------------------------------------------- encoding

    /**
     * `active|lastFired|lastOnset`, with an empty third field for null.
     *
     * Three primitives do not justify a JSON document and a serializer; what
     * they do justify is a decoder that treats anything it does not recognise as
     * "no state", since the cost of getting that wrong is one duplicate
     * notification and the cost of throwing is a worker that never runs again.
     */
    private fun encode(state: RuleState): String =
        "${if (state.active) 1 else 0}|${state.lastFired}|${state.lastOnset ?: ""}"

    private fun decode(text: String): RuleState? {
        val parts = text.split('|')
        if (parts.size != 3) return null
        val active = parts[0] == "1"
        val lastFired = parts[1].toLongOrNull() ?: return null
        val lastOnset = parts[2].takeIf { it.isNotEmpty() }?.toLongOrNull()
        return RuleState(active = active, lastFired = lastFired, lastOnset = lastOnset)
    }

    private fun fresh() = RuleState(active = false, lastFired = 0L, lastOnset = null)

    private const val STATE_PREFIX = "alert:state:"

    private val LAST_SUCCESS = longPreferencesKey("alert:last-success")
    private val LAST_COMPLAINT = longPreferencesKey("alert:last-complaint")

    private fun stateKey(ruleKey: String) = stringPreferencesKey(STATE_PREFIX + ruleKey)

    private fun coverageKey(target: WidgetTarget) =
        booleanPreferencesKey("alert:coverage-noticed:${target.storageKey}")
}
