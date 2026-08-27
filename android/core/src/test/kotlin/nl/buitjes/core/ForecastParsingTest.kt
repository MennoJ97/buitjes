package nl.buitjes.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * The fixtures here are not hand-written: they are real responses captured from
 * a running server — `/api/point?lat&lon` for the coordinate ones, the
 * ingestor's own published `point_home.json` for the named one — and trimmed to
 * a few timesteps. That matters more than it might look. The client and the
 * server share nothing but this document, so a model that parses an idealised
 * version of it proves only that the author was consistent with themselves.
 * Parsing what the server actually emitted is the only test that can catch the
 * two drifting apart.
 *
 * Trimmed means the summary describes more steps than the series holds. That is
 * deliberate: nothing here should be asserting that a captured sentence matches
 * a captured series, only that both survive the crossing.
 */
class ForecastParsingTest {

    private fun fixture(name: String): String =
        checkNotNull(javaClass.classLoader.getResourceAsStream(name)) {
            "missing test fixture $name"
        }.bufferedReader().readText()

    @Test
    fun `parses an ad-hoc point document`() {
        val forecast = ForecastJson.parse(fixture("point_ad_hoc.json"))

        assertTrue(forecast.hasRainSeries)
        assertFalse(forecast.outOfCoverage)
        assertTrue(forecast.location.adHoc)
        assertEquals("this point", forecast.location.name)
        assertTrue(forecast.summary.text.isNotEmpty())
        assertNotNull(forecast.summary.peakMmH)

        // The observed hour is a measurement and says so; nothing else on that
        // step, because there is no ensemble behind a radar composite.
        val observed = forecast.rainSteps.first()
        assertEquals("observed", observed.kind)
        assertEquals(0.0, observed.measured)
        assertNull(observed.nearbyMedian)
        assertNull(observed.median, "a coordinate has never carried a members' median")
    }

    /**
     * The point of the whole change: a coordinate nobody configured comes back
     * with a real band, taken over a small radius around its pixel.
     */
    @Test
    fun `gives an ad-hoc coordinate a band, and says what it is a band of`() {
        val forecast = ForecastJson.parse(fixture("point_ad_hoc.json"))
        val block = assertNotNull(forecast.precipitation)

        assertEquals(3.0, block.nearbyRadiusKm)
        assertEquals(3.0, block.bandRadiusKm)
        assertFalse(block.frameOnly, "there is a spread layer behind this one")
        assertEquals("probability-matched mean", block.fieldProduct)

        val band = assertNotNull(forecast.rain.band)
        assertEquals(CentreKey.NearbyP10, band.low)
        assertEquals(CentreKey.NearbyP90, band.high)
        assertEquals("80% of members within 3 km", band.label)

        // And it has width where the weather does: the members disagree by
        // about 11 mm/h at the peak of this cycle.
        val wettest = forecast.rainSteps.maxBy { forecast.centre(it) ?: 0.0 }
        assertTrue(wettest.nearbyP90!! - wettest.nearbyP10!! > 1.0)
    }

    /**
     * One series, two kinds of number, and a chain that reads both. Getting
     * this wrong leaves the observed hour blank — which looks like an hour of
     * dry weather rather than like a missing key.
     */
    @Test
    fun `reads the measured hour and the forecast through one chain`() {
        val forecast = ForecastJson.parse(fixture("point_ad_hoc.json"))

        assertEquals(
            listOf(CentreKey.NearbyMedian, CentreKey.Measured, CentreKey.Field),
            forecast.rain.keys,
        )
        assertEquals("median within 3 km", forecast.rain.label)

        val observed = forecast.rainSteps.first { it.kind == "observed" }
        val forecastStep = forecast.rainSteps.last()
        assertEquals(CentreKey.Measured, forecast.rain.keyOf(observed))
        assertEquals(CentreKey.NearbyMedian, forecast.rain.keyOf(forecastStep))
        assertEquals(observed.measured, forecast.centre(observed))
        assertEquals(forecastStep.nearbyMedian, forecast.centre(forecastStep))
    }

    /**
     * A configured location's document is the other shape this model has to
     * read: the members' own percentiles and a probability, alongside the same
     * neighbourhood band a coordinate gets.
     */
    @Test
    fun `parses a named location document`() {
        val forecast = ForecastJson.parse(fixture("point_named.json"))

        assertFalse(forecast.location.adHoc)
        assertEquals(20, forecast.precipitation?.members)
        assertEquals(10.0, forecast.precipitation?.neighbourhoodKm)

        val wettest = forecast.rainSteps.maxBy { it.median ?: 0.0 }
        assertNotNull(wettest.probability, "the members can be counted here")
        assertNotNull(wettest.probabilityNearby)
        assertTrue(wettest.p90!! > wettest.p10!!, "and they carry their own spread")

        // It still leads with the neighbourhood median, exactly as the web app
        // does, so the two surfaces draw the same line from the same document.
        assertEquals(CentreKey.NearbyMedian, forecast.rain.keys.first())
        assertEquals(CentreKey.NearbyP10, forecast.rain.band?.low)
    }

    /**
     * With no spread layer published there are no `nearby_*` keys, and a named
     * location falls back to the members it has always carried.
     */
    @Test
    fun `falls back to the members when there is no neighbourhood band`() {
        val forecast = ForecastJson.parse(
            """
            {
              "reference_time": 100,
              "location": {"name": "home", "lat": 52.37, "lon": 4.9},
              "precipitation": {"unit": "mm/h", "members": 20, "series": [
                {"t": 100, "p10": 0.1, "p25": 0.4, "median": 1.1, "p75": 2.0,
                 "p90": 3.2, "mean": 1.3, "probability": 0.85}
              ]}
            }
            """.trimIndent(),
        )

        assertEquals(listOf(CentreKey.Median), forecast.rain.keys)
        assertEquals(1.1, forecast.centre(forecast.rainSteps.single()))
        val band = assertNotNull(forecast.rain.band)
        assertEquals(CentreKey.P10, band.low)
        assertEquals(CentreKey.P90, band.high)
    }

    /**
     * A server that predates the spread layer serves a coordinate as five
     * copies of one number under `median_only`. An installed app has to keep
     * telling the truth about that — the copies are not a band, and shading
     * between them would draw impossible confidence.
     */
    @Test
    fun `refuses to band a median-only series from an older server`() {
        val forecast = ForecastJson.parse(
            """
            {
              "reference_time": 100,
              "precipitation": {"unit": "mm/h", "median_only": true, "series": [
                {"t": 100, "p10": 1.1, "p25": 1.1, "median": 1.1, "p75": 1.1, "p90": 1.1}
              ]}
            }
            """.trimIndent(),
        )

        assertEquals(listOf(CentreKey.Median), forecast.rain.keys)
        assertEquals(1.1, forecast.centre(forecast.rainSteps.single()))
        assertNull(forecast.rain.band, "the percentiles are copies, not a spread")
    }

    /**
     * The band stops where the ensemble does. Drawn as one shape across the
     * whole series it would run from the forecast's first band value down to
     * nothing over the measured hour, which is uncertainty about a measurement.
     */
    @Test
    fun `bands only the steps that have both edges`() {
        val forecast = ForecastJson.parse(fixture("point_ad_hoc.json"))
        val runs = forecast.rain.bandRuns(forecast.rainSteps)

        assertEquals(1, runs.size, "one unbroken stretch")
        val run = runs.single()
        assertTrue(run.none { it.kind == "observed" }, "the measured hour is not banded")
        assertEquals(forecast.rainSteps.count { it.nearbyMedian != null }, run.size)
    }

    /** A gap in the middle splits the band rather than bridging it. */
    @Test
    fun `breaks the band across a step that has none`() {
        val forecast = ForecastJson.parse(
            """
            {
              "precipitation": {"unit": "mm/h", "nearby_radius_km": 3.0, "series": [
                {"t": 0, "nearby_p10": 0.0, "nearby_median": 1.0, "nearby_p90": 2.0},
                {"t": 300, "field": 1.0},
                {"t": 600, "nearby_p10": 0.0, "nearby_median": 1.0, "nearby_p90": 2.0}
              ]}
            }
            """.trimIndent(),
        )

        assertEquals(listOf(1, 1), forecast.rain.bandRuns(forecast.rainSteps).map { it.size })
    }

    @Test
    fun `has no runs to draw when there is no band`() {
        val forecast = ForecastJson.parse(
            """{"precipitation": {"unit": "mm/h", "series": [{"t": 0, "median": 1.0}]}}""",
        )
        assertTrue(forecast.rain.bandRuns(forecast.rainSteps).isEmpty())
    }

    @Test
    fun `reads out-of-coverage as an absence, not a dry forecast`() {
        val forecast = ForecastJson.parse(fixture("point_out_of_coverage.json"))

        assertTrue(forecast.outOfCoverage)
        assertFalse(forecast.hasRainSeries, "no series at all, rather than zeros")
        assertTrue(forecast.rainSteps.isEmpty())
        assertNull(forecast.rainEndsAt, "nothing stopped, because nothing started")
        // The conditions still answer abroad; only the radar does not.
        assertNotNull(forecast.temperature)
        assertNotNull(forecast.precipitationOutlook)
    }

    /**
     * A missing value is missing, not zero. This is the one that would fail
     * silently and read as a confident forecast of dry weather.
     */
    @Test
    fun `leaves an absent value absent`() {
        val forecast = ForecastJson.parse(
            """{"precipitation": {"unit": "mm/h", "series": [{"t": 100}]}}""",
        )
        val step = forecast.rainSteps.single()

        assertNull(step.median)
        assertNull(step.measured)
        assertNull(step.drawn)
        assertNull(forecast.centre(step), "nothing to draw, rather than 0 mm/h")
    }

    /** The outlook picks up where the radar stops; the two never overlap. */
    @Test
    fun `carries the hourly outlook past the end of the radar series`() {
        val forecast = ForecastJson.parse(fixture("point_ad_hoc.json"))
        val ends = assertNotNull(forecast.rainEndsAt)
        val outlook = assertNotNull(forecast.precipitationOutlook)

        assertEquals(forecast.rainSteps.last().t, ends)
        assertTrue(outlook.series.all { it.t > ends })
        // A real ensemble, unlike the radar half of the same document.
        assertTrue(outlook.series.any { (it.p90 ?: 0.0) >= (it.p10 ?: 0.0) })
    }

    @Test
    fun `reads both credit lines`() {
        val forecast = ForecastJson.parse(fixture("point_ad_hoc.json"))

        assertEquals(
            "KNMI (CC BY 4.0) — probability-matched mean of 20 ensemble members",
            forecast.source?.text,
        )
        assertEquals("Open-Meteo (CC BY 4.0) — on-demand ensemble", forecast.conditionsSource?.text)
    }

    /**
     * An installed app must survive the server growing a field, so unknown keys
     * are ignored rather than fatal.
     */
    @Test
    fun `ignores fields it does not know`() {
        val forecast = ForecastJson.parse(
            """{"reference_time": 5, "something_new": {"a": 1}, "summary": {"text": "ok"}}""",
        )
        assertEquals(5, forecast.referenceTime)
        assertEquals("ok", forecast.summary.text)
    }

    @Test
    fun `round-trips through encoding, for the widget cache`() {
        val original = ForecastJson.parse(fixture("point_ad_hoc.json"))
        val restored = ForecastJson.parse(ForecastJson.encode(original))
        assertEquals(original, restored)
    }
}
