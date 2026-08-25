package nl.buitjes.android.work

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import nl.buitjes.android.MainActivity
import nl.buitjes.android.R
import nl.buitjes.android.data.WidgetTarget

/**
 * Two channels, and the reason there are two rather than one.
 *
 * Rain arriving in twenty minutes is worth a sound and a heads-up banner; a
 * server that has not answered for two hours is worth knowing and worth nothing
 * else. Putting both on one channel forces a choice between an alarm that also
 * shouts about network problems and a rain alert that arrives silently — and
 * whichever is chosen, the reader's only recourse is to turn the whole thing
 * off. Separate channels hand that decision to them properly: the OS lets each
 * be tuned or muted on its own, and muting the quiet one costs nothing.
 */
object Notifications {

    const val CHANNEL_ALERTS = "alerts"
    const val CHANNEL_ISSUES = "issues"

    /**
     * Issue notifications all share one id.
     *
     * There is only ever one thing wrong worth saying, and the alternative —
     * an id per kind of problem — produces a notification shade with three
     * different complaints about the same unreachable server. The newest
     * replaces the last.
     */
    private const val ISSUE_NOTIFICATION_ID = 1

    fun ensureChannels(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return

        // Created on every pass rather than once at startup. Creating a channel
        // that already exists is a no-op — the system keeps whatever the reader
        // has since configured — and there is no reliable single moment to do it
        // instead, because a widget-only install may never open the activity.
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ALERTS,
                context.getString(R.string.channel_alerts_name),
                NotificationManager.IMPORTANCE_HIGH,
            ).apply {
                description = context.getString(R.string.channel_alerts_description)
            }
        )

        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ISSUES,
                context.getString(R.string.channel_issues_name),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = context.getString(R.string.channel_issues_description)
                setShowBadge(false)
            }
        )
    }

    /**
     * Whether a notification posted now would be seen by anybody.
     *
     * Checked before posting rather than caught afterwards, because
     * `NotificationManagerCompat.notify` without the runtime permission throws
     * on API 33+ — inside a worker, which would fail the whole refresh pass and
     * leave the widgets un-updated over a notification that was never going to
     * appear.
     */
    fun canPost(context: Context): Boolean {
        val granted = android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        return granted && NotificationManagerCompat.from(context).areNotificationsEnabled()
    }

    fun postAlert(context: Context, ruleKey: String, target: WidgetTarget, title: String, body: String) {
        if (!canPost(context)) return

        val notification = NotificationCompat.Builder(context, CHANNEL_ALERTS)
            .setSmallIcon(R.drawable.ic_stat_rain)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_EVENT)
            .setAutoCancel(true)
            .setContentIntent(openForecast(context, target))
            .build()

        // Keyed by the rule, so a rule that fires again after its quiet period
        // replaces its own previous notification instead of stacking a second
        // one about the same place. Two rules watching different places keep
        // their own rows, which is the case where two rows are wanted.
        post(context, ruleKey.hashCode(), notification)
    }

    fun postIssue(context: Context, title: String, body: String) {
        if (!canPost(context)) return

        val notification = NotificationCompat.Builder(context, CHANNEL_ISSUES)
            .setSmallIcon(R.drawable.ic_stat_rain)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setSilent(true)
            .setAutoCancel(true)
            .setContentIntent(openForecast(context, target = null))
            .build()

        post(context, ISSUE_NOTIFICATION_ID, notification)
    }

    private fun post(context: Context, id: Int, notification: android.app.Notification) {
        // Belt and braces around the permission check above: it can be revoked
        // between the two calls, and a SecurityException here would take the
        // widget refresh down with it.
        runCatching { NotificationManagerCompat.from(context).notify(id, notification) }
    }

    private fun openForecast(context: Context, target: WidgetTarget?): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .apply { target?.let { putExtra(MainActivity.EXTRA_TARGET, it.storageKey) } }

        return PendingIntent.getActivity(
            context,
            // The target is folded into the request code so that two alerts for
            // different places do not collide onto one PendingIntent — without
            // it, the second alert would silently reuse the first one's extras
            // and open the wrong forecast.
            target?.storageKey?.hashCode() ?: 0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }
}
