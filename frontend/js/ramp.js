/**
 * The precipitation colour ramp — the single source of truth.
 *
 * Everything that needs to turn a rain rate into a colour goes through here:
 * the WebGL shader (via a 256x1 lookup texture), the legend under the timeline,
 * and the sparkline in the point popup. Because they all build from the same
 * stops, the legend cannot drift out of sync with the map.
 *
 * Stops are placed on a log scale: most Dutch rain sits below 5 mm/h, and a
 * linear ramp spends almost all of its colour range on downpours that hardly
 * ever happen.
 */

export const RAMP_MIN_MM_H = 0.1;
export const RAMP_MAX_MM_H = 100;

export const RAMP_STOPS = [
    { mmh: 0.1, color: '#c2e6ff' },
    { mmh: 0.3, color: '#84c6f2' },
    { mmh: 1.0, color: '#3b82f6' },
    { mmh: 2.5, color: '#1e40c8' },
    { mmh: 5.0, color: '#16a34a' },
    { mmh: 10, color: '#facc15' },
    { mmh: 20, color: '#f97316' },
    { mmh: 50, color: '#ef4444' },
    { mmh: 100, color: '#c026d3' },
];

const LOG_MIN = Math.log(RAMP_MIN_MM_H);
const LOG_RANGE = Math.log(RAMP_MAX_MM_H) - LOG_MIN;

/** Where a rain rate sits along the ramp, 0..1. */
export function rampPosition(mmh) {
    if (!(mmh > 0)) return 0;
    return Math.min(1, Math.max(0, (Math.log(mmh) - LOG_MIN) / LOG_RANGE));
}

/** Log-scale constants the fragment shader needs to do the same mapping. */
export const SHADER_LOG_MIN = LOG_MIN;
export const SHADER_LOG_RANGE = LOG_RANGE;

/** A horizontal CSS gradient across the ramp, for any 2D canvas context. */
export function createRampGradient(ctx, x0, x1) {
    const gradient = ctx.createLinearGradient(x0, 0, x1, 0);
    for (const stop of RAMP_STOPS) {
        gradient.addColorStop(rampPosition(stop.mmh), stop.color);
    }
    return gradient;
}

/**
 * A 256x1 canvas of the ramp, uploaded to the GPU as the shader's lookup table.
 */
export function createRampLookupCanvas() {
    const canvas = document.createElement('canvas');
    canvas.width = 256;
    canvas.height = 1;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = createRampGradient(ctx, 0, 256);
    ctx.fillRect(0, 0, 256, 1);
    return canvas;
}

/** The ramp colour for a rain rate, as a CSS string (popup sparkline, chips). */
export function colorForRate(mmh) {
    const p = rampPosition(mmh);
    let lower = RAMP_STOPS[0];
    let upper = RAMP_STOPS[RAMP_STOPS.length - 1];
    for (let i = 0; i < RAMP_STOPS.length - 1; i++) {
        if (p >= rampPosition(RAMP_STOPS[i].mmh) && p <= rampPosition(RAMP_STOPS[i + 1].mmh)) {
            lower = RAMP_STOPS[i];
            upper = RAMP_STOPS[i + 1];
            break;
        }
    }
    const lo = rampPosition(lower.mmh);
    const hi = rampPosition(upper.mmh);
    const f = hi === lo ? 0 : (p - lo) / (hi - lo);
    const a = hexToRgb(lower.color);
    const b = hexToRgb(upper.color);
    return `rgb(${Math.round(a[0] + (b[0] - a[0]) * f)}, ${Math.round(
        a[1] + (b[1] - a[1]) * f
    )}, ${Math.round(a[2] + (b[2] - a[2]) * f)})`;
}

function hexToRgb(hex) {
    const n = parseInt(hex.slice(1), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/**
 * Draw the legend bar and its tick labels from the same stops the shader uses.
 */
export function renderLegend(canvas, labelsEl) {
    const width = canvas.clientWidth || 320;
    const height = canvas.clientHeight || 12;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = createRampGradient(ctx, 0, width);
    ctx.fillRect(0, 0, width, height);

    if (!labelsEl) return;
    labelsEl.innerHTML = '';
    for (const stop of RAMP_STOPS) {
        const span = document.createElement('span');
        span.className = 'scale-label';
        span.textContent = formatRate(stop.mmh);
        span.style.left = `${rampPosition(stop.mmh) * 100}%`;
        labelsEl.appendChild(span);
    }
}

/**
 * Compact rain-rate text: fewer decimals the heavier it rains, trailing zeros
 * trimmed, and the top of the scale marked as open-ended. Used for both the
 * legend stops and arbitrary measured values, so it must not print raw floats.
 */
export function formatRate(mmh) {
    if (mmh >= RAMP_MAX_MM_H) return `${RAMP_MAX_MM_H}+`;
    if (mmh >= 10) return String(Math.round(mmh));
    if (mmh >= 1) return trimZeros(mmh.toFixed(1));
    return trimZeros(mmh.toFixed(2));
}

function trimZeros(text) {
    return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text;
}
