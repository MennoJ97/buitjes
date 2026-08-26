"""Resampling a regular lat/lon grid onto the raster MapLibre expects.

MapLibre stretches a canvas/image source linearly across its four corner
coordinates *in Web Mercator space*. The KNMI grid is regular in latitude, and
latitude is not linear in Mercator, so handing the grid over unchanged would
misplace rain by kilometres away from the middle of the domain. Rows are
therefore resampled onto a Mercator-linear axis here.

Columns need no resampling: longitude is linear in Mercator, and the crop is
snapped to whole source cells, so the horizontal axis is a plain array slice.

The row weights depend only on the grid and the crop, never on the data, so they
are computed once and reused for every frame in a cycle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from pyproj import CRS, Transformer


def mercator_y(lat_deg: float) -> float:
    """Web Mercator northing, in the usual normalised units."""
    return math.log(math.tan(math.pi / 4 + math.radians(lat_deg) / 2))


def inverse_mercator_y(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


@dataclass(frozen=True)
class TargetGrid:
    """The raster every source is resampled onto.

    There is exactly one of these per manifest: the frontend stretches a single
    canvas across one set of corner coordinates, so observed and forecast frames
    have to share a grid pixel for pixel.
    """

    west: float
    east: float
    south: float
    north: float
    width: int
    height: int

    @property
    def bounds(self):
        """Corner coordinates in MapLibre's order: NW, NE, SE, SW."""
        return [
            [self.west, self.north],
            [self.east, self.north],
            [self.east, self.south],
            [self.west, self.south],
        ]

    def signature(self) -> str:
        """Identifies the grid, so frames from a changed grid can be discarded."""
        return (
            f'{self.west:.6f},{self.south:.6f},{self.east:.6f},'
            f'{self.north:.6f},{self.width},{self.height}'
        )

    def lonlat_mesh(self):
        """Longitude and latitude of every output pixel centre."""
        lons = self.west + (np.arange(self.width) + 0.5) / self.width * (self.east - self.west)
        merc_north = mercator_y(self.north)
        merc_south = mercator_y(self.south)
        centres = merc_north - (np.arange(self.height) + 0.5) / self.height * (
            merc_north - merc_south
        )
        lats = np.degrees(2 * np.arctan(np.exp(centres)) - math.pi / 2)
        return np.meshgrid(lons, lats)


class MercatorResampler:
    """Maps one regular lat/lon field onto a Mercator-linear raster.

    ``lat`` must be ascending (the KNMI files put the southern edge first);
    output row 0 is the northern edge, matching image convention.
    """

    def __init__(self, lat, lon, bounds=None, out_height=None):
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        if lat[1] <= lat[0]:
            raise ValueError('latitude axis must be ascending')

        self._lat0 = float(lat[0])
        self._dlat = float(lat[1] - lat[0])
        lon0 = float(lon[0])
        dlon = float(lon[1] - lon[0])

        west, south, east, north = bounds or (
            lon0 - dlon / 2,
            float(lat[0]) - self._dlat / 2,
            float(lon[-1]) + dlon / 2,
            float(lat[-1]) + self._dlat / 2,
        )

        # Snap the crop out to whole source cells so columns stay a pure slice.
        col0 = max(0, math.floor((west - lon0) / dlon))
        col1 = min(len(lon), math.ceil((east - lon0) / dlon) + 1)
        row0 = max(0, math.floor((south - self._lat0) / self._dlat))
        row1 = min(len(lat), math.ceil((north - self._lat0) / self._dlat) + 1)
        if col1 - col0 < 2 or row1 - row0 < 2:
            raise ValueError('crop bounds do not overlap the grid')

        self._rows = slice(row0, row1)
        self._cols = slice(col0, col1)
        self.width = col1 - col0
        self.height = int(out_height or (row1 - row0))

        # Outer edges of the selected cells - the extent the raster covers.
        self.west = lon0 + (col0 - 0.5) * dlon
        self.east = lon0 + (col1 - 1 + 0.5) * dlon
        self.south = self._lat0 + (row0 - 0.5) * self._dlat
        self.north = self._lat0 + (row1 - 1 + 0.5) * self._dlat

        merc_north = mercator_y(self.north)
        merc_south = mercator_y(self.south)
        step = (merc_north - merc_south) / self.height
        centres = merc_north - (np.arange(self.height) + 0.5) * step
        lats = np.degrees(2 * np.arctan(np.exp(centres)) - math.pi / 2)

        # Fractional position on the source row axis, then linear blend weights.
        # Indices are rebased onto the crop, because that is the only part of
        # the source this class ever reads - see :meth:`crop`.
        source_rows = np.clip((lats - self._lat0) / self._dlat, row0, row1 - 1)
        self._row_lo = np.floor(source_rows).astype(np.int64) - row0
        self._row_hi = np.minimum(self._row_lo + 1, (row1 - row0) - 1)
        self._row_weight = (
            source_rows - row0 - self._row_lo
        ).astype(np.float32)[:, None]

    @property
    def target(self) -> TargetGrid:
        return TargetGrid(
            west=self.west,
            east=self.east,
            south=self.south,
            north=self.north,
            width=self.width,
            height=self.height,
        )

    @property
    def bounds(self):
        """Corner coordinates in MapLibre's order: NW, NE, SE, SW."""
        return self.target.bounds

    @property
    def source_shape(self):
        """Shape of the source window, which is what :meth:`__call__` expects."""
        return (self._rows.stop - self._rows.start, self._cols.stop - self._cols.start)

    def crop(self, values):
        """Restrict a ``(..., nlat, nlon)`` array to the window this reads.

        Reductions across ensemble members belong *inside* this window rather
        than outside it: a probability-matched mean pools every value in the
        array it is handed, so reducing the full KNMI domain and cropping
        afterwards would let rain the map never shows set the intensities on
        the part it does.
        """
        return np.asarray(values)[..., self._rows, self._cols]

    def __call__(self, field):
        """Resample a cropped field; returns (height, width) float32.

        ``field`` must already be through :meth:`crop`.
        """
        field = np.asarray(field)
        if field.shape != self.source_shape:
            raise ValueError(
                f'expected a field cropped to {self.source_shape}, got {field.shape}; '
                'pass it through .crop() first'
            )
        low = field[self._row_lo].astype(np.float32)
        high = field[self._row_hi].astype(np.float32)
        return low + (high - low) * self._row_weight


class StereographicResampler:
    """Maps a KNMI polar-stereographic radar grid onto a :class:`TargetGrid`.

    Unlike the forecast product, the radar composite is a rectangle in *projected*
    space, so its edges are not parallels and meridians. The mapping is therefore
    computed the other way round from the forecast one: every output pixel's
    lon/lat is projected into the radar's stereographic plane and turned into a
    source column/row.

    Sampling is nearest-neighbour. The output grid is within a few percent of the
    radar's own 1 km spacing, so interpolation would buy nothing, and it would
    smear the hard edge of radar coverage into the surrounding no-data area.

    The grid affine is derived from the file's declared corner coordinates rather
    than its offset attributes: KNMI states ``geo_row_offset`` as a magnitude
    while the projected y is negative, and deriving from corners sidesteps that
    convention entirely. The result is cross-checked against the declared pixel
    size, so a change in convention fails loudly instead of misplacing rain.
    """

    #: Tolerance when checking the derived pixel size, in km.
    PIXEL_SIZE_TOLERANCE_KM = 0.01

    def __init__(self, target: TargetGrid, proj4: str, corners, columns: int, rows: int,
                 pixel_size=None):
        # corners arrive as the file stores them: SW, NW, NE, SE lon/lat pairs.
        corners = np.asarray(corners, dtype=np.float64).reshape(4, 2)
        south_west, north_west, north_east, _ = corners

        to_projected = Transformer.from_crs(
            CRS.from_epsg(4326), CRS.from_proj4(proj4), always_xy=True
        )
        x_nw, y_nw = to_projected.transform(*north_west)
        x_ne, _ = to_projected.transform(*north_east)
        _, y_sw = to_projected.transform(*south_west)

        step_x = (x_ne - x_nw) / columns
        step_y = (y_sw - y_nw) / rows
        if pixel_size is not None:
            declared_x, declared_y = (float(v) for v in pixel_size)
            if (abs(step_x - declared_x) > self.PIXEL_SIZE_TOLERANCE_KM
                    or abs(step_y - declared_y) > self.PIXEL_SIZE_TOLERANCE_KM):
                raise ValueError(
                    f'grid affine derived from corners ({step_x:.6f}, {step_y:.6f}) '
                    f'disagrees with the declared pixel size ({declared_x}, {declared_y})'
                )

        lon, lat = target.lonlat_mesh()
        x, y = to_projected.transform(lon, lat)

        # Fractional index of the pixel *centre* nearest each output point.
        column = (x - x_nw) / step_x - 0.5
        row = (y - y_nw) / step_y - 0.5
        column_index = np.rint(column).astype(np.int64)
        row_index = np.rint(row).astype(np.int64)

        self._inside = (
            (column_index >= 0) & (column_index < columns)
            & (row_index >= 0) & (row_index < rows)
        )
        # Clip so the gather is always in bounds; masked out afterwards anyway.
        self._columns = np.clip(column_index, 0, columns - 1)
        self._rows = np.clip(row_index, 0, rows - 1)
        self.coverage = float(self._inside.mean())

    def __call__(self, field, valid=None):
        """Resample a (rows, columns) source field onto the target grid.

        Returns ``(values, measured)``: the rain rate, and a mask of the output
        pixels a radar actually looked at. The two are separate on purpose. The
        radar composite is a smaller box than the forecast domain the map is
        drawn on, and it has holes of its own, so a good part of the frame is
        somewhere no radar can see. Folding that into a zero would publish "no
        rain" for a place we know nothing about, which is the one thing an
        observation frame must never claim - the frontend already has words for
        "no radar here" and could not reach them.

        ``valid`` is an optional boolean mask of usable source pixels.
        """
        gathered = np.asarray(field)[self._rows, self._columns].astype(np.float32)
        measured = self._inside
        if valid is not None:
            measured = measured & np.asarray(valid)[self._rows, self._columns]
        return np.where(measured, gathered, 0.0), measured
