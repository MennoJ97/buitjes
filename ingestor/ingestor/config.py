"""Configuration, all from the environment so the container stays declarative."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float_list(raw: str):
    parts = [p.strip() for p in raw.split(',')]
    if len(parts) != 4:
        raise ValueError('CROP_BOUNDS must be "west,south,east,north"')
    return tuple(float(p) for p in parts)


@dataclass(frozen=True)
class Config:
    api_key: str
    dataset: str
    version: str
    frame_dir: str
    poll_retry: int
    source_interval: int
    publish_lag: int
    notification_api_key: str
    notification_host: str
    notification_port: int
    notification_idle_timeout: int
    crop_bounds: tuple | None
    output_height: int | None
    ensemble_stat: str
    max_precip: float
    nowcast_minutes: int
    keep_cycles: int
    observed_dataset: str
    observed_version: str
    history_minutes: int
    observed_max_fetch: int

    @classmethod
    def from_env(cls) -> 'Config':
        api_key = os.environ.get('KNMI_API_KEY', '').strip()
        if not api_key or api_key == 'your_knmi_api_key_here':
            raise SystemExit(
                'KNMI_API_KEY is not set. Request a key at '
                'https://developer.dataplatform.knmi.nl/open-data-api#token'
            )

        crop = os.environ.get('CROP_BOUNDS', '').strip()
        height = os.environ.get('OUTPUT_HEIGHT', '').strip()
        stat = os.environ.get('ENSEMBLE_STAT', 'median').strip().lower()
        if stat not in ('median', 'mean', 'max'):
            raise SystemExit(f'ENSEMBLE_STAT must be median, mean or max (got {stat!r})')

        return cls(
            api_key=api_key,
            dataset=os.environ.get('KNMI_DATASET', 'seamless_precipitation_ensemble_forecast_members'),
            version=os.environ.get('KNMI_VERSION', '1.0'),
            frame_dir=os.environ.get('FRAME_DIR', '/data'),
            # Only used when the expected file is late or history is still
            # backfilling. Steady state waits for the next publication instead;
            # KNMI treats repeated blind polling as abuse of the platform.
            poll_retry=int(os.environ.get('POLL_RETRY_SECONDS', '60')),
            # How often the source publishes, and how long after a cycle's
            # nominal time its file actually appears. Together these say when it
            # is worth asking at all.
            source_interval=int(os.environ.get('SOURCE_INTERVAL_SECONDS', '300')),
            publish_lag=int(os.environ.get('PUBLISH_LAG_SECONDS', '240')),
            # Set this to stop polling entirely and let KNMI push instead. It is
            # a *separate* key from the Open Data one, requested from the same
            # developer portal.
            notification_api_key=os.environ.get('KNMI_NOTIFICATION_API_KEY', '').strip(),
            notification_host=os.environ.get('MQTT_HOST', 'mqtt.dataplatform.knmi.nl'),
            notification_port=int(os.environ.get('MQTT_PORT', '443')),
            # Safety net: run a cycle anyway if the broker has gone quiet for
            # this long, in case the connection died without us noticing.
            notification_idle_timeout=int(os.environ.get('NOTIFICATION_IDLE_TIMEOUT', '900')),
            crop_bounds=_float_list(crop) if crop else None,
            output_height=int(height) if height else None,
            ensemble_stat=stat,
            max_precip=float(os.environ.get('MAX_PRECIP_MM_H', '100')),
            # Where the timeline stops calling the blend a nowcast. The product
            # itself is seamless; this is a presentation cue, not a data boundary.
            nowcast_minutes=int(os.environ.get('NOWCAST_MINUTES', '120')),
            keep_cycles=int(os.environ.get('KEEP_CYCLES', '3')),
            # Observed history: KNMI's real-time gauge-corrected radar composite.
            observed_dataset=os.environ.get('KNMI_OBSERVED_DATASET', 'nl_rdr_data_rtcor_5m'),
            observed_version=os.environ.get('KNMI_OBSERVED_VERSION', '1.0'),
            # 0 disables observed history entirely.
            history_minutes=int(os.environ.get('HISTORY_MINUTES', '60')),
            # Backfill is deliberately slow: resolving a download URL costs an API
            # call each, and KNMI rate-limits well below a dozen calls in a row.
            observed_max_fetch=int(os.environ.get('OBSERVED_MAX_FETCH_PER_CYCLE', '3')),
        )
