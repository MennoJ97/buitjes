# Notes for coding agents

## Open: generate `<lastmod>` in the sitemap (handoff, 2026-10-19; updated 2026-11-17)

`frontend/sitemap.xml` has no `<lastmod>`, on purpose: a hand-typed date goes
stale, and once a site's lastmod proves wrong Google ignores it for every URL
on the site. It lists two pages now, `/` and `/accuracy.html`. Both get their
data by fetch(), which is not a content change, so each page's lastmod is its
HTML file's last commit date. Never the build time itself. W3C date
(`YYYY-MM-DD`); leave the tag out rather than guess.

Stamping it in the Docker build does not work: `.dockerignore` leaves `.git`
out of the build context, so the history is not there. Done at commit time
instead: `.githooks/stamp_sitemap.py`, run by `.githooks/pre-commit`.

Not active yet, and only the owner can switch it on: the hook needs
`chmod +x .githooks/pre-commit` and, once per clone,
`git config core.hooksPath .githooks`. An agent's attempt at either was
refused as changing what runs on every commit. Once it is on and a commit has
stamped both pages, delete this section.
