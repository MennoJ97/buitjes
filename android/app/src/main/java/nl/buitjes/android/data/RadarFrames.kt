package nl.buitjes.android.data

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlin.math.ln
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * The precipitation colour ramp, ported from the web app's `ramp.js`.
 *
 * Stops on a log scale, because most Dutch rain sits below 5 mm/h and a linear
 * ramp spends nearly all of its colour on downpours that hardly ever happen.
 * The numbers are the web's exactly: a phone and a browser showing the same
 * cycle have to paint the same shower the same colour, or one of them is
 * telling a different story about how hard it is raining.
 */
object RainRamp {
    private const val MIN_MM_H = 0.1
    private const val MAX_MM_H = 100.0

    private val stops = listOf(
        0.1 to 0xFFC2E6FF.toInt(),
        0.3 to 0xFF84C6F2.toInt(),
        1.0 to 0xFF3B82F6.toInt(),
        2.5 to 0xFF1E40C8.toInt(),
        5.0 to 0xFF16A34A.toInt(),
        10.0 to 0xFFFACC15.toInt(),
        20.0 to 0xFFF97316.toInt(),
        50.0 to 0xFFEF4444.toInt(),
        100.0 to 0xFFC026D3.toInt(),
    )

    private val logMin = ln(MIN_MM_H)
    private val logRange = ln(MAX_MM_H) - logMin

    /** Where a rate sits along the ramp, 0..1. */
    fun position(mmh: Double): Double =
        if (mmh <= 0.0) 0.0 else min(1.0, max(0.0, (ln(mmh) - logMin) / logRange))

    /**
     * A 256-entry lookup table, which is the same trick the web plays on the
     * GPU: build the ramp once and index it, rather than walking nine stops per
     * pixel across six hundred thousand of them.
     */
    val lookup: IntArray = IntArray(256) { index ->
        val p = index / 255.0
        val upperIndex = stops.indexOfFirst { position(it.first) >= p }.let {
            if (it <= 0) 1 else it
        }
        val (lowMmh, lowColour) = stops[upperIndex - 1]
        val (highMmh, highColour) = stops[upperIndex]
        val lowPosition = position(lowMmh)
        val highPosition = position(highMmh)
        val within = if (highPosition <= lowPosition) 0.0
        else ((p - lowPosition) / (highPosition - lowPosition)).coerceIn(0.0, 1.0)
        blend(lowColour, highColour, within)
    }

    private fun blend(from: Int, to: Int, amount: Double): Int {
        fun channel(shift: Int): Int {
            val a = (from shr shift) and 0xFF
            val b = (to shr shift) and 0xFF
            return (a + (b - a) * amount).roundToInt().coerceIn(0, 255)
        }
        return (0xFF shl 24) or (channel(16) shl 16) or (channel(8) shl 8) or channel(0)
    }

    /** The colour for a rate, opaque, or fully transparent below the ramp. */
    fun colourFor(mmh: Double): Int =
        if (mmh < MIN_MM_H) 0 else lookup[(position(mmh) * 255).roundToInt().coerceIn(0, 255)]
}

/**
 * Turning a published frame into something a map can lay over the Netherlands.
 *
 * A frame is a lossless WebP carrying a rain rate as a 16-bit fraction of full
 * scale, split across red and green, with blue flagging the pixels no radar
 * measured. None of that means anything to an image view, so this decodes it
 * and paints it through the ramp.
 *
 * Two things it will not do, both for the same reason — that the packing is not
 * a picture:
 *
 *  * It never asks `BitmapFactory` to downsample. `inSampleSize` averages
 *    neighbouring pixels, and averaging the two halves of a 16-bit number is
 *    arithmetic on nonsense: a green channel that wraps from 255 to 0 as the
 *    rate crosses a boundary would average to mid-grey, painting a dry cell
 *    beside a wet one as moderate rain. It decodes at full size and thins the
 *    *colours* afterwards, where averaging is harmless and sampling is honest.
 *  * It leaves unmeasured pixels transparent rather than dry. A quarter of an
 *    observed frame is unmeasured, and a map that paints those as "no rain"
 *    claims coverage it does not have.
 */
object RadarFrames {

    /**
     * How much to thin the colour raster by.
     *
     * The domain is 780 pixels across for about 700 km, so a full-resolution
     * overlay is a kilometre per pixel — finer than a phone screen showing the
     * whole country can resolve, and four times the memory. Two keeps a frame
     * at 0.6 MB, which is what makes holding a handful of them for an animation
     * reasonable at all.
     */
    private const val THIN = 2

    /** Blue at or above this marks a pixel no radar looked at. */
    private const val NO_DATA_FLAG = 255

    suspend fun decode(bytes: ByteArray, maxPrecipMmH: Double): Bitmap? =
        withContext(Dispatchers.Default) {
            val source = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: return@withContext null
            try {
                val width = source.width
                val height = source.height
                if (width <= 0 || height <= 0) return@withContext null

                val row = IntArray(width)
                val outWidth = (width + THIN - 1) / THIN
                val outHeight = (height + THIN - 1) / THIN
                val out = IntArray(outWidth * outHeight)

                var outY = 0
                var y = 0
                while (y < height && outY < outHeight) {
                    source.getPixels(row, 0, width, 0, y, width, 1)
                    var outX = 0
                    var x = 0
                    while (x < width && outX < outWidth) {
                        val pixel = row[x]
                        val blue = pixel and 0xFF
                        out[outY * outWidth + outX] = if (blue >= NO_DATA_FLAG) {
                            0
                        } else {
                            val packed = ((pixel shr 16) and 0xFF) * 256 + ((pixel shr 8) and 0xFF)
                            RainRamp.colourFor(packed / 65535.0 * maxPrecipMmH)
                        }
                        x += THIN
                        outX++
                    }
                    y += THIN
                    outY++
                }
                Bitmap.createBitmap(out, outWidth, outHeight, Bitmap.Config.ARGB_8888)
            } finally {
                source.recycle()
            }
        }
}
