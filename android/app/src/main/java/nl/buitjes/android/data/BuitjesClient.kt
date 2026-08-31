package nl.buitjes.android.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import nl.buitjes.core.Forecast
import nl.buitjes.core.ForecastJson
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.coroutineContext

/**
 * How a request ended, in the only five ways that lead anywhere different.
 *
 * The distinction that earns this type its existence is `Offline` versus
 * everything else. A widget showing an hour-old graph because the phone is in a
 * tunnel is behaving correctly and should keep its data, greyed; a widget
 * showing an hour-old graph because the API key was revoked is showing data
 * that will never refresh, and its owner needs telling. Collapsing both into
 * "failed" makes those two indistinguishable at exactly the moment the
 * difference matters, so nothing in this app is allowed to see a bare boolean.
 *
 * `Success` carries the response body alongside the parsed value because the
 * repository caches what the server actually said. Re-encoding the model and
 * storing that would mean the cached document is a round-trip through this
 * app's understanding of the schema — so a field :core learns to read next year
 * would be silently absent from everything already on disk.
 */
sealed interface FetchResult<out T> {
    data class Success<T>(val value: T, val body: String) : FetchResult<T>

    /** The server answered, and said no. A key problem, not a network problem. */
    data object Unauthorized : FetchResult<Nothing>

    /** 503 `warming_up`: the ingestor has not published a cycle yet. Wait. */
    data object WarmingUp : FetchResult<Nothing>

    /** Nothing came back at all. Includes timeouts — see `fetch` below. */
    data object Offline : FetchResult<Nothing>

    data class Failed(val reason: String) : FetchResult<Nothing>
}

/** One entry from the manifest's `points`, which is the app's list of places. */
@Serializable
data class NamedPoint(
    val name: String,
    val lat: Double,
    val lon: Double,
)

/**
 * As much of `/api/config` as this app has any use for.
 *
 * The frames, bounds and grid dimensions are the map's business, and the map is
 * the web app's. All that is wanted here is the list of configured locations —
 * and the fact that the request succeeded at all, which is what makes this the
 * natural thing for the setup screen's connection test to fetch.
 *
 * `points` is absent rather than empty when the server has keys configured and
 * the request did not present a valid one, so an empty list here means either
 * "no locations configured" or "you are not trusted with them". The setup
 * screen says as much rather than guessing.
 */
@Serializable
data class Manifest(
    @SerialName("generated_at") val generatedAt: Long = 0,
    @SerialName("reference_time") val referenceTime: Long = 0,
    val points: List<NamedPoint> = emptyList(),
    /**
     * The corners the frames are stretched across, in MapLibre's order — NW,
     * NE, SE, SW. Read as four corners rather than as a bounding box because
     * that is the shape the map wants back.
     */
    val bounds: List<List<Double>> = emptyList(),
    val width: Int = 0,
    val height: Int = 0,
    /** Full scale for the 16-bit packing. Every frame in the cycle shares it. */
    @SerialName("max_precip_mm_h") val maxPrecipMmH: Double = 0.0,
    val frames: List<FrameRef> = emptyList(),
) {
    /** Whether this manifest describes a cycle a map could draw. */
    val drawable: Boolean
        get() = bounds.size == 4 && width > 0 && height > 0 &&
            maxPrecipMmH > 0.0 && frames.isNotEmpty()
}

/** One published frame: when it is for, what kind it is, and its file. */
@Serializable
data class FrameRef(
    val t: Long,
    val kind: String = "",
    val file: String = "",
    /**
     * This step stood in for one KNMI published empty. Carried so the timeline
     * can say so rather than presenting a repaired step as a measured one.
     */
    val estimated: Boolean = false,
)

class BuitjesClient(
    private val baseUrl: String,
    private val apiKey: String,
) {

    suspend fun manifest(): FetchResult<Manifest> =
        fetch(url("api", "config")) { body -> json.decodeFromString<Manifest>(body) }

    /**
     * One radar frame, as the bytes the ingestor wrote.
     *
     * Not JSON, so it does not go through [fetch]: the whole point of a frame is
     * that it is a lossless WebP of packed 16-bit values, and decoding it is the
     * caller's business.
     *
     * The frames are public — the server gates `/api/point` and `/api/current`,
     * not these, because a rain map that needs a login is not a rain map. The
     * key goes along anyway, since a server whose owner *has* protected them
     * should not find this client mysteriously unable to draw.
     */
    suspend fun frame(file: String): ByteArray? {
        val url = url("api", "frames", file) ?: return null
        val request = Request.Builder()
            .url(url)
            .apply { if (apiKey.isNotBlank()) header("X-API-Key", apiKey) }
            .build()

        return withContext(Dispatchers.IO) {
            val call = shared.newCall(request)
            coroutineContext[Job]?.invokeOnCompletion { call.cancel() }
            try {
                call.execute().use { response ->
                    if (response.isSuccessful) response.body?.bytes() else null
                }
            } catch (error: IOException) {
                null
            }
        }
    }

    suspend fun pointForName(name: String): FetchResult<Forecast> =
        fetch(url("api", "point", name)) { body -> ForecastJson.parse(body) }

    /**
     * The ad-hoc endpoint, for coordinates nobody configured in advance.
     *
     * Coordinates go in with six decimals, which is far more precision than
     * survives the trip: the server rounds to hundredths of a degree for its
     * cache key, and the radar grid under that is about a kilometre. Sending
     * the fix as it arrived is simply the least surprising thing to read in a
     * log; nothing downstream can act on the extra digits.
     */
    suspend fun pointAt(lat: Double, lon: Double): FetchResult<Forecast> {
        val url = url("api", "point") { builder ->
            builder.addQueryParameter("lat", formatCoordinate(lat))
            builder.addQueryParameter("lon", formatCoordinate(lon))
        }
        return fetch(url) { body -> ForecastJson.parse(body) }
    }

    private fun url(
        vararg segments: String,
        extra: (HttpUrl.Builder) -> Unit = {},
    ): HttpUrl? {
        val builder = baseUrl.toHttpUrlOrNull()?.newBuilder() ?: return null
        segments.forEach(builder::addPathSegment)
        extra(builder)
        return builder.build()
    }

    /**
     * One request, mapped onto [FetchResult].
     *
     * The key is presented as a header and never as `?key=`, even though the
     * server accepts both. The query form exists for `<img>` tags in a web page
     * that cannot set headers; this app can, and a key in a URL is a key in
     * every proxy log between here and the server.
     */
    private suspend fun <T> fetch(url: HttpUrl?, parse: (String) -> T): FetchResult<T> {
        if (url == null) {
            return FetchResult.Failed("that server address is not a URL")
        }

        val request = Request.Builder()
            .url(url)
            .header("Accept", "application/json")
            .apply { if (apiKey.isNotBlank()) header("X-API-Key", apiKey) }
            .build()

        return withContext(Dispatchers.IO) {
            val call = shared.newCall(request)
            // A screen that has been navigated away from should not keep a
            // socket open for the fifteen seconds the read timeout allows.
            // `withContext` alone does not do this: a blocking `execute()` is
            // not interruptible, so cancellation has to be forwarded to OkHttp
            // by hand. Cancelling an already-finished call is a no-op, so the
            // normal-completion case costs nothing.
            coroutineContext[Job]?.invokeOnCompletion { call.cancel() }

            try {
                call.execute().use { response ->
                    val body = response.body?.string().orEmpty()
                    when {
                        response.isSuccessful -> runCatching { parse(body) }.fold(
                            onSuccess = { FetchResult.Success(it, body) },
                            // Reached the server, got a 200, could not read it.
                            // Almost always something other than Buitjes
                            // answering — a captive portal, or a reverse proxy
                            // serving its own error page with the wrong status.
                            onFailure = {
                                FetchResult.Failed("that did not look like a Buitjes server")
                            },
                        )

                        response.code == 401 -> FetchResult.Unauthorized
                        response.code == 400 -> FetchResult.Failed("the server rejected the coordinates")
                        response.code == 404 -> FetchResult.Failed("no forecast published for that location")

                        // The server sends `warming_up` for a cycle that has not
                        // landed yet, and plain 503 for a manifest it cannot
                        // read. Both mean "ask again shortly", which is the only
                        // thing a caller does differently.
                        response.code == 503 -> FetchResult.WarmingUp

                        else -> FetchResult.Failed("the server answered ${response.code}")
                    }
                }
            } catch (error: IOException) {
                // Timeouts land here alongside DNS and connection failures, and
                // are deliberately not separated out. From the widget's point of
                // view they are the same event: no answer arrived, so what is on
                // screen stays on screen with its age shown. Distinguishing "the
                // server is slow" from "there is no server" would produce two
                // states that render identically.
                FetchResult.Offline
            }
        }
    }

    private fun formatCoordinate(value: Double): String =
        // Locale-independent on purpose: half of Europe formats decimals with a
        // comma, and `lat=52,379` is a 400 from the server that would only ever
        // reproduce on somebody else's phone.
        String.format(java.util.Locale.ROOT, "%.6f", value)

    private companion object {
        /**
         * Shared across every instance, because the connection pool is the point.
         *
         * A `BuitjesClient` is cheap and short-lived — the worker builds one per
         * pass, screens build one per config change — but the TLS handshake to a
         * self-hosted box over a mobile connection is not. One pool means the
         * second of three requests in a refresh reuses the first's connection.
         *
         * The timeouts are short because both callers are on a clock: the widget
         * wants to be redrawn, and the worker holds a wakelock while it waits.
         * Fifteen seconds of read timeout against a server that is not answering
         * is fifteen seconds of radio, for a stale label either way.
         */
        val shared: OkHttpClient by lazy {
            OkHttpClient.Builder()
                .connectTimeout(10, TimeUnit.SECONDS)
                .readTimeout(15, TimeUnit.SECONDS)
                .callTimeout(25, TimeUnit.SECONDS)
                .retryOnConnectionFailure(true)
                .build()
        }

        val json = Json { ignoreUnknownKeys = true }
    }
}
