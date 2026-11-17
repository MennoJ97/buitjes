/**
 * The accuracy page: how well past forecasts matched the radar.
 *
 * Everything comes from /api/accuracy, a summary the ingestor's verification
 * rewrites once an hour (see ingestor/verify.py, `summary`). The page only
 * draws it. The charts are hand-built SVG rather than chart.js, which draws
 * percentile bands against clock time; these are lines against lead time.
 *
 * Colours come from CSS by class (`acc-s-*`), so the theme switch recolours
 * the charts without a redraw. Persistence is a dashed neutral line, the
 * convention for a baseline, and every series is named in a legend and at
 * the end of its line, so nothing is told apart by colour alone.
 */

import { applySiteHome } from './site.js';

const NS = 'http://www.w3.org/2000/svg';

const SERIES = {
    forecast: { label: 'Forecast', cls: 'acc-s-forecast' },
    persistence: { label: 'Persistence', cls: 'acc-s-baseline', dashed: true },
    above: { label: 'Above High (p90)', short: 'Above High', cls: 'acc-s-above' },
    below: { label: 'Below Low (p10)', short: 'Below Low', cls: 'acc-s-below' },
};

/** Ticks for the lead-time axis, in minutes. */
const LEAD_TICKS = [5, 15, 30, 60, 120, 180, 360];

function el(name, attributes = {}, parent = null) {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attributes)) {
        if (value != null) node.setAttribute(key, String(value));
    }
    if (parent) parent.append(node);
    return node;
}

function html(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
}

// ------------------------------------------------------------ formatting

export function formatLead(minutes) {
    if (minutes < 60) return `${minutes} min`;
    const hours = minutes / 60;
    return `${Number.isInteger(hours) ? hours : hours.toFixed(1)} h`;
}

const score = (value) => (value == null ? '–' : value.toFixed(2));
const percent = (value, digits = 0) => (value == null ? '–' : `${(value * 100).toFixed(digits)}%`);

function formatDate(iso, options = { day: 'numeric', month: 'short' }) {
    const [year, month, day] = iso.split('-').map(Number);
    return new Date(year, month - 1, day).toLocaleDateString('en-GB', options);
}

function formatMonth(iso) {
    const [year, month] = iso.split('-').map(Number);
    return new Date(year, month - 1, 1).toLocaleDateString('en-GB', { month: 'long', year: 'numeric' });
}

// ------------------------------------------------------------ headline

/** The first lead from which the forecast stays ahead of persistence. */
export function beatsPersistenceFrom(leads) {
    let from = null;
    for (const row of leads) {
        if (row.skill25 != null && row.skill25 > 0) {
            if (from == null) from = row.lead;
        } else {
            from = null;
        }
    }
    return from;
}

function headline(data) {
    const useful = data.useful_to;
    const beats = beatsPersistenceFrom(data.leads);
    const days = Math.max(1, Math.round((data.to - data.from) / 86400));
    const span = days >= data.window_days - 1 ? `the last ${data.window_days} days` : `the last ${days} days`;
    const parts = [];
    if (useful) parts.push(`was useful up to about ${formatLead(useful)} ahead`);
    else parts.push('was not yet useful at any lead time on the 25 km scale');
    if (beats != null) {
        parts.push(beats <= 5
            ? 'beat assuming the rain stays put at every lead time'
            : `beat assuming the rain stays put from ${formatLead(beats)} ahead`);
    }
    return `Over ${span}, the forecast ${parts.join(' and ')}.`;
}

function stats(data) {
    const at = (lead) => data.leads.find((row) => row.lead === lead) || {};
    const hour = at(60);
    const beats = beatsPersistenceFrom(data.leads);
    const bias = hour.bias == null ? '–'
        : hour.bias >= 1 ? `${Math.round((hour.bias - 1) * 100)}% too much`
            : `${Math.round((1 - hour.bias) * 100)}% too little`;
    return [
        ['Useful up to', data.useful_to ? formatLead(data.useful_to) : '–', 'FSS 25 km ≥ 0.5'],
        ['Beats persistence', beats == null ? '–' : `from ${formatLead(beats)}`, 'FSS 25 km'],
        ['Rain area, +1 h', bias, 'bias at 1 mm/h'],
        ['Above High, +1 h', percent(hour.above_p90), 'honest band: 10%'],
        ['Forecasts scored', data.cycles.toLocaleString('en-GB'),
            `cycles since ${formatDate(new Date(data.from * 1000).toISOString().slice(0, 10))}`],
    ];
}

// ------------------------------------------------------------ charts

/**
 * A line chart. `xs` are numbers on a log or linear axis, or category
 * indices; each series is `{key, values}` aligned with `xs`, null for a gap.
 */
function lineChart(container, options) {
    const {
        xs, series, refs = [], yMin = 0, yMax, yFormat = score, xScale = 'log',
        xTicks, xTickLabel, tipTitle, bars = false, height: fixedHeight,
    } = options;
    container.replaceChildren();
    const width = Math.max(260, container.clientWidth);
    const height = fixedHeight || Math.max(200, Math.min(280, Math.round(width * 0.42)));
    const labelRoom = bars ? 8 : 84;
    const pad = { top: 12, right: labelRoom, bottom: 26, left: 40 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;

    const values = series.flatMap((s) => s.values).filter((v) => v != null);
    const { top, ticks } = niceAxis(yMin, yMax ?? Math.max(...values, ...refs.map((r) => r.y), 0.01));
    const axisFormat = options.axisFormat || yFormat;
    const y = (v) => pad.top + plotHeight - ((v - yMin) / (top - yMin)) * plotHeight;
    let x;
    if (xScale === 'log') {
        const [lo, hi] = [Math.log(xs[0]), Math.log(xs[xs.length - 1])];
        x = (v) => pad.left + ((Math.log(v) - lo) / (hi - lo || 1)) * plotWidth;
    } else {
        const step = plotWidth / Math.max(1, xs.length - (bars ? 0 : 1));
        x = (v) => pad.left + (bars ? (v + 0.5) * step : v * step);
    }

    const svg = el('svg', { width, height, viewBox: `0 0 ${width} ${height}` }, container);

    // Grid and y labels: recessive, four steps.
    for (const value of ticks) {
        el('line', { x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value), class: 'chart-grid' }, svg);
        el('text', { x: pad.left - 6, y: y(value) + 3, class: 'chart-axis' }, svg).textContent = axisFormat(value);
    }
    // Ticks that would crowd the one before are skipped, so a phone-width
    // chart shows fewer times rather than overprinting them.
    let lastTick = -Infinity;
    for (const tick of xTicks) {
        const position = x(tick);
        if (position - lastTick < 44) continue;
        lastTick = position;
        const label = el('text', { x: position, y: height - 8, class: 'chart-axis chart-axis--time' }, svg);
        label.textContent = xTickLabel(tick);
    }
    // Reference lines are labelled in the right margin with the series, so the
    // nudging below keeps the two kinds of label apart and neither sits on a line.
    const ends = [];
    for (const ref of refs) {
        el('line', { x1: pad.left, x2: width - pad.right, y1: y(ref.y), y2: y(ref.y), class: 'acc-ref' }, svg);
        ends.push({ y: y(ref.y), x: width - pad.right, text: ref.label, cls: 'acc-ref-label' });
    }

    if (bars) {
        const step = plotWidth / xs.length;
        const barWidth = Math.max(2, Math.min(24, step - 2));
        for (const s of series) {
            s.values.forEach((value, index) => {
                if (value == null) return;
                const barHeight = Math.max(1, y(yMin) - y(value));
                el('path', {
                    d: roundedBar(x(index) - barWidth / 2, y(value), barWidth, barHeight, Math.min(4, barWidth / 2)),
                    class: `acc-bar ${SERIES[s.key]?.cls || s.cls || ''}`,
                }, svg);
            });
        }
    } else {
        for (const s of series) {
            const meta = SERIES[s.key];
            const points = s.values.map((v, i) => (v == null ? null : [x(xs[i]), y(Math.max(yMin, Math.min(top, v)))]));
            el('path', { d: linePath(points), class: `acc-line ${meta.cls}${meta.dashed ? ' acc-line--dashed' : ''}` }, svg);
            for (const point of points) {
                if (point) el('circle', { cx: point[0], cy: point[1], r: 3.5, class: `acc-dot ${meta.cls}` }, svg);
            }
            const last = [...points].reverse().find(Boolean);
            if (last) ends.push({ y: last[1], x: last[0], text: meta.short || meta.label, cls: 'acc-end-label' });
        }
    }
    // Direct labels at the end of each line, and the reference labels beside
    // them, nudged apart so none sits on top of another.
    ends.sort((a, b) => a.y - b.y);
    for (let i = 1; i < ends.length; i += 1) {
        if (ends[i].y - ends[i - 1].y < 13) ends[i].y = ends[i - 1].y + 13;
    }
    for (const end of ends) {
        el('text', { x: end.x + 8, y: end.y + 3.5, class: end.cls }, svg).textContent = end.text;
    }

    attachHover(container, svg, {
        xs, series, x, y, pad, width, height, tipTitle, yFormat, bars,
        xPosition: (index) => (xScale === 'log' ? x(xs[index]) : x(bars ? index : xs[index])),
    });
}

/** A round step giving about four gridlines, and the top it rounds `max` up to. */
function niceAxis(lo, max) {
    const rough = (max - lo) / 4;
    const magnitude = 10 ** Math.floor(Math.log10(rough));
    const normalised = rough / magnitude;
    const step = (normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 2.5 ? 2.5 : normalised <= 5 ? 5 : 10)
        * magnitude;
    const top = lo + Math.ceil((max - lo) / step - 1e-9) * step;
    const ticks = [];
    for (let value = lo; value <= top + step / 2; value += step) ticks.push(Number(value.toPrecision(6)));
    return { top, ticks };
}

function linePath(points) {
    let d = '';
    let pen = false;
    for (const point of points) {
        if (!point) { pen = false; continue; }
        d += `${pen ? 'L' : 'M'}${point[0].toFixed(1)},${point[1].toFixed(1)}`;
        pen = true;
    }
    return d;
}

/** A bar with its data end rounded and its baseline square. */
function roundedBar(left, top, width, height, radius) {
    const r = Math.min(radius, height);
    const bottom = top + height;
    return `M${left},${bottom}V${top + r}Q${left},${top} ${left + r},${top}`
        + `H${left + width - r}Q${left + width},${top} ${left + width},${top + r}V${bottom}Z`;
}

function attachHover(container, svg, geometry) {
    const { xs, series, pad, height, tipTitle, yFormat, xPosition } = geometry;
    const crosshair = el('line', { class: 'chart-crosshair', y1: pad.top, y2: height - pad.bottom, x1: 0, x2: 0, visibility: 'hidden' }, svg);
    const tooltip = html('div', 'chart-tooltip');
    tooltip.hidden = true;
    container.append(tooltip);

    const positions = xs.map((_, index) => xPosition(index));
    const nearest = (px) => {
        let best = 0;
        for (let i = 1; i < positions.length; i += 1) {
            if (Math.abs(positions[i] - px) < Math.abs(positions[best] - px)) best = i;
        }
        return best;
    };

    const show = (event) => {
        const box = svg.getBoundingClientRect();
        const index = nearest(event.clientX - box.left);
        const px = positions[index];
        crosshair.setAttribute('x1', px);
        crosshair.setAttribute('x2', px);
        crosshair.setAttribute('visibility', 'visible');

        tooltip.replaceChildren(html('span', 'tip-time', tipTitle(index)));
        for (const s of series) {
            const meta = SERIES[s.key] || { label: s.label, cls: s.cls };
            const row = html('span', 'tip-band');
            const name = html('i', null);
            name.append(html('b', `acc-swatch ${meta.cls}${meta.dashed ? ' acc-swatch--dashed' : ''}`), meta.label || s.label);
            row.append(name, document.createTextNode(yFormat(s.values[index])));
            tooltip.append(row);
        }
        tooltip.hidden = false;
        const room = container.clientWidth - tooltip.offsetWidth - 6;
        tooltip.style.left = `${Math.max(6, Math.min(room, px + 12))}px`;
    };
    const hide = () => {
        tooltip.hidden = true;
        crosshair.setAttribute('visibility', 'hidden');
    };
    // The whole plot is the hit target, not the dots: the reader snaps to the
    // nearest lead wherever they point.
    svg.addEventListener('pointermove', show);
    svg.addEventListener('pointerdown', show);
    svg.addEventListener('pointerleave', hide);
}

// ------------------------------------------------------------ tables

function table(target, head, rows) {
    target.replaceChildren();
    const thead = html('thead');
    const tr = html('tr');
    head.forEach((label) => tr.append(html('th', null, label)));
    thead.append(tr);
    const tbody = html('tbody');
    for (const row of rows) {
        const line = html('tr');
        row.forEach((cell, index) => line.append(html(index === 0 ? 'th' : 'td', null, cell)));
        tbody.append(line);
    }
    target.append(thead, tbody);
}

// ------------------------------------------------------------ the page

function legends() {
    for (const node of document.querySelectorAll('[data-legend]')) {
        node.replaceChildren();
        for (const key of node.dataset.legend.split(' ')) {
            const meta = SERIES[key];
            const item = html('span', 'acc-legend-item');
            item.append(html('b', `acc-swatch ${meta.cls}${meta.dashed ? ' acc-swatch--dashed' : ''}`), meta.label);
            node.append(item);
        }
    }
}

function draw(data) {
    const leads = data.leads.filter((row) => row.lead > 0);
    const xs = leads.map((row) => row.lead);
    const pick = (key) => leads.map((row) => row[key]);
    const leadTicks = LEAD_TICKS.filter((tick) => tick >= xs[0] && tick <= xs[xs.length - 1]);
    const leadTitle = (index) => `+${formatLead(xs[index])}`;
    const leadAxis = { xs, xTicks: leadTicks, xTickLabel: formatLead, tipTitle: leadTitle };

    lineChart(document.getElementById('chart-fss'), {
        ...leadAxis, yMax: 1,
        series: [{ key: 'forecast', values: pick('fss25') }, { key: 'persistence', values: pick('p_fss25') }],
        refs: [{ y: data.definitions.useful_fss, label: 'useful' }],
    });
    lineChart(document.getElementById('chart-csi'), {
        ...leadAxis,
        series: [{ key: 'forecast', values: pick('csi') }, { key: 'persistence', values: pick('p_csi') }],
    });
    lineChart(document.getElementById('chart-bias'), {
        ...leadAxis, yFormat: (v) => (v == null ? '–' : v.toFixed(1)),
        series: [{ key: 'forecast', values: pick('bias') }],
        refs: [{ y: 1, label: 'right amount' }],
    });
    lineChart(document.getElementById('chart-band'), {
        ...leadAxis, yFormat: (v) => percent(v),
        series: [{ key: 'above', values: pick('above_p90') }, { key: 'below', values: pick('below_p10') }],
        refs: [{ y: 0.1, label: 'honest: 10%' }],
    });

    const days = data.daily;
    const dayIndex = days.map((_, index) => index);
    const every = Math.max(1, Math.ceil(days.length / 8));
    const dayTicks = dayIndex.filter((index) => index % every === 0);
    const dayTitle = (index) => formatDate(days[index].date, { weekday: 'short', day: 'numeric', month: 'short' });
    const dayLabel = (index) => formatDate(days[index].date);
    lineChart(document.getElementById('chart-daily'), {
        xs: dayIndex, xScale: 'linear', xTicks: dayTicks, xTickLabel: dayLabel, tipTitle: dayTitle, yMax: 1,
        series: [
            { key: 'forecast', values: days.map((day) => day.h1?.fss25 ?? null) },
            { key: 'persistence', values: days.map((day) => day.h1?.p_fss25 ?? null) },
        ],
        refs: [{ y: data.definitions.useful_fss, label: 'useful' }],
    });
    lineChart(document.getElementById('chart-rain'), {
        xs: dayIndex, xScale: 'linear', bars: true, height: 110, xTicks: dayTicks, xTickLabel: dayLabel,
        tipTitle: dayTitle, yFormat: (v) => percent(v, 1),
        axisFormat: (v) => percent(v, v > 0 && v < 0.01 ? 1 : 0),
        series: [{ key: 'rain', label: 'Map with rain', cls: 'acc-s-rain', values: days.map((day) => day.rain) }],
    });

    table(document.getElementById('acc-leads'),
        ['Lead', 'Pairs', 'CSI', 'Persist. CSI', 'POD', 'FAR', 'Bias', 'FSS 5 km', 'FSS 25 km',
            'Persist. FSS 25', 'Skill vs persist.', 'MAE mm/h', 'Above High', 'Below Low'],
        leads.map((row) => [
            `+${formatLead(row.lead)}`, row.pairs.toLocaleString('en-GB'), score(row.csi), score(row.p_csi),
            score(row.pod), score(row.far), score(row.bias), score(row.fss5), score(row.fss25),
            score(row.p_fss25), score(row.skill25), row.mae == null ? '–' : row.mae.toFixed(3),
            percent(row.above_p90), percent(row.below_p10),
        ]));

    const card = document.getElementById('acc-months-card');
    card.hidden = data.months.length === 0;
    table(document.getElementById('acc-months'),
        ['Month', 'Rain', 'FSS +1 h', 'Skill +1 h', 'FSS +3 h', 'Skill +3 h', 'Useful up to', 'Above High'],
        data.months.map((month) => [
            formatMonth(month.month), percent(month.rain, 1), score(month.h1?.fss25), score(month.h1?.skill25),
            score(month.h3?.fss25), score(month.h3?.skill25),
            month.useful_to ? formatLead(month.useful_to) : '–', percent(month.above_p90),
        ]));
}

function summarise(data) {
    document.getElementById('acc-headline').textContent = headline(data);
    const list = document.getElementById('acc-stats');
    list.replaceChildren();
    for (const [label, value, note] of stats(data)) {
        const item = html('div', 'summary-stat');
        item.append(html('dt', null, label), html('dd', null, value), html('span', 'acc-stat-note', note));
        list.append(item);
    }
    const updated = new Date(data.generated_at * 1000);
    const clock = document.getElementById('acc-updated');
    clock.textContent = updated.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
    clock.dateTime = updated.toISOString();
    document.getElementById('acc-foot').textContent =
        `Scores ${data.cycles.toLocaleString('en-GB')} forecast cycles from `
        + `${formatDate(new Date(data.from * 1000).toISOString().slice(0, 10))} on, against KNMI's `
        + 'real-time radar (CC BY 4.0). Rewritten every hour.'
        + (data.stand_in_cycles ? ` ${data.stand_in_cycles} cycles from the stand-in forecast are left out.` : '');
}

function unavailable(message) {
    document.getElementById('acc-headline').textContent = message;
    for (const chart of document.querySelectorAll('.acc-chart')) {
        chart.replaceChildren(html('p', 'chart-empty', 'Nothing to draw yet.'));
    }
}

async function start() {
    legends();
    fetch('/api/config').then((response) => (response.ok ? response.json() : null))
        .then((manifest) => applySiteHome(manifest?.site)).catch(() => applySiteHome(null));

    let data;
    try {
        const response = await fetch('/api/accuracy');
        if (response.status === 404) {
            unavailable('No accuracy scores yet. This server is not keeping score, or it started less than an hour ago.');
            return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        data = await response.json();
    } catch (error) {
        unavailable(`Could not load the scores (${error.message}). Reload to try again.`);
        return;
    }
    summarise(data);
    draw(data);
    // Redraw on a width change only: the charts size themselves to their card.
    let lastWidth = document.documentElement.clientWidth;
    let pending = 0;
    window.addEventListener('resize', () => {
        const width = document.documentElement.clientWidth;
        if (width === lastWidth) return;
        lastWidth = width;
        cancelAnimationFrame(pending);
        pending = requestAnimationFrame(() => draw(data));
    });
}

start();
