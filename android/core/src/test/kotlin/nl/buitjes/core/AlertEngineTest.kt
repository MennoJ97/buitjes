package nl.buitjes.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * These mirror `ingestor/tests/test_alerts.py`, and for the same reason it
 * gives: the interesting question about an alerting system is not "does it
 * fire" but "does it stay quiet". Every test here that asserts silence is
 * guarding against the failure mode that makes people turn notifications off.
 */
class AlertEngineTest {

    private val now = 1_700_000_000L

    private fun forecast(
        vararg rates: Double,
        start: Long = 0,
        probability: Double? = null,
        outOfCoverage: Boolean = false,
        medianOnly: Boolean = true,
    ): Forecast {
        val steps = rates.mapIndexed { index, rate ->
            Step(
                t = (if (start == 0L) now else start) + index * 300L,
                median = rate,
                p10 = rate,
                p90 = rate,
                probability = probability,
            )
        }
        return Forecast(
            referenceTime = now,
            precipitation = if (outOfCoverage) null else Series(
                unit = "mm/h",
                medianOnly = medianOnly,
                series = steps,
            ),
            outOfCoverage = outOfCoverage,
        )
    }

    private val rule = AlertRule(
        targetKind = AlertTargetKind.CURRENT_LOCATION,
        thresholdMmH = 1.0,
        withinSeconds = 3600,
        quietSeconds = 3600,
    )

    @Test
    fun `fires once when rain enters the window`() {
        val outcome = AlertEngine.consider(forecast(0.0, 0.0, 1.2), rule, RuleState(), now)
        assertNotNull(outcome.fire, "rain above the threshold should fire")
        assertTrue(outcome.state.active, "and the rule should latch")
    }

    @Test
    fun `stays quiet while latched`() {
        val first = AlertEngine.consider(forecast(0.0, 0.0, 1.2), rule, RuleState(), now)
        val second = AlertEngine.consider(
            forecast(0.0, 1.2, 1.4),
            rule,
            first.state,
            now + 300,
        )
        assertNull(second.fire, "the same shower must not be announced twice")
        assertTrue(second.state.active)
    }

    /**
     * The hysteresis case. Just under the threshold is not "clearly dry", so
     * the rule stays latched — otherwise a shower hovering either side of the
     * line rings on alternating cycles.
     */
    @Test
    fun `does not re-arm merely below the threshold`() {
        val latched = RuleState(active = true, lastFired = now)
        val outcome = AlertEngine.consider(forecast(0.9, 0.8, 0.7), rule, latched, now + 600)
        assertNull(outcome.fire)
        assertTrue(outcome.state.active, "0.7 of a 1.0 threshold is not clearly dry")
    }

    @Test
    fun `re-arms once the window is clearly dry`() {
        val latched = RuleState(active = true, lastFired = now)
        val outcome = AlertEngine.consider(forecast(0.0, 0.1, 0.0), rule, latched, now + 600)
        assertFalse(outcome.state.active, "below 60% of the threshold is clearly dry")
        assertNull(outcome.fire, "re-arming is not itself an alert")
    }

    /**
     * Each cycle gets a forecast anchored at the moment it is evaluated, the
     * way the server republishes one every five minutes. Reusing a stale
     * forecast across an hour would test nothing but the window filter, which
     * has its own test below.
     */
    @Test
    fun `quiet period holds after re-arming`() {
        val rearmed = RuleState(active = false, lastFired = now)

        val tooSoon = AlertEngine.consider(
            forecast(2.0, start = now + 1800),
            rule,
            rearmed,
            now + 1800,
        )
        assertNull(tooSoon.fire, "half an hour into a one-hour quiet period")

        val later = AlertEngine.consider(
            forecast(2.0, start = now + 3601),
            rule,
            rearmed,
            now + 3601,
        )
        assertNotNull(later.fire, "past the quiet period it may speak again")
    }

    /**
     * Editing a rule is asking a new question, so the latch of the rule it
     * replaced must not carry over.
     */
    @Test
    fun `an edited rule has its own identity`() {
        val stricter = rule.copy(thresholdMmH = 2.0)
        assertTrue(rule.key != stricter.key)
    }

    /**
     * Crossing out of the radar domain must neither fire nor re-arm: the shower
     * that latched the rule may still be falling back home.
     */
    @Test
    fun `out of coverage leaves the latch untouched`() {
        val latched = RuleState(active = true, lastFired = now)
        val outcome = AlertEngine.consider(
            forecast(outOfCoverage = true),
            rule,
            latched,
            now + 7200,
        )
        assertNull(outcome.fire)
        assertEquals(latched, outcome.state, "state must survive a trip abroad unchanged")
    }

    @Test
    fun `rain beyond the lead time is not yet news`() {
        val short = rule.copy(withinSeconds = 600)
        // Dry for half an hour, then rain — past a ten-minute lead time.
        val outcome = AlertEngine.consider(
            forecast(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 3.0),
            short,
            RuleState(),
            now,
        )
        assertNull(outcome.fire)
    }

    @Test
    fun `rain in the past is not news at all`() {
        // The series starts an hour ago and is dry from now on.
        val past = forecast(5.0, 5.0, 0.0, 0.0, start = now - 3600)
        val outcome = AlertEngine.consider(past, rule, RuleState(), now)
        assertNull(outcome.fire, "a shower that already passed must not fire")
    }

    /**
     * A probability floor cannot bite on an ad-hoc point, where there are no
     * members to count. It must not silently prevent the rule from ever firing.
     */
    @Test
    fun `a probability floor does not mute a median-only forecast`() {
        val demanding = rule.copy(probability = 0.8)
        val outcome = AlertEngine.consider(forecast(2.0), demanding, RuleState(), now)
        assertNotNull(outcome.fire, "no members means the median decides")
    }

    @Test
    fun `a probability floor still bites when members disagree`() {
        val demanding = rule.copy(probability = 0.8)
        val outcome = AlertEngine.consider(
            forecast(2.0, 2.0, probability = 0.2),
            demanding,
            RuleState(),
            now,
        )
        assertNull(outcome.fire, "two members in ten is not enough to wake someone")
    }

    @Test
    fun `describes rain already falling`() {
        val event = AlertEngine.evaluate(forecast(1.5, 2.0), rule, now)
        assertTrue(event.rainingNow)
        val (title, body) = AlertEngine.describe(event, now, "here")
        assertEquals("Rain here", title)
        assertTrue(body.contains("Raining now"), body)
        assertTrue(body.contains("2 mm/h"), body)
    }

    @Test
    fun `describes rain that is still coming`() {
        val event = AlertEngine.evaluate(forecast(0.0, 0.0, 0.0, 0.0, 3.0), rule, now)
        assertFalse(event.rainingNow)
        val (title, body) = AlertEngine.describe(event, now, "at home")
        assertEquals("Rain at home in 20 min", title)
        assertTrue(body.contains("3 mm/h"), body)
    }

    @Test
    fun `formats rates like the frontend`() {
        assertEquals("0.25", AlertEngine.formatRate(0.25))
        assertEquals("0.5", AlertEngine.formatRate(0.5))
        assertEquals("1", AlertEngine.formatRate(1.0))
        assertEquals("2.5", AlertEngine.formatRate(2.5))
        assertEquals("12", AlertEngine.formatRate(12.4))
    }

    /**
     * The whole point, as one sequence: a shower arrives, is announced once,
     * stays silent while it passes, and a genuinely new shower later is
     * announced again.
     */
    @Test
    fun `one shower is one notification`() {
        var state = RuleState()
        var announcements = 0
        val quick = rule.copy(quietSeconds = 600)

        // Twelve cycles, five minutes apart, through one shower and out again.
        val cycles = listOf(
            0.0, 0.0, 0.8, 1.4, 2.2, 3.0, 2.0, 1.1, 0.4, 0.0, 0.0, 0.0,
        )
        cycles.forEachIndexed { index, rate ->
            val at = now + index * 300L
            val outcome = AlertEngine.consider(forecast(rate, rate, start = at), quick, state, at)
            state = outcome.state
            if (outcome.fire != null) announcements++
        }
        assertEquals(1, announcements, "one shower, one notification")
        assertFalse(state.active, "and the rule is armed again once it cleared")

        // A second shower, well after the first cleared and the quiet period ran out.
        val muchLater = now + 12 * 300L + 1200
        val later = AlertEngine.consider(
            forecast(2.0, 2.0, start = muchLater),
            quick,
            state,
            muchLater,
        )
        assertNotNull(later.fire, "a genuinely new shower is news again")
    }
}
