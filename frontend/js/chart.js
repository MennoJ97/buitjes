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
    const everyNth = Math.max(1, Math.round(series.length / Math.floor(plotWidth / 58)));
    series.forEach((entry, index) => {
        if (index % everyNth !== 0) return;
        const label = element('text', {
            x: x(entry.t), y: height - 7, class: 'chart-axis chart-axis--time',
        });
        label.textContent = new Date(entry.t * 1000).toLocaleTimeString([], {
            hour: '2-digit', minute: '2-digit',
        });
        svg.appendChild(label);
    });

    container.appendChild(svg);
    if (unit) {
        const caption = document.createElement('span');
        caption.className = 'chart-unit';
        caption.textContent = unit;
        container.appendChild(caption);
    }
}
