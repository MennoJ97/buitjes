"""Standing in for the seamless blend when KNMI stops publishing it.

The primary product, ``seamless_precipitation_ensemble_forecast_members``, is an
experimental pilot: KNMI's own dataset page says it "may undergo changes or be
discontinued at any time without prior notice". On 2026-09-07 it stopped mid
morning during UWC-West HPC maintenance — the blend is seeded with the
HARMONIE-AROME ensemble, and the maintenance took the ensemble away — and then
did not restart when the ensemble came back. Nineteen hours of a site showing
yesterday's rain is a poor answer to a product that is up but not being watched.

So this module rebuilds the same *shape* of forecast out of two datasets that
stayed up throughout, and that KNMI's maintenance notice lists as unaffected:

* ``radar_forecast`` v2.0 (``RAD_NL25_RAC_FM``) — radar extrapolation, 25 steps
  of 5 minutes out to +2 h, ~1 MiB. Polar stereographic, and byte for byte the
  same encoding as the RTCOR observations in :mod:`ingestor.radar`: uint16 with
  ``GEO=0.010000*PV+0.000000``, a 5-minute accumulation in mm. Checked against
  the concurrent RTCOR frame at the same stamp, the two agree to the last digit
  on the domain maximum, so :data:`ingestor.radar.ACCUMULATION_TO_RATE` carries
  over unchanged.
* ``uwcw-ha-det-nl-s1``, parameter ``total-precipitation-rate-gl`` — the NL 2 km
  deterministic HARMONIE run, hourly to +59 h, ~7 MiB, and already in mm/h on a
  regular lat/lon grid. Deliberately *not* ``harmonie_arome_cy43_p1``: that one
  is 862 MiB of GRIB in a tar every hour (p3 is 3.4 GiB, p5 is 17 GiB) and would
  need eccodes in the image, where this is NetCDF4 and therefore h5py.

Spliced at the radar's horizon, the two cover +0 to +6 h — the primary's range,
at the primary's cadence inside the nowcast window. What is lost is the
ensemble: one deterministic member instead of twenty, so no spread band, no
percentiles, and no member probabilities. That is a real downgrade and the
manifest says so rather than dressing a single run up as a reduction of many.

The source classes here present exactly the interface
:class:`ingestor.blend.BlendFile` does — ``lat``, ``lon``, ``reference_time``,
``valid_times``, ``member_count``, ``len()`` and ``members(index)`` — so
``build_from_source`` in :mod:`ingestor.main` consumes them without knowing
which product it was handed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import h5py
import numpy as np

from .radar import ACCUMULATION_TO_RATE, _parse_calibration, _parse_knmi_datetime
from .raster import StereographicResampler

log = logging.getLogger(__name__)

#: RAD_NL25_RAC_FM_<YYYYMMDDHHMM>.h5 — the stamp is the run's T+0.
FORECAST_PATTERN = re.compile(r'^RAD_NL25_RAC_FM_(\d{12})\.h5$')

#: uwcw-ha-det-nl-2km_<YYYYMMDD>T<HH>_<parameter>.nc
MODEL_PATTERN = re.compile(r'^uwcw-ha-det-nl-2km_(\d{8})T(\d{2})_(.+)\.nc$')

#: The parameter file to ask for. Total rather than rainfall so sleet and snow
#: are counted: the map is "how wet is it", not "is it liquid".
MODEL_PARAMETER = 'total-precipitation-rate-gl'

#: What the manifest calls the stand-in. Read by a person in an About dialog, so
#: it names both halves rather than the union of two dataset slugs.
PRODUCT_LABEL = ('deterministic fallback: radar extrapolation to +2 h, '
                 'then HARMONIE 2 km')

#: What to call a single value from it, where the primary would say "median".
REDUCER_LABEL = 'deterministic forecast'


def run_time_from_forecast(filename: str) -> int | None:
    match = FORECAST_PATTERN.match(filename)
    if not match:
        return None
    stamp = datetime.strptime(match.group(1), '%Y%m%d%H%M').replace(tzinfo=timezone.utc)
    return int(stamp.timestamp())


def run_time_from_model(filename: str) -> int | None:
    match = MODEL_PATTERN.match(filename)
    if not match:
        return None
    stamp = datetime.strptime(match.group(1) + match.group(2), '%Y%m%d%H')
    return int(stamp.replace(tzinfo=timezone.utc).timestamp())


def is_model_parameter(filename: str, parameter: str = MODEL_PARAMETER) -> bool:
    match = MODEL_PATTERN.match(filename)
    return bool(match) and match.group(3) == parameter


class _LatLonTarget:
    """Enough of a :class:`~ingestor.raster.TargetGrid` to resample radar onto.

    :class:`~ingestor.raster.StereographicResampler` touches exactly one thing on
    the target it is given — ``lonlat_mesh()`` — because the projection maths runs
    from output coordinates back into the radar's plane. Every Mercator
    assumption in that module lives in ``TargetGrid.lonlat_mesh`` itself, not in
    the resampler, so handing it a mesh built from HARMONIE's own regular axes
    lands the radar on HARMONIE's grid rather than on the display raster.

    That ordering matters: the radar has to be on the *source* grid before
    ``build_from_source`` resamples the spliced product onto the display grid,
    because that second step is what makes rows Mercator-linear. Doing it the
    other way round — reprojecting radar straight onto the display grid — would
    put the two halves of the timeline on axes that disagree by kilometres away
    from the middle of the domain.
    """

    def __init__(self, lat, lon):
        self._lat = np.asarray(lat, dtype=np.float64)
        self._lon = np.asarray(lon, dtype=np.float64)

    def lonlat_mesh(self):
        return np.meshgrid(self._lon, self._lat)


class RadarForecastFile:
    """One ``RAD_NL25_RAC_FM`` run: 25 five-minute steps out to +2 h.

    Same file family as the RTCOR observations, with one difference that matters:
    the lead times are separate ``imageN`` groups rather than one image per file,
    each carrying its own ``image_datetime_valid``. Use as a context manager.
    """

    def __init__(self, path: str):
        self._file = h5py.File(path, 'r')

        geographic = self._file['geographic']
        self.proj4 = _text(geographic['map_projection'].attrs['projection_proj4_params'])
        self.columns = int(geographic.attrs['geo_number_columns'][0])
        self.rows = int(geographic.attrs['geo_number_rows'][0])
        self.corners = np.asarray(geographic.attrs['geo_product_corners'], dtype=np.float64)
        self.pixel_size = (
            float(geographic.attrs['geo_pixel_size_x'][0]),
            float(geographic.attrs['geo_pixel_size_y'][0]),
        )

        # Sorted numerically: 'image10' sorts before 'image2' as a string, and
        # a timeline shuffled into lexical order would be silently wrong rather
        # than loudly broken.
        self._groups = sorted(
            (name for name in self._file if name.startswith('image')),
            key=lambda name: int(name[len('image'):]),
        )
        self.valid_times = [
            _parse_knmi_datetime(_text(np.ravel(
                self._file[name].attrs['image_datetime_valid'])[0]))
            for name in self._groups
        ]
        self.reference_time = self.valid_times[0] if self.valid_times else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._file.close()

    def __len__(self):
        return len(self._groups)

    def rate_and_validity(self, index: int):
        """Rain rate in mm/h at one lead time, plus the measured-pixel mask."""
        group = self._file[self._groups[index]]
        calibration = group['calibration'].attrs
        scale, offset = _parse_calibration(_text(calibration['calibration_formulas']))
        missing = int(np.ravel(calibration['calibration_missing_data'])[0])
        out_of_image = int(np.ravel(calibration['calibration_out_of_image'])[0])

        raw = group['image_data'][:]
        valid = (raw != missing) & (raw != out_of_image)
        rate = (raw.astype(np.float32) * scale + offset) * ACCUMULATION_TO_RATE
        return np.where(valid, rate, 0.0), valid


class HarmonieFile:
    """One ``total-precipitation-rate-gl`` file: hourly steps on a 2 km NL grid.

    NetCDF4, so h5py reads it directly. The variable is
    ``(time, ground_level, lat, lon)`` float32 already in mm/h, which is the unit
    every other part of the pipeline speaks — no calibration, no accumulation
    arithmetic. Use as a context manager.
    """

    def __init__(self, path: str, parameter: str = MODEL_PARAMETER):
        self._file = h5py.File(path, 'r')
        self._variable = self._file[parameter]
        self.lat = self._file['latitude'][:]
        self.lon = self._file['longitude'][:]
        self.valid_times = [int(t) for t in self._file['time'][:]]
        self.reference_time = int(np.ravel(self._file['forecast_reference_time'][()])[0])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._file.close()

    def __len__(self):
        return len(self.valid_times)

    def field(self, index: int):
        """Rain rate in mm/h at one step, with the singleton level squeezed out."""
        values = np.asarray(self._variable[index], dtype=np.float32)
        # (ground_level, lat, lon) -> (lat, lon). Indexed rather than squeezed:
        # squeeze() would also collapse a 1-cell spatial axis, which is a
        # different bug wearing the same shape.
        if values.ndim == 3:
            values = values[0]
        # A model field has no "unmeasured" concept the way radar does, but it
        # does carry NaN outside the domain in some parameters.
        return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)


class SplicedSource:
    """Radar extrapolation for the nowcast window, HARMONIE beyond it.

    Presents :class:`~ingestor.blend.BlendFile`'s interface with one member, so
    ``build_from_source`` treats it exactly like a cycle of the real product.
    Everything downstream that reasons about ensembles then does the right thing
    on its own: ``is_degenerate`` declines to call a one-member product dead, and
    ``reduce_members`` over a single member is the member itself whatever
    ``ENSEMBLE_STAT`` is set to.

    The grid is HARMONIE's, because it is the half that arrives as regular
    lat/lon and so is the half ``MercatorResampler`` can consume. The radar is
    reprojected onto it once, and every radar step reuses that mapping.

    ``reference_time`` is the radar's T+0 rather than the model run's: the model
    is up to an hour old by the time it publishes, and the timeline's "now" is
    the freshest thing in it. HARMONIE steps at or before the radar's horizon are
    dropped rather than averaged in — a splice, not a blend. Doing this properly
    is what pySTEPS is for, and this module is explicitly the cruder stand-in.
    """

    #: What a caller reads instead of guessing from ``member_count``.
    member_count = 1
    product_label = PRODUCT_LABEL
    reducer_label = REDUCER_LABEL

    def __init__(self, radar: RadarForecastFile, model: HarmonieFile,
                 horizon_minutes: int = 360, refine: int = 1):
        self._refine = max(1, int(refine))
        self.lat = refined_axis(model.lat, self._refine)
        self.lon = refined_axis(model.lon, self._refine)
        self.reference_time = radar.reference_time

        resampler = StereographicResampler(
            _LatLonTarget(self.lat, self.lon), radar.proj4, radar.corners,
            radar.columns, radar.rows, radar.pixel_size,
        )
        log.info('fallback: radar covers %.0f%% of the %dx%d output grid%s',
                 resampler.coverage * 100, len(self.lon), len(self.lat),
                 f' ({self._refine}x the model grid, to match the primary)'
                 if self._refine > 1 else '')

        horizon = self.reference_time + horizon_minutes * 60
        radar_ends = max(radar.valid_times) if radar.valid_times else self.reference_time

        # (valid_time, source, index), in time order. Built once so members()
        # is a lookup rather than a search, and so valid_times and the reads
        # can never disagree about which file owns a stamp.
        self._steps = []
        for index, valid_time in enumerate(radar.valid_times):
            if valid_time <= horizon:
                self._steps.append((valid_time, 'radar', index))
        for index, valid_time in enumerate(model.valid_times):
            if radar_ends < valid_time <= horizon:
                self._steps.append((valid_time, 'model', index))
        self._steps.sort(key=lambda step: step[0])

        self.valid_times = [valid_time for valid_time, _, _ in self._steps]
        self._radar = radar
        self._model = model
        self._resampler = resampler

        log.info('fallback: %d radar steps to +%d min, then %d model steps to +%d min',
                 sum(1 for _, kind, _ in self._steps if kind == 'radar'),
                 (radar_ends - self.reference_time) // 60,
                 sum(1 for _, kind, _ in self._steps if kind == 'model'),
                 (self.valid_times[-1] - self.reference_time) // 60
                 if self.valid_times else 0)

    def __len__(self):
        return len(self._steps)

    def members(self, index: int):
        """One step as ``(1, lat, lon)`` in mm/h, whichever half owns it."""
        _, kind, source_index = self._steps[index]
        if kind == 'radar':
            # Straight onto the refined mesh: the radar is 1 km native, so this
            # half is genuinely sharper rather than merely larger.
            rate, valid = self._radar.rate_and_validity(source_index)
            field, _ = self._resampler(rate, valid)
        else:
            field = self._model.field(source_index)
            if self._refine > 1:
                # Nearest-neighbour on purpose. Interpolating would invent a
                # gradient the 2 km model does not have and make the seam
                # between the two halves of the timeline look like a change in
                # the weather rather than a change of source.
                field = np.repeat(np.repeat(field, self._refine, axis=0),
                                  self._refine, axis=1)
        return field[np.newaxis, :, :].astype(np.float32)


def refined_axis(values, factor: int):
    """The finer axis a pooled one was made from: ``factor`` cells per cell.

    The exact inverse of :func:`ingestor.blend.pooled_axis`, and it has to be,
    or the stand-in lands half a cell off the product it stands in for. Pooling
    replaces ``factor`` centres with their mean, which sits half a source cell
    in from the first of them; refining puts them back by stepping out from that
    mean rather than starting on it.

    The point is not extra detail — the model has none to give — but an output
    raster whose corner coordinates and pixel count match the primary's exactly,
    so :func:`ingestor.main.reconcile_grid` sees one grid across a switchover
    and the measured hour survives it. The radar half does gain real detail:
    it is 1 km native and was being thrown away at 2.9 km.
    """
    values = np.asarray(values, dtype=np.float64)
    if factor <= 1:
        return values
    step = (values[1] - values[0]) / factor
    first = values[0] - step * (factor - 1) / 2
    return first + step * np.arange(len(values) * factor)


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
