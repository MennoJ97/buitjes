/**
 * Clock formatting, in one place.
 *
 * Everything here is 24-hour, and explicitly so. Leaving it to the locale meant
 * the same page showed "14:35" or "02:35 PM" depending on whose browser it was,
 * with no way to ask for the one you wanted — and a radar timeline reads badly
 * in twelve-hour time, where "12:05" needs a suffix to say whether it is lunch
 * or midnight.
 *
 * `hourCycle: 'h23'` rather than `hour12: false`: the latter is specified to
 * select an hour cycle the locale prefers, which for a few locales is h24 —
 * midnight rendered as "24:00" rather than "00:00". 'h23' names the one we
 * actually want. The locale still decides separators and weekday names.
 *
 * These used to be six copies of the same options object across three modules,
 * which is how one of them ends up different from the others.
 */

const CLOCK = new Intl.DateTimeFormat([], {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
});

const DAY_CLOCK = new Intl.DateTimeFormat([], {
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
});

const WEEKDAY = new Intl.DateTimeFormat([], { weekday: 'short' });

/** Accepts a Date, or epoch seconds — which is what the API speaks. */
const asDate = (value) => (value instanceof Date ? value : new Date(value * 1000));

/** "14:35" */
export const formatClock = (value) => CLOCK.format(asDate(value));

/** "Tue 14:35", for a chart spanning more than a day. */
export const formatDayClock = (value) => DAY_CLOCK.format(asDate(value));

/** "Tue" */
export const formatWeekday = (value) => WEEKDAY.format(asDate(value));
