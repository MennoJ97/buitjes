package nl.buitjes.android.widget

import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.LocalContext
import androidx.glance.LocalSize
import androidx.glance.action.ActionParameters
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.SizeMode
import androidx.glance.appwidget.action.ActionCallback
import androidx.glance.appwidget.action.actionRunCallback
import androidx.glance.appwidget.action.actionStartActivity
import androidx.glance.appwidget.provideContent
import androidx.glance.background
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.ContentScale
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.state.GlanceStateDefinition
import androidx.glance.state.PreferencesGlanceStateDefinition
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import nl.buitjes.android.MainActivity
import nl.buitjes.android.data.ForecastRepository
import nl.buitjes.android.data.Snapshot
import nl.buitjes.android.data.WidgetTarget
import nl.buitjes.android.ui.ChartPalette
import nl.buitjes.android.ui.ChartRenderer
import nl.buitjes.android.ui.formatAge
import nl.buitjes.android.work.RefreshWorker

/**
 * Six hours of rain for one place, on the home screen.
 *
 * The widget never fetches. `provideGlance` runs whenever the launcher wants a
 * redraw — a resize, a theme change, a reboot — and doing network work on that
 * path would mean a home screen that stutters on rotation and a socket opened
 * every time somebody drags an icon past. Fetching belongs to `RefreshWorker`,
 * which calls `updateAll` when it has something new; this reads the cache and
 * draws it, honestly labelled with its age.
 *
 * The one exception is a widget that has never had data, which asks for a
 * refresh on its first render — otherwise a widget placed at 14:02 would show
 * nothing at all until the periodic pass came round.
 */
class BuitjesWidget : GlanceAppWidget() {

    override val stateDefinition: GlanceStateDefinition<Preferences> =
        PreferencesGlanceStateDefinition

    // Exact, because the chart is a bitmap and a bitmap has to be drawn at the
    // size it will be shown. The alternative modes hand out one of a few
    // buckets and let the layout stretch, which for a raster chart means blurred
    // axis labels on every size that is not exactly a bucket.
    override val sizeMode: SizeMode = SizeMode.Exact

    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val target = WidgetTarget.parse(currentTarget(context, id))
        val snapshot = target?.let { ForecastRepository(context).cached(it) }

        // Only for a configured widget with nothing to draw. An unconfigured one
        // has no location to fetch for, and a widget with stale data already has
        // something to show — enqueueing on every redraw would turn a rotation
        // into a fetch. `refreshNow` is unique-KEEP, so the repeats that do get
        // through collapse into one.
        if (target != null && snapshot?.forecast == null) {
            RefreshWorker.schedule(context)
            RefreshWorker.refreshNow(context)
        }

        provideContent {
            if (target == null || snapshot == null) {
                Unconfigured()
            } else {
                Body(snapshot)
            }
        }
    }

    private suspend fun currentTarget(context: Context, id: GlanceId): String? = runCatching {
        androidx.glance.appwidget.state.getAppWidgetState(
            context, PreferencesGlanceStateDefinition, id,
        )[TARGET_KEY]
    }.getOrNull()

    companion object {
        /** Per-widget, so two widgets can watch two different places. */
        val TARGET_KEY = stringPreferencesKey("widget:target")
    }
}

/**
 * The widget's own colours, resolved the hard way.
 *
 * Text and backgrounds could be handed to Glance as day/night pairs and left to
 * the framework — but the chart cannot, because it is a bitmap and its colours
 * are baked at the moment it is drawn. So night mode is read from the
 * configuration here and both halves are resolved from the same decision. Left
 * to their own devices, the framework would pick for the text and this file
 * would pick for the chart, and the two would disagree on precisely the phones
 * where somebody has forced dark mode for one app.
 */
private object WidgetPalette {
    fun isNight(context: Context): Boolean =
        (context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
            Configuration.UI_MODE_NIGHT_YES

    fun surface(night: Boolean) = Color(ChartPalette.of(night).surface)
    fun primaryText(night: Boolean) = if (night) Color(0xFFE6EAF2) else Color(0xFF111827)
    fun secondaryText(night: Boolean) = if (night) Color(0xFF8B94A7) else Color(0xFF6B7280)

    /** Faded, matching what `ChartPalette.muted()` does to the chart beside it. */
    fun fadedText(night: Boolean) = if (night) Color(0xFF5C667A) else Color(0xFF9CA3AF)
}

@Composable
private fun Unconfigured() {
    val context = LocalContext.current
    val night = WidgetPalette.isNight(context)
    Column(
        modifier = GlanceModifier
            .fillMaxSize()
            .background(WidgetPalette.surface(night))
            .padding(12.dp)
            // The Intent overload throughout, rather than the reified
            // `actionStartActivity<MainActivity>()`: the two live in different
            // packages under the same name, and importing both is a clash for
            // no gain when one of them can express everything.
            .clickable(actionStartActivity(openIntent(context, target = null))),
        verticalAlignment = Alignment.Vertical.CenterVertically,
        horizontalAlignment = Alignment.Horizontal.CenterHorizontally,
    ) {
        Text(
            "Tap to set this widget up",
            style = TextStyle(
                color = ColorProvider(WidgetPalette.secondaryText(night)),
                fontSize = 13.sp,
            ),
        )
    }
}

@Composable
private fun Body(snapshot: Snapshot) {
    val context = LocalContext.current
    val size = LocalSize.current
    val night = WidgetPalette.isNight(context)
    val now = System.currentTimeMillis() / 1000
    val stale = snapshot.isStale(now)

    val density = context.resources.displayMetrics.density
    val palette = ChartPalette.of(night).let { if (stale) it.muted() else it }

    // The chart gets whatever is left after the two text rows. Computed rather
    // than weighted because the bitmap must be sized in pixels before the layout
    // exists — Glance has no measure pass to ask.
    val chartHeightDp = (size.height.value - CHROME_HEIGHT_DP).coerceAtLeast(40f)

    val bitmap = ChartRenderer.render(
        forecast = snapshot.forecast,
        widthPx = ((size.width.value - 16f) * density).toInt(),
        heightPx = (chartHeightDp * density).toInt(),
        density = density,
        palette = palette,
    )

    Column(
        modifier = GlanceModifier
            .fillMaxSize()
            .background(WidgetPalette.surface(night))
            .padding(8.dp)
            // Tapping the body opens the app on this location. The footer,
            // below, is the refresh affordance — putting refresh on the whole
            // widget would make every accidental brush of the home screen fetch,
            // and putting "open" nowhere would make the widget a dead end.
            .clickable(actionStartActivity(openIntent(context, snapshot.target))),
    ) {
        Text(
            snapshot.label,
            maxLines = 1,
            style = TextStyle(
                color = ColorProvider(
                    if (stale) WidgetPalette.fadedText(night) else WidgetPalette.primaryText(night)
                ),
                fontSize = 14.sp,
                fontWeight = FontWeight.Medium,
            ),
        )

        Spacer(GlanceModifier.height(4.dp))

        Image(
            provider = ImageProvider(bitmap),
            contentDescription = snapshot.forecast?.summary?.text,
            contentScale = ContentScale.FillBounds,
            modifier = GlanceModifier.fillMaxWidth().height(chartHeightDp.dp),
        )

        Spacer(GlanceModifier.height(4.dp))

        Row(
            modifier = GlanceModifier
                .fillMaxWidth()
                .clickable(actionRunCallback<RefreshAction>()),
            verticalAlignment = Alignment.Vertical.CenterVertically,
        ) {
            Text(
                footerText(snapshot, now, stale),
                maxLines = 2,
                style = TextStyle(
                    color = ColorProvider(
                        if (stale) WidgetPalette.fadedText(night)
                        else WidgetPalette.secondaryText(night)
                    ),
                    fontSize = 11.sp,
                ),
            )
        }
    }
}

/**
 * The line under the chart: what is happening, and how much to trust it.
 *
 * Stale data says so first and in as many words. The greyed chart is the signal
 * somebody notices; the word is what stops them talking themselves out of it.
 * The server's own sentence follows, because it is the same sentence the web app
 * shows and it is better written than anything this app would assemble — but it
 * is the *cached* sentence, and saying "20 min ago" next to it is what keeps
 * that honest.
 */
private fun footerText(snapshot: Snapshot, now: Long, stale: Boolean): String {
    val summary = snapshot.forecast?.summary?.text
    val age = snapshot.fetchedAt.takeIf { it > 0 }?.let { formatAge(snapshot.ageSeconds(now)) }

    return when {
        summary == null -> snapshot.problem?.text ?: "No forecast yet"
        stale -> "Stale — last checked ${age ?: "a while ago"}. $summary"
        snapshot.problem != null -> "${snapshot.problem?.text}. Showing $age. $summary"
        else -> "$summary  ·  ${age ?: ""}"
    }
}

/** Two text rows plus their padding and spacers, in dp. */
private const val CHROME_HEIGHT_DP = 62f

private fun openIntent(context: Context, target: WidgetTarget?): Intent =
    Intent(context, MainActivity::class.java)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        .apply { target?.let { putExtra(MainActivity.EXTRA_TARGET, it.storageKey) } }

/**
 * Tap-to-refresh.
 *
 * Enqueues work rather than fetching inline: an `ActionCallback` runs on a
 * short-lived broadcast-style scope that the system is free to kill, and a fetch
 * over a slow connection would outlive it. WorkManager is the thing that is
 * allowed to take twenty seconds.
 */
class RefreshAction : ActionCallback {
    override suspend fun onAction(
        context: Context,
        glanceId: GlanceId,
        parameters: ActionParameters,
    ) {
        RefreshWorker.refreshNow(context)
    }
}
