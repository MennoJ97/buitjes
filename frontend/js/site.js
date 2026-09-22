/**
 * The link back to the site this instance belongs to.
 *
 * Buitjes usually runs on a subdomain of something larger, and which site that
 * is — if any — is a deployment's choice rather than this code's. The backend
 * puts `site` in the manifest when `SITE_HOME_URL` is configured; a deployment
 * that configures nothing gets no link at all, instead of everyone's copy
 * pointing at whoever happened to write this.
 *
 * So the markup ships the link hidden and empty, and this fills it in.
 */

/**
 * Fill in and reveal every `[data-site-home]`, or leave them all hidden.
 *
 * The attribute goes on whatever should appear and disappear as a unit: the
 * anchor itself where it stands alone, or a wrapper where there is prose around
 * it — hiding only the anchor there would leave a dangling "Part of".
 *
 * @param {{url?: unknown, label?: unknown} | null | undefined} site
 *   The manifest's `site` block, or nothing.
 */
export function applySiteHome(site) {
    const url = typeof site?.url === 'string' ? site.url : '';
    const label = typeof site?.label === 'string' ? site.label.trim() : '';
    // The backend rejects anything that is not a plain http(s) URL, and this is
    // its own origin answering. But the value is about to become an href, and
    // checking it twice costs one regex.
    const ok = /^https?:\/\//i.test(url) && label !== '';

    for (const node of document.querySelectorAll('[data-site-home]')) {
        const anchor = node.matches('a') ? node : node.querySelector('a');
        if (!ok || !anchor) {
            node.hidden = true;
            continue;
        }
        anchor.href = url;
        anchor.textContent = label;
        node.hidden = false;
    }
}
