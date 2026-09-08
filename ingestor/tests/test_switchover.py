"""Tests for noticing the primary has stopped, and standing in for it.

Run inside the ingestor image, which has no pytest:

    docker compose run --rm --entrypoint python ingestor -m tests.test_switchover

Three separate things have to hold for the stand-in to be worth having, and they
fail in different ways:

* **Noticing.** The forecast subscription can go quiet while the connection is
  healthy, because the radar topic on the same connection heartbeats every five
  minutes. That is what happened on 2026-09-07, and the per-connection idle
  timeout could not see it: `announced` was never empty, so the forecast was
  never looked at again. The clock has to be per-dataset or it is decorative.

* **Deciding.** Once the stand-in is on air the published cycle is fresh, so
  anything that measures staleness off the *manifest* will immediately conclude
  the primary is fine and flap back. The primary's own clock has to be tracked
  apart from what is being published.

* **Switching.** Going out and coming back both have to be idempotent: a cycle
  that rebuilds the same stand-in from the same two files must not republish,
  and a primary that returns has to leave no state behind that would stop the
  next outage being noticed.

The splice itself is tested against a real reprojection rather than a stubbed
one, because the half of it worth doubting is whether the radar lands on the
model's grid at all - the two products are in different projections, and getting
that wrong puts rain in the North Sea rather than raising anything.
"""

import os
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from types import SimpleNamespace  # noqa: E402

from ingestor import main as m  # noqa: E402
from ingestor.blend import pooled_axis  # noqa: E402
from ingestor.fallback import SplicedSource, refined_axis  # noqa: E402
from ingestor.raster import MercatorResampler, TargetGrid  # noqa: E402

#: 5-minute aligned, as every reference time from KNMI is.
REF = 1_788_500_400
IDLE = 900
PRIMARY = 'seamless_precipitation_ensemble_forecast_members'
RADAR = 'nl_rdr_data_rtcor_5m'

#: The real RTCOR projection and domain, so the reprojection under test is the
#: one that actually runs rather than a flat approximation of it.
RADAR_PROJ4 = ('+proj=stere +lat_0=90 +lon_0=0 +lat_ts=60 +a=6378137 +b=6356752 '
               '+x_0=0 +y_0=0 +units=km')
#: SW, NW, NE, SE lon/lat pairs, as KNMI stores them.
RADAR_CORNERS = np.array([[0.0, 49.362064], [0.0, 55.973602],
                          [10.856453, 55.389141], [9.0093, 48.8953]])

failures = []


def check(name, condition, detail=''):
    if condition:
        print(f'  ok   {name}')
    else:
        print(f'  FAIL {name} {detail}')
        failures.append(name)


def config(frame_dir='/tmp', fallback_after=1800, idle=IDLE):
    """Only the fields the functions under test actually read."""
    return SimpleNamespace(
        dataset=PRIMARY, version='1.0', frame_dir=frame_dir,
        notification_idle_timeout=idle, fallback_after=fallback_after,
        fallback_nowcast_dataset='radar_forecast', fallback_nowcast_version='2.0',
        fallback_model_dataset='uwcw-ha-det-nl-s1', fallback_model_version='1.0',
        fallback_model_parameter='total-precipitation-rate-gl',
        fallback_horizon_minutes=360, fallback_refine=0,
    )


def state(**overrides):
    st = m.State()
    for key, value in overrides.items():
        setattr(st, key, value)
    return st


# ------------------------------------------------------- noticing the silence

print('noticing that the forecast topic has gone quiet')

cfg = config()

# The failure of 2026-09-07 in one line: the radar topic is the only one
# talking, and while the clock is young that correctly costs no request.
due, clock = m.forecast_check_due(cfg, {RADAR}, quiet_since=1000.0, now=1000.0 + 300)
check('a radar-only announcement leaves the forecast alone at first',
      due is False and clock == 1000.0)

# ...and once the clock runs out it is polled anyway, which is the whole point:
# the connection is demonstrably alive, so the per-connection timeout never
# fires and this is the only thing that can tell "KNMI is quiet" from
# "we stopped hearing KNMI".
due, clock = m.forecast_check_due(cfg, {RADAR}, quiet_since=1000.0, now=1000.0 + IDLE)
check('a radar-only announcement forces a poll once the dataset clock expires',
      due is True and clock == 1000.0 + IDLE)

# The regression guard proper: any number of radar heartbeats, none of which
# reset the forecast's clock, must still end in a poll.
clock = 1000.0
now = 1000.0
polled = False
for _ in range(20):                      # 20 heartbeats at 5 minutes = 100 min
    now += 300
    due, clock = m.forecast_check_due(cfg, {RADAR}, clock, now)
    polled = polled or due
check('a steady radar heartbeat cannot hold the forecast clock open for ever',
      polled)

due, clock = m.forecast_check_due(cfg, {PRIMARY, RADAR}, quiet_since=1000.0, now=1500.0)
check('an announcement naming the forecast checks it and resets the clock',
      due is True and clock == 1500.0)

due, clock = m.forecast_check_due(cfg, set(), quiet_since=1000.0, now=1200.0)
check('an empty announcement still runs a cycle, as the connection may be dead',
      due is True and clock == 1200.0)

# Reset on every check, not only on an announcement: otherwise a dataset that
# stays quiet is polled on every pass round the loop rather than once a timeout.
_, clock = m.forecast_check_due(cfg, {RADAR}, quiet_since=1000.0, now=1000.0 + IDLE)
due, _ = m.forecast_check_due(cfg, {RADAR}, clock, now=1000.0 + IDLE + 300)
check('polling resets the clock, so a quiet dataset costs one request per timeout',
      due is False)

check('the clock is disabled with the rest when the timeout is not reached',
      m.forecast_check_due(cfg, {RADAR}, 1000.0, 1000.0 + IDLE - 1)[0] is False)


# ------------------------------------------------- deciding to stand in for it

print()
print('deciding the primary has stopped')

now = REF + 10_000

check('a fallback switched off in config never triggers',
      m.primary_is_stale(config(fallback_after=0), state(primary_reference=REF), now)
      is False)

check('a primary that advanced recently is not stale',
      m.primary_is_stale(cfg, state(primary_reference=now - 600), now) is False)

check('a primary quiet for longer than the threshold is stale',
      m.primary_is_stale(cfg, state(primary_reference=now - 3600), now) is True)

# The one the design turns on. In fallback mode `meta` describes the stand-in
# and is always fresh, so a staleness test that read the manifest would say the
# primary had recovered and flap straight back to it every cycle.
fresh_meta = {'reference_time': now, 'dataset': 'radar_forecast + uwcw-ha-det-nl-s1'}
check('a fresh stand-in does not make the primary look recovered',
      m.primary_is_stale(cfg, state(primary_reference=now - 3600, meta=fresh_meta,
                                    fallback_active=True), now) is True)

# Cold start: nothing ingested and nothing to compare against. Standing in
# immediately would mean a container restarted during a healthy minute
# publishes the stand-in before it has looked at the real product once.
young = state(started_at=now - 60)
check('a cold start waits before standing in for a primary it has never seen',
      m.primary_is_stale(cfg, young, now) is False)

old = state(started_at=now - 3600)
check('a process up for a while with nothing ingested does stand in',
      m.primary_is_stale(cfg, old, now) is True)

# Having ingested something, the started_at path is irrelevant: the primary's
# own stamp is the better answer and must win.
seen = state(started_at=now - 3600, primary_reference=now - 60,
             meta={'reference_time': now - 60})
check('a primary seen recently beats a long uptime',
      m.primary_is_stale(cfg, seen, now) is False)


# ---------------------------------------------------------------- the splice

print()
print('splicing radar onto the model grid')


class FakeRadar:
    """A RAD_NL25_RAC_FM stand-in: real geometry, sentinel values."""

    proj4 = RADAR_PROJ4
    corners = RADAR_CORNERS
    columns = 700
    rows = 765
    #: y is negative because the rows run north to south, which is KNMI's own
    #: convention and what StereographicResampler cross-checks against.
    pixel_size = (1.0, -1.0)

    def __init__(self, reference_time=REF, steps=25, value=7.0):
        self.reference_time = reference_time
        self.valid_times = [reference_time + step * 300 for step in range(steps)]
        self._value = value
        self.read = []

    def rate_and_validity(self, index):
        self.read.append(index)
        rate = np.full((self.rows, self.columns), self._value, dtype=np.float32)
        return rate, np.ones((self.rows, self.columns), dtype=bool)


class FakeModel:
    """A HARMONIE parameter file stand-in on a small regular lat/lon grid."""

    def __init__(self, reference_time=REF, hours=8, value=3.0, size=48):
        self.reference_time = reference_time
        self.lat = np.linspace(50.0, 54.0, size)
        self.lon = np.linspace(3.0, 8.0, size)
        # Hourly and *not* aligned to the radar's T+0, as the real pair are not.
        self.valid_times = [reference_time + hour * 3600 for hour in range(1, hours + 1)]
        self._value = value
        self.size = size
        self.read = []

    def field(self, index):
        self.read.append(index)
        return np.full((self.size, self.size), self._value, dtype=np.float32)


radar, model = FakeRadar(), FakeModel()
spliced = SplicedSource(radar, model, horizon_minutes=360)

check('the splice takes the model grid, which is the one that is lat/lon',
      spliced.lat.shape == model.lat.shape and spliced.lon.shape == model.lon.shape)
check('and the radar run time, which is the fresher of the two',
      spliced.reference_time == radar.reference_time)
check('it presents itself as a single deterministic member',
      spliced.member_count == 1)
check('and says so in words the manifest can publish',
      'radar' in spliced.product_label and spliced.reducer_label != '')

check('the timeline is sorted and has no repeated stamps',
      spliced.valid_times == sorted(spliced.valid_times)
      and len(set(spliced.valid_times)) == len(spliced.valid_times))
check('len() agrees with the stamps, as every consumer assumes',
      len(spliced) == len(spliced.valid_times))

radar_end = max(radar.valid_times)
check('every model step used is beyond where the radar stops',
      all(t > radar_end for t in spliced.valid_times if t not in radar.valid_times))
check('and the radar owns everything up to its own horizon',
      [t for t in spliced.valid_times if t <= radar_end] == radar.valid_times)

# Which half answered is checked by value, not by index arithmetic: the whole
# risk in the splice is a step served from the wrong file.
first = spliced.members(0)
last = spliced.members(len(spliced) - 1)
check('a step inside the nowcast window is served by the radar',
      first.shape == (1, model.size, model.size) and np.isclose(first.max(), 7.0))
check('a step past it is served by the model',
      np.isclose(last.max(), 3.0))
check('and the radar really was reprojected onto the model grid',
      first.shape[1:] == (model.size, model.size) and float(first.min()) >= 0.0)

# A horizon shorter than the radar's own reach has to cut the radar too, or the
# setting would only ever mean "how much model to add".
short = SplicedSource(FakeRadar(), FakeModel(), horizon_minutes=60)
check('a short horizon truncates the radar half as well as the model half',
      max(short.valid_times) <= REF + 60 * 60)
check('and a horizon inside the radar window drops the model entirely',
      all(t in FakeRadar().valid_times for t in short.valid_times))

# Reading a step must not read the other file: the members() call is the
# expensive part of a cycle and doing both would double it.
probe_radar, probe_model = FakeRadar(), FakeModel()
probe = SplicedSource(probe_radar, probe_model, horizon_minutes=360)
probe.members(0)
check('one step reads one file', len(probe_radar.read) == 1 and not probe_model.read)


# ------------------------------------------------------ switching, and back

print()
print('switching to the stand-in and back')

GRID = TargetGrid(west=3.0, east=8.0, south=50.0, north=54.0, width=100, height=80)
OTHER_GRID = TargetGrid(west=3.0, east=8.0, south=50.0, north=54.0, width=50, height=40)


class FakeClient:
    def __init__(self, primary=None, files=None):
        self.primary = primary
        self.files = files or []
        self.downloads = []

    def latest_filename(self, dataset, version):
        return self.primary if dataset == PRIMARY else 'newest.h5'

    def newest_filenames(self, dataset, version, count=1):
        return self.files[:count]

    def download(self, dataset, version, filename, destination):
        self.downloads.append(filename)
        open(destination, 'wb').close()
        return destination


class FakeStall:
    def __init__(self):
        self.cycles = 0

    def cycle(self, now):
        self.cycles += 1


def run(client, cfg, st, stall=None, primary_meta=None, fallback=None):
    """run_once with the two builders and the observed top-up stubbed out."""
    built_primary = ([{'t': REF}], primary_meta or {'reference_time': REF}, GRID)
    observed = []
    original = (m.build_forecast, m.build_fallback, m.update_observed)
    m.build_forecast = lambda *a, **k: built_primary
    m.build_fallback = lambda *a, **k: fallback
    m.update_observed = lambda *a, **k: observed.append(True)
    try:
        m.run_once(client, cfg, st, stall=stall)
    finally:
        m.build_forecast, m.build_fallback, m.update_observed = original
    return observed


with tempfile.TemporaryDirectory() as frame_dir:
    cfg = config(frame_dir=frame_dir)
    now = time.time()

    # A primary that advances is ingested, and nothing about the stand-in is
    # touched — including the clock the next outage will be measured against.
    st = state()
    stall = FakeStall()
    run(FakeClient(primary='cycle_a.nc'), cfg, st, stall=stall,
        primary_meta={'reference_time': int(now)})
    check('a primary cycle is ingested and dates the primary clock',
          st.last_forecast_file == 'cycle_a.nc' and st.primary_reference == int(now)
          and st.fallback_active is False)
    check('and it counts as progress for the stall watch', stall.cycles == 1)

    # The same file again, with the primary long stale: the stand-in takes over.
    built = ([{'t': REF}], {'reference_time': int(now), 'dataset': 'radar + harmonie'},
             OTHER_GRID, 'radarA+modelA')
    st = state(last_forecast_file='cycle_a.nc', primary_reference=int(now) - 3600,
               grid=GRID, meta={'reference_time': int(now) - 3600})
    stall = FakeStall()
    run(FakeClient(primary='cycle_a.nc'), cfg, st, stall=stall, fallback=built)
    check('a stale primary hands over to the stand-in',
          st.fallback_active is True and st.last_fallback_run == 'radarA+modelA'
          and st.grid is OTHER_GRID)
    check('the stand-in counts as progress too, so the stall alert stops shouting',
          stall.cycles == 1)
    check('and the primary clock is left where it was, not refreshed by the stand-in',
          st.primary_reference == int(now) - 3600)

    # Same two source files next time round: nothing to republish.
    stall = FakeStall()
    st.meta = {'reference_time': int(now)}
    run(FakeClient(primary='cycle_a.nc'), cfg, st, stall=stall, fallback=built)
    check('rebuilding the same stand-in from the same files republishes nothing',
          stall.cycles == 0 and st.last_fallback_run == 'radarA+modelA')

    # New radar file: it does republish.
    moved = ([{'t': REF}], {'reference_time': int(now), 'dataset': 'radar + harmonie'},
             OTHER_GRID, 'radarB+modelA')
    stall = FakeStall()
    run(FakeClient(primary='cycle_a.nc'), cfg, st, stall=stall, fallback=moved)
    check('a newer radar file does republish the stand-in',
          stall.cycles == 1 and st.last_fallback_run == 'radarB+modelA')

    # The primary comes back.
    stall = FakeStall()
    run(FakeClient(primary='cycle_b.nc'), cfg, st, stall=stall,
        primary_meta={'reference_time': int(now) + 300})
    check('a returning primary takes over again',
          st.last_forecast_file == 'cycle_b.nc' and st.fallback_active is False
          and st.grid is GRID)
    check('and forgets the stand-in, so the next outage rebuilds it from scratch',
          st.last_fallback_run is None)
    check('and re-dates the primary clock', st.primary_reference == int(now) + 300)

    # Neither half of the stand-in available: hold what is published rather
    # than half a timeline, and do not record a run that never happened.
    st = state(last_forecast_file='cycle_a.nc', primary_reference=int(now) - 3600,
               grid=GRID, meta={'reference_time': int(now) - 3600})
    stall = FakeStall()
    observed = run(FakeClient(primary='cycle_a.nc'), cfg, st, stall=stall, fallback=None)
    check('a stand-in that cannot be built leaves the published cycle alone',
          st.fallback_active is False and st.last_fallback_run is None
          and st.grid is GRID and stall.cycles == 0)
    check('and the observed history is still topped up meanwhile', observed == [True])

    # The pilot withdrawn outright: latest_filename finds nothing at all. This
    # is the failure the dataset page actually warns about, and bailing on it
    # would make it the one case the stand-in cannot answer.
    st = state(started_at=now - 3600)
    stall = FakeStall()
    run(FakeClient(primary=None), cfg, st, stall=stall, fallback=built)
    check('a withdrawn primary dataset still reaches the stand-in',
          st.fallback_active is True and st.grid is OTHER_GRID)

    # And with the fallback switched off, that same case must not crash or
    # publish: it holds, exactly as it did before any of this existed.
    st = state(started_at=now - 3600)
    run(FakeClient(primary=None), config(frame_dir=frame_dir, fallback_after=0), st)
    check('with the fallback disabled a withdrawn dataset simply holds',
          st.fallback_active is False and st.grid is None)


# ------------------------------------------- matching the primary's raster

print()
print('publishing the stand-in on the primary\'s own grid')

# The blend is 780 rows over the domain the 2 km model covers in 390, so the
# model axis is exactly what pooling the primary's would give. Refining has to
# be the exact inverse of that, or the stand-in lands half a cell off the
# product it stands in for and every switchover shifts the map.
primary_lat = np.linspace(48.991, 56.011, 780)
model_lat = pooled_axis(primary_lat, 2)

check('a pooled axis refines back to the one it came from',
      np.allclose(refined_axis(model_lat, 2), primary_lat))
check('refining preserves the outer edges rather than the centres',
      np.isclose(refined_axis(model_lat, 2)[0], primary_lat[0])
      and np.isclose(refined_axis(model_lat, 2)[-1], primary_lat[-1]))
check('a factor of one leaves the axis alone',
      np.allclose(refined_axis(model_lat, 1), model_lat))
check('and refining then pooling is a round trip at any factor',
      all(np.allclose(pooled_axis(refined_axis(model_lat, f), f), model_lat)
          for f in (1, 2, 3, 4)))

# The assertion the whole feature exists for: same signature, so
# reconcile_grid sees one grid across a switchover and keeps the measured hour.
primary_lon = np.linspace(-0.0145, 11.2955, 780)
model_lon = pooled_axis(primary_lon, 2)
primary_grid = MercatorResampler(primary_lat, primary_lon).target
native = MercatorResampler(model_lat, model_lon).target
refined = MercatorResampler(refined_axis(model_lat, 2), refined_axis(model_lon, 2)).target

check('left alone the stand-in publishes a different grid from the primary',
      native.signature() != primary_grid.signature())
check('refined to the primary\'s row count it publishes the same one',
      refined.signature() == primary_grid.signature(),
      f'{refined.signature()} vs {primary_grid.signature()}')

# Picking the factor.
cfg = config()
check('the factor is worked out from the primary grid when it is known',
      m.fallback_refine(cfg, model_rows=390, target_height=780) == 2)
check('an unknown primary grid publishes at the model resolution',
      m.fallback_refine(cfg, model_rows=390, target_height=None) == 1)
check('a ratio that does not divide is refused rather than rounded',
      m.fallback_refine(cfg, model_rows=390, target_height=1000) == 1)
check('an explicit setting overrides the derivation',
      m.fallback_refine(SimpleNamespace(fallback_refine=3), 390, 780) == 3)

# And the source really does build at the finer size, with the model half
# repeated rather than smoothed.
coarse = SplicedSource(FakeRadar(), FakeModel(), horizon_minutes=360, refine=1)
fine = SplicedSource(FakeRadar(), FakeModel(), horizon_minutes=360, refine=2)
size = FakeModel().size
check('refining doubles the published axes',
      len(fine.lat) == 2 * len(coarse.lat) and len(fine.lon) == 2 * len(coarse.lon))
check('and the frames that come out of it',
      fine.members(0).shape == (1, 2 * size, 2 * size))
model_step = fine.members(len(fine) - 1)
check('the model half is repeated, not interpolated into a gradient',
      float(model_step.min()) == float(model_step.max()) == 3.0)
check('and the timeline is unchanged by the resolution',
      fine.valid_times == coarse.valid_times)


# --------------------------------------------- remembering which grid to hit

print()
print('remembering the primary grid across an outage')

with tempfile.TemporaryDirectory() as frame_dir:
    cfg = config(frame_dir=frame_dir)
    check('nothing is remembered before a primary cycle',
          m.primary_height(cfg) is None)

    m.reconcile_grid(cfg, primary_grid, primary=True)
    check('a primary cycle records its row count', m.primary_height(cfg) == 780)

    # A stand-in that had to publish at its own resolution must not overwrite
    # the target it was trying to hit, or the next switchover forgets it.
    m.reconcile_grid(cfg, native, primary=False)
    check('a stand-in publishing coarser does not clobber the remembered height',
          m.primary_height(cfg) == 780)
    check('and it is still there for the next stand-in to aim at',
          m.fallback_refine(cfg, 390, m.primary_height(cfg)) == 2)

    # A primary on a genuinely new grid does replace it.
    m.reconcile_grid(cfg, native, primary=True)
    check('a primary on a new grid does replace the remembered height',
          m.primary_height(cfg) == native.height)


# --------------------------------------------------- picking the model file

print()
print('picking the one model parameter out of a run')

cfg = config()
run_files = [
    'uwcw-ha-det-nl-2km_20260908T02_wind-speed-hagl.nc',
    'uwcw-ha-det-nl-2km_20260908T02_air-temperature-hagl.nc',
    'uwcw-ha-det-nl-2km_20260908T02_total-precipitation-rate-gl.nc',
    'uwcw-ha-det-nl-2km_20260908T01_total-precipitation-rate-gl.nc',
]
check('the wanted parameter is picked out, not merely the newest file',
      m.latest_model_file(FakeClient(files=run_files), cfg)
      == 'uwcw-ha-det-nl-2km_20260908T02_total-precipitation-rate-gl.nc')

check('a run without that parameter yet reports nothing rather than the wrong file',
      m.latest_model_file(FakeClient(files=run_files[:2]), cfg) is None)

check('an empty dataset is survivable',
      m.latest_model_file(FakeClient(files=[]), cfg) is None)

# The accumulation file shares a prefix with the rate one; matching on a
# substring rather than the whole parameter would take the wrong product.
check('a parameter that merely starts the same is not mistaken for it',
      m.latest_model_file(
          FakeClient(files=['uwcw-ha-det-nl-2km_20260908T02_'
                            'total-precipitation-accumulation-01h-gl.nc']), cfg) is None)


print()
if failures:
    print(f'{len(failures)} check(s) failed: {", ".join(failures)}')
    sys.exit(1)
print('all switchover tests passed')
