//! Point forecasts for coordinates nobody configured in advance.
//!
//! `/api/point/<name>` hands over a document the ingestor precomputed while
//! KNMI's 20 members were still in memory. A phone is not a configured
//! location, so this assembles the same document shape on demand: the rain
//! series is sampled out of the published median frames, and the spread those
//! frames no longer carry comes from the Open-Meteo ensemble that the
//! conditions proxy already fetches for arbitrary points.
//!
//! The web frontend has done exactly this in JavaScript for as long as
//! click-to-inspect has existed (`point.js`, `ensemble.js`). Doing it again
//! here is not duplication for its own sake — it is about what the caller has
//! to be. A phone widget refreshing on a timer, or a background worker
//! deciding whether to wake someone up, should not have to fetch eighty images
//! and run a WebGL context to learn whether it is going to rain. It asks once
//! and gets a document. The shapes are deliberately identical, so a client can
//! render a named location and a coordinate with the same code.
//!
//! What the two cannot share is what their uncertainty is *of*. A named
//! location carries the members at its own square kilometre, sampled while the
//! timestep was in memory, and a probability to go with them. A coordinate is
//! read back off the published frames: the field the map draws, and the
//! p10/p50/p90 the spread frame carries for a small radius around each pixel.
//! So a coordinate does get a real band — but one answering "near here" rather
//! than "here", and with nothing behind it that can count members. The keys are
//! what say so. `nearby_median` and `nearby_radius_km`, never a bare `median`,
//! and `measured` over the observed hour rather than `field`, because an hour
//! of radar composite is a measurement with no ensemble behind it at all.
//!
//! Past the radar's horizon the hourly Open-Meteo band travels alongside under
//! `precipitation_outlook`, rather than being blended in and passed off as the
//! same measurement.

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};

use chrono::{Local, NaiveDateTime, TimeZone};
use serde_json::{json, Map, Value};

/// A member counts as "raining" at or above this rate. Matches the ingestor's
/// `WET_THRESHOLD_MM_H` and the bottom of the map's colour ramp.
const WET_THRESHOLD_MM_H: f64 = 0.1;

/// Percentiles published for each hourly timestep, matching `points.PERCENTILES`.
/// The rain series has its own three, which the manifest names — see [`Spread`].
const PERCENTILES: [u8; 5] = [10, 25, 50, 75, 90];

/// Blue at or above this marks a pixel no radar looked at, matching
/// `encode.NO_DATA_FLAG`. It is not the same as dry, and this module drops such
/// a step rather than publishing a confident zero for a hole in the composite.
const NO_DATA_FLAG: u8 = 255;

// ----------------------------------------------------------------- the grid

/// Web Mercator northing, clamped to the projection's usable latitude.
fn mercator_y(lat: f64) -> f64 {
    let clamped = lat.clamp(-85.051_129, 85.051_129);
    (std::f64::consts::FRAC_PI_4 + clamped.to_radians() / 2.0)
        .tan()
        .ln()
}

/// The raster every frame in a cycle shares, as the manifest describes it.
pub struct Grid {
    west: f64,
    east: f64,
    south: f64,
    north: f64,
    width: usize,
    height: usize,
    max_precip: f64,
}

impl Grid {
    /// Read the grid out of a parsed manifest, or `None` if it is not one.
    ///
    /// `bounds` is stored in MapLibre's corner order — NW, NE, SE, SW — so the
    /// four numbers that matter are read from three of the corners rather than
    /// assumed.
    pub fn from_manifest(manifest: &Value) -> Option<Grid> {
        let bounds = manifest.get("bounds")?.as_array()?;
        if bounds.len() != 4 {
            return None;
        }
        let corner = |index: usize, axis: usize| -> Option<f64> {
            bounds.get(index)?.as_array()?.get(axis)?.as_f64()
        };

        let grid = Grid {
            west: corner(0, 0)?,
            north: corner(0, 1)?,
            east: corner(1, 0)?,
            south: corner(2, 1)?,
            width: manifest.get("width")?.as_u64()? as usize,
            height: manifest.get("height")?.as_u64()? as usize,
            max_precip: manifest.get("max_precip_mm_h")?.as_f64()?,
        };
        if grid.east <= grid.west || grid.north <= grid.south || grid.width == 0 || grid.height == 0
        {
            return None;
        }
        Some(grid)
    }

    /// The pixel covering a coordinate, or `None` outside the domain.
    ///
    /// The frontend stretches a frame across its corners *in Web Mercator*, so
    /// the vertical lookup has to be mercator too. Treating the row axis as
    /// linear latitude would misplace rain by kilometres near the edges — the
    /// same reason the ingestor resamples rows in the first place.
    pub fn pixel(&self, lat: f64, lon: f64) -> Option<(usize, usize)> {
        self.pixel_in(lat, lon, self.width, self.height)
    }

    /// The same lookup against a raster of a different size over the same
    /// corners.
    ///
    /// The spread layer is published pooled — half the rain layer's resolution
    /// at the time of writing — because "how hard could it rain within 3 km of
    /// here" does not carry kilometre-scale detail. Both layers span the same
    /// bounding box, so only the divisor changes; using the rain layer's size
    /// on the band would read a pixel twice as far north-west as asked for, and
    /// return nothing at all for the half of the domain past its edge.
    pub fn pixel_in(
        &self,
        lat: f64,
        lon: f64,
        width: usize,
        height: usize,
    ) -> Option<(usize, usize)> {
        if width == 0 || height == 0 {
            return None;
        }
        let x_fraction = (lon - self.west) / (self.east - self.west);
        let merc_north = mercator_y(self.north);
        let merc_south = mercator_y(self.south);
        let y_fraction = (merc_north - mercator_y(lat)) / (merc_north - merc_south);
        if !(0.0..1.0).contains(&x_fraction) || !(0.0..1.0).contains(&y_fraction) {
            return None;
        }
        Some((
            ((x_fraction * width as f64) as usize).min(width - 1),
            ((y_fraction * height as f64) as usize).min(height - 1),
        ))
    }

    /// Undo the frame encoding: a 16-bit fraction of full-scale rain rate.
    fn rate(&self, packed: u16) -> f64 {
        packed as f64 / 65535.0 * self.max_precip
    }
}

// ---------------------------------------------------------------- the spread

/// The spread layer, as the manifest describes it.
///
/// Three percentile fields packed a byte each into one frame's R, G and B, on a
/// logarithmic scale — which is how three rates fit where the rain frame spends
/// sixteen bits on one. Over the ramp's own range that is a step of under 3% of
/// the rate, finer than the source and far finer than a colour ramp can show.
///
/// The scale is read from the manifest rather than assumed. The floor, the
/// ceiling and which percentiles were packed in which order are the ingestor's
/// to change, and three bytes say nothing about any of them on their own.
pub struct Spread {
    /// What the band is a band *over*: the radius around each pixel the
    /// percentiles were taken across.
    radius_km: f64,
    percentiles: Vec<u8>,
    floor_mm_h: f64,
    max_mm_h: f64,
}

impl Spread {
    /// Read the block, or `None` when the layer is off.
    ///
    /// The manifest writes `"spread": null` in that case — a present key with
    /// nothing in it — so absence and disabled arrive the same way, which is
    /// right: both mean there is no band to sample.
    pub fn from_manifest(manifest: &Value) -> Option<Spread> {
        let block = manifest.get("spread")?.as_object()?;
        let floor = block.get("floor_mm_h")?.as_f64()?;
        let max = block.get("max_mm_h")?.as_f64()?;
        // The scale is a ratio of these two; equal or inverted makes the
        // logarithm below either zero or undefined.
        if floor <= 0.0 || max <= floor {
            return None;
        }

        let percentiles: Vec<u8> = block
            .get("percentiles")?
            .as_array()?
            .iter()
            .filter_map(Value::as_u64)
            .map(|percentile| percentile as u8)
            .collect();
        // Three channels carry three percentiles. Anything else is a manifest
        // this code cannot read, and guessing which channel is which would put
        // a p90 where a reader expects a median.
        if percentiles.len() != 3 {
            return None;
        }

        Some(Spread {
            radius_km: block.get("radius_km").and_then(Value::as_f64).unwrap_or(0.0),
            percentiles,
            floor_mm_h: floor,
            max_mm_h: max,
        })
    }

    /// Undo `encode._to_log_byte`. Zero is dry rather than the floor, so a dry
    /// pixel stays dry through the round trip instead of acquiring 0.1 mm/h it
    /// never had.
    fn rate(&self, level: u8) -> f64 {
        if level == 0 {
            return 0.0;
        }
        let decades = (self.max_mm_h / self.floor_mm_h).log10();
        self.floor_mm_h * 10f64.powf((level - 1) as f64 / 254.0 * decades)
    }

    /// `nearby_median` for the middle one, `nearby_p10`/`nearby_p90` either
    /// side. Prefixed because a named location publishes its own `median` from
    /// the members at one cell, and the two are different numbers.
    fn key(percentile: u8) -> String {
        if percentile == 50 {
            "nearby_median".to_string()
        } else {
            format!("nearby_p{percentile}")
        }
    }
}

// ---------------------------------------------------------------- the frames

/// What one pixel of a rain frame says.
///
/// An enum rather than a `u16`, because blue carries a flag that a rate cannot
/// express: a pixel no radar looked at is not a pixel where it is not raining,
/// and collapsing the two publishes a confident dry forecast for a hole in the
/// composite. About a quarter of an observed frame used to go out that way.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Reading {
    /// A rate, packed as `encode.encode_frame` writes it.
    Rate(u16),
    /// No radar measured here.
    NoData,
}

/// One decoded frame, kept as a reading per pixel rather than as RGBA.
pub struct DecodedFrame {
    width: usize,
    height: usize,
    readings: Vec<Reading>,
}

impl DecodedFrame {
    fn at(&self, px: usize, py: usize) -> Option<Reading> {
        if px >= self.width || py >= self.height {
            return None;
        }
        self.readings.get(py * self.width + px).copied()
    }
}

/// Decode a frame written by `ingestor.encode.encode_frame`.
fn decode(bytes: &[u8]) -> Result<DecodedFrame, String> {
    let image = image::load_from_memory_with_format(bytes, image::ImageFormat::WebP)
        .map_err(|error| error.to_string())?
        .to_rgba8();
    let (width, height) = (image.width() as usize, image.height() as usize);

    let readings = image
        .pixels()
        .map(|pixel| {
            let [red, green, blue, alpha] = pixel.0;
            if blue >= NO_DATA_FLAG {
                Reading::NoData
            } else if alpha == 0 {
                // Frames written before the format went opaque, still on the
                // volume until they age out. Dry, rather than 65 mm/h of noise.
                Reading::Rate(0)
            } else {
                Reading::Rate(((red as u16) << 8) | green as u16)
            }
        })
        .collect();

    Ok(DecodedFrame {
        width,
        height,
        readings,
    })
}

/// One decoded spread frame: three byte levels per pixel, in the manifest's
/// percentile order.
pub struct DecodedSpread {
    width: usize,
    height: usize,
    levels: Vec<[u8; 3]>,
}

impl DecodedSpread {
    fn at(&self, px: usize, py: usize) -> Option<[u8; 3]> {
        if px >= self.width || py >= self.height {
            return None;
        }
        self.levels.get(py * self.width + px).copied()
    }
}

/// Decode a frame written by `ingestor.encode.encode_spread_frame`.
///
/// Alpha is ignored on purpose. A band's lower edge sits exactly where the map
/// is dry, and the ingestor writes these opaque so that edge survives being
/// read back; treating a transparent pixel as empty would delete the very
/// values the layer exists to carry.
fn decode_spread(bytes: &[u8]) -> Result<DecodedSpread, String> {
    let image = image::load_from_memory_with_format(bytes, image::ImageFormat::WebP)
        .map_err(|error| error.to_string())?
        .to_rgba8();
    let (width, height) = (image.width() as usize, image.height() as usize);

    let levels = image
        .pixels()
        .map(|pixel| {
            let [red, green, blue, _] = pixel.0;
            [red, green, blue]
        })
        .collect();

    Ok(DecodedSpread {
        width,
        height,
        levels,
    })
}

/// Sample every frame the manifest lists at one pixel.
///
/// Blocking: it reads files and decodes images, so callers run it on a blocking
/// thread. A frame that will not load is skipped rather than failing the
/// request — a cycle mid-publish can list a frame a moment before it lands, and
/// eighty-three steps out of eighty-four is still a forecast.
///
/// Frames are decoded and dropped one at a time on purpose. Holding a whole
/// cycle costs about 100 MB, and this container is capped at 128 with a
/// measured baseline of 16: the memory is the scarce thing here, not the
/// milliseconds. Repeat requests are served from the document cache below
/// instead, which holds the answer rather than the pixels it came from. The
/// spread frame beside each rain frame doubles the images decoded and changes
/// none of that — it is decoded, sampled and dropped in the same breath.
pub fn sample_series(
    frame_dir: &Path,
    grid: &Grid,
    spread: Option<&Spread>,
    frames: &[Value],
    latitude: f64,
    longitude: f64,
) -> Vec<Value> {
    // The coordinate rather than a pixel, because there is no longer one
    // pixel: the band is published at its own resolution, so it has its own
    // lookup against the same corners.
    let Some((px, py)) = grid.pixel(latitude, longitude) else {
        return Vec::new(); // Outside the domain; assemble() says so.
    };
    let mut series = Vec::with_capacity(frames.len());
    let mut failures = 0usize;

    for frame in frames {
        let (Some(file), Some(valid_time)) = (
            frame.get("file").and_then(Value::as_str),
            frame.get("t").and_then(Value::as_i64),
        ) else {
            continue;
        };

        let decoded = match std::fs::read(frame_dir.join(file)).map_err(|e| e.to_string())
            .and_then(|bytes| decode(&bytes))
        {
            Ok(decoded) => decoded,
            Err(_) => {
                failures += 1;
                continue;
            }
        };
        let Some(reading) = decoded.at(px, py) else {
            continue;
        };
        // A step no radar measured is dropped, not published as zero. The
        // frontend drops it too (`point.js` filters a null sample), so the two
        // agree about the length of the series as well as its values.
        let Reading::Rate(packed) = reading else {
            continue;
        };

        let kind = frame.get("kind").and_then(Value::as_str);
        let mut entry = Map::new();
        entry.insert("t".into(), json!(valid_time));
        if let Some(kind) = kind {
            // Which part is measured and which is extrapolated matters more
            // than the numbers do; a client should not have to join back to
            // the manifest to draw the boundary.
            entry.insert("kind".into(), json!(kind));
        }

        // `measured` over the observed hour and `field` beyond it. The radar
        // composite is a measurement with no ensemble behind it, while a
        // forecast frame holds whatever the ingestor reduced its members into —
        // a probability-matched mean by default, which is not a median of
        // anything. One key for both would caption an hour of measured rain as
        // a statistic of members that were never involved in it.
        let value_key = if kind == Some("observed") { "measured" } else { "field" };
        entry.insert(value_key.into(), json!(round(grid.rate(packed), 2)));

        if let Some(spread) = spread {
            if let Some(levels) = frame
                .get("spread")
                .and_then(Value::as_str)
                .and_then(|file| sample_spread(frame_dir, file, grid, latitude, longitude))
            {
                for (percentile, level) in spread.percentiles.iter().zip(levels) {
                    entry.insert(
                        Spread::key(*percentile),
                        json!(round(spread.rate(level), 2)),
                    );
                }
            }
        }

        series.push(Value::Object(entry));
    }

    if failures > 0 {
        tracing::warn!(failures, "some frames could not be sampled");
    }
    series
}

/// One spread frame at one coordinate, or `None` if it will not load.
///
/// The pixel is worked out against the decoded image's own dimensions rather
/// than the manifest's, which is what the frontend settled on and for the
/// reason it gives: the image is the one thing that cannot disagree with the
/// pixels being read, and it stays right for a frame written before the layer
/// was pooled and still sitting on the volume.
///
/// A missing band costs that step its band and nothing else: the entry still
/// carries its value, and `centre_keys` falls through to it. Losing the whole
/// step because the second of two files was mid-write would be a worse trade.
fn sample_spread(
    frame_dir: &Path,
    file: &str,
    grid: &Grid,
    latitude: f64,
    longitude: f64,
) -> Option<[u8; 3]> {
    let bytes = std::fs::read(frame_dir.join(file)).ok()?;
    let decoded = decode_spread(&bytes).ok()?;
    let (px, py) = grid.pixel_in(latitude, longitude, decoded.width, decoded.height)?;
    decoded.at(px, py)
}

/// Assembled documents, keyed by rounded coordinate.
///
/// The obvious cache here would hold decoded frames, and it was the first
/// thing tried. It is the wrong one twice over: a cycle of frames is ~100 MB
/// against this container's 128 MB limit, and a budget small enough to be safe
/// would be cleared *during* a single request, since one request touches every
/// frame. It would cost memory to make things slower.
///
/// What repeats is not frames but questions — the same phone asking about the
/// same kilometre every quarter of an hour — so the answer is what is worth
/// keeping. A document is a few kilobytes.
///
/// Validity is the manifest's `generated_at` rather than a clock: the ingestor
/// rewrites the manifest exactly when the frame list changes, which is exactly
/// when a cached answer stops being the current one. A TTL would either serve
/// a superseded nowcast or throw away a good answer early.
#[derive(Clone)]
pub struct DocumentCache {
    entries: Arc<Mutex<HashMap<(i32, i32), (i64, Arc<Vec<u8>>)>>>,
}

impl Default for DocumentCache {
    fn default() -> Self {
        Self::new()
    }
}

impl DocumentCache {
    pub fn new() -> Self {
        Self {
            entries: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Hundredths of a degree, about a kilometre — the same rounding the
    /// conditions proxy uses, and finer than the grid being sampled.
    fn key(latitude: f64, longitude: f64) -> (i32, i32) {
        (
            (latitude * 100.0).round() as i32,
            (longitude * 100.0).round() as i32,
        )
    }

    pub fn get(&self, latitude: f64, longitude: f64, published: i64) -> Option<Arc<Vec<u8>>> {
        let entries = self.entries.lock().ok()?;
        let (stored, body) = entries.get(&Self::key(latitude, longitude))?;
        (*stored == published).then(|| Arc::clone(body))
    }

    pub fn insert(&self, latitude: f64, longitude: f64, published: i64, body: Arc<Vec<u8>>) {
        let Ok(mut entries) = self.entries.lock() else {
            return;
        };
        // Bounded so a scripted sweep of coordinates cannot grow it forever.
        if entries.len() > 512 {
            entries.clear();
        }
        entries.insert(Self::key(latitude, longitude), (published, body));
    }
}

fn percentile_key(percentile: u8) -> String {
    if percentile == 50 {
        "median".to_string()
    } else {
        format!("p{percentile}")
    }
}

// ------------------------------------------------------------- the ensemble

fn round(value: f64, decimals: u32) -> f64 {
    let factor = 10f64.powi(decimals as i32);
    (value * factor).round() / factor
}

/// Linear-interpolated percentile, matching numpy's default and `ensemble.js`.
fn percentile_of(sorted: &[f64], fraction: f64) -> f64 {
    if sorted.len() == 1 {
        return sorted[0];
    }
    let position = fraction * (sorted.len() - 1) as f64;
    let lower = position.floor() as usize;
    let upper = position.ceil() as usize;
    if lower == upper {
        return sorted[lower];
    }
    sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower as f64)
}

/// Open-Meteo timestamps with `timezone=UTC`: `2026-08-25T12:00`, no offset.
fn epoch_from_iso(text: &str) -> Option<i64> {
    NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M")
        .ok()
        .map(|naive| naive.and_utc().timestamp())
}

/// Reduce one Open-Meteo variable to the percentile series the charts draw.
///
/// Members are the numbered `<variable>_memberNN` keys plus the unnumbered
/// control run — the same rule `ensemble.js` applies, so a client cannot tell
/// which side of the wire did the reducing.
fn series_from_ensemble(hourly: &Map<String, Value>, variable: &str, decimals: u32) -> Vec<Value> {
    let member_prefix = format!("{variable}_member");
    let members: Vec<&Vec<Value>> = hourly
        .iter()
        .filter(|(key, _)| key.as_str() == variable || key.starts_with(&member_prefix))
        .filter_map(|(_, value)| value.as_array())
        .collect();

    let Some(times) = hourly.get("time").and_then(Value::as_array) else {
        return Vec::new();
    };
    if members.is_empty() {
        return Vec::new();
    }

    let mut series = Vec::with_capacity(times.len());
    for (index, time) in times.iter().enumerate() {
        let Some(valid_time) = time.as_str().and_then(epoch_from_iso) else {
            continue;
        };

        let mut values: Vec<f64> = members
            .iter()
            .filter_map(|member| member.get(index))
            .filter_map(Value::as_f64)
            .collect();
        if values.is_empty() {
            continue;
        }
        values.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));

        let mut entry = Map::new();
        entry.insert("t".into(), json!(valid_time));
        for percentile in PERCENTILES {
            entry.insert(
                percentile_key(percentile),
                json!(round(
                    percentile_of(&values, percentile as f64 / 100.0),
                    decimals
                )),
            );
        }
        series.push(Value::Object(entry));
    }
    series
}

/// The blocks a point document carries, built from a raw ensemble response.
fn conditions_blocks(payload: &Value) -> Map<String, Value> {
    let mut blocks = Map::new();
    let Some(hourly) = payload.get("hourly").and_then(Value::as_object) else {
        return blocks;
    };

    for (key, variable, unit, decimals) in [
        ("temperature", "temperature_2m", "°C", 1),
        ("wind", "wind_speed_10m", "m/s", 1),
        ("solar", "shortwave_radiation", "W/m²", 0),
        ("precipitation_outlook", "precipitation", "mm/h", 1),
    ] {
        let series = series_from_ensemble(hourly, variable, decimals);
        if !series.is_empty() {
            blocks.insert(key.into(), json!({ "unit": unit, "series": series }));
        }
    }
    blocks
}

// -------------------------------------------------------------- the sentence

/// Match the frontend's rate formatting, so the two never disagree.
fn format_rate(mmh: f64) -> String {
    if mmh >= 10.0 {
        return format!("{}", mmh.round() as i64);
    }
    let text = if mmh >= 1.0 {
        format!("{mmh:.1}")
    } else {
        format!("{mmh:.2}")
    };
    text.trim_end_matches('0').trim_end_matches('.').to_string()
}

/// Local 24-hour time, matching `points._clock`.
fn clock(timestamp: i64) -> String {
    Local
        .timestamp_opt(timestamp, 0)
        .single()
        .map(|moment| moment.format("%H:%M").to_string())
        .unwrap_or_default()
}

/// Which entry fields carry a step's drawn number, best first.
///
/// A chain rather than one key, because a coordinate's series is not
/// homogeneous. The observed hour in front of the forecast is measurement, with
/// no ensemble behind it and so no neighbourhood median; the forecast steps
/// have one. Naming the key the forecast half carries would leave the measured
/// half with no number at all — and a sentence describing half a series is
/// worse than one describing none of it, because it looks complete.
///
/// The order is the order of usefulness. The neighbourhood median leads because
/// it is the number the band belongs to: the field is dealt by rank, so it can
/// sit at zero through the band's whole peak and then spike after it, and a
/// line that does that beside its own band looks unrelated to it.
///
/// Ported from `centreOf` in `point.js`, and checked against the series rather
/// than against the block's metadata for the same reason it is there: a key the
/// document does not actually carry is not a fallback, it is a blank chart.
pub fn centre_keys(series: &[Value], has_radius: bool) -> Vec<&'static str> {
    let carries = |key: &str| series.iter().any(|entry| entry.get(key).is_some());
    let mut chain = Vec::new();
    if has_radius && carries("nearby_median") {
        chain.push("nearby_median");
    }
    if carries("measured") {
        chain.push("measured");
    }
    if carries("field") {
        chain.push("field");
    }
    if carries("median") || chain.is_empty() {
        chain.push("median");
    }
    chain
}

/// The number one step is drawn as, or `None` where it carries none of them.
///
/// Absent resolves to absent rather than to zero. A step with no value is not a
/// dry step, and the difference decides whether a spell is described as ending.
fn centre_value(entry: &Value, keys: &[&str]) -> Option<f64> {
    keys.iter().find_map(|key| entry.get(*key).and_then(Value::as_f64))
}

/// A plain-language read of the series, ported from `points.summarise`.
///
/// Describes the *spell* — the contiguous run of wet steps — rather than the
/// moment it begins, for the reasons set out at length in the Python. The one
/// branch that does not survive the crossing is the "probably dry, but a 40%
/// chance" case: it reads how many members are raining near the location, and
/// the frames carry percentiles rather than members. A dry line here means the
/// sentence says dry, and the band beside it is what shows the disagreement.
pub fn summarise(series: &[Value], centre_keys: &[&str], reference_time: i64) -> Value {
    let centre = |entry: &Value| centre_value(entry, centre_keys).unwrap_or(0.0);
    let at = |entry: &Value| entry.get("t").and_then(Value::as_i64).unwrap_or(0);

    let future: Vec<&Value> = series
        .iter()
        .filter(|entry| at(entry) >= reference_time && centre_value(entry, centre_keys).is_some())
        .collect();
    if future.is_empty() {
        return json!({ "raining_now": false, "text": "No forecast available." });
    }

    let Some(onset_index) = future
        .iter()
        .position(|entry| centre(entry) >= WET_THRESHOLD_MM_H)
    else {
        return json!({ "raining_now": false, "starts_at": Value::Null, "text": "Staying dry." });
    };

    let dry_after = future[onset_index + 1..]
        .iter()
        .position(|entry| centre(entry) < WET_THRESHOLD_MM_H)
        .map(|offset| onset_index + 1 + offset);
    let spell = &future[onset_index..dry_after.unwrap_or(future.len())];
    let clearing = dry_after.map(|index| future[index]);

    let onset = spell[0];
    let peak = spell
        .iter()
        .max_by(|a, b| {
            centre(a)
                .partial_cmp(&centre(b))
                .unwrap_or(std::cmp::Ordering::Equal)
        })
        .copied()
        .unwrap_or(onset);
    let raining_now = onset_index == 0;

    // Only worth its own clause if it is meaningfully heavier than the start,
    // otherwise it is the same number twice.
    let peak_matters = at(peak) != at(onset) && centre(peak) >= centre(onset) * 1.5 + 0.05;

    let lead_minutes = ((at(onset) - reference_time) as f64 / 60.0).round() as i64;
    // Whether the sentence counts minutes from now or names clock times. Mixing
    // the two makes "rain at 11:25, up to 4 mm/h within 30 min" ambiguous.
    let relative = raining_now || lead_minutes < 90;

    let mut parts = Vec::new();
    if raining_now {
        parts.push(format!("Raining now at {} mm/h", format_rate(centre(onset))));
    } else {
        let when = if relative {
            format!("in {lead_minutes} min")
        } else {
            format!("at {}", clock(at(onset)))
        };
        parts.push(if peak_matters {
            format!("Dry now — rain {when}")
        } else {
            format!("Dry now — rain {when} at {} mm/h", format_rate(centre(onset)))
        });
    }

    if peak_matters {
        let climb = ((at(peak) - at(onset)) as f64 / 60.0).round() as i64;
        parts.push(if relative && climb <= 30 {
            format!(
                "up to {} mm/h within {climb} min",
                format_rate(centre(peak))
            )
        } else {
            format!(
                "peaking {} mm/h around {}",
                format_rate(centre(peak)),
                clock(at(peak))
            )
        });
    }

    parts.push(match clearing {
        Some(entry) => format!("easing off around {}", clock(at(entry))),
        None => "lasting past the end of the forecast".to_string(),
    });

    json!({
        "raining_now": raining_now,
        "starts_at": if raining_now { Value::Null } else { json!(at(onset)) },
        "stops_at": clearing.map(at).map(Value::from).unwrap_or(Value::Null),
        "peak_mm_h": centre(peak),
        "peak_at": at(peak),
        "text": format!("{}.", parts.join(", ")),
    })
}

// -------------------------------------------------------------- the document

/// Assemble the document served for a coordinate.
///
/// `rain` is the sampled KNMI series (empty when the point falls outside the
/// radar domain) and `conditions` is the raw Open-Meteo response, either of
/// which may be missing without making the other worthless: abroad, the
/// conditions still answer; with Open-Meteo down, the next six hours of rain
/// still do.
///
/// The manifest is read a second time here, for the spread block. That is on
/// purpose rather than threaded through from the sampler: whether the document
/// may claim a band is a property of the cycle it describes, and deriving it
/// from the same place the sampler did keeps the claim and the numbers from
/// ever disagreeing about it.
pub fn assemble(
    manifest: &Value,
    latitude: f64,
    longitude: f64,
    rain: Vec<Value>,
    conditions: Option<&Value>,
    generated_at: i64,
) -> Value {
    let reference_time = manifest
        .get("reference_time")
        .and_then(Value::as_i64)
        .unwrap_or(generated_at);

    let mut document = Map::new();
    document.insert("generated_at".into(), json!(generated_at));
    document.insert("reference_time".into(), json!(reference_time));
    document.insert(
        "location".into(),
        json!({ "name": "this point", "lat": latitude, "lon": longitude, "ad_hoc": true }),
    );
    document.insert(
        "source".into(),
        manifest.get("source").cloned().unwrap_or_else(|| json!({})),
    );

    if let Some(payload) = conditions {
        for (key, value) in conditions_blocks(payload) {
            document.insert(key, value);
        }
        document.insert(
            "conditions_source".into(),
            json!({ "model": "on-demand ensemble", "attribution": "Open-Meteo (CC BY 4.0)" }),
        );
    }

    if rain.is_empty() {
        // Say so rather than serving a flat zero line, which reads exactly like
        // a confident forecast of dry weather. A client outside the domain
        // should show nothing and pause its alerting, not relax.
        document.insert("out_of_coverage".into(), json!(true));
        document.insert(
            "summary".into(),
            json!({
                "raining_now": false,
                "text": "Outside the radar domain — no rain forecast for this point.",
            }),
        );
    } else {
        let spread = Spread::from_manifest(manifest);
        let keys = centre_keys(&rain, spread.is_some());
        document.insert("summary".into(), summarise(&rain, &keys, reference_time));

        let knmi_ends = rain
            .last()
            .and_then(|entry| entry.get("t"))
            .and_then(Value::as_i64);

        let mut block = Map::new();
        block.insert("unit".into(), json!("mm/h"));
        // Read off the frames, not from the members: whoever draws this is
        // looking at a picture of a forecast rather than at the forecast.
        block.insert("frame_only".into(), json!(spread.is_none()));
        if let Some(spread) = &spread {
            // Two keys, one number, and not a slip. `band_radius_km` is what
            // the percentiles beside it were taken across; `nearby_radius_km`
            // is the name a named location publishes the same thing under, so
            // both documents can be drawn by one path. What neither of them is
            // is `neighbourhood_km`, which a named location also carries and
            // which means the radius of its *probability*.
            block.insert("band_radius_km".into(), json!(spread.radius_km));
            block.insert("nearby_radius_km".into(), json!(spread.radius_km));
        }
        // What the forecast steps' `field` actually is, so a reader can label
        // it rather than calling every central number a median. The short form
        // where the manifest has one, since this ends up under a value.
        if let Some(product) = manifest
            .get("source")
            .and_then(|source| source.get("reducer").or_else(|| source.get("product")))
            .and_then(Value::as_str)
        {
            block.insert("field_product".into(), json!(product));
        }
        block.insert("series".into(), json!(rain));
        document.insert("precipitation".into(), Value::Object(block));

        // The hourly outlook only earns its place past where KNMI stops.
        if let (Some(ends), Some(outlook)) = (
            knmi_ends,
            document.get_mut("precipitation_outlook").and_then(Value::as_object_mut),
        ) {
            if let Some(series) = outlook.get_mut("series").and_then(Value::as_array_mut) {
                series.retain(|entry| {
                    entry.get("t").and_then(Value::as_i64).is_some_and(|t| t > ends)
                });
            }
        }
    }

    Value::Object(document)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grid() -> Grid {
        Grid {
            west: 1.0,
            east: 9.5,
            south: 49.5,
            north: 55.0,
            width: 850,
            height: 700,
            max_precip: 100.0,
        }
    }

    #[test]
    fn reads_the_grid_from_a_manifest() {
        let manifest = json!({
            "bounds": [[1.0, 55.0], [9.5, 55.0], [9.5, 49.5], [1.0, 49.5]],
            "width": 850, "height": 700, "max_precip_mm_h": 100.0,
        });
        let grid = Grid::from_manifest(&manifest).expect("a valid manifest");
        assert_eq!((grid.width, grid.height), (850, 700));
        assert_eq!(grid.west, 1.0);
        assert_eq!(grid.north, 55.0);
    }

    #[test]
    fn rejects_a_manifest_without_a_grid() {
        assert!(Grid::from_manifest(&json!({ "frames": [] })).is_none());
        // A degenerate bounding box would divide by zero on the next line.
        assert!(Grid::from_manifest(&json!({
            "bounds": [[1.0, 55.0], [1.0, 55.0], [1.0, 55.0], [1.0, 55.0]],
            "width": 10, "height": 10, "max_precip_mm_h": 100.0,
        }))
        .is_none());
    }

    #[test]
    fn places_the_corners_in_the_corner_pixels() {
        let grid = grid();
        assert_eq!(grid.pixel(54.999, 1.001), Some((0, 0)));
        let (px, py) = grid.pixel(49.501, 9.499).expect("inside");
        assert_eq!((px, py), (849, 699));
    }

    /// The band is published pooled, so it has to be read at its own size.
    ///
    /// This is the whole failure this test exists for: reading a half-size
    /// raster at the full raster's coordinates lands twice as far north-west as
    /// asked, and for anything in the south-east quadrant — which is most of
    /// the country — falls off the image entirely and silently returns no band.
    #[test]
    fn samples_a_pooled_raster_at_its_own_size() {
        let grid = grid();
        let (px, py) = grid.pixel(50.5, 8.0).expect("inside the domain");
        assert!(
            px >= grid.width / 2 && py >= grid.height / 2,
            "a point that a half-size read at full-size coordinates would miss",
        );

        let pooled = grid
            .pixel_in(50.5, 8.0, grid.width / 2, grid.height / 2)
            .expect("still inside the domain, at half the resolution");
        assert_eq!(pooled, (px / 2, py / 2), "the pooled cell covering the same place");
    }

    #[test]
    fn refuses_a_raster_with_no_pixels() {
        let grid = grid();
        assert!(grid.pixel_in(52.0, 5.0, 0, 0).is_none());
    }

    #[test]
    fn rejects_coordinates_outside_the_domain() {
        let grid = grid();
        assert_eq!(grid.pixel(52.0, 0.5), None); // west of the domain
        assert_eq!(grid.pixel(60.0, 5.0), None); // north of it
        assert_eq!(grid.pixel(52.0, 12.0), None);
    }

    /// The vertical axis is mercator, not linear latitude.
    ///
    /// Mercator stretches toward the pole, so the northern half of the domain
    /// occupies more of the image than the southern half — and the latitude
    /// half way between the edges in *degrees* therefore falls below the row
    /// half way down in *pixels*. Getting this backwards is the bug that would
    /// misplace rain by kilometres, so it is asserted rather than assumed.
    ///
    /// The numbers are cross-checked against the producer: every row centre
    /// from the ingestor's own construction (`raster.py`) round-trips back to
    /// its row through this function.
    #[test]
    fn samples_rows_in_mercator_not_linear_latitude() {
        let grid = grid();
        let middle_by_degrees = (55.0 + 49.5) / 2.0;
        let (_, py) = grid.pixel(middle_by_degrees, 5.0).expect("inside");
        let linear_row = grid.height / 2;
        assert!(
            py > linear_row,
            "mercator row {py} should sit south of the linear row {linear_row}"
        );
        assert_eq!(py, 360, "the offset is ten rows for this domain, not rounding");
    }

    /// Row centres from `raster.py` must land back on their own row. This is
    /// the actual contract with the ingestor — the grid is only shared through
    /// the manifest, so nothing but a test like this keeps the two in step.
    #[test]
    fn round_trips_the_ingestors_row_centres() {
        let grid = grid();
        let merc_north = mercator_y(grid.north);
        let merc_south = mercator_y(grid.south);

        for row in [0, 1, 175, 349, 350, 351, 698, 699] {
            let centre =
                merc_north - (row as f64 + 0.5) / grid.height as f64 * (merc_north - merc_south);
            let latitude = (2.0 * centre.exp().atan() - std::f64::consts::FRAC_PI_2).to_degrees();
            let (_, py) = grid.pixel(latitude, 5.0).expect("inside the domain");
            assert_eq!(py, row, "row {row} did not round-trip");
        }
    }

    #[test]
    fn undoes_the_frame_encoding() {
        let grid = grid();
        assert_eq!(grid.rate(0), 0.0);
        assert_eq!(grid.rate(65535), 100.0);
        assert!((grid.rate(32768) - 50.0).abs() < 0.01);
    }

    #[test]
    fn interpolates_percentiles_like_numpy() {
        let sorted = [1.0, 2.0, 3.0, 4.0];
        assert_eq!(percentile_of(&sorted, 0.5), 2.5);
        assert_eq!(percentile_of(&sorted, 0.0), 1.0);
        assert_eq!(percentile_of(&sorted, 1.0), 4.0);
        assert_eq!(percentile_of(&[7.0], 0.9), 7.0);
    }

    #[test]
    fn parses_open_meteo_timestamps_as_utc() {
        assert_eq!(epoch_from_iso("1970-01-01T00:00"), Some(0));
        assert_eq!(epoch_from_iso("2026-08-25T12:00"), Some(1_787_659_200));
        assert_eq!(epoch_from_iso("not a time"), None);
    }

    #[test]
    fn reduces_members_to_percentiles() {
        let payload = json!({
            "hourly": {
                "time": ["2026-08-25T12:00"],
                "temperature_2m": [10.0],
                "temperature_2m_member01": [20.0],
                "temperature_2m_member02": [30.0],
            }
        });
        let blocks = conditions_blocks(&payload);
        let series = blocks["temperature"]["series"].as_array().expect("a series");
        assert_eq!(series.len(), 1);
        assert_eq!(series[0]["median"], json!(20.0));
        assert_eq!(series[0]["t"], json!(1_787_659_200i64));
        // Only variables that are present get a block.
        assert!(!blocks.contains_key("wind"));
    }

    /// A sampled coordinate's series: the neighbourhood median is the line,
    /// with the field beside it. Deliberately not `median` — nothing a
    /// coordinate publishes carries that key, and a helper that invented one
    /// would test a document this server never serves.
    fn rain_series(rates: &[(i64, f64)]) -> Vec<Value> {
        rates
            .iter()
            .map(|(t, mmh)| json!({
                "t": t, "kind": "forecast", "field": mmh,
                "nearby_p10": 0.0, "nearby_median": mmh, "nearby_p90": mmh + 1.0,
            }))
            .collect()
    }

    /// The manifest a sampled series comes with, spread layer and all.
    fn manifest_with_spread() -> Value {
        json!({
            "reference_time": 0,
            "source": { "reducer": "probability-matched mean" },
            "spread": {
                "radius_km": 2.5, "percentiles": [10, 50, 90], "channels": ["r", "g", "b"],
                "encoding": "log8", "floor_mm_h": 0.1, "max_mm_h": 100.0, "shape": "square",
            },
        })
    }

    fn summary_of(series: &[Value]) -> Value {
        summarise(series, &centre_keys(series, true), 0)
    }

    #[test]
    fn says_when_it_is_staying_dry() {
        let series = rain_series(&[(0, 0.0), (300, 0.0), (600, 0.02)]);
        let summary = summary_of(&series);
        assert_eq!(summary["text"], json!("Staying dry."));
        assert_eq!(summary["raining_now"], json!(false));
    }

    #[test]
    fn describes_a_shower_that_has_not_started() {
        // Dry now, rain in 20 minutes, peaking, then clearing.
        let series = rain_series(&[
            (0, 0.0),
            (600, 0.0),
            (1200, 0.3),
            (1500, 2.0),
            (1800, 0.0),
        ]);
        let summary = summary_of(&series);
        assert_eq!(summary["raining_now"], json!(false));
        assert_eq!(summary["starts_at"], json!(1200));
        assert_eq!(summary["stops_at"], json!(1800));
        assert_eq!(summary["peak_mm_h"], json!(2.0));

        let text = summary["text"].as_str().expect("a sentence");
        assert!(text.starts_with("Dry now — rain in 20 min"), "{text}");
        assert!(text.contains("up to 2 mm/h within 5 min"), "{text}");
        assert!(text.contains("easing off around"), "{text}");
    }

    #[test]
    fn describes_rain_already_falling() {
        let series = rain_series(&[(0, 1.5), (300, 1.6), (600, 0.0)]);
        let summary = summary_of(&series);
        assert_eq!(summary["raining_now"], json!(true));
        assert_eq!(summary["starts_at"], Value::Null);
        assert!(summary["text"]
            .as_str()
            .expect("a sentence")
            .starts_with("Raining now at 1.5 mm/h"));
    }

    /// A spell that never ends must not claim a clearing time it does not have.
    #[test]
    fn admits_when_rain_outlasts_the_forecast() {
        let series = rain_series(&[(0, 2.0), (300, 2.0)]);
        let summary = summary_of(&series);
        assert_eq!(summary["stops_at"], Value::Null);
        assert!(summary["text"]
            .as_str()
            .expect("a sentence")
            .contains("lasting past the end of the forecast"));
    }

    /// The peak clause is suppressed when it would restate the onset.
    #[test]
    fn does_not_quote_the_peak_twice() {
        let series = rain_series(&[(0, 0.0), (600, 1.0), (900, 1.1), (1200, 0.0)]);
        let text = summary_of(&series)["text"].as_str().expect("a sentence").to_string();
        assert!(text.contains("rain in 10 min at 1 mm/h"), "{text}");
        assert!(!text.contains("up to"), "{text}");
    }

    #[test]
    fn a_second_shower_does_not_lend_its_severity_to_the_first() {
        // 0.3 mm/h now, dry, then a downpour two hours out: the sentence is
        // about the spell in progress, not the worst thing on the horizon.
        let series = rain_series(&[(0, 0.3), (300, 0.0), (7200, 12.0)]);
        let summary = summary_of(&series);
        assert_eq!(summary["peak_mm_h"], json!(0.3));
    }

    #[test]
    fn formats_rates_like_the_frontend() {
        assert_eq!(format_rate(0.25), "0.25");
        assert_eq!(format_rate(0.5), "0.5");
        assert_eq!(format_rate(1.0), "1");
        assert_eq!(format_rate(2.5), "2.5");
        assert_eq!(format_rate(12.4), "12");
    }

    #[test]
    fn out_of_coverage_is_stated_not_implied() {
        let manifest = json!({ "reference_time": 1000, "source": { "attribution": "KNMI" } });
        let document = assemble(&manifest, 40.0, -3.0, Vec::new(), None, 1200);
        assert_eq!(document["out_of_coverage"], json!(true));
        assert!(document.get("precipitation").is_none());
        assert_eq!(document["location"]["ad_hoc"], json!(true));
        assert_eq!(document["summary"]["raining_now"], json!(false));
    }

    /// What a coordinate's block claims about itself: read off frames, banded
    /// over a radius, and drawn as something the manifest can name.
    #[test]
    fn says_what_a_sampled_series_is_drawn_from() {
        let document = assemble(
            &manifest_with_spread(), 52.0, 5.0, rain_series(&[(0, 0.0)]), None, 0,
        );
        let block = &document["precipitation"];

        assert_eq!(block["frame_only"], json!(false), "there is a spread layer");
        assert_eq!(block["band_radius_km"], json!(2.5));
        assert_eq!(block["nearby_radius_km"], json!(2.5));
        assert_eq!(block["field_product"], json!("probability-matched mean"));
        assert!(block.get("median_only").is_none(), "that flag is gone with the median");
        assert!(document.get("out_of_coverage").is_none());
    }

    /// With the layer switched off there is no band, and the document says so
    /// rather than leaving a reader to infer it from missing keys.
    #[test]
    fn admits_when_there_is_no_spread_layer() {
        let manifest = json!({ "reference_time": 0, "source": {}, "spread": Value::Null });
        let series = vec![json!({ "t": 0, "kind": "forecast", "field": 0.0 })];
        let document = assemble(&manifest, 52.0, 5.0, series, None, 0);
        let block = &document["precipitation"];

        assert_eq!(block["frame_only"], json!(true));
        assert!(block.get("band_radius_km").is_none());
        assert!(block.get("nearby_radius_km").is_none());
    }

    /// The three percentiles the manifest names, at the scale it names, in the
    /// order it packed them. Checked against `encode._to_log_byte`'s own
    /// numbers: byte 1 is the floor, byte 255 the ceiling, and the middle of
    /// the range is the geometric middle rather than the arithmetic one.
    #[test]
    fn undoes_the_spread_encoding() {
        let spread = Spread::from_manifest(&manifest_with_spread()).expect("a spread block");

        assert_eq!(spread.rate(0), 0.0, "dry stays dry, rather than becoming the floor");
        assert!((spread.rate(1) - 0.1).abs() < 1e-9);
        assert!((spread.rate(255) - 100.0).abs() < 1e-6);
        // Half way up a *log* scale is the geometric middle of 0.1 and 100,
        // which is 3.16 mm/h and not 50. Asserted because reading the scale as
        // linear is the mistake that would put light drizzle in the red.
        assert!((spread.rate(128) - 3.162).abs() < 0.005, "{}", spread.rate(128));
    }

    #[test]
    fn reads_no_spread_from_a_manifest_that_has_none() {
        assert!(Spread::from_manifest(&json!({ "frames": [] })).is_none());
        // The layer switched off is published as an explicit null.
        assert!(Spread::from_manifest(&json!({ "spread": Value::Null })).is_none());
        // A scale that is not a scale: the logarithm below would be zero or NaN.
        assert!(Spread::from_manifest(&json!({
            "spread": { "percentiles": [10, 50, 90], "floor_mm_h": 0.0, "max_mm_h": 100.0 },
        }))
        .is_none());
        // Three channels carry three percentiles; anything else is unreadable.
        assert!(Spread::from_manifest(&json!({
            "spread": { "percentiles": [10, 90], "floor_mm_h": 0.1, "max_mm_h": 100.0 },
        }))
        .is_none());
    }

    /// The chain is what lets one sentence describe a series that is measured
    /// for an hour and modelled after it.
    #[test]
    fn falls_through_to_the_best_key_each_step_has() {
        let series = vec![
            json!({ "t": 0, "kind": "observed", "measured": 1.0 }),
            json!({ "t": 300, "kind": "forecast", "field": 2.0, "nearby_median": 3.0 }),
        ];
        assert_eq!(
            centre_keys(&series, true),
            vec!["nearby_median", "measured", "field"],
        );
        // No band published: the neighbourhood median is not offered even
        // though an entry happens to carry one.
        assert_eq!(centre_keys(&series, false), vec!["measured", "field"]);

        let keys = centre_keys(&series, true);
        assert_eq!(centre_value(&series[0], &keys), Some(1.0), "measured, not the band");
        assert_eq!(centre_value(&series[1], &keys), Some(3.0), "the band's own line");
    }

    /// A named location's document is drawn through the same code, and it is
    /// the one document that really does carry a `median` of its members.
    #[test]
    fn still_reads_a_named_locations_median() {
        let series = vec![json!({ "t": 0, "median": 1.0, "p10": 0.5, "p90": 2.0 })];
        assert_eq!(centre_keys(&series, false), vec!["median"]);
    }

    /// A step with no value at all is not a dry step. It drops out of the
    /// sentence rather than ending a spell that is still going.
    #[test]
    fn ignores_a_step_that_carries_no_value() {
        let series = vec![
            json!({ "t": 0, "kind": "forecast", "field": 2.0 }),
            json!({ "t": 300, "kind": "forecast" }),
            json!({ "t": 600, "kind": "forecast", "field": 2.0 }),
        ];
        let summary = summarise(&series, &centre_keys(&series, false), 0);
        assert_eq!(summary["raining_now"], json!(true));
        assert_eq!(
            summary["stops_at"],
            Value::Null,
            "the gap is a step with nothing in it, not a dry one",
        );
    }

    /// The hourly outlook exists to cover the hours past KNMI's horizon; the
    /// overlap would otherwise draw a second, coarser rain series on top of the
    /// first and disagree with it.
    #[test]
    fn trims_the_outlook_to_where_knmi_stops() {
        let manifest = json!({ "reference_time": 0, "source": {} });
        let conditions = json!({
            "hourly": {
                "time": ["1970-01-01T00:00", "1970-01-01T01:00", "1970-01-01T02:00"],
                "precipitation": [0.0, 1.0, 2.0],
            }
        });
        let rain = rain_series(&[(0, 0.0), (3600, 0.0)]);
        let document = assemble(&manifest, 52.0, 5.0, rain, Some(&conditions), 0);

        let outlook = document["precipitation_outlook"]["series"]
            .as_array()
            .expect("an outlook");
        assert_eq!(outlook.len(), 1, "only the hour past KNMI's last step");
        assert_eq!(outlook[0]["t"], json!(7200));
    }

    /// A new manifest must invalidate the answer, and a coordinate a kilometre
    /// away must share it — the two halves of why this cache is keyed the way
    /// it is.
    #[test]
    fn caches_documents_until_the_manifest_moves() {
        let cache = DocumentCache::new();
        let body = Arc::new(b"{}".to_vec());
        cache.insert(52.3791, 4.9003, 1000, Arc::clone(&body));

        assert!(cache.get(52.3791, 4.9003, 1000).is_some());
        assert!(
            cache.get(52.3788, 4.9001, 1000).is_some(),
            "the same rounded kilometre is the same question"
        );
        assert!(
            cache.get(52.3791, 4.9003, 1001).is_none(),
            "a republished manifest is a different answer"
        );
        assert!(cache.get(51.0, 4.9003, 1000).is_none());
    }

    #[test]
    fn decodes_what_the_ingestor_encodes() {
        // A 2x1 frame: one dry pixel, one at half of full scale.
        let mut image = image::RgbaImage::new(2, 1);
        image.put_pixel(0, 0, image::Rgba([0, 0, 0, 0]));
        image.put_pixel(1, 0, image::Rgba([0x80, 0x00, 0, 255]));
        let mut bytes = std::io::Cursor::new(Vec::new());
        image
            .write_to(&mut bytes, image::ImageFormat::WebP)
            .expect("encodes");

        let frame = decode(bytes.get_ref()).expect("decodes");
        assert_eq!(frame.at(0, 0), Some(Reading::Rate(0)));
        assert_eq!(frame.at(1, 0), Some(Reading::Rate(0x8000)));
        assert_eq!(frame.at(5, 0), None, "outside the frame");
    }

    /// The flag that says no radar looked here, which is not the same as dry
    /// and must not be read as a rate. Blue is the flag's channel, so a pixel
    /// carrying it would otherwise decode as whatever red and green held.
    #[test]
    fn reads_the_no_data_flag_rather_than_a_rate() {
        let mut image = image::RgbaImage::new(2, 1);
        image.put_pixel(0, 0, image::Rgba([0x40, 0x00, NO_DATA_FLAG, 255]));
        image.put_pixel(1, 0, image::Rgba([0x40, 0x00, 0, 255]));
        let mut bytes = std::io::Cursor::new(Vec::new());
        image
            .write_to(&mut bytes, image::ImageFormat::WebP)
            .expect("encodes");

        let frame = decode(bytes.get_ref()).expect("decodes");
        assert_eq!(frame.at(0, 0), Some(Reading::NoData));
        assert_eq!(frame.at(1, 0), Some(Reading::Rate(0x4000)));
    }

    /// Three bytes, three rates, in the order the manifest packed them.
    #[test]
    fn samples_three_percentiles_from_one_spread_frame() {
        let mut image = image::RgbaImage::new(1, 1);
        image.put_pixel(0, 0, image::Rgba([0, 128, 255, 255]));
        let mut bytes = std::io::Cursor::new(Vec::new());
        image
            .write_to(&mut bytes, image::ImageFormat::WebP)
            .expect("encodes");

        let decoded = decode_spread(bytes.get_ref()).expect("decodes");
        let levels = decoded.at(0, 0).expect("inside the frame");
        assert_eq!(levels, [0, 128, 255]);

        let spread = Spread::from_manifest(&manifest_with_spread()).expect("a spread block");
        let rates: Vec<f64> = levels.iter().map(|level| spread.rate(*level)).collect();
        assert_eq!(rates[0], 0.0, "p10 dry");
        assert!(rates[1] < rates[2], "the band widens upwards");
        assert!((rates[2] - 100.0).abs() < 1e-6, "p90 at the ceiling");
    }
}
