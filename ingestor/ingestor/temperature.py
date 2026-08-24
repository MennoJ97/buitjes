"""Temperature for the point forecast, with ensemble spread.

KNMI's seamless product is precipitation only, so temperature comes from
Open-Meteo's *ensemble* endpoint rather than its deterministic one: it returns
every member of the underlying model, which is what lets the widget draw a band
instead of a bare line, matching how precipitation is presented.

This is the one external dependency in the pipeline. It is deliberately
best-effort: the homepage still only ever talks to this server, and if
Open-Meteo is unreachable the point forecast is published without temperature
rather than not published at all.

Temperature moves far more slowly than rain, and is only published hourly, so it
is refreshed on its own much slower schedule instead of once per 5-minute cycle.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import numpy as np
import requests

from .points import PERCENTILES

log = logging.getLogger(__name__)

ENDPOINT = 'https://ensemble-api.open-meteo.com/v1/ensemble'
ATTRIBUTION = 'Open-Meteo (CC BY 4.0)'


def _to_timestamp(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


class TemperatureSource:
    """Hourly temperature percentiles per location, cached between refreshes."""

    def __init__(self, model: str, past_hours: int, forecast_hours: int,
                 refresh_seconds: int, timeout: int = 20):
        self.model = model
        self._past_hours = past_hours
        self._forecast_hours = forecast_hours
        self._refresh_seconds = refresh_seconds
        self._timeout = timeout
        self._cache: dict[str, tuple[float, list]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.model)

    def series_for(self, location) -> list | None:
        """Percentile series for a location, refetching only when stale."""
        if not self.enabled:
            return None

        cached = self._cache.get(location.name)
        if cached and time.time() - cached[0] < self._refresh_seconds:
            return cached[1]

        try:
            series = self._fetch(location)
        except Exception as exc:
            # Never let a third party stop the precipitation forecast going out.
            log.warning('temperature fetch failed for %s (%s); %s',
                        location.name, exc,
                        'reusing cached values' if cached else 'publishing without temperature')
            return cached[1] if cached else None

        self._cache[location.name] = (time.time(), series)
        return series

    def _fetch(self, location) -> list:
        response = requests.get(
            ENDPOINT,
            params={
                'latitude': location.lat,
                'longitude': location.lon,
                'hourly': 'temperature_2m',
                'models': self.model,
                'past_hours': self._past_hours,
                'forecast_hours': self._forecast_hours,
                'timezone': 'UTC',
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        payload = response.json()

        hourly = payload['hourly']
        times = hourly['time']
        # The unnumbered series is the control run; the rest are the members.
        member_keys = [key for key in hourly if key.startswith('temperature_2m')]

        series = []
        for index, stamp in enumerate(times):
            values = [
                hourly[key][index]
                for key in member_keys
                if hourly[key][index] is not None
            ]
            if not values:
                continue
            quantiles = np.percentile(np.asarray(values, dtype=np.float64), PERCENTILES)
            entry = {'t': _to_timestamp(stamp)}
            for percentile, value in zip(PERCENTILES, quantiles):
                key = 'median' if percentile == 50 else f'p{percentile}'
                entry[key] = round(float(value), 1)
            series.append(entry)

        log.info('temperature for %s: %d hours from %d members (%s)',
                 location.name, len(series), len(member_keys), self.model)
        return series
