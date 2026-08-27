package nl.buitjes.core

import kotlinx.serialization.Serializable
import kotlin.math.max
import kotlin.math.roundToInt
import kotlin.math.roundToLong

/**
 * Deciding when rain is worth interrupting someone for.
 *
 * This is a port of the ingestor's `alerts.py`, and the port is deliberate
 * rather than incidental: an alert should mean the same thing whichever surface
 * raised it. The server watches configured locations and delivers to a webhook;
 * the phone watches wherever it happens to be and raises a local notification.
 * If the two used different rules, "tell me when it is about to rain" would
 * quietly mean two different things depending on which one spoke.
 *
 * The problem being solved is not detection — the forecast already says whether
 * rain is coming — but restraint. The forecast is republished every five
 * minutes, so the naive rule ("notify while rain is in the window") would fire
 * a dozen times an hour for a single shower, and an alerting system that cries
 * wolf is one people turn off. What is worth a notification is the *edge*: rain
 * appearing in the window when it was not there a moment ago.
 *
 * Three mechanisms, because one is not enough:
 *
 *  * **Latching.** Once a rule fires it is active, and an active rule stays
 *    silent no matter how many cycles keep matching.
 *  * **Hysteresis.** It re-arms only when the window drops *clearly* below the
 *    threshold, not merely below it, so a shower hovering either side of the
 *    line cannot ring twice.
 *  * **A quiet period.** A floor on how often one rule may speak at all, for
 *    the case the other two did not anticipate.
 *
 * One thing differs from the server, and it is a consequence of the phone
 * moving. State is keyed by *rule*, never by coordinate: walking across town
 * during one shower is still one shower, and re-keying on position would make
 * every few hundred metres a fresh chance to be told about the same rain.
 */
object AlertEngine {

    const val DEFAULT_THRESHOLD_MM_H: Double = 0.5
    const val DEFAULT_WITHIN_MINUTES: Int = 60
    const val DEFAULT_QUIET_MINUTES: Int = 60

    /**
     * A rule re-arms when the window falls to this fraction of its threshold.
     * Below 1.0 on purpose: re-arming at exactly the threshold means a shower
     * sitting on the line re-arms and re-fires on alternating cycles.
     */
    const val REARM_FRACTION: Double = 0.6

    /**
     * How far back a step may sit and still count as "in the window".
     *
     * One step, so rain that started a moment ago still reads as raining now
     * rather than as already missed. The server uses the same 300 seconds.
     */
    private const val PAST_TOLERANCE_SECONDS = 300L

    /**
     * Read one forecast through one rule.
     *
     * Only the window between now and now + `within` is considered: rain the
     * forecast places beyond the lead time is not yet news, and rain in the
     * past is not news at all.
     */
    fun evaluate(forecast: Forecast, rule: AlertRule, now: Long): AlertEvent {
        // Whatever this document draws its line from — the neighbourhood median
        // for a coordinate, the members' own median for a configured location,
        // the radar composite over the observed hour. Thresholding on one named
        // key would mean "0.5 mm/h" quietly meant different things on the two
        // endpoints, and on an ad-hoc document it would mean nothing at all.
        val centre = forecast.rain
        val window = forecast.rainSteps.filter { step ->
            step.t >= now - PAST_TOLERANCE_SECONDS &&
                step.t <= now + rule.withinSeconds &&
                // A step carrying no value is not a dry step. It drops out
                // rather than counting as zero, which would let a hole in the
                // radar composite re-arm a latched rule mid-shower.
                centre.valueOf(step) != null
        }
        if (window.isEmpty()) return AlertEvent(rule = rule)

        val rate = { step: Step -> centre.valueOf(step) ?: 0.0 }
        val peak = window.maxBy(rate)
        // A step with no `probability` is a coordinate, where there are no
        // members to count. Treating that as 1.0 lets one rule serve both kinds
        // of document: a probability floor simply cannot bite where nothing can
        // answer it, which is the honest reading rather than a silent failure
        // to ever fire.
        val onset = window.firstOrNull { step ->
            rate(step) >= rule.thresholdMmH && (step.probability ?: 1.0) >= rule.probability
        }

        return AlertEvent(
            rule = rule,
            onset = onset?.t,
            peakMmH = rate(peak),
            peakAt = peak.t,
            probability = onset?.probability ?: 0.0,
            rainingNow = onset != null && onset.t <= now + PAST_TOLERANCE_SECONDS,
            readings = window.size,
        )
    }

    /**
     * Decide what one rule does this cycle, given what it did last time.
     *
     * Pure on purpose — state in, state out, no clock and no storage — because
     * the whole value of this class is that its behaviour over a sequence of
     * cycles can be asserted in a test rather than observed in the wild over an
     * afternoon of actual weather.
     *
     * A forecast that is out of coverage returns the state untouched: crossing
     * a border must neither fire an alert nor re-arm a latched one, since the
     * shower that latched it may well still be falling on the other side.
     */
    fun consider(
        forecast: Forecast,
        rule: AlertRule,
        previous: RuleState,
        now: Long,
    ): AlertOutcome {
        if (forecast.outOfCoverage || !forecast.hasRainSeries) {
            return AlertOutcome(previous, null)
        }

        val event = evaluate(forecast, rule, now)

        // Nothing in the window said anything — every step in it was missing,
        // or there were no steps at all. Same treatment as being out of
        // coverage, and for the same reason: silence is not a report of dry
        // weather, and re-arming on it would let one shower crossing a hole in
        // the composite be announced twice.
        if (event.readings == 0) return AlertOutcome(previous, null)

        if (!event.matched) {
            // Re-arm only once the window is clearly dry, not merely under the
            // threshold.
            val clearlyDry = event.peakMmH < rule.thresholdMmH * REARM_FRACTION
            return if (previous.active && clearlyDry) {
                AlertOutcome(previous.copy(active = false), null)
            } else {
                AlertOutcome(previous, null)
            }
        }

        if (previous.active) return AlertOutcome(previous, null)
        if (now - previous.lastFired < rule.quietSeconds) return AlertOutcome(previous, null)

        return AlertOutcome(
            state = RuleState(active = true, lastFired = now, lastOnset = event.onset),
            fire = event,
        )
    }

    /**
     * A title and a body for the notification.
     *
     * `where` is passed in rather than read off the rule because the phone's
     * answer to "where" is a sentence, not an identifier: "here" reads better
     * than a coordinate, and a named location should use the name the reader
     * chose.
     */
    fun describe(event: AlertEvent, now: Long, where: String): Pair<String, String> {
        val peak = formatRate(event.peakMmH)
        if (event.rainingNow) {
            return "Rain $where" to "Raining now, peaking around $peak mm/h."
        }

        val onset = event.onset ?: return "Rain $where" to "Rain expected, peaking around $peak mm/h."
        val minutes = max(1L, ((onset - now).toDouble() / 60.0).roundToLong())
        val chance = if (event.rule.probability > 0.0 && event.probability > 0.0) {
            " (${(event.probability * 100).roundToInt()}% of members)"
        } else {
            ""
        }
        return "Rain $where in $minutes min" to "Peaking around $peak mm/h$chance."
    }

    /** Match the frontend's rate formatting, so the surfaces never disagree. */
    internal fun formatRate(mmh: Double): String {
        if (mmh >= 10.0) return mmh.roundToInt().toString()
        val text = if (mmh >= 1.0) {
            ((mmh * 10).roundToInt() / 10.0).toString()
        } else {
            ((mmh * 100).roundToInt() / 100.0).toString()
        }
        return text.trimEnd('0').trimEnd('.').ifEmpty { "0" }
    }
}

enum class AlertTargetKind { NAMED, CURRENT_LOCATION }

/**
 * One "tell me when" — either for a location the server publishes, or for
 * wherever the phone is.
 */
@Serializable
data class AlertRule(
    val targetKind: AlertTargetKind = AlertTargetKind.CURRENT_LOCATION,
    val locationName: String? = null,
    val thresholdMmH: Double = AlertEngine.DEFAULT_THRESHOLD_MM_H,
    val withinSeconds: Int = AlertEngine.DEFAULT_WITHIN_MINUTES * 60,
    /**
     * Minimum share of members that must agree. Zero accepts the drawn line
     * alone, which is all a coordinate can answer: the frames carry
     * percentiles, and counting members needs the members.
     */
    val probability: Double = 0.0,
    val quietSeconds: Int = AlertEngine.DEFAULT_QUIET_MINUTES * 60,
) {
    /**
     * Identity in stored state.
     *
     * Includes the thresholds, so editing a rule re-arms it rather than
     * inheriting the latch of the rule it replaced — an edited rule is a new
     * question, and the reader is entitled to an answer to it.
     */
    val key: String
        get() = listOf(
            targetKind.name.lowercase(),
            locationName ?: "-",
            thresholdMmH,
            withinSeconds,
            probability,
        ).joinToString(":")

    fun validationError(): String? = when {
        thresholdMmH <= 0 -> "the threshold must be above zero"
        withinSeconds <= 0 -> "the lead time must be above zero"
        probability !in 0.0..1.0 -> "the probability is a fraction, 0 to 1"
        targetKind == AlertTargetKind.NAMED && locationName.isNullOrBlank() ->
            "a named rule needs a location"
        else -> null
    }
}

/** Whether a rule is latched, and when it last spoke. */
@Serializable
data class RuleState(
    val active: Boolean = false,
    val lastFired: Long = 0,
    val lastOnset: Long? = null,
)

/** A rule's answer for one cycle: what the window holds. */
data class AlertEvent(
    val rule: AlertRule,
    val onset: Long? = null,
    val peakMmH: Double = 0.0,
    val peakAt: Long? = null,
    val probability: Double = 0.0,
    val rainingNow: Boolean = false,
    /**
     * How many steps in the window carried a number at all.
     *
     * Zero is what tells "the window is dry" apart from "the window is empty",
     * and the two must not be confused: a peak of 0.0 mm/h is what both look
     * like from the outside, and only one of them is a reason to re-arm.
     */
    val readings: Int = 0,
) {
    val matched: Boolean get() = onset != null
}

data class AlertOutcome(val state: RuleState, val fire: AlertEvent?)
