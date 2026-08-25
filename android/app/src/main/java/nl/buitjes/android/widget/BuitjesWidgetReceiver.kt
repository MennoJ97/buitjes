package nl.buitjes.android.widget

import android.appwidget.AppWidgetManager
import android.content.Context
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import nl.buitjes.android.work.RefreshWorker

/**
 * The manifest's entry point into the widget.
 *
 * Almost everything is inherited; the only addition is making sure the refresh
 * schedule exists once a widget is on the home screen. That matters for the
 * install that never opens the app at all — somebody who sets the server up,
 * drops a widget, and then only ever looks at the home screen. Without this,
 * nothing would ever have enqueued the periodic pass and the widget would sit
 * on its first fetch forever.
 *
 * There is no `onDeleted` cleanup here on purpose. The ids it hands over are not
 * what the cache is keyed by, and two widgets can watch the same location, so
 * evicting on delete would sometimes take the document another widget is still
 * drawing. `ForecastRepository.prune` reconciles against the live set of targets
 * on the next refresh instead, which is the version of this that is correct
 * rather than merely prompt.
 */
class BuitjesWidgetReceiver : GlanceAppWidgetReceiver() {

    override val glanceAppWidget: GlanceAppWidget = BuitjesWidget()

    override fun onEnabled(context: Context) {
        super.onEnabled(context)
        RefreshWorker.schedule(context.applicationContext)
    }

    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray,
    ) {
        super.onUpdate(context, appWidgetManager, appWidgetIds)
        // This arrives from `updatePeriodMillis`, which the platform clamps to
        // half an hour and fires at a moment of its own choosing. Treat it as a
        // hint that the data may be old rather than as a redraw: the redraw
        // super.onUpdate already triggered will render whatever is cached, and
        // this asks for something newer to render next time.
        RefreshWorker.schedule(context.applicationContext)
        RefreshWorker.refreshNow(context.applicationContext)
    }
}
