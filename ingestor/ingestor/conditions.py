"""Non-precipitation conditions for the point forecast, with ensemble spread.

KNMI's seamless product is precipitation only. Everything else — temperature,
wind, solar radiation — comes from Open-Meteo's *ensemble* endpoint rather than
its deterministic one: it returns every member of the underlying model, so these
get the same kind of spread the rain forecast has instead of a bare line.

Why not KNMI: they do publish these with uncertainty (HARMONIE-AROME Cy43 EPS),
but that is a 5.5 GB tar of GRIB per hourly cycle to read a few numbers at one
point. See docs/IMPLEMENTATION_PLAN.md.

This is the one external dependency in the pipeline and it is deliberately
best-effort: the homepage still only ever talks to this server, and if
Open-Meteo is unreachable the point forecast is published without these rather
than not published at all. All variables come from a single request, and are
refreshed on their own slow schedule since they are hourly and slow-moving.
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

#: Open-Meteo variable -> (key in the published document, unit label, decimals).
VARIABLES = {
    'temperature_2m': ('temperature', '°C', 1),
    'wind_speed_10m': ('wind', 'm/s', 1),
    'shortwave_radiation': ('solar', 'W/m²', 0),
}


def _to_timestamp(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


class ConditionsSource:
    """Hourly percentile series per location, cached between refreshes."""

    def __init__(self, model: str, past_hours: int, forecast_hours: int,
                 refresh_seconds: int, timeout: int = 20):
        self.model = model
        self._past_hours = past_hours
        self._forecast_hours = forecast_hours
        self._refresh_seconds = refresh_seconds
        self._timeout = timeout
        self._cache: dict[str, tuple[float, dict]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.model)

    def for_location(self, location) -> dict | None:
        """``{'temperature': {...}, 'wind': {...}, 'solar': {...}}`` or None."""
        if not self.enabled:
            return None

        cached = self._cache.get(location.name)
        if cached and time.time() - cached[0] < self._refresh_seconds:
            return cached[1]

        try:
            blocks = self._fetch(location)
        except Exception as exc:
            # Never let a third party stop the precipitation forecast going out.
            log.warning('conditions fetch failed for %s (%s); %s',
                        location.name, exc,
                        'reusing cached values' if cached else 'publishing precipitation only')
            return cached[1] if cached else None

        self._cache[location.name] = (time.time(), blocks)
        return blocks

    def _fetch(self, location) -> dict:
        response = requests.get(
            ENDPOINT,
            params={
                'latitude': location.lat,
                'longitude': location.lon,
                'hourly': ','.join(VARIABLES),
                'models': self.model,
                'past_hours': self._past_hours,
                'forecast_hours': self._forecast_hours,
                'wind_speed_unit': 'ms',
                'timezone': 'UTC',
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        hourly = response.json()['hourly']
        times = hourly['time']

        blocks = {}
        for variable, (key, unit, decimals) in VARIABLES.items():
            # The unnumbered series is the control run, the rest are members.
            member_keys = [
                name for name in hourly
                if name == variable or name.startswith(f'{variable}_member')
            ]
            series = _percentile_series(hourly, member_keys, times, decimals)
            if series:
                blocks[key] = {'unit': unit, 'series': series}
                log.info('%s for %s: %d hours from %d members (%s)',
                         key, location.name, len(series), len(member_keys), self.model)
        return blocks


def _percentile_series(hourly, member_keys, times, decimals) -> list[dict]:
    series = []
    for index, stamp in enumerate(times):
        values = [
            hourly[key][index] for key in member_keys if hourly[key][index] is not None
        ]
        if not values:
            continue
        quantiles = np.percentile(np.asarray(values, dtype=np.float64), PERCENTILES)
        entry = {'t': _to_timestamp(stamp)}
        for percentile, value in zip(PERCENTILES, quantiles):
            name = 'median' if percentile == 50 else f'p{percentile}'
            entry[name] = round(float(value), decimals) if decimals else round(float(value))
        series.append(entry)
    return series
