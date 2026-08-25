package nl.buitjes.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * The point-forecast document, as both `/api/point/<name>` and
 * `/api/point?lat&lon` serve it.
 *
 * The two differ in exactly one way that matters to a reader, and the model is
 * built around making that difference impossible to miss. A configured location
 * carries real KNMI spread — twenty members sampled while the timestep was in
 * memory — so `p10` and `p90` mean something and `probability` says how many
 * members agreed. A coordinate is sampled from the published median frames
 * after the members have been averaged away, so every percentile on a step is
 * the same number. Reading a band off that would be reading noise as certainty,
 * which is why `medianOnly` exists and why the chart consults it before drawing
 * anything but the line.
 *
 * Fields are lenient by design. The server omits blocks rather than sending
 * empty ones — a dry summary has no `stops_at`, an ad-hoc point has no
 * `probability`, a point outside the domain has no `precipitation` at all — so
 * everything optional is nullable with a default and unknown keys are ignored.
 * A future field on the server must not stop an installed app from working.
 */
@Serializable
data class Forecast(
    @SerialName("generated_at") val generatedAt: Long = 0,
    @SerialName("reference_time") val referenceTime: Long = 0,
    val location: PointLocation = PointLocation(),
    val summary: Summary = Summary(),
    val precipitation: Series? = null,
    @SerialName("precipitation_outlook") val precipitationOutlook: Series? = null,
    val temperature: Series? = null,
    val wind: Series? = null,
    val solar: Series? = null,
    /**
     * The point is outside the radar domain. The server says this explicitly
     * rather than serving a flat line of zeros, because zeros read exactly like
     * a confident forecast of dry weather — and a phone that has crossed a
     * border should show nothing and stop alerting, not relax.
     */
    @SerialName("out_of_coverage") val outOfCoverage: Boolean = false,
) {
    val hasRainSeries: Boolean
        get() = !outOfCoverage && (precipitation?.series?.isNotEmpty() == true)

    /** Whether the rain series carries real ensemble spread. */
    val medianOnly: Boolean
        get() = precipitation?.medianOnly ?: true

    val rainSteps: List<Step>
        get() = precipitation?.series.orEmpty()
}

@Serializable
data class PointLocation(
    val name: String = "",
    val lat: Double = 0.0,
    val lon: Double = 0.0,
    @SerialName("ad_hoc") val adHoc: Boolean = false,
)

@Serializable
data class Summary(
    @SerialName("raining_now") val rainingNow: Boolean = false,
    @SerialName("starts_at") val startsAt: Long? = null,
    @SerialName("stops_at") val stopsAt: Long? = null,
    @SerialName("peak_mm_h") val peakMmH: Double? = null,
    @SerialName("peak_at") val peakAt: Long? = null,
    val text: String = "",
)

@Serializable
data class Series(
    val unit: String = "",
    @SerialName("median_only") val medianOnly: Boolean = false,
    val series: List<Step> = emptyList(),
)

/**
 * One timestep.
 *
 * `kind` is only present on ad-hoc documents, where it says whether the step is
 * `observed`, `nowcast` or `forecast`. It is worth carrying: which part is
 * measured and which is extrapolated is the distinction the whole project is
 * built around, and a chart that draws them identically is quietly lying.
 */
@Serializable
data class Step(
    val t: Long,
    val kind: String? = null,
    val p10: Double = 0.0,
    val p25: Double = 0.0,
    val median: Double = 0.0,
    val p75: Double = 0.0,
    val p90: Double = 0.0,
    val mean: Double? = null,
    val probability: Double? = null,
)

object ForecastJson {
    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        coerceInputValues = true
    }

    fun parse(text: String): Forecast = json.decodeFromString(Forecast.serializer(), text)

    fun encode(forecast: Forecast): String =
        json.encodeToString(Forecast.serializer(), forecast)
}
