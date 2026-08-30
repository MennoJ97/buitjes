package nl.buitjes.android.ui

import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Clock times, decided once.
 *
 * 24-hour everywhere, matching the rest of Buitjes — the web frontend settled
 * this ("Show 24-hour time, and decide it once") and the server's summary
 * sentences are already formatted that way before they reach this app. A
 * locale-aware format would put a 12-hour clock next to the server's 24-hour
 * one inside the same widget, in the same sentence, which is worse than being
 * wrong in one direction consistently.
 *
 * `HH:mm` is a fixed pattern rather than a locale skeleton for exactly that
 * reason, but the default locale is still passed so that a locale with
 * non-Western digits renders them.
 */
fun formatClock(epochSeconds: Long): String =
    SimpleDateFormat("HH:mm", Locale.getDefault()).format(Date(epochSeconds * 1000))

/**
 * How old a document is, in the roughest terms that are still true.
 *
 * Deliberately imprecise past the first hour. The reader's decision is binary —
 * trust this or go and look — and "3 hours ago" supports it exactly as well as
 * "3h 12m ago" while being readable at a glance on a home screen.
 */
fun formatAge(seconds: Long): String = when {
    seconds < 0 -> "just now"
    seconds < 90 -> "just now"
    seconds < 3600 -> "${seconds / 60} min ago"
    seconds < 7200 -> "an hour ago"
    seconds < 86400 -> "${seconds / 3600} hours ago"
    else -> "over a day ago"
}

/**
 * Rain rates, formatted the way the server formats them.
 *
 * Ported from `format_rate` in the backend so that a number this app draws on
 * an axis and the same number inside the server's summary sentence never
 * disagree about how many decimals it deserves.
 */
fun formatRate(mmh: Double): String {
    if (mmh >= 10.0) return mmh.toInt().toString()
    val text = if (mmh >= 1.0) {
        String.format(Locale.ROOT, "%.1f", mmh)
    } else {
        String.format(Locale.ROOT, "%.2f", mmh)
    }
    return text.trimEnd('0').trimEnd('.').ifEmpty { "0" }
}

/**
 * A short weekday, for a chart whose window is long enough that a bare clock
 * time would be ambiguous about which day it belongs to.
 */
fun formatWeekday(epochSeconds: Long): String =
    SimpleDateFormat("EEE", Locale.getDefault()).format(Date(epochSeconds * 1000))
