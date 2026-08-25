package nl.buitjes.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * The fixtures here are not hand-written: they are real responses from
 * `/api/point?lat&lon`, captured from the server and trimmed to a few
 * timesteps. That matters more than it might look. The client and the server
 * share nothing but this document, so a model that parses an idealised version
 * of it proves only that the author was consistent with themselves. Parsing
 * what the server actually emitted is the only test that can catch the two
 * drifting apart.
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

        // The distinction the whole model is built around.
        assertTrue(forecast.medianOnly, "a sampled coordinate has no spread")
        val step = forecast.rainSteps.first()
        assertEquals(step.median, step.p10)
        assertEquals(step.median, step.p90)
        assertNull(step.probability, "no members means no probability")
        assertEquals("observed", step.kind)

        assertTrue(forecast.summary.text.isNotEmpty())
        assertNotNull(forecast.summary.peakMmH)
    }

    @Test
    fun `carries the hourly blocks alongside the rain series`() {
        val forecast = ForecastJson.parse(fixture("point_ad_hoc.json"))
        assertNotNull(forecast.temperature, "temperature comes from the ensemble proxy")
        val outlookBlock = assertNotNull(forecast.precipitationOutlook)

        // The outlook is a real ensemble, so unlike the sampled rain it does
        // have spread — this is the block a chart may legitimately band.
        val outlook = outlookBlock.series.first()
        assertTrue(outlook.p90 >= outlook.p10)
    }

    @Test
    fun `reads out-of-coverage as an absence, not a dry forecast`() {
        val forecast = ForecastJson.parse(fixture("point_out_of_coverage.json"))

        assertTrue(forecast.outOfCoverage)
        assertFalse(forecast.hasRainSeries, "no series at all, rather than zeros")
        assertTrue(forecast.rainSteps.isEmpty())
        // The conditions still answer abroad; only the radar does not.
        assertNotNull(forecast.temperature)
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

    /**
     * A named location's document is the other shape this model has to read:
     * real percentiles, a `probability` per step, and no `median_only` flag.
     */
    @Test
    fun `parses a named location document with real spread`() {
        val forecast = ForecastJson.parse(
            """
            {
              "generated_at": 100, "reference_time": 100,
              "location": {"name": "home", "lat": 52.37, "lon": 4.9},
              "summary": {"raining_now": true, "peak_mm_h": 2.2, "text": "Raining now."},
              "precipitation": {"unit": "mm/h", "series": [
                {"t": 100, "p10": 0.1, "p25": 0.4, "median": 1.1, "p75": 2.0,
                 "p90": 3.2, "mean": 1.3, "probability": 0.85}
              ]}
            }
            """.trimIndent(),
        )

        assertFalse(forecast.medianOnly, "a configured location keeps its members")
        val step = forecast.rainSteps.single()
        assertTrue(step.p90 > step.p10, "there is a real band to draw")
        assertEquals(0.85, step.probability)
        assertNull(step.kind, "only ad-hoc documents label the segments")
    }

    @Test
    fun `round-trips through encoding, for the widget cache`() {
        val original = ForecastJson.parse(fixture("point_ad_hoc.json"))
        val restored = ForecastJson.parse(ForecastJson.encode(original))
        assertEquals(original, restored)
    }
}
