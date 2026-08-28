"""Configuration, all from the environment so the container stays declarative."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .alerts import parse_rules
from .points import NEIGHBOURHOOD_KM, parse_locations


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
    neighbourhood_km: float
    spread_radius_km: float | None
    max_precip: float
    nowcast_minutes: int
    full_cadence_minutes: int
    tail_step_minutes: int
    keep_cycles: int
    observed_dataset: str
    observed_version: str
    history_minutes: int
    observed_max_fetch: int
    widget_locations: tuple
    conditions_model: str
    conditions_past_hours: int
    conditions_forecast_hours: int
    conditions_refresh: int
    alert_rules: tuple
    alert_webhook: str
    alert_format: str
    alert_auth: str
    alert_state_file: str
    stall_alert: int
    stall_webhook: str
    stall_format: str
    stall_auth: str

    @classmethod
    def from_env(cls) -> 'Config':
        api_key = os.environ.get('KNMI_API_KEY', '').strip()
        if not api_key or api_key == 'your_knmi_api_key_here':
            raise SystemExit(
                'KNMI_API_KEY is not set. Request a key at '
                'https://developer.dataplatform.knmi.nl/open-data-api#token'
            )

        alert_format = os.environ.get('ALERT_FORMAT', 'json').strip().lower()
        if alert_format not in ('json', 'ntfy'):
            raise SystemExit(f'ALERT_FORMAT must be json or ntfy (got {alert_format!r})')
        stall_format = os.environ.get('STALL_ALERT_FORMAT', alert_format).strip().lower()
        if stall_format not in ('json', 'ntfy'):
            raise SystemExit(
                f'STALL_ALERT_FORMAT must be json or ntfy (got {stall_format!r})')
        try:
            alert_rules = parse_rules(os.environ.get('ALERT_RULES', ''))
        except ValueError as error:
            # A misspelt rule is silence exactly when the alert was wanted, so
            # refuse to start rather than run with a rule that will never fire.
            raise SystemExit(str(error)) from error

        known = {location.name for location in
                 parse_locations(os.environ.get('WIDGET_LOCATIONS', ''))}
        unknown = sorted({rule.location for rule in alert_rules} - known)
        if unknown:
            raise SystemExit(
                f'ALERT_RULES names {", ".join(unknown)}, which WIDGET_LOCATIONS does not '
                'publish. Alerts can only watch a location with a point forecast.'
            )

        crop = os.environ.get('CROP_BOUNDS', '').strip()
        height = os.environ.get('OUTPUT_HEIGHT', '').strip()
        stat = os.environ.get('ENSEMBLE_STAT', 'pmm').strip().lower()
        if stat not in ('pmm', 'median', 'mean', 'max'):
            raise SystemExit(
                f'ENSEMBLE_STAT must be pmm, median, mean or max (got {stat!r})'
            )

        neighbourhood = float(os.environ.get('NEIGHBOURHOOD_KM', NEIGHBOURHOOD_KM))
        if neighbourhood < 0:
            raise SystemExit('NEIGHBOURHOOD_KM cannot be negative')

        if int(os.environ.get('TAIL_STEP_MINUTES', '10')) < 0:
            raise SystemExit(
                'TAIL_STEP_MINUTES cannot be negative (0 publishes every step)')

        # Blank switches the spread layer off entirely rather than meaning zero,
        # because zero is a legitimate setting here: it asks for the percentiles
        # of this square kilometre alone. Same idea as OUTPUT_HEIGHT.
        spread = os.environ.get('SPREAD_RADIUS_KM', '3').strip()
        spread_radius = float(spread) if spread else None
        if spread_radius is not None and spread_radius < 0:
            raise SystemExit('SPREAD_RADIUS_KM cannot be negative (leave it blank to disable)')

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
            neighbourhood_km=neighbourhood,
            spread_radius_km=spread_radius,
            max_precip=float(os.environ.get('MAX_PRECIP_MM_H', '100')),
            # Where the timeline stops calling the blend a nowcast. The product
            # itself is seamless; this is a presentation cue, not a data boundary.
            nowcast_minutes=int(os.environ.get('NOWCAST_MINUTES', '120')),
            # How far out every published timestep gets a frame of its own, and
            # how far apart they are after that. KNMI publishes 72 five-minute
            # steps out to +6 h, and the last two hours of that were half the
            # bytes and most of the CPU of a cycle - a five-minute cadence four
            # hours out is a precision the blend does not have that far ahead,
            # where it is essentially the hourly HARMONIE ensemble. The window
            # defaults to the nowcast horizon because that is the same boundary
            # under a different name; set TAIL_STEP_MINUTES to 0 to publish
            # every step as before.
            full_cadence_minutes=int(os.environ.get(
                'FULL_CADENCE_MINUTES', os.environ.get('NOWCAST_MINUTES', '120'))),
            tail_step_minutes=int(os.environ.get('TAIL_STEP_MINUTES', '10')),
            keep_cycles=int(os.environ.get('KEEP_CYCLES', '3')),
            # Observed history: KNMI's real-time gauge-corrected radar composite.
            observed_dataset=os.environ.get('KNMI_OBSERVED_DATASET', 'nl_rdr_data_rtcor_5m'),
            observed_version=os.environ.get('KNMI_OBSERVED_VERSION', '1.0'),
            # 0 disables observed history entirely.
            history_minutes=int(os.environ.get('HISTORY_MINUTES', '60')),
            # Backfill is deliberately slow: resolving a download URL costs an API
            # call each, and KNMI rate-limits well below a dozen calls in a row.
            observed_max_fetch=int(os.environ.get('OBSERVED_MAX_FETCH_PER_CYCLE', '3')),
            # Locations to publish a point forecast for, as "name:lat:lon"
            # separated by semicolons. These are the only points that get
            # ensemble spread; see ingestor/points.py for why.
            widget_locations=tuple(parse_locations(os.environ.get('WIDGET_LOCATIONS', ''))),
            # Temperature, wind and solar come from Open-Meteo's ensemble
            # endpoint, so they get a spread like precipitation does. Set the
            # model empty to publish precipitation only.
            conditions_model=os.environ.get('CONDITIONS_MODEL',
                                            os.environ.get('TEMPERATURE_MODEL', 'icon_seamless')).strip(),
            conditions_past_hours=int(os.environ.get('CONDITIONS_PAST_HOURS', '3')),
            conditions_forecast_hours=int(os.environ.get('CONDITIONS_FORECAST_HOURS', '48')),
            # Temperature is hourly and slow-moving; no need to refetch it on
            # every 5-minute precipitation cycle.
            conditions_refresh=int(os.environ.get('CONDITIONS_REFRESH_MINUTES', '30')) * 60,
            # "tell me when it is about to rain at X", as
            # name:[metric@]threshold:lead_minutes[:probability[:quiet_minutes]],
            # semicolons between rules. Delivered to one webhook, which is what
            # ntfy, Gotify, Home Assistant and a shell script all accept.
            alert_rules=alert_rules,
            alert_webhook=os.environ.get('ALERT_WEBHOOK_URL', '').strip(),
            alert_format=alert_format,
            # For an ntfy access token: "Bearer tk_...".
            alert_auth=os.environ.get('ALERT_WEBHOOK_AUTH', '').strip(),
            # Lives beside the frames so it survives a restart on the same volume.
            alert_state_file=os.environ.get(
                'ALERT_STATE_FILE',
                os.path.join(os.environ.get('FRAME_DIR', '/data'), 'alerts.json'),
            ),
            # How long the forecast may stop advancing before that is worth an
            # alert of its own. Wants only ALERT_WEBHOOK_URL: this reports the
            # pipeline, not the weather, so it is useful to someone who
            # configured no rain rules at all. 0 disables it.
            stall_alert=int(os.environ.get('STALL_ALERT_SECONDS', '1800')),
            # Its own webhook rather than the rain rules'. A stall is a
            # different kind of news — it is about the pipeline, not the
            # weather — and it usually wants a different ntfy topic, sometimes
            # a different service, and often a reader who set no rain rules at
            # all. Each of the three falls back to its ALERT_* equivalent, so a
            # deployment that wants one webhook for both still gets it by
            # setting only those.
            stall_webhook=(os.environ.get('STALL_WEBHOOK_URL', '').strip()
                           or os.environ.get('ALERT_WEBHOOK_URL', '').strip()),
            stall_format=stall_format,
            stall_auth=(os.environ.get('STALL_WEBHOOK_AUTH', '').strip()
                        or os.environ.get('ALERT_WEBHOOK_AUTH', '').strip()),
        )
