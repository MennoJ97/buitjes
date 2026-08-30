package nl.buitjes.android.work

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Puts the refresh schedule back after the phone or the app restarts.
 *
 * WorkManager already restores its own periodic work across a reboot, so on a
 * healthy install this receiver does nothing that would not have happened
 * anyway — which is exactly why it is safe to have. It is here for the cases
 * where that persistence has gone: a force-stop followed by an update, a
 * restore onto a new phone, a "clear data" that somebody meant to do to a
 * different app.
 *
 * `MY_PACKAGE_REPLACED` is the more useful of the two actions it listens for. A
 * force-stopped app is not running any workers and will not be, and nothing
 * would re-enqueue them until somebody opened the app — which, for a widget
 * that is quietly not updating and alerts that are quietly not firing, could be
 * a very long time.
 *
 * `schedule` uses UPDATE, so arriving here when everything was already fine
 * costs one no-op write and does not disturb the existing timing.
 */
class BootReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED,
            -> RefreshWorker.schedule(context.applicationContext)
        }
    }
}
