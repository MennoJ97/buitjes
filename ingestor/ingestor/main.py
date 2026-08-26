"""Poll KNMI, turn each published cycle into frames, publish a manifest.

Two sources share one timeline and one output grid:

* the seamless ensemble forecast, +5 min to +6 h (``nowcast`` / ``forecast``)
* the real-time radar composite for the recent past (``observed``)

The forecast defines the output grid, so observed frames can only be produced
once a forecast cycle has been seen. Frames from a different grid are discarded
rather than trusted, since the frontend stretches one canvas over one set of
corner coordinates.

The manifest is the contract with the Rust server, the web frontend and the
homepage widget. It is written last and atomically, so a reader either sees the
previous complete cycle or the new one, never a half-written mixture.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import tempfile
import time

from .blend import BlendFile, reduce_members
from .config import Config
from .encode import encode_frame
from .knmi import KnmiClient, RateLimited
from .points import PERCENTILES, PointExtractor, current_conditions, summarise
from .radar import RadarFile, valid_time_from_filename
from .alerts import AlertRunner, StallWatch, describe as describe_alert
from .conditions import ATTRIBUTION as CONDITIONS_ATTRIBUTION, ConditionsSource
from .raster import MercatorResampler, StereographicResampler, TargetGrid

log = logging.getLogger('ingestor')

MANIFEST_NAME = 'manifest.json'
GRID_NAME = 'grid.json'
FORECAST_PATTERN = re.compile(r'^p_(\d+)_(\d+)\.webp$')
OBSERVED_PATTERN = re.compile(r'^o_(\d+)\.webp$')

#: Keep observed frames a little past the display window so a browser still
#: loading the previous manifest does not start hitting 404s.
OBSERVED_GRACE_SECONDS = 900


def forecast_frame_name(reference_time: int, valid_time: int) -> str:
    # The reference time is part of the name so a re-forecast of the same valid
    # time gets a new URL: frames are served with immutable caching.
    return f'p_{reference_time}_{valid_time}.webp'


def observed_frame_name(valid_time: int) -> str:
    # A measurement of a past instant does not change, so the valid time alone
    # identifies it - and the browser keeps it cached across forecast cycles.
    return f'o_{valid_time}.webp'


def write_atomic(directory: str, name: str, payload: bytes) -> None:
    handle, temporary = tempfile.mkstemp(dir=directory, prefix='.tmp-')
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(payload)
        os.replace(temporary, os.path.join(directory, name))
    except BaseException:
        os.unlink(temporary)
        raise


# ------------------------------------------------------------------ forecast


def build_forecast(path: str, config: Config, conditions=None, alert_runner=None):
    """Decode one forecast cycle into frames. Returns (frames, meta, grid)."""
    with BlendFile(path) as source:
        resampler = MercatorResampler(
            source.lat, source.lon, config.crop_bounds, config.output_height
        )
        log.info(
            'forecast %s: %d steps, %d members, output %dx%d',
            source.reference_time, len(source), source.member_count,
            resampler.width, resampler.height,
        )

        extractor = PointExtractor(config.widget_locations, source.lat, source.lon)

        nowcast_until = source.reference_time + config.nowcast_minutes * 60
        frames = []
        written = 0
        for index, valid_time in enumerate(source.valid_times):
            # One read serves both outputs: the reduced field for the map and,
            # while the members are still in memory, the point samples.
            members = source.members(index)
            extractor.observe(valid_time, members)
            field = resampler(reduce_members(members, config.ensemble_stat))
            payload = encode_frame(field, config.max_precip)
            name = forecast_frame_name(source.reference_time, valid_time)
            write_atomic(config.frame_dir, name, payload)
            written += len(payload)
            frames.append({
                't': valid_time,
                'kind': 'nowcast' if valid_time <= nowcast_until else 'forecast',
                'file': name,
            })

        log.info('forecast %s: wrote %d frames, %.1f MiB',
                 source.reference_time, len(frames), written / 1024 / 1024)

        publish_points(config, source, extractor, conditions, alert_runner)

        meta = {
            'reference_time': source.reference_time,
            'product': f'{config.ensemble_stat} of {source.member_count} ensemble members',
            # Coordinates too: a client that knows where the user clicked can
            # then pick the nearest published location without another request.
            'points': [
                {'name': location.name, 'lat': location.lat, 'lon': location.lon}
                for location in extractor.locations
            ],
        }
        return frames, meta, resampler.target


def point_file_name(name: str) -> str:
    return f'point_{name}.json'


def current_file_name(name: str) -> str:
    return f'current_{name}.json'


def publish_points(config: Config, source, extractor: PointExtractor,
                   conditions=None, alert_runner=None) -> None:
    """Write one point forecast per configured location, and act on alert rules."""
    if not extractor:
        return

    for index, location in enumerate(extractor.locations):
        series = extractor.series_for(index)
        document = {
            'generated_at': int(time.time()),
            'reference_time': source.reference_time,
            'location': {'name': location.name, 'lat': location.lat, 'lon': location.lon},
            'precipitation': {
                'unit': 'mm/h',
                'members': source.member_count,
                'percentiles': list(PERCENTILES),
                'series': series,
            },
            'summary': summarise(series, source.reference_time),
            'source': {
                'dataset': config.dataset,
                'version': config.version,
                'attribution': 'KNMI (CC BY 4.0)',
            },
        }

        blocks = conditions.for_location(location) if conditions else None
        if blocks:
            blocks = dict(blocks)
            outlook = blocks.get('precipitation_outlook')
            if outlook and series:
                # KNMI is better inside its own horizon, so the outlook only
                # covers what lies beyond it.
                knmi_ends = series[-1]['t']
                outlook['series'] = [
                    entry for entry in outlook['series'] if entry['t'] > knmi_ends
                ]
                if not outlook['series']:
                    blocks.pop('precipitation_outlook')
            document.update(blocks)
            document['conditions_source'] = {
                'model': conditions.model,
                'step': 'hourly',
                'percentiles': list(PERCENTILES),
                'attribution': CONDITIONS_ATTRIBUTION,
            }
        write_atomic(
            config.frame_dir, point_file_name(location.name), json.dumps(document).encode()
        )
        write_atomic(
            config.frame_dir, current_file_name(location.name),
            json.dumps(current_conditions(document)).encode(),
        )

        # After the write, not before: an alert that arrives while the forecast
        # it describes is still half-published sends the reader to a page that
        # disagrees with it.
        if alert_runner is not None:
            for event in alert_runner.consider(document):
                title, body = describe_alert(event, time.time())
                log.info('alert sent: %s — %s', title, body)

    if alert_runner is not None:
        alert_runner.save()

    log.info('point forecasts published for %s',
             ', '.join(location.name for location in extractor.locations))


# ------------------------------------------------------------------ observed


def wanted_observed_times(reference_time: int, config: Config) -> set[int]:
    """The 5-minute stamps the history window should contain."""
    if config.history_minutes <= 0:
        return set()
    latest = reference_time - (reference_time % 300)
    count = config.history_minutes // 5
    return {latest - step * 300 for step in range(count + 1)}


def existing_observed_times(config: Config) -> set[int]:
    found = set()
    for entry in os.listdir(config.frame_dir):
        match = OBSERVED_PATTERN.match(entry)
        if match:
            found.add(int(match.group(1)))
    return found


def fetch_observed(client: KnmiClient, config: Config, grid: TargetGrid,
                   reference_time: int):
    """Download and convert missing observed frames.

    Returns ``(added, still_missing)``, where ``still_missing`` counts only
    frames KNMI actually has. A stamp the platform never published — a radar
    outage, say — must not count as missing, or the loop would treat history as
    permanently incomplete and keep polling at the impatient interval forever.

    Only a few are fetched per cycle: each one costs an API call to resolve its
    download URL, and KNMI's rate limiting is strict enough that a dozen in a row
    reliably trips it. History therefore fills in over the first few cycles.
    """
    if config.history_minutes <= 0:
        return 0, 0

    missing = wanted_observed_times(reference_time, config) - existing_observed_times(config)
    if not missing:
        return 0, 0

    available = client.newest_filenames(
        config.observed_dataset, config.observed_version, config.history_minutes // 5 + 2
    )
    by_time = {
        valid_time_from_filename(name): name
        for name in available
        if valid_time_from_filename(name) is not None
    }
    candidates = sorted(
        ((t, name) for t, name in by_time.items() if t in missing),
        reverse=True,  # newest first: the most useful history
    )

    added = 0
    resampler = None
    for valid_time, filename in candidates[: config.observed_max_fetch]:
        with tempfile.TemporaryDirectory() as scratch:
            downloaded = client.download(
                config.observed_dataset, config.observed_version,
                filename, os.path.join(scratch, filename),
            )
            with RadarFile(downloaded) as source:
                if resampler is None:
                    resampler = StereographicResampler(
                        grid, source.proj4, source.corners,
                        source.columns, source.rows, source.pixel_size,
                    )
                    log.info('observed: radar covers %.0f%% of the output grid',
                             resampler.coverage * 100)
                rate, valid = source.rate_and_validity()
                field, measured = resampler(rate, valid)
                payload = encode_frame(field, config.max_precip, measured)
                write_atomic(config.frame_dir, observed_frame_name(source.valid_time), payload)
                added += 1

    still_missing = len(candidates) - added
    unavailable = len(missing) - len(candidates)
    log.info('observed: added %d frames (%d still to fetch%s)', added, still_missing,
             f', {unavailable} not published by KNMI' if unavailable else '')
    return added, still_missing


def observed_frames(config: Config, reference_time: int):
    """Manifest entries for the observed frames currently on disk."""
    wanted = wanted_observed_times(reference_time, config)
    present = sorted(t for t in existing_observed_times(config) if t in wanted)
    return [{'t': t, 'kind': 'observed', 'file': observed_frame_name(t)} for t in present]


# ------------------------------------------------------------------ retention


def prune(config: Config, keep_references: set[int], reference_time: int) -> None:
    oldest_observed = reference_time - config.history_minutes * 60 - OBSERVED_GRACE_SECONDS
    removed = 0
    for entry in os.listdir(config.frame_dir):
        forecast = FORECAST_PATTERN.match(entry)
        observed = OBSERVED_PATTERN.match(entry)
        stale = (
            (forecast and int(forecast.group(1)) not in keep_references)
            or (observed and int(observed.group(1)) < oldest_observed)
        )
        if stale:
            try:
                os.unlink(os.path.join(config.frame_dir, entry))
                removed += 1
            except FileNotFoundError:
                pass
    if removed:
        log.info('pruned %d stale frames', removed)


def known_reference_times(config: Config) -> set[int]:
    found = set()
    for entry in os.listdir(config.frame_dir):
        match = FORECAST_PATTERN.match(entry)
        if match:
            found.add(int(match.group(1)))
    return found


def reconcile_grid(config: Config, grid: TargetGrid) -> None:
    """Drop observed frames if the output grid has changed under them.

    They are resampled onto the forecast's grid, so a changed domain would leave
    them silently misaligned rather than merely stale.
    """
    path = os.path.join(config.frame_dir, GRID_NAME)
    signature = grid.signature()
    try:
        with open(path) as handle:
            previous = json.load(handle).get('signature')
    except (OSError, ValueError):
        previous = None

    if previous is not None and previous != signature:
        log.warning('output grid changed (%s -> %s); discarding observed frames',
                    previous, signature)
        for entry in os.listdir(config.frame_dir):
            if OBSERVED_PATTERN.match(entry):
                os.unlink(os.path.join(config.frame_dir, entry))

    if previous != signature:
        write_atomic(config.frame_dir, GRID_NAME, json.dumps({'signature': signature}).encode())


# ------------------------------------------------------------------ loop


def publish(config: Config, grid: TargetGrid, meta: dict, frames: list) -> None:
    frames = sorted(frames, key=lambda frame: frame['t'])
    manifest = {
        'generated_at': int(time.time()),
        'reference_time': meta['reference_time'],
        'bounds': grid.bounds,
        'width': grid.width,
        'height': grid.height,
        'max_precip_mm_h': config.max_precip,
        'source': {
            'dataset': config.dataset,
            'version': config.version,
            'product': meta['product'],
            'observed': config.observed_dataset if config.history_minutes > 0 else None,
            'attribution': 'KNMI (CC BY 4.0)',
        },
        'points': meta.get('points', []),
        'frames': frames,
    }
    write_atomic(config.frame_dir, MANIFEST_NAME, json.dumps(manifest).encode())


class State:
    def __init__(self):
        self.last_forecast_file: str | None = None
        self.forecast_frames: list = []
        self.meta: dict | None = None
        self.grid: TargetGrid | None = None
        self.published: tuple | None = None
        self.backfilling: bool = False


def seconds_until_next_publication(config: Config, state: State, now: float) -> float:
    """How long to wait before it is worth asking KNMI for a new file.

    KNMI considers repeated blind polling abuse of the platform, so instead of a
    fixed interval this waits for the moment the next cycle is actually due:
    one source interval after the last one we ingested, plus the lag between a
    cycle's nominal time and its file appearing. Steady state is therefore one
    request per published file rather than several per file.
    """
    if state.meta is None:
        return 0.0  # nothing ingested yet; look immediately
    if state.backfilling:
        # History is still filling in, and that needs requests of its own.
        return config.poll_retry

    due = state.meta['reference_time'] + config.source_interval + config.publish_lag
    remaining = due - now
    # Already past due means the file is late; ask again politely rather than
    # hammering, since it will appear when it appears.
    return remaining if remaining > 0 else config.poll_retry


def run_once(client: KnmiClient, config: Config, state: State, conditions=None,
             check_forecast: bool = True, alert_runner=None, stall=None) -> None:
    # A radar-only notification means the forecast cannot have moved, so looking
    # it up would spend a request to learn nothing.
    if not check_forecast and state.grid is not None:
        return update_observed(client, config, state)

    filename = client.latest_filename(config.dataset, config.version)
    if not filename:
        log.warning('dataset %s has no files', config.dataset)
        return

    if filename != state.last_forecast_file:
        log.info('new forecast cycle: %s', filename)
        with tempfile.TemporaryDirectory() as scratch:
            downloaded = client.download(
                config.dataset, config.version, filename, os.path.join(scratch, filename)
            )
            frames, meta, grid = build_forecast(downloaded, config, conditions, alert_runner)
        state.last_forecast_file = filename
        state.forecast_frames, state.meta, state.grid = frames, meta, grid
        reconcile_grid(config, grid)
        # The transition the stall watch counts from, and deliberately down
        # here: a cycle that KNMI announced but that failed to download is not
        # progress, and marking it as such would keep resetting the clock
        # through exactly the failure worth being told about. Equally
        # deliberately not a notification — the subscription covers the radar
        # topic too, so it keeps ticking through a gap in *forecast*
        # publishing, which is the outage that started all this.
        if stall:
            stall.cycle(time.time())
    elif state.grid is None:
        return  # nothing published yet and no new cycle to build one from

    update_observed(client, config, state)


def update_observed(client: KnmiClient, config: Config, state: State) -> None:
    """Top up observed history and republish if the frame list changed."""
    reference_time = state.meta['reference_time']
    _, still_missing = fetch_observed(client, config, state.grid, reference_time)
    state.backfilling = still_missing > 0

    observed = observed_frames(config, reference_time)
    # Only rewrite the manifest when the frame list actually changed, so a
    # quiet poll leaves the published state untouched.
    fingerprint = (reference_time, tuple(frame['file'] for frame in observed))
    if fingerprint != state.published:
        publish(config, state.grid, state.meta, observed + state.forecast_frames)
        state.published = fingerprint
        log.info('published manifest: %d observed + %d forecast frames',
                 len(observed), len(state.forecast_frames))

    keep = sorted(known_reference_times(config), reverse=True)[: config.keep_cycles]
    prune(config, set(keep), reference_time)


def build_listener(config: Config):
    """The notification listener, or None when no notification key is set."""
    if not config.notification_api_key:
        return None
    try:
        from .notifications import NotificationListener, TOPIC_TEMPLATE, stable_client_id

        topics = [TOPIC_TEMPLATE.format(dataset=config.dataset, version=config.version)]
        if config.history_minutes > 0:
            topics.append(TOPIC_TEMPLATE.format(
                dataset=config.observed_dataset, version=config.observed_version))

        listener = NotificationListener(
            config.notification_api_key, topics,
            stable_client_id(config.frame_dir),
            config.notification_host, config.notification_port,
        )
        listener.start()
        return listener
    except Exception:
        log.exception('could not start the notification service; falling back to polling')
        return None


def main() -> None:
    logging.basicConfig(
        level=os.environ.get('LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)-7s %(name)s %(message)s',
    )
    config = Config.from_env()
    os.makedirs(config.frame_dir, exist_ok=True)
    client = KnmiClient(config.api_key)
    listener = build_listener(config)
    conditions = ConditionsSource(
        config.conditions_model, config.conditions_past_hours,
        config.conditions_forecast_hours, config.conditions_refresh,
    ) if config.widget_locations else None

    log.info('ingesting %s v%s into %s (history %d min from %s); discovery: %s',
             config.dataset, config.version, config.frame_dir,
             config.history_minutes, config.observed_dataset,
             'notification service' if listener else 'scheduled polling')
    if config.widget_locations:
        log.info('point forecasts for %s; conditions: %s',
                 ', '.join(l.name for l in config.widget_locations),
                 config.conditions_model or 'disabled')

    alert_runner = AlertRunner.from_config(config)
    if alert_runner:
        log.info('alert rules: %s; delivery: %s',
                 ', '.join(rule.key for rule in config.alert_rules),
                 f'{config.alert_format} webhook' if config.alert_webhook else 'log only')

    stall = StallWatch.from_config(config)
    if stall:
        log.info('stall alert after %ds without a new forecast cycle', stall.threshold)

    state = State()
    if stall:
        # Start the clock at boot, so a process that never manages to ingest
        # anything reports that too, rather than waiting forever in silence.
        stall.cycle(time.time())
    continue_kwargs = {'check_forecast': True}
    while True:
        try:
            run_once(client, config, state, conditions,
                     alert_runner=alert_runner, stall=stall, **continue_kwargs)
            continue_kwargs = {'check_forecast': True}
            delay = seconds_until_next_publication(config, state, time.time())
        except RateLimited as exc:
            delay = max(config.poll_retry, exc.retry_after)
            log.warning('%s', exc)
        except Exception:
            log.exception('ingest cycle failed')
            delay = config.poll_retry

        # Outside the try, and before the wait: a stall has to be noticed on
        # both the notification and the polling path, and on the cycles that
        # raised as much as the ones that came back empty.
        if stall:
            stall.check(time.time())

        # With notifications there is nothing to wait *for* on a timer: sit on
        # the subscription until KNMI announces a file. The timeout is only a
        # safety net against a connection that died without saying so.
        if listener is not None and not state.backfilling:
            announced = listener.wait(config.notification_idle_timeout)
            if not announced:
                log.warning('no notification for %ds; running a cycle anyway',
                            config.notification_idle_timeout)
                check_forecast = True
            else:
                check_forecast = config.dataset in announced
            continue_kwargs['check_forecast'] = check_forecast
            continue

        # Jitter so a restart loop does not resynchronise onto the same second.
        time.sleep(delay + random.uniform(0, 5))


if __name__ == '__main__':
    main()
