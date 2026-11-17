"""Keep <lastmod> in frontend/sitemap.xml true, from the pages' own history.

A hand-typed lastmod goes stale, and once a site's lastmod proves wrong Google
ignores it for every URL on the site. So the date is never typed: for each
<loc>, this finds the page it serves and stamps the date of that page's last
commit. A page in the commit being made gets today's date, since that is the
commit about to exist. A page with no history and nothing staged gets no
lastmod at all, rather than a guess.

Run from .githooks/pre-commit. It cannot run at image build time: the build
context leaves .git out (see .dockerignore), so the history is not there.

The pages' content arriving by fetch() (radar frames, accuracy scores) is not
a change to the page, so only the HTML file's own commits count.
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
from urllib.parse import urlparse

SITEMAP = 'frontend/sitemap.xml'


def git(*args: str) -> str:
    return subprocess.run(('git', *args), check=True, capture_output=True, text=True).stdout


def page_for(loc: str) -> str:
    path = urlparse(loc).path
    return 'frontend/index.html' if path in ('', '/') else 'frontend' + path


def main() -> int:
    os.chdir(git('rev-parse', '--show-toplevel').strip())
    if not os.path.exists(SITEMAP):
        return 0
    staged = set(git('diff', '--cached', '--name-only').split())
    today = datetime.date.today().isoformat()

    def stamp(match: re.Match) -> str:
        block = re.sub(r'\s*<lastmod>[^<]*</lastmod>', '', match.group(0))
        loc = re.search(r'<loc>([^<]+)</loc>', block)
        if not loc:
            return block
        page = page_for(loc.group(1).strip())
        date = today if page in staged else git('log', '-1', '--format=%cs', '--', page).strip()
        if not date:
            return block
        return block.replace('</loc>', f'</loc><lastmod>{date}</lastmod>', 1)

    with open(SITEMAP, encoding='utf-8') as handle:
        before = handle.read()
    after = re.sub(r'<url>.*?</url>', stamp, before, flags=re.S)
    if after != before:
        with open(SITEMAP, 'w', encoding='utf-8') as handle:
            handle.write(after)
        git('add', SITEMAP)
        print(f'stamp_sitemap: updated <lastmod> in {SITEMAP}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
