package nl.buitjes.core

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/**
 * The point-forecast document, as both `/api/point/<name>` and
 * `/api/point?lat&lon` serve it.
 *
 * The two are the same shape and describe different things, and the model is
 * built around making that difference impossible to miss rather than papering
 * over it. A configured location is sampled from KNMI's twenty members at its
 * own square kilometre while the timestep is still in memory, so it carries
 * `median`, `p10`…`p90` and a `probability` saying how many members agreed. A
 * coordinate is read back off the published frames: `measured` over the hour of
 * radar composite, `field` past it — whatever the ingestor reduced the members
 * into, a probability-matched mean by default — and the `nearby_*` percentiles
 * the spread frame carries for a small radius around each pixel.
 *
 * So a coordinate does get a real band. It answers "near here" rather than
 * "here", and has no probability behind it, which is why [centre] exists: no
 * one key is present on every step of every document, and picking one would
 * leave either the observed hour or the whole ad-hoc case with nothing to draw.
 * Read through [Centre] and a phone's own position renders through exactly the
 * same path as a location somebody configured.
 *
 * Every value is nullable and none defaults to zero. That is the one decision
 * here worth defending on its own: a step with no number is not a step with no
 * rain, and `0.0` for an absent key is a confident forecast of dry weather
 * assembled out of nothing. It read that way for `median` — a key no coordinate
 * has carried since the frames started publishing a field.
 *
 * Everything else is lenient by design. The server omits blocks rather than
 * sending empty ones, so unknown keys are ignored and absence is normal: a
 * future field on the server must not stop an installed app from working, and
 * an older server must not stop a newer app from reading it.
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
    /** Who the radar half of this document belongs to. */
    val source: SourceRef? = null,
    /** Who the hourly blocks belong to. Absent when none were fetched. */
    @SerialName("conditions_source") val conditionsSource: SourceRef? = null,
) {
    val hasRainSeries: Boolean
        get() = !outOfCoverage && (precipitation?.series?.isNotEmpty() == true)

    val rainSteps: List<Step>
        get() = precipitation?.series.orEmpty()

    /** Where the radar series stops, and the hourly outlook picks up. */
    val rainEndsAt: Long?
        get() = if (hasRainSeries) rainSteps.lastOrNull()?.t else null

    /** How to read the rain series: which value per step, and which band. */
    val rain: Centre
        get() = Centre.of(precipitation, fallbackLabel = "median of the members")

    /** The drawn value for one rain step, or `null` where there is nothing. */
    fun centre(step: Step): Double? = rain.valueOf(step)
}

/**
 * How to read one series: the chain of keys a step's value may come from, best
 * first, and the band to draw around it.
 *
 * A chain rather than one key, because a series is not homogeneous. The hour of
 * observed radar in front of a coordinate's forecast is measurement, with no
 * ensemble behind it and so no neighbourhood median, while the forecast steps
 * have one. Naming the key the forecast half carries leaves the measured half
 * with no number at all — which is worse than drawing nothing, because half a
 * chart looks like a whole one.
 *
 * The order is the order of usefulness, and the band follows the line rather
 * than the other way round. The neighbourhood median leads because it is the
 * number that band belongs to: the field is dealt by rank, so it can sit at
 * zero through the band's whole peak and then spike after it, and a line doing
 * that inside its own band looks unrelated to it.
 *
 * Ported from `centreOf` in the web app's `point.js`, key for key, so a phone
 * and a browser open side by side draw the same line from the same document.
 */
data class Centre(
    val keys: List<CentreKey>,
    /** What to call each key to a reader, parallel to [keys]. */
    val labels: List<String>,
    /**
     * The pairs of keys to shade between, widest first, or empty when there is
     * no band. More than one because a band of bands reads as confidence
     * without needing a legend: the outer covering 80% of the members and the
     * inner 50% says "likely" and "very likely" in one picture.
     */
    val bands: List<BandKeys> = emptyList(),
) {
    /** The value this step is drawn as, or `null` if it carries none of them. */
    fun valueOf(step: Step): Double? = keys.firstNotNullOfOrNull { step.value(it) }

    /** Which key that value came from, for a label that is true of it. */
    fun keyOf(step: Step): CentreKey? = keys.firstOrNull { step.value(it) != null }

    /** How to name the line, best key first. Empty when there is nothing. */
    val label: String
        get() = labels.firstOrNull().orEmpty()

    /**
     * The stretches of a series where both edges of the band are present.
     *
     * A coordinate's series changes kind part way through: an hour of radar
     * composite, which has no ensemble behind it and so no band, then forecast
     * steps that do. Anything drawing one shape over the whole series would
     * slope from the band's first real value down to nothing across the
     * measured hour — drawing uncertainty about a measurement, which is the
     * opposite of what the band is for.
     *
     * Empty when there is no band at all, so a caller can iterate without
     * asking first.
     */
    fun bandRuns(steps: List<Step>, band: BandKeys): List<List<Step>> {
        val runs = mutableListOf<List<Step>>()
        var current = mutableListOf<Step>()
        for (step in steps) {
            if (step.value(band.low) != null && step.value(band.high) != null) {
                current.add(step)
            } else if (current.isNotEmpty()) {
                runs.add(current)
                current = mutableListOf()
            }
        }
        if (current.isNotEmpty()) runs.add(current)
        return runs
    }

    companion object {
        fun of(block: Series?, fallbackLabel: String = ""): Centre {
            val steps = block?.series.orEmpty()
            fun carries(key: CentreKey) = steps.any { it.value(key) != null }

            val chain = mutableListOf<Pair<CentreKey, String>>()
            val bands = mutableListOf<BandKeys>()

            // The ad-hoc endpoint publishes the radius under both names, a
            // configured location under only the first. Either will do to say
            // what the band is a band across.
            val radius = block?.nearbyRadiusKm ?: block?.bandRadiusKm
            if (radius != null && carries(CentreKey.NearbyMedian)) {
                val km = Math.round(radius).toString()
                chain += CentreKey.NearbyMedian to "median within $km km"
                // Decided here, with the line, rather than further down: the
                // per-cell percentiles are a different kind of number from the
                // neighbourhood median, and drawing them around it is the
                // mismatch this whole chain exists to avoid.
                bands += BandKeys(
                    CentreKey.NearbyP10,
                    CentreKey.NearbyP90,
                    "80% of members within $km km",
                )
            }
            if (carries(CentreKey.Measured)) {
                chain += CentreKey.Measured to "measured by radar"
            }
            if (carries(CentreKey.Field)) {
                chain += CentreKey.Field to block?.fieldProduct.orEmpty()
            }
            if (carries(CentreKey.Median) || chain.isEmpty()) {
                chain += CentreKey.Median to fallbackLabel
            }

            // A block marked `median_only` carries percentiles that are all
            // equal to the line — the server kept the shape so one code path
            // could read it. Servers that predate the spread layer still send
            // that for a coordinate, and shading between copies of one number
            // would draw impossible confidence.
            if (bands.isEmpty() && block?.medianOnly != true) {
                if (carries(CentreKey.P10) && carries(CentreKey.P90)) {
                    bands += BandKeys(CentreKey.P10, CentreKey.P90, "80% of the members")
                }
                // Inner band second, so a caller drawing them in order paints
                // the narrower one on top of the wider.
                if (carries(CentreKey.P25) && carries(CentreKey.P75)) {
                    bands += BandKeys(CentreKey.P25, CentreKey.P75, "50% of the members")
                }
            }

            return Centre(chain.map { it.first }, chain.map { it.second }, bands)
        }
    }
}

/** The two keys a shaded band runs between, and what it means to a reader. */
data class BandKeys(val low: CentreKey, val high: CentreKey, val label: String)

/**
 * Every entry field a line or a band may be read from.
 *
 * An enum rather than string keys so that a chart cannot ask for something the
 * model does not know how to answer, and so adding a key to the document is one
 * change here rather than a search for string literals.
 */
enum class CentreKey {
    /** The radar composite: a measurement, with no ensemble behind it. */
    Measured,

    /** What the map paints — see `field_product` for what it is a reduction of. */
    Field,

    /** The members' own median at one square kilometre. Named locations only. */
    Median,

    NearbyP10,
    NearbyMedian,
    NearbyP90,
    P10,
    P25,
    P75,
    P90,
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
    /** Members raining near the location while the cell itself stays dry. */
    @SerialName("chance_nearby") val chanceNearby: Double? = null,
    @SerialName("chance_at") val chanceAt: Long? = null,
    val text: String = "",
)

/**
 * One block of the document: a series and what it is a series of.
 *
 * The metadata is what stops a reader having to guess. `nearbyRadiusKm` says
 * what the `nearby_*` percentiles were taken across, `fieldProduct` says what
 * the field is a reduction of, and `frameOnly` says the whole thing was read
 * back off published pictures rather than out of the members.
 */
@Serializable
data class Series(
    val unit: String = "",
    /**
     * The percentiles are copies of the line. Served by versions of the point
     * endpoint that predate the spread layer, and still worth reading: it is
     * the difference between a band and a ribbon of nothing.
     */
    @SerialName("median_only") val medianOnly: Boolean = false,
    /** Sampled from the published frames rather than from the members. */
    @SerialName("frame_only") val frameOnly: Boolean = false,
    /** What the `nearby_*` percentiles are a band across. */
    @SerialName("nearby_radius_km") val nearbyRadiusKm: Double? = null,
    /** The same radius under the name the ad-hoc endpoint publishes it. */
    @SerialName("band_radius_km") val bandRadiusKm: Double? = null,
    /** What `field` is a reduction of — "probability-matched mean", usually. */
    @SerialName("field_product") val fieldProduct: String? = null,
    /** The radius of `probability_nearby`, which is not the band's radius. */
    @SerialName("neighbourhood_km") val neighbourhoodKm: Double? = null,
    /** How many members the percentiles were taken over, where they were. */
    val members: Int? = null,
    val series: List<Step> = emptyList(),
)

/**
 * One timestep.
 *
 * `kind` is only present on ad-hoc documents, where it says whether the step is
 * `observed`, `nowcast` or `forecast`. It is worth carrying: which part is
 * measured and which is extrapolated is the distinction the whole project is
 * built around, and a chart that draws them identically is quietly lying.
 *
 * Nothing here defaults to zero. See [Forecast].
 */
@Serializable
data class Step(
    val t: Long,
    val kind: String? = null,
    /** The radar composite's own rate. Observed steps of a coordinate. */
    val measured: Double? = null,
    /** What the map paints. Named `field` in the document. */
    @SerialName("field") val drawn: Double? = null,
    val median: Double? = null,
    val p10: Double? = null,
    val p25: Double? = null,
    val p75: Double? = null,
    val p90: Double? = null,
    val mean: Double? = null,
    /** Share of members raining at this cell. Members-backed documents only. */
    val probability: Double? = null,
    /** Share of members raining within `neighbourhood_km` of it. */
    @SerialName("probability_nearby") val probabilityNearby: Double? = null,
    @SerialName("nearby_p10") val nearbyP10: Double? = null,
    @SerialName("nearby_median") val nearbyMedian: Double? = null,
    @SerialName("nearby_p90") val nearbyP90: Double? = null,
    /** This step stood in for one KNMI published empty. */
    val estimated: Boolean = false,
) {
    fun value(key: CentreKey): Double? = when (key) {
        CentreKey.Measured -> measured
        CentreKey.Field -> drawn
        CentreKey.Median -> median
        CentreKey.NearbyP10 -> nearbyP10
        CentreKey.NearbyMedian -> nearbyMedian
        CentreKey.NearbyP90 -> nearbyP90
        CentreKey.P10 -> p10
        CentreKey.P25 -> p25
        CentreKey.P75 -> p75
        CentreKey.P90 -> p90
    }
}

/** A credit line, as both `source` and `conditions_source` carry it. */
@Serializable
data class SourceRef(
    val attribution: String = "",
    val dataset: String? = null,
    val product: String? = null,
    val model: String? = null,
) {
    /** "KNMI (CC BY 4.0) — probability-matched mean of 20 members". */
    val text: String
        get() = listOfNotNull(
            attribution.takeIf { it.isNotBlank() },
            product ?: model ?: dataset,
        ).joinToString(" — ")
}

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
