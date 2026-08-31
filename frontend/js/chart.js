/**
 * Band charts for the point forecast.
 *
 * Every series the API publishes is a set of percentiles rather than a single
 * line, so the chart's job is to show the spread as prominently as the median:
 * a wide p10–p90 band means the ensemble disagrees, and a line alone would hide
 * that. Two nested bands (p10–p90 outer, p25–p75 inner) plus the median read as
 * "likely range" and "very likely range" without needing a legend.
 *
 * SVG rather than canvas: crisp at any zoom with no devicePixelRatio handling,
 * and the elements are inspectable, which matters for a page whose whole point
 * is reading values off a graph.
 */

import { formatClock, formatDayClock, formatWeekday } from './time.js';

const NS = 'http://www.w3.org/2000/svg';

function element(name, attributes = {}) {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attributes)) {
        node.setAttribute(key, String(value));
    }
    return node;
}

/** Choose a round axis step that yields roughly `target` gridlines. */
function niceStep(range, target = 4) {
    if (!(range > 0)) return 1;
    const rough = range / target;
    const magnitude = 10 ** Math.floor(Math.log10(rough));
    const normalised = rough / magnitude;
    const step = normalised >= 5 ? 10 : normalised >= 2 ? 5 : normalised >= 1 ? 2 : 1;
    return step * magnitude;
}

/**
 * Which of a chain's keys an entry's line comes from, or undefined if it
 * carries none of them.
 *
 * Absent is resolved to absent rather than to a number. Reading a key straight
 * off an entry gave `undefined` for a step that did not have it, and undefined
 * arrives at the axis as NaN: one such step took `Math.max` for the whole
 * series with it, so the axis had no range, every coordinate was NaN and the
 * chart went blank — the forecast half that did have the key included.
 */
export function centreKeyFor(entry, centreKeys) {
    return centreKeys.find((key) => entry[key] != null);
}

/** The value a chart draws for one entry, or null where it draws nothing. */
export function centreValue(entry, centreKeys) {
    const key = centreKeyFor(entry, centreKeys);
    return key === undefined ? null : entry[key];
}

/**
 * Render one series into a container.
 *
 * `series` entries are `{t, p10, p25, median, p75, p90}`. `options.zeroFloor`
 * pins the axis at zero, which is right for rain and solar (a band dipping
 * below zero would be nonsense) but wrong for temperature, where the interesting
 * variation is a few degrees somewhere well above it.
 *
 * `options.centreKeys` names the entry fields the line may come from, best
 * first, because it is not always a median and calling it one in the code is how
 * it ends up being called one to a reader. A temperature series really is a
 * median of its members; a rain series is drawn as the neighbourhood median the
 * map's spread layer carries. `options.centreLabels` names them in the tooltip,
 * one label per key, so the tooltip says which of them the number under the
 * pointer actually came from.
 *
 * More than one key because a series need not be homogeneous. A clicked point's
 * rain series is an hour of observed radar in front of the forecast, and a
 * measurement has no ensemble behind it, so those steps carry no neighbourhood
 * median and the chain falls through to the measured value for them alone.
 */
/** Floor for a chart with nothing to stretch into — a narrow single column. */
const MIN_CHART_HEIGHT = 190;

export function renderBandChart(container, series, options = {}) {
    const {
        unit = '',
        colour = '#3b82f6',
        zeroFloor = false,
        height: fixedHeight = null,
        formatValue = (value) => String(value),
        now = null,
        minSpan = 1,
        centreKeys = ['median'],
        centreLabels = [''],
        // Outer band first, inner second. Named rather than assumed, because a
        // line and a band drawn from different kinds of number look unrelated:
        // a neighbourhood median rises when rain arrives near you, a per-cell
        // p10–p90 rises when it arrives *on* you, and plotting one inside the
        // other put the peak of each an hour from the other.
        // Labelled here rather than at the tooltip: a shared fallback label
        // named both of these bands "80% of members", so the inner one
        // claimed to be the outer one with narrower numbers.
        bands = [['p10', 'p90', 0.16, '80% of the members'],
                 ['p25', 'p75', 0.26, '50% of the members']],
    } = options;
    const centre = (entry) => centreValue(entry, centreKeys);
    const centreLabel = (entry) => {
        const index = centreKeys.indexOf(centreKeyFor(entry, centreKeys));
        return index === -1 ? '' : centreLabels[index] ?? '';
    };

    container.innerHTML = '';
    if (!series || series.length < 2) {
        const empty = document.createElement('p');
        empty.className = 'chart-empty';
        empty.textContent = 'No data for this location.';
        container.appendChild(empty);
        return;
    }

    // Height comes from the container for the same reason the width does: the
    // cards are grid items that stretch to their row, and a fixed 190 left the
    // chart beside the radar floating in 200px of empty card. Measured after
    // the container is emptied, and the container is `flex: 1` so its height is
    // the leftover space in the card rather than a function of its own content
    // — otherwise this would feed back on itself and never settle.
    const height = fixedHeight ?? Math.max(MIN_CHART_HEIGHT, Math.round(container.clientHeight) || MIN_CHART_HEIGHT);
    const width = Math.max(240, container.clientWidth || 480);
    const pad = { top: 10, right: 8, bottom: 22, left: 42 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;

    // The centre has to be inside the axis, not merely near it. It used to be
    // safe to derive the range from the band alone, back when the line was that
    // band's own median and could not leave it. A rain chart draws the field
    // instead, which is a whole-domain reconstruction and can sit well above
    // p90 — 141 mm/h against a p90 of 3.1 where a shower only a fifth of the
    // members forecast lands on one cell — and the line simply left the plot.
    //
    // Collected rather than reduced per entry, so a step missing the centre or
    // the band contributes nothing instead of poisoning the pair it is in:
    // `Math.min(Infinity, undefined)` is NaN, and one NaN in here is a chart
    // with no axis at all.
    const [outerLow, outerHigh] = bands[0] ?? [];
    const lows = [];
    const highs = [];
    for (const entry of series) {
        const value = centre(entry);
        if (value !== null) {
            lows.push(value);
            highs.push(value);
        }
        if (entry[outerLow] != null) lows.push(entry[outerLow]);
        if (entry[outerHigh] != null) highs.push(entry[outerHigh]);
    }
    if (!lows.length) {
        const empty = document.createElement('p');
        empty.className = 'chart-empty';
        empty.textContent = 'No data for this location.';
        container.appendChild(empty);
        return;
    }
    let min = zeroFloor ? 0 : Math.min(...lows);
    let max = Math.max(...highs);
    // A flat series - a dry night, darkness - would otherwise collapse the axis
    // to a hair's breadth, and every gridline would round to the same label.
    // `minSpan` is the smallest range worth drawing in the series' own units.
    if (max - min < minSpan) max = min + minSpan;
    const step = niceStep(max - min);
    min = zeroFloor ? 0 : Math.floor(min / step) * step;
    max = Math.ceil(max / step) * step;

    const firstTime = series[0].t;
    const span = series[series.length - 1].t - firstTime || 1;
    const x = (t) => pad.left + ((t - firstTime) / span) * plotWidth;
    const y = (value) => pad.top + plotHeight - ((value - min) / (max - min)) * plotHeight;

    const svg = element('svg', {
        viewBox: `0 0 ${width} ${height}`,
        width: '100%',
        height,
        role: 'img',
        preserveAspectRatio: 'none',
    });

    // Gridlines and value labels.
    for (let value = min; value <= max + 1e-9; value += step) {
        svg.appendChild(element('line', {
            x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value), class: 'chart-grid',
        }));
        const label = element('text', { x: pad.left - 6, y: y(value) + 3, class: 'chart-axis' });
        label.textContent = formatValue(value);
        svg.appendChild(label);
    }

    // One polygon per unbroken run that has the pair, rather than one for the
    // whole series. Skipped where it is missing, because a band faked from
    // absent keys collapses onto the line and reads as certainty — but skipped
    // per step, not wholesale. A clicked point's series carries an hour of
    // observed radar in front of the forecast, and a measurement has no
    // ensemble behind it, so those thirteen steps have no band; dropping the
    // whole thing over them left every clicked point with a bare line and no
    // spread anywhere, which is how this was found.
    const band = (lowKey, highKey, opacity) => {
        let run = [];
        const flush = () => {
            if (run.length > 1) {
                const top = run.map((entry) => `${x(entry.t)},${y(entry[highKey])}`);
                const bottom = run.map((entry) => `${x(entry.t)},${y(entry[lowKey])}`).reverse();
                svg.appendChild(element('polygon', {
                    points: [...top, ...bottom].join(' '),
                    fill: colour,
                    'fill-opacity': opacity,
                }));
            }
            run = [];
        };
        for (const entry of series) {
            if (entry[lowKey] == null || entry[highKey] == null) flush();
            else run.push(entry);
        }
        flush();
    };
    for (const [low, high, opacity] of bands) band(low, high, opacity);

    // One polyline per unbroken run that has a centre, on the same rule as the
    // bands above: a step carrying none of the keys is a hole in the line, not a
    // point at zero. With the chain in place a hole means the series really has
    // nothing there, which is rare — but it used to render as NaN coordinates,
    // and an SVG polyline with a NaN in its points draws nothing at all.
    let line = [];
    const flushLine = () => {
        if (line.length > 1) {
            svg.appendChild(element('polyline', {
                points: line.map((entry) => `${x(entry.t)},${y(centre(entry))}`).join(' '),
                fill: 'none',
                stroke: colour,
                'stroke-width': 2,
                'stroke-linejoin': 'round',
            }));
        }
        line = [];
    };
    for (const entry of series) {
        if (centre(entry) === null) flushLine();
        else line.push(entry);
    }
    flushLine();

    // Steps the ingestor stood in for. KNMI publishes a timestep with no
    // ensemble behind it about once a cycle, and what is plotted there is the
    // average of the five minutes either side rather than anything forecast.
    // Marked rather than broken out of the line: a gap in a rain chart reads as
    // a dry spell, which is the very thing the stand-in exists to avoid.
    for (const entry of series) {
        if (!entry.estimated) continue;
        const mark = element('line', {
            x1: x(entry.t), x2: x(entry.t),
            y1: pad.top, y2: pad.top + plotHeight,
            class: 'chart-estimated',
        });
        const caption = element('title');
        caption.textContent = 'Estimated from the steps either side — KNMI '
            + 'published no ensemble for this one.';
        mark.appendChild(caption);
        svg.appendChild(mark);
    }

    // Where "now" falls, so past and future are distinguishable at a glance.
    if (now !== null && now > firstTime && now < series[series.length - 1].t) {
        svg.appendChild(element('line', {
            x1: x(now), x2: x(now), y1: pad.top, y2: pad.top + plotHeight, class: 'chart-now',
        }));
    }

    // Time axis: a label every few points, thinned to whatever fits.
    //
    // Past about a day, a bare clock time is ambiguous — "19:00" could be either
    // end of a two-day forecast — so the weekday is shown whenever the label
    // crosses into a new day, and a divider is drawn at midnight.
    const multiDay = span > 18 * 3600;
    const minGap = multiDay ? 74 : 58; // room for "Wed 00:00" vs just "00:00"

    // Midnights come first: they anchor a multi-day axis, and their dividers are
    // drawn whether or not the label survives thinning.
    const dayStarts = [];
    let previousDay = null;
    series.forEach((entry, index) => {
        const day = new Date(entry.t * 1000).toDateString();
        if (previousDay !== null && day !== previousDay) {
            dayStarts.push(index);
            if (multiDay) {
                svg.appendChild(element('line', {
                    x1: x(entry.t), x2: x(entry.t),
                    y1: pad.top, y2: pad.top + plotHeight,
                    class: 'chart-daybreak',
                }));
            }
        }
        previousDay = day;
    });

    // Candidates in x order, day boundaries taking precedence over the regular
    // cadence, then dropped if they would land on top of the previous label.
    const anchors = multiDay ? new Set([0, ...dayStarts]) : new Set([0]);
    const everyNth = Math.max(1, Math.round(series.length / Math.floor(plotWidth / minGap)));
    const candidates = series
        .map((entry, index) => ({ entry, index }))
        .filter(({ index }) => anchors.has(index) || index % everyNth === 0)
        .sort((a, b) => (anchors.has(b.index) ? 1 : 0) - (anchors.has(a.index) ? 1 : 0));

    const placed = [];
    for (const { entry, index } of candidates) {
        const position = x(entry.t);
        if (placed.some((other) => Math.abs(other - position) < minGap)) continue;
        placed.push(position);

        const clock = formatClock(entry.t);
        const label = element('text', {
            x: position, y: height - 7, class: 'chart-axis chart-axis--time',
        });
        label.textContent = multiDay && anchors.has(index)
            ? `${formatWeekday(entry.t)} ${clock}`
            : clock;
        svg.appendChild(label);
    }

    container.appendChild(svg);
    if (unit) {
        const caption = document.createElement('span');
        caption.className = 'chart-unit';
        caption.textContent = unit;
        container.appendChild(caption);
    }

    attachHover({
        container, svg, series, unit, colour, formatValue,
        width, padLeft: pad.left, plotWidth, firstTime, span, x, y,
        centre, centreLabel, bands,
    });
}

/**
 * Crosshair and tooltip reporting the value *and its spread* under the pointer.
 *
 * Reading a band off an axis gives you the median easily enough; the useful
 * numbers — how far apart the members are at that moment — are the ones you
 * cannot eyeball. So the tooltip leads with the median and then names each
 * band it actually has explicitly.
 */
/** Tooltip offset from the crosshair, and from the edges of the card. */
const TIP_GAP = 10;
const TIP_EDGE = 6;

function attachHover({ container, svg, series, unit, colour, formatValue,
                       width, padLeft, plotWidth, firstTime, span, x, y,
                       centre, centreLabel, bands }) {
    // `centreLabel` is a function of the entry here, not a string.
    const crosshair = element('line', { class: 'chart-crosshair', y1: 0, y2: 0, x1: 0, x2: 0 });
    crosshair.style.display = 'none';
    svg.appendChild(crosshair);

    const marker = element('circle', { r: 3.5, fill: colour, stroke: '#0a0c11', 'stroke-width': 1.5 });
    marker.style.display = 'none';
    svg.appendChild(marker);

    const tooltip = document.createElement('div');
    tooltip.className = 'chart-tooltip';
    tooltip.hidden = true;
    container.appendChild(tooltip);

    const plotTop = Number(svg.querySelector('polygon') ? 10 : 10);

    const show = (event) => {
        const rect = svg.getBoundingClientRect();
        if (!rect.width) return;
        const viewX = ((event.clientX - rect.left) / rect.width) * width;
        const clamped = Math.min(Math.max(viewX, padLeft), padLeft + plotWidth);
        const target = firstTime + ((clamped - padLeft) / plotWidth) * span;

        let entry = series[0];
        for (const candidate of series) {
            if (Math.abs(candidate.t - target) < Math.abs(entry.t - target)) entry = candidate;
        }

        const px = x(entry.t);
        crosshair.setAttribute('x1', px);
        crosshair.setAttribute('x2', px);
        crosshair.setAttribute('y1', plotTop);
        crosshair.setAttribute('y2', svg.viewBox.baseVal.height - 22);
        crosshair.style.display = '';
        marker.setAttribute('cx', px);
        // Hidden rather than parked at NaN where this step has no line: the
        // crosshair and the readings below it are still worth having.
        const value = centre(entry);
        marker.setAttribute('cy', value === null ? 0 : y(value));
        marker.style.display = value === null ? 'none' : '';

        const stamp = span > 18 * 3600 ? formatDayClock(entry.t) : formatClock(entry.t);
        // The axis formatter is deliberately coarse; reusing it here would round
        // a sub-degree spread away and show both bands as the same numbers.
        const precise = (value) =>
            Math.abs(value) < 100 ? (Math.round(value * 10) / 10).toFixed(1) : String(Math.round(value));
        const range = (low, high) =>
            (precise(low) === precise(high) ? precise(low) : `${precise(low)}–${precise(high)}`);
        // A row per band the series actually carries, and nothing where it does
        // not. A point read off the map frames has p10, the median and p90 and
        // no quartiles — three channels is three percentiles — and naming a
        // band that is not there printed "50% of members NaN–NaN". Same rule as
        // the polygons above: absent is drawn as absent, never as a value.
        const bandRow = (label, low, high) =>
            (low == null || high == null
                ? ''
                : `<span class="tip-band"><i>${label}</i>${range(low, high)}</span>`);

        // The label follows the value: which key supplied it varies along a
        // mixed series, and naming the neighbourhood median over a measured
        // step would describe a number that is not there.
        const centreName = centreLabel(entry);
        tooltip.innerHTML =
            `<span class="tip-time">${stamp}</span>` +
            `<span class="tip-value">${value === null ? '—' : precise(value)}` +
            `<small>${unit}</small></span>` +
            (value !== null && centreName
                ? `<span class="tip-centre">${centreName}</span>` : '') +
            bands.map(([low, high, , label]) =>
                bandRow(label ?? '', entry[low], entry[high])).join('') +
            (entry.probability !== undefined
                ? `<span class="tip-band"><i>chance of rain</i>${Math.round(entry.probability * 100)}%</span>`
                : '');
        tooltip.hidden = false;

        // Right of the crosshair by preference, flipped to its left where that
        // would run past the card — and then clamped inside the card either
        // way. Flipping alone is only enough while the card has room for the
        // tooltip on both sides of the pointer; on a phone the card is barely
        // wider than the tooltip, so a flip near the middle of the plot put its
        // left edge at a negative offset and half the numbers off the screen.
        const cardWidth = container.clientWidth || rect.width;
        const tipWidth = tooltip.offsetWidth;
        const leftPx = (px / width) * rect.width;
        let left = leftPx + TIP_GAP;
        if (left + tipWidth > cardWidth - TIP_EDGE) left = leftPx - TIP_GAP - tipWidth;
        // Lower bound last: where the tooltip is wider than the space either
        // side of the pointer it sits against the left edge and overlaps the
        // crosshair, which is still readable, unlike hanging off the card.
        left = Math.max(TIP_EDGE, Math.min(left, cardWidth - tipWidth - TIP_EDGE));
        tooltip.style.left = `${left}px`;
    };

    const hide = () => {
        tooltip.hidden = true;
        crosshair.style.display = 'none';
        marker.style.display = 'none';
    };

    svg.addEventListener('pointermove', show);
    svg.addEventListener('pointerdown', show);
    svg.addEventListener('pointerleave', hide);
}
