/**
 * Turning ensemble members into the percentile series the charts draw.
 *
 * Configured locations get this done in the ingestor, from KNMI's own members.
 * A coordinate the map was clicked on has no precomputed forecast, so the raw
 * Open-Meteo ensemble is reduced here instead — same shape out, so the charts
 * cannot tell the difference.
 */

export const PERCENTILES = [10, 25, 50, 75, 90];

/** Linear-interpolated percentile, matching numpy's default and the ingestor. */
function percentile(sorted, fraction) {
    if (sorted.length === 1) return sorted[0];
    const position = fraction * (sorted.length - 1);
    const lower = Math.floor(position);
    const upper = Math.ceil(position);
    if (lower === upper) return sorted[lower];
    return sorted[lower] + (sorted[upper] - sorted[lower]) * (position - lower);
}

const round = (value, decimals) => {
    const factor = 10 ** decimals;
    return Math.round(value * factor) / factor;
};

/**
 * Reduce one Open-Meteo variable to a percentile series.
 *
 * Members are the numbered `<variable>_memberNN` keys plus the unnumbered
 * control run.
 */
export function seriesFromEnsemble(hourly, variable, decimals = 1) {
    const times = hourly.time ?? [];
    const memberKeys = Object.keys(hourly).filter(
        (key) => key === variable || key.startsWith(`${variable}_member`)
    );
    if (!times.length || !memberKeys.length) return [];

    const series = [];
    for (let index = 0; index < times.length; index++) {
        const values = memberKeys
            .map((key) => hourly[key][index])
            .filter((value) => value !== null && value !== undefined);
        if (!values.length) continue;

        values.sort((a, b) => a - b);
        const entry = { t: Math.floor(Date.parse(`${times[index]}Z`) / 1000) };
        for (const p of PERCENTILES) {
            const key = p === 50 ? 'median' : `p${p}`;
            entry[key] = round(percentile(values, p / 100), decimals);
        }
        series.push(entry);
    }
    return series;
}

/** The blocks a point document carries, built from a raw ensemble response. */
export function conditionsFromEnsemble(payload) {
    const hourly = payload?.hourly;
    if (!hourly) return {};
    const blocks = {};
    const add = (key, variable, unit, decimals) => {
        const series = seriesFromEnsemble(hourly, variable, decimals);
        if (series.length) blocks[key] = { unit, series };
    };
    add('temperature', 'temperature_2m', '°C', 1);
    add('wind', 'wind_speed_10m', 'm/s', 1);
    add('solar', 'shortwave_radiation', 'W/m²', 0);
    add('precipitation_outlook', 'precipitation', 'mm/h', 1);
    return blocks;
}
