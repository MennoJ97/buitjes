package nl.buitjes.android.ui

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import nl.buitjes.core.Centre
import nl.buitjes.core.Forecast
import nl.buitjes.core.Series
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
/**
 * Where a chart's plot area ended up, and what its axes mean.
 *
 * Fractions of the bitmap rather than pixels, because the bitmap is stretched
 * to fill its box: the renderer may have shrunk it to stay inside the widget's
 * Binder budget, and a caller drawing a crosshair on top has no idea by how
 * much. A fraction survives that; a pixel does not.
 *
 * This is what makes a touch on the chart mean something — without it an
 * overlay would have to guess the padding the renderer chose.
 */
data class ChartGeometry(
    val left: Float,
    val right: Float,
    val top: Float,
    val bottom: Float,
    val firstT: Long,
    val lastT: Long,
    val min: Double,
    val max: Double,
) {
    /** The time under a horizontal position given as a fraction of the image. */
    fun timeAt(fraction: Float): Long {
        val within = ((fraction - left) / (right - left)).coerceIn(0f, 1f)
        return firstT + ((lastT - firstT) * within).toLong()
    }

    /** Where a timestamp sits, as a fraction of the image. */
    fun fractionOf(t: Long): Float {
        val span = (lastT - firstT).coerceAtLeast(1L)
        return left + (right - left) * ((t - firstT).toFloat() / span)
    }

    /** Where a value sits vertically, as a fraction of the image. */
    fun fractionOfValue(value: Double): Float {
        val within = ((value - min) / (max - min)).coerceIn(0.0, 1.0).toFloat()
        return bottom - (bottom - top) * within
    }
}

/**
 * A drawn chart: the bitmap, and where its plot is.
 *
 * `geometry` is null when the renderer drew a message instead of a chart —
 * "outside radar coverage", "no forecast yet" — because there is no plot for a
 * crosshair to land in, and a caller must not invent one.
 */
data class Rendered(val bitmap: Bitmap, val geometry: ChartGeometry?)

object ChartRenderer {

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
    ): Rendered {
        val (width, height) = fit(widthPx, heightPx)
        val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bitmap)
        canvas.drawColor(palette.surface)

        val scale = density.coerceIn(0.75f, 4f)
        fun dp(value: Float) = value * scale

        val series = forecast?.precipitation?.series.orEmpty()
        // Worked out once. `Forecast.rain` walks the series to decide which
        // keys are present, so asking it per step would be quadratic for no
        // reason — and this is a widget's render pass.
        val centre = forecast?.rain
        val message = when {
            forecast == null -> "No forecast yet"
            // Said explicitly, because the alternative reading of an empty chart
            // is "no rain", and the server went out of its way not to serve a
            // flat zero line for exactly this reason.
            forecast.outOfCoverage -> "Outside radar coverage"
            !forecast.hasRainSeries || series.size < 2 -> "No rain data for this point"
            // Every step in the series carrying nothing. Rare — it takes a
            // cycle of unreadable frames — but a bare axis with no line on it
            // is indistinguishable from six dry hours.
            series.none { centre?.valueOf(it) != null } -> "No rain data for this point"
            else -> null
        }
        if (message != null) {
            drawMessage(canvas, width, height, dp(13f), palette, message)
            return Rendered(bitmap, geometry = null)
        }

        val geometry = drawSeries(canvas, width, height, ::dp, palette, forecast!!, centre!!, series)
        return Rendered(bitmap, geometry)
    }

    /**
     * One hourly block — temperature, wind, sunlight, the rain outlook — as a
     * line inside its bands.
     *
     * The axis here does not start at zero unless the caller asks, which is the
     * one thing that separates this from the rain chart above. These are levels
     * rather than amounts: sixteen degrees measured from nothing is a chart
     * about the distance to absolute zero, not about the afternoon.
     *
     * `zeroFloor` and `minSpan` come from the caller for the same reason they
     * do in the web app's card configuration: what counts as a sensible axis is
     * a fact about the quantity, not about this drawing code. A flat night of
     * darkness would otherwise collapse the solar axis to a hair's breadth and
     * label every gridline the same number.
     */
    fun renderBand(
        block: Series,
        centre: Centre,
        referenceTime: Long,
        widthPx: Int,
        heightPx: Int,
        density: Float,
        palette: ChartPalette,
        zeroFloor: Boolean,
        minSpan: Double,
        format: (Double) -> String,
    ): Rendered {
        val (width, height) = fit(widthPx, heightPx)
        val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bitmap)
        canvas.drawColor(palette.surface)

        val scale = density.coerceIn(0.75f, 4f)
        fun dp(value: Float) = value * scale

        val series = block.series.filter { centre.valueOf(it) != null }
        if (series.size < 2) {
            drawMessage(canvas, width, height, dp(13f), palette, "No data for this point")
            return Rendered(bitmap, geometry = null)
        }

        val axisTextSize = dp(9f)
        val padLeft = dp(30f)
        val padRight = dp(4f)
        val padTop = dp(6f)
        val padBottom = axisTextSize + dp(6f)
        val plotLeft = padLeft
        val plotRight = width - padRight
        val plotTop = padTop
        val plotBottom = height - padBottom
        if (plotRight - plotLeft <= dp(20f) || plotBottom - plotTop <= dp(20f)) {
            drawMessage(canvas, width, height, dp(10f), palette, "Too small")
            return Rendered(bitmap, geometry = null)
        }

        val firstT = series.first().t
        val lastT = series.last().t
        val span = (lastT - firstT).coerceAtLeast(1L)
        fun x(t: Long) = plotLeft + ((t - firstT).toFloat() / span.toFloat()) * (plotRight - plotLeft)

        // The axis covers the outermost band as well as the line, or the ribbon
        // would be clipped by the very axis drawn to contain it.
        val edges = centre.bands.flatMap { listOf(it.low, it.high) }
        val values = series.flatMap { step ->
            listOfNotNull(centre.valueOf(step)) + edges.mapNotNull { step.value(it) }
        }
        var low = if (zeroFloor) 0.0 else values.min()
        var high = values.max()
        if (high - low < minSpan) high = low + minSpan
        val step = niceStep(high - low, target = 3)
        if (!zeroFloor) low = floor(low / step) * step
        high = ceil(high / step) * step

        fun y(value: Double) =
            plotBottom - ((value - low) / (high - low)).coerceIn(0.0, 1.0).toFloat() * (plotBottom - plotTop)

        val paint = Paint(Paint.ANTI_ALIAS_FLAG)
        drawValueAxis(
            canvas, ::dp, palette, axisTextSize, plotLeft, plotRight,
            min = low, max = high, step = step, y = ::y, format = format,
        )
        drawTimeAxis(canvas, ::dp, palette, axisTextSize, series, ::x, plotTop, plotBottom, plotLeft, plotRight)
        drawBands(canvas, palette, centre, series, ::x, ::y, paint)

        drawLine(canvas, palette.line, kotlin.math.max(1f, dp(1.4f)), centre, series, ::x, ::y, paint)

        // Where now falls, so past and future are told apart at a glance. These
        // blocks carry the last few hours as well as the next two days, and
        // without this the measured half reads as forecast.
        if (referenceTime in firstT..lastT) {
            paint.strokeWidth = kotlin.math.max(1f, dp(1f))
            paint.color = palette.now
            paint.pathEffect = DashPathEffect(floatArrayOf(dp(3f), dp(3f)), 0f)
            canvas.drawLine(x(referenceTime), plotTop, x(referenceTime), plotBottom, paint)
            paint.pathEffect = null
        }

        canvas.drawText(
            block.unit,
            plotRight,
            plotTop + axisTextSize,
            Paint(Paint.ANTI_ALIAS_FLAG).apply {
                color = palette.axis
                textSize = axisTextSize * 0.9f
                textAlign = Paint.Align.RIGHT
            },
        )

        return Rendered(
            bitmap,
            ChartGeometry(
                left = plotLeft / width,
                right = plotRight / width,
                top = plotTop / height,
                bottom = plotBottom / height,
                firstT = firstT,
                lastT = lastT,
                min = low,
                max = high,
            ),
        )
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
        centre: Centre,
        series: List<Step>,
    ): ChartGeometry? {
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
            return null
        }

        // What this document draws its line from, and what to shade around it,
        // both decided in :core rather than here — the web app makes the same
        // choice from the same keys, and the two charts have to agree about
        // which number they are showing.
        val band = centre.bands.firstOrNull()

        val firstT = series.first().t
        val lastT = series.last().t
        val span = (lastT - firstT).coerceAtLeast(1L)

        fun x(t: Long) = plotLeft + ((t - firstT).toFloat() / span.toFloat()) * plotWidth

        // The axis top. `max(..., 1.0)` keeps a dry afternoon from being drawn
        // as a full-height wall of 0.04 mm/h: without a floor the axis rescales
        // to whatever noise is in the frame and every chart looks like weather.
        val observedMax = series.maxOf { step ->
            val line = centre.valueOf(step) ?: 0.0
            if (band != null) max(line, step.value(band.high) ?: 0.0) else line
        }
        val step = niceStep(max(observedMax, 1.0))
        val top = max(ceil(max(observedMax, 1.0) / step) * step, step)

        fun y(value: Double) =
            plotBottom - (value / top).coerceIn(0.0, 1.0).toFloat() * plotHeight

        val paint = Paint(Paint.ANTI_ALIAS_FLAG)

        drawValueAxis(
            canvas, dp, palette, axisTextSize, plotLeft, plotRight,
            min = 0.0, max = top, step = step, y = ::y, format = ::formatRate,
        )
        drawTimeAxis(canvas, dp, palette, axisTextSize, series, ::x, plotTop, plotBottom, plotLeft, plotRight)

        // The band, where the document has one — for a coordinate the ensemble
        // within a few kilometres of it, for a configured location the members
        // at its own cell. Which pair of keys that is, and whether there is a
        // pair at all, is :core's decision.
        //
        // Absence is the honest rendering of a document with no band. An older
        // server serves a coordinate as five copies of one number; shading
        // between them would paint a zero-width ribbon that reads, at a glance,
        // as a forecast of impossible confidence.
        //
        // Drawn only across the steps that actually carry both edges. The
        // observed hour has no band — it is measurement, not an ensemble — so a
        // polygon spanning the whole series would fill that hour with a wedge
        // running to zero and invent disagreement about a number that was
        // measured.
        drawBands(canvas, palette, centre, series, ::x, ::y, paint)

        // The line.
        //
        // A line rather than the columns this chart used to draw. A column is
        // opaque and runs from zero to the value, so it covered the half of the
        // band that lies *below* the line — and the lower edge is the number a
        // reader wants when they ask how bad it might not be. Only the upper
        // half was ever visible, which reads as a forecast that can turn out
        // worse than expected and never better.
        //
        // Drawing the band on top instead would have traded that for a tint
        // over every column. A line costs nothing and is what the web app draws
        // from the same key, so a phone and a browser open side by side now
        // show the same picture rather than two dialects of it.
        drawLine(canvas, palette.line, max(1f, dp(1.4f)), centre, series, ::x, ::y, paint)

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

        return ChartGeometry(
            left = plotLeft / width,
            right = plotRight / width,
            top = plotTop / height,
            bottom = plotBottom / height,
            firstT = firstT,
            lastT = lastT,
            min = 0.0,
            max = top,
        )
    }

    /** Value gridlines and their labels, shared by both kinds of chart. */
    private fun drawValueAxis(
        canvas: Canvas,
        dp: (Float) -> Float,
        palette: ChartPalette,
        textSize: Float,
        plotLeft: Float,
        plotRight: Float,
        min: Double,
        max: Double,
        step: Double,
        y: (Double) -> Float,
        format: (Double) -> String,
    ) {
        val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = kotlin.math.max(1f, dp(0.6f))
            color = palette.grid
        }
        val labelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = palette.axis
            this.textSize = textSize
            textAlign = Paint.Align.RIGHT
        }

        var value = min
        // A guard rather than a `while (value <= max)` alone: a step that came
        // out at zero or a NaN bound would spin here forever, on the widget's
        // render pass, holding the launcher's thread.
        var lines = 0
        while (value <= max + 1e-9 && lines < 64) {
            val lineY = y(value)
            canvas.drawLine(plotLeft, lineY, plotRight, lineY, linePaint)
            canvas.drawText(format(value), plotLeft - dp(3f), lineY + textSize * 0.35f, labelPaint)
            value += step
            lines++
        }
    }

    /**
     * The bands, widest first, so the narrower is painted on top of the wider.
     *
     * Each is drawn only across the stretches that carry both its edges, which
     * keeps a band off the hour of measured radar in front of a coordinate's
     * forecast: one shape over the whole series would slope from the band's
     * first real value down to nothing across that hour, drawing uncertainty
     * about a measurement.
     */
    private fun drawBands(
        canvas: Canvas,
        palette: ChartPalette,
        centre: Centre,
        series: List<Step>,
        x: (Long) -> Float,
        y: (Double) -> Float,
        paint: Paint,
    ) {
        centre.bands.forEachIndexed { index, band ->
            for (run in centre.bandRuns(series, band)) {
                if (run.size < 2) continue
                val ribbon = Path()
                run.forEachIndexed { position, entry ->
                    val px = x(entry.t)
                    val py = y(entry.value(band.high) ?: 0.0)
                    if (position == 0) ribbon.moveTo(px, py) else ribbon.lineTo(px, py)
                }
                for (position in run.indices.reversed()) {
                    ribbon.lineTo(x(run[position].t), y(run[position].value(band.low) ?: 0.0))
                }
                ribbon.close()
                paint.style = Paint.Style.FILL
                // Nested bands read as "likely" and "very likely" without a
                // legend, which they only do if the inner one is denser. The
                // first band keeps the palette's own alpha, so a chart with
                // one band looks exactly as it did.
                paint.color = if (index == 0) palette.band else denser(palette.band, 1.6f)
                canvas.drawPath(ribbon, paint)
            }
        }
    }

    /**
     * The line, in one path per unbroken run of steps that carry a value.
     *
     * A step with nothing is a hole in the line, not a point at zero. The rain
     * series can have one — a pixel no radar measured, which the server
     * publishes as a gap rather than as dry — and joining across it would draw
     * a five-minute dry spell in the middle of a shower. Zero is not a hole: a
     * dry step is a measurement, and the line sits on the axis through it.
     *
     * The same rule the bands follow, and the same one the web app's polyline
     * runs on, down to dropping a run of a single point: there is no line
     * through one step, and a lone value between two holes takes a cycle of
     * unreadable frames on both sides to produce.
     */
    private fun drawLine(
        canvas: Canvas,
        colour: Int,
        strokeWidth: Float,
        centre: Centre,
        series: List<Step>,
        x: (Long) -> Float,
        y: (Double) -> Float,
        paint: Paint,
    ) {
        paint.style = Paint.Style.STROKE
        paint.strokeWidth = strokeWidth
        paint.strokeJoin = Paint.Join.ROUND
        paint.strokeCap = Paint.Cap.ROUND
        paint.color = colour
        paint.pathEffect = null

        val path = Path()
        var points = 0
        fun flush() {
            if (points > 1) canvas.drawPath(path, paint)
            path.reset()
            points = 0
        }
        for (entry in series) {
            val value = centre.valueOf(entry)
            if (value == null) {
                flush()
                continue
            }
            val px = x(entry.t)
            val py = y(value)
            if (points == 0) path.moveTo(px, py) else path.lineTo(px, py)
            points++
        }
        flush()
        paint.strokeCap = Paint.Cap.BUTT
    }

    /** The same colour, carrying more alpha. Clamped, so it cannot go opaque. */
    private fun denser(colour: Int, factor: Float): Int {
        val alpha = ((colour ushr 24) * factor).roundToInt().coerceIn(0, 255)
        return (alpha shl 24) or (colour and 0x00FFFFFF)
    }

    /**
     * Vertical gridlines on the clock, labelled where there is room.
     *
     * The interval adapts to the span, which is what lets one axis serve a
     * six-hour rain chart and a two-day temperature one: an hourly gridline is
     * right for the first and forty-eight lines of clutter for the second. The
     * step is the coarsest that still fills the plot, so the rain chart is
     * drawn exactly as it was before this had to be general.
     *
     * Past about a day a bare clock time is ambiguous — "19:00" could be either
     * end of the window — so a series crossing midnight gets a divider there
     * and the weekday as its label.
     *
     * The offset is read per timestamp rather than once, so a window straddling
     * a DST change still puts its lines on the hours a clock in the room would
     * show.
     */
    private fun drawTimeAxis(
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
        val span = (lastT - firstT).coerceAtLeast(1L)
        val plotWidth = plotRight - plotLeft

        // The coarsest interval that still puts a line every ~40dp or wider.
        val hourStep = listOf(1, 2, 3, 6, 12, 24)
            .firstOrNull { hours -> plotWidth * (hours * 3600f / span) >= dp(40f) }
            ?: 24
        val multiDay = span > 18 * 3600

        val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = kotlin.math.max(1f, dp(0.6f))
            color = palette.grid
        }
        val dayPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            style = Paint.Style.STROKE
            strokeWidth = kotlin.math.max(1f, dp(0.8f))
            color = palette.boundary
        }
        val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = palette.axis
            this.textSize = textSize
            textAlign = Paint.Align.CENTER
        }

        val minGap = dp(if (multiDay) 34f else 30f)
        var lastLabelX = Float.NEGATIVE_INFINITY

        val startLocal = firstT + offsetAt(firstT)
        val stepSeconds = hourStep * 3600L
        var localHour = ceil(startLocal / stepSeconds.toDouble()).toLong() * stepSeconds
        while (true) {
            // Convert back using the offset at the approximate instant. Off by
            // an hour for the single boundary tick inside a DST transition,
            // which is one line in one chart twice a year.
            val t = localHour - offsetAt(localHour - offsetAt(firstT))
            if (t > lastT) break
            if (t >= firstT) {
                val px = x(t)
                val hour = (((localHour % 86400L) + 86400L) % 86400L) / 3600L
                val midnight = hour == 0L
                canvas.drawLine(px, plotTop, px, plotBottom, if (midnight && multiDay) dayPaint else linePaint)

                val label = if (midnight && multiDay) formatWeekday(t) else
                    String.format(java.util.Locale.ROOT, "%02d:00", hour)
                val halfWidth = textPaint.measureText(label) / 2f
                val fitsInPlot = px - halfWidth >= plotLeft - dp(6f) && px + halfWidth <= plotRight
                if (px - lastLabelX >= minGap && fitsInPlot) {
                    canvas.drawText(label, px, plotBottom + textSize + dp(3f), textPaint)
                    lastLabelX = px
                }
            }
            localHour += stepSeconds
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
    /** The series itself. Named for what it draws now that nothing is a bar. */
    val line: Int,
    val band: Int,
    val now: Int,
    val boundary: Int,
    val message: Int,
) {
    /**
     * The same palette in one series' own colour.
     *
     * The hourly blocks are drawn beside each other on one screen, and four
     * charts in the same blue would invite reading them as four views of the
     * same quantity. The colours are the web app's, so a phone and a browser
     * open side by side agree about which card is the temperature.
     */
    fun accented(colour: Int): ChartPalette = copy(
        line = colour,
        // Sixteen percent, matching `chart.js`'s outer band. The inner one is
        // derived from this at draw time rather than stored, so a palette that
        // has been muted for stale data mutes both together.
        band = (colour and 0x00FFFFFF) or (0x29 shl 24),
    )

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
        line = blend(line, surface, 0.62f),
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
            line = 0xFF3B82F6.toInt(),
            band = 0x4093C5FD,
            now = 0xFFEF4444.toInt(),
            boundary = 0xFF9CA3AF.toInt(),
            message = 0xFF6B7280.toInt(),
        )

        val Dark = ChartPalette(
            surface = 0xFF0A0C11.toInt(),
            grid = 0xFF1F2733.toInt(),
            axis = 0xFF8B94A7.toInt(),
            line = 0xFF60A5FA.toInt(),
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
