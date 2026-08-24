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
 * Render one series into a container.
 *
 * `series` entries are `{t, p10, p25, median, p75, p90}`. `options.zeroFloor`
 * pins the axis at zero, which is right for rain and solar (a band dipping
 * below zero would be nonsense) but wrong for temperature, where the interesting
 * variation is a few degrees somewhere well above it.
 */
export function renderBandChart(container, series, options = {}) {
    const {
        unit = '',
        colour = '#3b82f6',
        zeroFloor = false,
        height = 190,
        formatValue = (value) => String(value),
        now = null,
        minSpan = 1,
    } = options;

    container.innerHTML = '';
    if (!series || series.length < 2) {
        const empty = document.createElement('p');
        empty.className = 'chart-empty';
        empty.textContent = 'No data for this location.';
        container.appendChild(empty);
        return;
    }

    const width = Math.max(240, container.clientWidth || 480);
    const pad = { top: 10, right: 8, bottom: 22, left: 42 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;

    const lows = series.map((entry) => entry.p10);
    const highs = series.map((entry) => entry.p90);
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

    const band = (lowKey, highKey, opacity) => {
        const top = series.map((entry) => `${x(entry.t)},${y(entry[highKey])}`);
        const bottom = series.map((entry) => `${x(entry.t)},${y(entry[lowKey])}`).reverse();
        svg.appendChild(element('polygon', {
            points: [...top, ...bottom].join(' '),
            fill: colour,
            'fill-opacity': opacity,
        }));
    };
    band('p10', 'p90', 0.16);
    band('p25', 'p75', 0.26);

    svg.appendChild(element('polyline', {
        points: series.map((entry) => `${x(entry.t)},${y(entry.median)}`).join(' '),
        fill: 'none',
        stroke: colour,
        'stroke-width': 2,
        'stroke-linejoin': 'round',
    }));

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

        const date = new Date(entry.t * 1000);
        const clock = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const label = element('text', {
            x: position, y: height - 7, class: 'chart-axis chart-axis--time',
        });
        label.textContent = multiDay && anchors.has(index)
            ? `${date.toLocaleDateString([], { weekday: 'short' })} ${clock}`
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
    });
}

/**
 * Crosshair and tooltip reporting the value *and its spread* under the pointer.
 *
 * Reading a band off an axis gives you the median easily enough; the useful
 * numbers — how far apart the members are at that moment — are the ones you
 * cannot eyeball. So the tooltip leads with the median and then names both
 * bands explicitly.
 */
function attachHover({ container, svg, series, unit, colour, formatValue,
                       width, padLeft, plotWidth, firstTime, span, x, y }) {
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
        marker.setAttribute('cy', y(entry.median));
        marker.style.display = '';

        const when = new Date(entry.t * 1000);
        const stamp = span > 18 * 3600
            ? when.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })
            : when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        // The axis formatter is deliberately coarse; reusing it here would round
        // a sub-degree spread away and show both bands as the same numbers.
        const precise = (value) =>
            Math.abs(value) < 100 ? (Math.round(value * 10) / 10).toFixed(1) : String(Math.round(value));
        const range = (low, high) =>
            (precise(low) === precise(high) ? precise(low) : `${precise(low)}–${precise(high)}`);

        tooltip.innerHTML =
            `<span class="tip-time">${stamp}</span>` +
            `<span class="tip-value">${precise(entry.median)}<small>${unit}</small></span>` +
            `<span class="tip-band"><i>50% of members</i>${range(entry.p25, entry.p75)}</span>` +
            `<span class="tip-band"><i>80% of members</i>${range(entry.p10, entry.p90)}</span>` +
            (entry.probability !== undefined
                ? `<span class="tip-band"><i>chance of rain</i>${Math.round(entry.probability * 100)}%</span>`
                : '');
        tooltip.hidden = false;

        // Flip to the other side of the crosshair near the right edge so the
        // tooltip never leaves the card.
        const leftPx = (px / width) * rect.width;
        const flip = leftPx > rect.width - tooltip.offsetWidth - 16;
        tooltip.style.left = `${flip ? leftPx - tooltip.offsetWidth - 10 : leftPx + 10}px`;
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
