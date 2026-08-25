/**
 * Is what the page is showing still current?
 *
 * The server already answers this on `/healthz`: it knows the newest reference
 * time and the tolerance it was configured with. Asking it, rather than
 * subtracting `reference_time` from `Date.now()` here, is not just tidiness —
 * the browser's arithmetic measures the *reader's* clock against the server's
 * data, so a laptop half an hour out would report a perfectly healthy server as
 * broken. It also means the threshold lives in exactly one place
 * (`MAX_MANIFEST_AGE_SECONDS`) instead of being duplicated into a constant here
 * that nobody remembers to change.
 *
 * `/healthz` is deliberately never behind the API key — it is the endpoint you
 * need most when something is wrong — so this uses a plain fetch and carries no
 * dependency on key.js.
 */

/** A minute: fast enough that "40 min old" is never wrong by much, cheap enough
 *  that it does not matter. The response is a few hundred bytes. */
export const HEALTH_POLL_MS = 60 * 1000;

/**
 * Ask once. Returns the parsed body, or null if the server could not be reached.
 *
 * A degraded server answers 503 *with* the body we want, so the status is not
 * checked — `!response.ok` is the normal case here, not a failure. null means
 * something quite different: we have no idea, and whatever else the page is
 * doing will be failing too and reporting it.
 */
export async function fetchHealth() {
    try {
        const response = await fetch('/healthz', { cache: 'no-store' });
        const body = await response.json();
        return typeof body?.status === 'string' ? body : null;
    } catch {
        return null;
    }
}

/**
 * Read a health document into what the UI needs.
 *
 * `stale` is narrower than "not ok" on purpose. A server with no manifest at
 * all is also not ok, but that is a cold start, not stale data — the pages
 * already have a warm-up path that says so and retries quickly, and labelling
 * an empty server "40 minutes old" would be both wrong and alarming.
 */
export function readHealth(health) {
    const age = health?.forecast_age_seconds;
    const stale = !!health && health.status !== 'ok' && typeof age === 'number';
    return { stale, age: stale ? age : null, detail: health?.detail ?? '' };
}

/** "8 min", "1 h 12 min" — an age, not a duration to do sums with. */
export function describeAge(seconds) {
    const minutes = Math.max(1, Math.round(seconds / 60));
    if (minutes < 90) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return rest ? `${hours} h ${rest} min` : `${hours} h`;
}
