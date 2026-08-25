package nl.buitjes.android.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import nl.buitjes.core.Forecast
import nl.buitjes.core.Step
import java.util.TimeZone
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.pow
import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * The rain chart, drawn onto a Bitmap with nothing but `android.graphics`.
 *
 * A bitmap because of Glance: a home-screen widget is RemoteViews underneath,
 * and RemoteViews can show an image but cannot be asked to draw a graph. Since
 * the widget needs a bitmap regardless, the in-app screen uses the same
 * renderer rather than a second Compose implementation — one chart to keep
 * honest instead of two that drift apart, and the widget is where the drawing
 * has to be right, because it is the surface people actually look at.
 *
 * Everything here is a pure function of its arguments. No resources, no
 * density lookups of its own, no theme: the palette and the density come in as
 * parameters, which is what lets the widget resolve night mode itself (see
 * `WidgetPalette`) and the app screen follow Compose's.
 */
object ChartRenderer {

    /**
     * The rate at which a step counts as wet, matching the server's
     * `WET_THRESHOLD_MM_H` and the bottom of the map's colour ramp. Used only to
     * decide whether a bar is worth forcing to a visible height.
     */
    private const val WET_THRESHOLD_MM_H = 0.1

    /**
     * A ceiling on the bitmap, and the least obvious constraint in this file.
     *
     * A widget's bitmap does not stay in this process: it crosses a Binder
     * transaction to the launcher, and the app-widget service rejects RemoteViews
     * whose bitmaps exceed a size budget derived from the screen. Over it, the
     * widget does not draw a smaller chart — it fails to update, silently, and
     * keeps whatever it last showed. Which is indistinguishable, from the home
     * screen, from the app having stopped working.
     *
     * 220k pixels is about 880 KB as ARGB_8888, comfortably inside the budget on
     * anything with a home screen, and more than enough resolution for a chart
     * that is 300dp wide. Oversized requests are scaled down rather than
     * refused, because a slightly soft chart beats no chart.
     */
    private const val MAX_PIXELS = 220_000

    fun render(
        forecast: Forecast?,
        widthPx: Int,
        heightPx: Int,
        density: Float,
        palette: ChartPalette,
    ): Bitmap {
        val (width, height) = fit(widthPx, heightPx)
        val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bitmap)
        canvas.drawColor(palette.surface)

        val scale = density.coerceIn(0.75f, 4f)
        fun dp(value: Float) = value * scale

        val series = forecast?.precipitation?.series.orEmpty()
        val message = when {
            forecast == null -> "No forecast yet"
            // Said explicitly, because the alternative reading of an empty chart
            // is "no rain", and the server went out of its way not to serve a
            // flat zero line for exactly this reason.
            forecast.outOfCoverage -> "Outside radar coverage"
            !forecast.hasRainSeries || series.size < 2 -> "No rain data for this point"
            else -> null
        }
        if (message != null) {
            drawMessage(canvas, width, height, dp(13f), palette, message)
            return bitmap
        }

        drawSeries(canvas, width, height, ::dp, palette, forecast!!, series)
        return bitmap
    }

    // ------------------------------------------------------------------ layout

    private fun fit(widthPx: Int, heightPx: Int): Pair<Int, Int> {
        val width = widthPx.coerceAtLeast(64)
        val height = heightPx.coerceAtLeast(48)
        val pixels = width.toLong() * height.toLong()
        if (pixels <= MAX_PIXELS) return width to height
        val shrink = sqrt(MAX_PIXELS.toDouble() / pixels.toDouble()).toFloat()
        return max(64, (width * shrink).toInt()) to max(48, (height * shrink).toInt())
    }

    private fun drawSeries(
        canvas: Canvas,
        width: Int,
        height: Int,
        dp: (Float) -> Float,
        palette: ChartPalette,
        forecast: Forecast,
        series: List<Step>,
    ) {
        val axisTextSize = dp(9f)
        val padLeft = dp(26f)
        val padRight = dp(4f)
        val padTop = dp(6f)
        val padBottom = axisTextSize + dp(6f)

        val plotLeft = padLeft
        val plotRight = width - padRight
        val plotTop = padTop
        val plotBottom = height - padBottom
        val plotWidth = plotRight - plotLeft
        val plotHeight = plotBottom - plotTop
        if (plotWidth <= dp(20f) || plotHeight <= dp(20f)) {
            // Resized to a sliver. An axis and two labels in this space is
            // illegible clutter; the summary line beside it still says something.
            drawMessage(canvas, width, height, dp(10f), palette, "Too small")
            return
        }

        val spread = forecast.precipitation?.medianOnly == false
        val firstT = series.first().t
        val lastT = series.last().t
        val span = (lastT - firstT).coerceAtLeast(1L)

        fun x(t: Long) = plotLeft + ((t - firstT).toFloat() / span.toFloat()) * plotWidth

        // The axis top. `max(..., 1.0)` keeps a dry afternoon from being drawn
        // as a full-height wall of 0.04 mm/h bars: without a floor the axis
        // rescales to whatever noise is in the frame and every chart looks like
        // weather.
        val observedMax = series.maxOf { step -> if (spread) max(step.median, step.p90) else step.median }
        val step = niceStep(max(observedMax, 1.0))
        val top = max(ceil(max(observedMax, 1.0) / step) * step, step)

        fun y(value: Double) =
            plotBottom - (value / top).coerceIn(0.0, 1.0).toFloat() * plotHeight

        val paint = Paint(Paint.ANTI_ALIAS_FLAG)

        // Value gridlines and their labels.
        paint.style = Paint.Style.STROKE
        paint.strokeWidth = max(1f, dp(0.6f))
        paint.color = palette.grid
        val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = palette.axis
            textSize = axisTextSize
            textAlign = Paint.Align.RIGHT
        }
        var value = 0.0
        while (value <= top + 1e-9) {
            val lineY = y(value)
            canvas.drawLine(plotLeft, lineY, plotRight, lineY, paint)
            canvas.drawText(
                formatRate(value),
                plotLeft - dp(3f),
                lineY + axisTextSize * 0.35f,
                labelPaint,
            )
            value += step
        }

        drawHourAxis(canvas, dp, palette, axisTextSize, series, ::x, plotTop, plotBottom, plotLeft, plotRight)

        // The p10–p90 band, and only when there is one.
        //
        // `median_only` documents carry percentiles that are all equal to the
        // median — the server keeps the shape so a client can use one code path
        // — so drawing the band unconditionally would paint a zero-width ribbon
        // over every ad-hoc point and look, at a glance, like a forecast with
        // impossibly high confidence. Absence is the honest rendering.
        if (spread) {
            val band = Path()
            series.forEachIndexed { index, entry ->
                val px = x(entry.t)
                val py = y(entry.p90)
                if (index == 0) band.moveTo(px, py) else band.lineTo(px, py)
            }
            for (index in series.indices.reversed()) {
                band.lineTo(x(series[index].t), y(series[index].p10))
            }
            band.close()
            paint.style = Paint.Style.FILL
            paint.color = palette.band
            canvas.drawPath(band, paint)
        }

        // Bars for the median.
        val slot = plotWidth / max(1, series.size - 1)
        val barWidth = max(1f, slot * 0.82f)
        paint.style = Paint.Style.FILL
        paint.color = palette.bar
        val baseline = y(0.0)
        for (entry in series) {
            if (entry.median <= 0.0) continue
            val centre = x(entry.t)
            var barTop = y(entry.median)
            // A wet step must be visible. At six hours across a widget each bar
            // is a couple of pixels wide, and 0.2 mm/h rounds to a bar of zero
            // height — which reads as dry, which is the one thing this chart
            // must never say when it is not true.
            if (entry.median >= WET_THRESHOLD_MM_H && baseline - barTop < 1f) {
                barTop = baseline - 1f
            }
            canvas.drawRect(centre - barWidth / 2f, barTop, centre + barWidth / 2f, baseline, paint)
        }

        // Where the measured part stops and the extrapolated part begins.
        val boundaryT = series
            .zipWithNext()
            .firstOrNull { (before, after) -> before.kind == "observed" && after.kind != "observed" }
            ?.second?.t

        val nowX = if (forecast.referenceTime in firstT..lastT) x(forecast.referenceTime) else null

        if (boundaryT != null && boundaryT in firstT..lastT) {
            val boundaryX = x(boundaryT)
            // Usually within a pixel or two of the now marker, since the cycle's
            // reference time is where observation hands over to nowcast. They
            // come apart when the observed frames lag the cycle, and drawing
            // both then is the honest thing — but drawing two lines on top of
            // each other the rest of the time is just noise.
            if (nowX == null || kotlin.math.abs(boundaryX - nowX) > dp(5f)) {
                paint.style = Paint.Style.STROKE
                paint.strokeWidth = max(1f, dp(0.8f))
                paint.color = palette.boundary
                paint.pathEffect = null
                canvas.drawLine(boundaryX, plotTop, boundaryX, plotBottom, paint)
            }
        }

        // The "now" marker.
        //
        // Drawn at the cycle's reference time, not at the phone's clock, and the
        // difference is deliberate. This line separates what the radar measured
        // from what the model extrapolated, and that boundary belongs to the
        // document. When a cached document is old the two disagree — and the
        // grey "stale" label beside the chart is what says so, rather than a
        // marker that quietly slides right and makes old data look current.
        if (nowX != null) {
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = max(1f, dp(1f))
            paint.color = palette.now
            paint.pathEffect = DashPathEffect(floatArrayOf(dp(3f), dp(3f)), 0f)
            canvas.drawLine(nowX, plotTop, nowX, plotBottom, paint)
            paint.pathEffect = null
        }

        // The unit, tucked into the corner the axis labels leave empty.
        canvas.drawText(
            "mm/h",
            plotRight,
            plotTop + axisTextSize,
            Paint(Paint.ANTI_ALIAS_FLAG).apply {
                color = palette.axis
                textSize = axisTextSize * 0.9f
                textAlign = Paint.Align.RIGHT
            },
        )
    }

    /**
     * Vertical gridlines on whole local hours, labelled where there is room.
     *
     * Gridlines every hour regardless, labels thinned to fit: the lines are what
     * make the horizontal axis readable, and they cost nothing, while overlapping
     * labels cost legibility everywhere. The offset is read per timestamp rather
     * than once, so a window straddling a DST change still puts its lines on the
     * hours a clock in the room would show.
     */
    private fun drawHourAxis(
        canvas: Canvas,
        dp: (Float) -> Float,
        palette: ChartPalette,
        textSize: Float,
        series: List<Step>,
        x: (Long) -> Float,
        plotTop: Float,
        plotBottom: Float,
        plotLeft: Float,
        plotRight: Float,
    ) {
        val zone = TimeZone.getDefault()
        fun offsetAt(t: Long) = zone.getOffset(t * 1000L) / 1000L

        val firstT = series.first().t
        val lastT = series.last().t

        val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = max(1f, dp(0.6f))
            color = palette.grid
        }
        val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = palette.axis
            this.textSize = textSize
            textAlign = Paint.Align.CENTER
        }

        val minGap = dp(30f)
        var lastLabelX = Float.NEGATIVE_INFINITY

        val startLocal = firstT + offsetAt(firstT)
        var localHour = ceil(startLocal / 3600.0).toLong() * 3600L
        while (true) {
            // Convert back using the offset at the approximate instant. Off by
            // an hour for the single boundary tick inside a DST transition,
            // which is one line in one chart twice a year.
            val t = localHour - offsetAt(localHour - offsetAt(firstT))
            if (t > lastT) break
            if (t >= firstT) {
                val px = x(t)
                canvas.drawLine(px, plotTop, px, plotBottom, linePaint)

                val hour = (((localHour % 86400L) + 86400L) % 86400L) / 3600L
                val label = String.format(java.util.Locale.ROOT, "%02d:00", hour)
                val halfWidth = textPaint.measureText(label) / 2f
                val fitsInPlot = px - halfWidth >= plotLeft - dp(6f) && px + halfWidth <= plotRight
                if (px - lastLabelX >= minGap && fitsInPlot) {
                    canvas.drawText(label, px, plotBottom + textSize + dp(3f), textPaint)
                    lastLabelX = px
                }
            }
            localHour += 3600L
        }
    }

    private fun drawMessage(
        canvas: Canvas,
        width: Int,
        height: Int,
        textSize: Float,
        palette: ChartPalette,
        text: String,
    ) {
        val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = palette.message
            this.textSize = textSize
            textAlign = Paint.Align.CENTER
        }
        // Shrunk to fit rather than ellipsised: these are five known strings,
        // all of them short, and a truncated "Outside radar cover…" would be
        // less useful than the same sentence a point smaller.
        while (paint.textSize > 6f && paint.measureText(text) > width - 8f) {
            paint.textSize -= 1f
        }
        canvas.drawText(text, width / 2f, height / 2f + paint.textSize * 0.35f, paint)
    }

    /** A round axis step giving roughly `target` gridlines, as `chart.js` does. */
    private fun niceStep(range: Double, target: Int = 3): Double {
        if (range <= 0.0) return 1.0
        val rough = range / target
        val magnitude = 10.0.pow(floor(log10(rough)))
        val normalised = rough / magnitude
        val multiplier = when {
            normalised >= 5 -> 10.0
            normalised >= 2 -> 5.0
            normalised >= 1 -> 2.0
            else -> 1.0
        }
        return multiplier * magnitude
    }
}

/**
 * Every colour the chart uses, supplied by the caller.
 *
 * Passed in rather than resolved from a theme because the two callers resolve
 * light and dark by different means — Compose knows from
 * `isSystemInDarkTheme()`, the widget has to read the host's configuration —
 * and because the widget's stale rendering needs a third variant that no theme
 * has a name for. Keeping the palette a plain value makes all three cases the
 * same code path with a different argument.
 *
 * The colours themselves are the web app's, so a phone and a browser open side
 * by side show recognisably the same chart.
 */
data class ChartPalette(
    val surface: Int,
    val grid: Int,
    val axis: Int,
    val bar: Int,
    val band: Int,
    val now: Int,
    val boundary: Int,
    val message: Int,
) {
    /**
     * The same chart, drained towards its own background.
     *
     * This is how stale data is shown, and the choice of *drained* rather than
     * *hidden* is the point. A widget that blanks when its data ages tells the
     * reader nothing at all, and one that keeps drawing at full strength tells
     * them something false. A greyed chart says "this is what it looked like",
     * which is true, and the label beside it says when.
     *
     * Blending towards the surface rather than towards grey keeps the result
     * readable in both themes: on a dark background the colours fade down, on a
     * light one they fade up, and in neither case does anything end up as a
     * mid-grey smear that happens to have good contrast.
     */
    fun muted(): ChartPalette = copy(
        grid = blend(grid, surface, 0.45f),
        axis = blend(axis, surface, 0.5f),
        bar = blend(bar, surface, 0.62f),
        band = blend(band, surface, 0.62f),
        now = blend(now, surface, 0.6f),
        boundary = blend(boundary, surface, 0.6f),
        message = blend(message, surface, 0.4f),
    )

    companion object {
        val Light = ChartPalette(
            surface = 0xFFFFFFFF.toInt(),
            grid = 0xFFE5E7EB.toInt(),
            axis = 0xFF6B7280.toInt(),
            bar = 0xFF3B82F6.toInt(),
            band = 0x4093C5FD,
            now = 0xFFEF4444.toInt(),
            boundary = 0xFF9CA3AF.toInt(),
            message = 0xFF6B7280.toInt(),
        )

        val Dark = ChartPalette(
            surface = 0xFF0A0C11.toInt(),
            grid = 0xFF1F2733.toInt(),
            axis = 0xFF8B94A7.toInt(),
            bar = 0xFF60A5FA.toInt(),
            band = 0x4D3B82F6,
            now = 0xFFF87171.toInt(),
            boundary = 0xFF4B5563.toInt(),
            message = 0xFF8B94A7.toInt(),
        )

        fun of(night: Boolean): ChartPalette = if (night) Dark else Light

        /** Mix towards `toward`, preserving the source's alpha. */
        private fun blend(colour: Int, toward: Int, amount: Float): Int {
            val keep = 1f - amount
            fun channel(shift: Int): Int {
                val from = (colour shr shift) and 0xFF
                val to = (toward shr shift) and 0xFF
                return (from * keep + to * amount).roundToInt().coerceIn(0, 255)
            }
            return ((colour ushr 24) shl 24) or
                (channel(16) shl 16) or
                (channel(8) shl 8) or
                channel(0)
        }
    }
}
