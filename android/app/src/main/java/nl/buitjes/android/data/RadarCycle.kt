package nl.buitjes.android.data

import android.graphics.Bitmap
import android.util.LruCache

/**
 * One published cycle of frames, and the decoded pictures made from them.
 *
 * Deliberately a plain object with suspend functions rather than anything
 * lifecycle-aware: what it holds is expensive (bitmaps) and what it does is
 * slow (network, decode), and both want to be driven by a screen that knows
 * when it is visible, not by a framework that thinks it does.
 *
 * The cache is small on purpose. A decoded frame is about 0.6 MB, and a cycle
 * has sixty of them: holding all of them would be 36 MB of a phone's heap for a
 * card somebody scrolled past. Sixteen covers the window an animation is
 * actually inside, and the ones either side of it that it is about to reach.
 */
class RadarCycle(private val client: BuitjesClient) {

    companion object {
        private var shared: Pair<String, RadarCycle>? = null

        /**
         * One cycle per server, shared across the screens that draw it.
         *
         * Both the radar tab and the card on the forecast screen want the same
         * frames, and they are the most expensive thing this app fetches — a
         * cycle is a couple of megabytes over the wire and tens of megabytes
         * decoded. Two instances would download and decode all of it twice, and
         * on a phone that is somebody's data allowance rather than a tidiness
         * argument.
         *
         * Keyed by base URL so that changing servers does not leave the old
         * server's rain on the map.
         */
        @Synchronized
        fun forServer(prefs: Prefs): RadarCycle {
            val existing = shared
            if (existing != null && existing.first == prefs.baseUrl) return existing.second
            val fresh = RadarCycle(prefs.buitjesClient())
            shared = prefs.baseUrl to fresh
            return fresh
        }
    }

    var manifest: Manifest? = null
        private set

    /**
     * Decoded frames, counted rather than weighed.
     *
     * Sized to hold a whole cycle once one is known, because the alternative is
     * what scrubbing looked like before: a cache of sixteen against sixty-one
     * frames, so dragging the timeline back and forth evicted the frame it was
     * about to need and re-downloaded it. A cycle is about 35 MB decoded, which
     * is real but bounded, and it is the difference between a timeline that
     * scrubs and one that stutters.
     */
    private val decoded = object : LruCache<String, Bitmap>(16) {
        override fun sizeOf(key: String, value: Bitmap) = 1
    }

    val frames: List<FrameRef>
        get() = manifest?.frames.orEmpty()

    /** Whether there is a cycle worth drawing a map for. */
    val ready: Boolean
        get() = manifest?.drawable == true

    /**
     * Fetch the manifest, or keep the one already in hand.
     *
     * A failed refresh is not a reason to throw away a cycle that is still
     * broadly right: the frames it names stay on the server for a while, and a
     * radar that blanks the moment a request times out is a radar that is
     * empty exactly when somebody is looking at it on a train.
     */
    suspend fun refresh(): Boolean {
        val result = client.manifest()
        if (result is FetchResult.Success && result.value.drawable) {
            val previous = manifest
            manifest = result.value
            // Room for the whole cycle, now that its length is known.
            decoded.resize(result.value.frames.size.coerceIn(16, 96))
            // Frames are named by their timestamps, so a new cycle simply does
            // not hit the old entries — except for the observed steps it shares
            // with the last one, which are byte-identical and worth keeping.
            if (previous != null && previous.generatedAt != result.value.generatedAt) {
                val live = result.value.frames.map { it.file }.toSet()
                decoded.snapshot().keys.forEach { key ->
                    if (key !in live) decoded.remove(key)
                }
            }
            return true
        }
        return manifest != null
    }

    /** The decoded picture for one frame, fetching and painting it if needed. */
    suspend fun bitmapFor(frame: FrameRef): Bitmap? {
        decoded.get(frame.file)?.let { return it }
        val maxPrecip = manifest?.maxPrecipMmH ?: return null
        val bytes = client.frame(frame.file) ?: return null
        val bitmap = RadarFrames.decode(bytes, maxPrecip) ?: return null
        decoded.put(frame.file, bitmap)
        return bitmap
    }

    /**
     * Fetch and decode the whole cycle, starting at `from` and working outward.
     *
     * Outward rather than left to right because the frame somebody is looking
     * at is the one nearest now, and the ends of a six-hour window are where
     * they will get to last if at all. A caller in a `LaunchedEffect` gets
     * cancellation for free: leave the screen and the remaining downloads stop.
     *
     * About 8 MB over the wire for a full cycle, which is why this is only done
     * on the radar screen — the card on the forecast page asks for the handful
     * of frames it loops and no more.
     */
    suspend fun prefetch(from: Int, onProgress: (done: Int, total: Int) -> Unit) {
        val all = frames
        if (all.isEmpty()) return

        val order = ArrayList<FrameRef>(all.size)
        var low = from.coerceIn(all.indices)
        var high = low
        order.add(all[low])
        while (order.size < all.size) {
            if (high < all.lastIndex) order.add(all[++high])
            if (low > 0 && order.size < all.size) order.add(all[--low])
        }

        var done = 0
        for (frame in order) {
            bitmapFor(frame)
            done++
            onProgress(done, all.size)
        }
    }

    /** Already decoded, or null — for a caller that must not block on a frame. */
    fun cached(frame: FrameRef): Bitmap? = decoded.get(frame.file)

    /**
     * The step nearest the cycle's own reference time.
     *
     * Where an animation starts and where a scrubber's "now" sits. Taken from
     * the manifest rather than the clock for the same reason the chart's now
     * marker is: this is the boundary between what the radar measured and what
     * the model extrapolated, and that belongs to the cycle.
     */
    /** The frames inside a window around the cycle's reference time. */
    fun window(beforeSeconds: Long, afterSeconds: Long): List<FrameRef> {
        val reference = manifest?.referenceTime ?: return emptyList()
        return frames.filter { it.t >= reference - beforeSeconds && it.t <= reference + afterSeconds }
    }

    fun indexOfNow(): Int {
        val reference = manifest?.referenceTime ?: return 0
        return frames.indices.minByOrNull { kotlin.math.abs(frames[it].t - reference) } ?: 0
    }
}
