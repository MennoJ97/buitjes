/**
 * The reader's API key, and the fetch wrapper that presents it.
 *
 * The map is public: bounds and frames are served to anyone, because a rain map
 * that needs a login is not a rain map. The list of *published locations* is
 * not public — it carries the names and coordinates its owner configured, which
 * is a fact about a person rather than about the weather. So the server hides
 * `points` from an unkeyed caller, and this is how a keyed one identifies
 * itself.
 *
 * The key arrives once as `?key=…`, is kept in localStorage and stripped from
 * the address bar immediately: leaving it there would put it in the history, in
 * screenshots, and in the referrer of every outbound link.
 *
 * Worth being plain about the threat model. This is the same key the homepage
 * widget carries, and a key held in a browser is an identifier, not a secret —
 * it keeps a location list from being handed to every passing crawler, and it
 * can be rotated. It is not authentication. Put the page behind SSO if
 * that is what you need.
 */

const STORAGE_KEY = 'stratus.key';

function readStoredKey() {
    try {
        return localStorage.getItem(STORAGE_KEY) || '';
    } catch {
        return '';
    }
}

/** Take `?key=` out of the URL and remember it. Returns the key in force. */
function adoptKeyFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const supplied = params.get('key');
    if (!supplied) return readStoredKey();

    try {
        localStorage.setItem(STORAGE_KEY, supplied);
    } catch {
        // Storage disabled: the key still works for this page load.
    }
    params.delete('key');
    const query = params.toString();
    history.replaceState(
        null,
        '',
        window.location.pathname + (query ? `?${query}` : '') + window.location.hash
    );
    return supplied;
}

let apiKey = adoptKeyFromUrl();

export const hasApiKey = () => Boolean(apiKey);

export function forgetApiKey() {
    apiKey = '';
    try {
        localStorage.removeItem(STORAGE_KEY);
    } catch {
        // Nothing to forget.
    }
}

/**
 * `fetch` with the key attached, for same-origin API calls.
 *
 * A header rather than a query parameter, so the key stays out of server access
 * logs. Frames are loaded as images and cannot carry one, which is why they are
 * not among the endpoints a key guards.
 */
export function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (apiKey) headers.set('X-API-Key', apiKey);
    return fetch(url, { ...options, headers });
}
