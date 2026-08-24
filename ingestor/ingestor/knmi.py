"""Minimal client for the KNMI Open Data API.

The API rate-limits hard and without documented headroom, so every call goes
through the same retry path: honour Retry-After when offered, otherwise back off
exponentially. A cycle costs two calls (list newest, then resolve a download
URL); the download itself is a pre-signed URL that does not count against the
API and does not carry the key.
"""

from __future__ import annotations

import logging
import time

import requests

BASE_URL = 'https://api.dataplatform.knmi.nl/open-data/v1'

log = logging.getLogger(__name__)


class RateLimited(RuntimeError):
    def __init__(self, retry_after: float):
        super().__init__(f'rate limited, retry in {retry_after:.0f}s')
        self.retry_after = retry_after


class KnmiClient:
    def __init__(self, api_key: str, timeout: int = 60):
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({'Authorization': api_key})

    @staticmethod
    def _files_url(dataset: str, version: str) -> str:
        return f'{BASE_URL}/datasets/{dataset}/versions/{version}/files'

    def _get(self, url: str, params=None, attempts: int = 4):
        delay = 5.0
        for attempt in range(1, attempts + 1):
            response = self._session.get(url, params=params, timeout=self._timeout)
            if response.status_code == 429:
                retry_after = float(response.headers.get('Retry-After') or delay)
                if attempt == attempts:
                    raise RateLimited(retry_after)
                log.warning('rate limited, sleeping %.0fs (attempt %d)', retry_after, attempt)
                time.sleep(retry_after)
                delay = min(delay * 2, 120)
                continue
            if response.status_code >= 500:
                if attempt == attempts:
                    response.raise_for_status()
                log.warning('server error %s, retrying in %.0fs', response.status_code, delay)
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            response.raise_for_status()
            return response
        raise RuntimeError('unreachable')

    def newest_filenames(self, dataset: str, version: str, count: int = 1) -> list[str]:
        """The ``count`` most recently published filenames, newest first."""
        payload = self._get(
            self._files_url(dataset, version),
            params={'maxKeys': count, 'orderBy': 'created', 'sorting': 'desc'},
        ).json()
        return [entry['filename'] for entry in payload.get('files') or []]

    def latest_filename(self, dataset: str, version: str) -> str | None:
        """Newest published file, or None when the dataset is empty."""
        names = self.newest_filenames(dataset, version, 1)
        return names[0] if names else None

    def download(self, dataset: str, version: str, filename: str, destination: str) -> str:
        url = self._get(
            f'{self._files_url(dataset, version)}/{filename}/url'
        ).json()['temporaryDownloadUrl']
        # Pre-signed URL: deliberately not sent through the authorised session.
        with requests.get(url, stream=True, timeout=self._timeout) as response:
            response.raise_for_status()
            with open(destination, 'wb') as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    handle.write(chunk)
        return destination
