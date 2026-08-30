package nl.buitjes.android.work

import android.content.Context
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.state.getAppWidgetState
import androidx.glance.appwidget.updateAll
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import nl.buitjes.android.data.ForecastRepository
import nl.buitjes.android.data.Prefs
import nl.buitjes.android.data.Settings
import nl.buitjes.android.data.Snapshot
import nl.buitjes.android.data.WidgetTarget
import nl.buitjes.android.data.target
import nl.buitjes.android.data.where
import nl.buitjes.android.ui.formatClock
import nl.buitjes.android.widget.BuitjesWidget
import nl.buitjes.core.AlertEngine
import nl.buitjes.core.AlertRule
import java.util.concurrent.TimeUnit

/**
 * One pass: fetch what is needed, redraw the widgets, consider the alerts.
 *
 * Both jobs in one worker because they want the same documents. Two workers on
 * the same fifteen-minute cadence would fetch `home` twice, get a coarse
 * location fix twice, and — on a phone in Doze, where the two wakeups land
 * minutes apart — evaluate an alert against a forecast the widget beside it is
 * not showing. One pass, one set of documents, one story.
 */
class RefreshWorker(
    context: Context,
    parameters: WorkerParameters,
) : CoroutineWorker(context, parameters) {

    override suspend fun doWork(): Result {
        val context = applicationContext
        val prefs = Settings.current(context)
        if (!prefs.configured) return Result.success()

        val rules = prefs.rules()
        val widgetTargets = widgetTargets(context)
        val targets = widgetTargets + rules.map { it.target() }
        if (targets.isEmpty()) {
            // No widgets placed and no alerts armed. Nothing to do, and nothing
            // to cancel either — see `schedule` for why this worker is left
            // running in this state rather than torn down.
            return Result.success()
        }

        Notifications.ensureChannels(context)

        val repository = ForecastRepository(context)
        // Sequential, not parallel. There are rarely more than three targets,
        // they share one connection, and a phone that has just woken from Doze
        // has one radio: firing three requests at once wins nothing measurable
        // and makes the failure modes harder to reason about.
        val snapshots = targets.associateWith { target -> repository.refresh(target, prefs) }

        // Widgets first. Alert evaluation posts notifications and touches more
        // state, and if something in it were ever to throw, the home screen
        // should already have been brought up to date.
        BuitjesWidget().updateAll(context)

        if (rules.isNotEmpty()) {
            considerAlerts(context, rules, snapshots)
        }

        repository.prune(targets)
        AlertStore.prune(context, rules.map { it.key }.toSet())
        noteProlongedOutage(context, prefs, snapshots)

        // Always success, never retry, and that is a decision rather than an
        // oversight. `Result.retry()` on periodic work stacks a backoff attempt
        // on top of a schedule that is already going to run again in fifteen
        // minutes — so a phone that has been offline all morning comes back to a
        // queue of redundant attempts. Every failure here has already been
        // recorded where it matters: on the widget, as a greyed chart with its
        // age on it.
        return Result.success()
    }

    /**
     * Run every rule against the documents this pass already fetched.
     *
     * The state machine itself lives in :core and is the ingestor's, ported.
     * Nothing here decides whether to fire — this only supplies the previous
     * state, persists the new one, and delivers what comes back.
     */
    private suspend fun considerAlerts(
        context: Context,
        rules: List<AlertRule>,
        snapshots: Map<WidgetTarget, Snapshot>,
    ) {
        val now = System.currentTimeMillis() / 1000

        for (rule in rules) {
            val snapshot = snapshots[rule.target()] ?: continue
            val forecast = snapshot.forecast ?: continue

            // Evaluating a cached document would be evaluating the same window
            // again and again while offline — harmless for the latch, which is
            // already set, but capable of firing on an hour-old forecast the
            // moment the quiet period lapses. Alerting is about what is coming,
            // and a document this old has nothing to say about that.
            if (snapshot.problem != null && snapshot.isStale(now)) continue

            if (forecast.outOfCoverage) {
                pauseForCoverage(context, rule)
                continue
            }
            AlertStore.setCoverageNoticed(context, rule.target(), noticed = false)

            val previous = AlertStore.state(context, rule.key)
            val outcome = AlertEngine.consider(forecast, rule, previous, now)
            AlertStore.remember(context, rule.key, outcome.state)

            val event = outcome.fire ?: continue
            val (title, body) = AlertEngine.describe(event, now, rule.where())
            Notifications.postAlert(context, rule.key, rule.target(), title, body)
        }
    }

    /**
     * Outside the radar domain, alerting stops rather than relaxes.
     *
     * The server declines to serve a flat zero line for a coordinate it cannot
     * see, precisely so that a client abroad does not read "no rain" off a
     * document that means "no idea". Honouring that means not evaluating at all
     * — and saying so once, quietly, because a rain alarm that has silently
     * stopped being a rain alarm is worse than one that never worked.
     */
    private suspend fun pauseForCoverage(context: Context, rule: AlertRule) {
        if (AlertStore.coverageNoticed(context, rule.target())) return
        AlertStore.setCoverageNoticed(context, rule.target(), noticed = true)
        Notifications.postIssue(
            context,
            "Rain alerts paused",
            "${rule.where().replaceFirstChar(Char::uppercaseChar)} is outside the radar's " +
                "coverage, so there is nothing to watch. Alerts resume by themselves once " +
                "you are back inside it.",
        )
    }

    /**
     * Say something after a long silence, once, and only if alerts are armed.
     *
     * A widget going grey is enough of a signal on its own — it is on the home
     * screen and it is visibly old. An alert rule is different: it is invisible
     * when it is working and invisible when it is broken, so somebody relying on
     * one to be woken for rain deserves to hear that it has not been able to
     * check since breakfast.
     */
    private suspend fun noteProlongedOutage(
        context: Context,
        prefs: Prefs,
        snapshots: Map<WidgetTarget, Snapshot>,
    ) {
        val now = System.currentTimeMillis() / 1000
        val anySuccess = snapshots.values.any { it.problem == null }
        if (anySuccess) {
            AlertStore.setLastSuccess(context, now)
            AlertStore.setLastComplaint(context, 0L)
            return
        }
        if (!prefs.alertsEnabled) return

        val lastSuccess = AlertStore.lastSuccess(context)
        if (lastSuccess == 0L) {
            // Never succeeded at all: this is a setup problem, and the setup
            // screen is a better place to discover it than the shade.
            AlertStore.setLastSuccess(context, now)
            return
        }
        if (now - lastSuccess < OUTAGE_SECONDS) return
        if (now - AlertStore.lastComplaint(context) < COMPLAINT_INTERVAL_SECONDS) return

        AlertStore.setLastComplaint(context, now)
        val problem = snapshots.values.firstNotNullOfOrNull { it.problem }
        Notifications.postIssue(
            context,
            "Not checking the forecast",
            "${problem?.text ?: "Something went wrong"}. Rain alerts have been unable to " +
                "check since ${formatClock(lastSuccess)}.",
        )
    }

    companion object {
        private const val PERIODIC_WORK = "buitjes-refresh"
        private const val IMMEDIATE_WORK = "buitjes-refresh-now"

        /** Two hours of silence before the issues channel gets involved. */
        private const val OUTAGE_SECONDS = 2 * 60 * 60L

        /** And at most one complaint every six hours after that. */
        private const val COMPLAINT_INTERVAL_SECONDS = 6 * 60 * 60L

        /**
         * Fifteen minutes, which is the floor WorkManager allows and coarser
         * than the five-minute cycle the server publishes.
         *
         * That gap is deliberate and was reasoned about in the plan: the
         * server's five-minute cadence exists so webhook alerting catches the
         * edge of a shower, but the question a phone is asking is "will it rain
         * within the hour", and that answer barely moves between one cycle and
         * the next. Doze will stretch this towards an hour anyway, which the
         * sixty-minute default lead time absorbs.
         */
        private const val PERIOD_MINUTES = 15L

        /**
         * Ensure the periodic pass is scheduled.
         *
         * Called from more places than strictly need it — app start, widget
         * placement, boot, settings changes — because it is idempotent and the
         * failure mode of *not* calling it is a widget that never updates.
         *
         * There is deliberately no matching `cancel`. Working out the exact
         * moment nothing is left to refresh means reading per-widget Glance
         * state, which is a suspending call, from places like a broadcast
         * receiver's main thread. What that complexity would save is a worker
         * that wakes every fifteen minutes, reads two preferences, finds nothing
         * to do and goes back to sleep — which costs far less than getting the
         * teardown subtly wrong and leaving someone's alerts silently disarmed.
         */
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<RefreshWorker>(
                PERIOD_MINUTES, TimeUnit.MINUTES,
            )
                .setConstraints(
                    Constraints.Builder()
                        // Nothing here works without the network, and letting the
                        // pass run without one would burn a wakeup to discover
                        // that. The cached widget state is what covers the gap.
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .build()

            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                PERIODIC_WORK,
                // UPDATE rather than REPLACE: it applies a changed spec without
                // resetting the schedule, so calling this on every app start
                // does not push the next run fifteen minutes into the future
                // each time the app is opened — which, for someone who checks
                // the app often, would mean it never runs on its own at all.
                ExistingPeriodicWorkPolicy.UPDATE,
                request,
            )
        }

        /**
         * A refresh right now — the widget's tap, or a settings change that
         * makes the displayed data wrong.
         *
         * KEEP rather than REPLACE, so that tapping a widget four times while
         * nothing appears to happen enqueues one fetch rather than four.
         */
        fun refreshNow(context: Context) {
            val request = OneTimeWorkRequestBuilder<RefreshWorker>()
                .setConstraints(
                    Constraints.Builder()
                        .setRequiredNetworkType(NetworkType.CONNECTED)
                        .build()
                )
                .build()

            WorkManager.getInstance(context).enqueueUniqueWork(
                IMMEDIATE_WORK,
                ExistingWorkPolicy.KEEP,
                request,
            )
        }

        /**
         * Which places the placed widgets are watching.
         *
         * Read from each widget's own Glance state rather than from a list the
         * app keeps, because the launcher owns the truth about which widgets
         * exist. A widget deleted while the app was not running leaves no trace
         * anywhere else.
         */
        suspend fun widgetTargets(context: Context): Set<WidgetTarget> {
            val manager = GlanceAppWidgetManager(context)
            val ids = runCatching { manager.getGlanceIds(BuitjesWidget::class.java) }
                .getOrDefault(emptyList())

            return ids.mapNotNull { id ->
                val state = runCatching {
                    getAppWidgetState(context, PreferencesGlanceStateDefinition, id)
                }.getOrNull()
                WidgetTarget.parse(state?.get(BuitjesWidget.TARGET_KEY))
            }.toSet()
        }
    }
}
