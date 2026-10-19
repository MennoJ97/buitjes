# Notes for coding agents

## Open: generate `<lastmod>` in the sitemap (handoff, 2026-10-19)

`frontend/sitemap.xml` is hand-written and has no `<lastmod>`, on purpose: a
hand-typed date goes stale, and once a site's lastmod proves wrong Google
ignores it for every URL on the site. Next session that touches the frontend
or its build: make code write it, then delete this section.

The map page's HTML only changes when `frontend/index.html` does (radar data
arrives by fetch() and is not a content change), so lastmod = that file's last
commit date. Simplest: stamp it in `backend/Dockerfile` at build time from
`git log -1 --format=%cs -- frontend/index.html`, or have the backend serve
/sitemap.xml with a date baked in at build. Never the build time itself.
W3C date (`YYYY-MM-DD`); leave the tag out rather than guess.
