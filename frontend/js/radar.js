/**
 * Radar frame decoding and rendering.
 *
 * Frames arrive as lossless WebP where each pixel carries a 16-bit fraction of
 * full-scale rain rate, split high byte -> R, low byte -> G, with alpha 0 where
 * it is dry. Colour is applied on the GPU, so the same frame can be recoloured
 * without refetching it.
 *
 * Blue is a flag, not data: 255 marks a pixel no radar measured. The shader
 * ignores it, because there is nothing to draw either way, but `sample` does
 * not — "no radar here" and "dry" are different answers and the readout under
 * the cursor distinguishes them.
 *
 * Frames are opaque: dry is a rate of zero, not an alpha of zero. That matters
 * to `sample`, which reads a pixel back through a 2D canvas — premultiplied, so
 * anything under alpha zero returns as zeros and the blue flag was unreadable.
 * See the ingestor's encode.py.
 */

import {
    createRampLookupCanvas,
    SHADER_LOG_MIN,
    SHADER_LOG_RANGE,
} from './ramp.js';

/** Blue-channel value marking a pixel no radar looked at. */
const NO_DATA_FLAG = 255;

const VERTEX_SHADER = `
    attribute vec2 a_position;
    varying vec2 v_texCoord;
    void main() {
        v_texCoord = a_position * 0.5 + 0.5;
        v_texCoord.y = 1.0 - v_texCoord.y;
        gl_Position = vec4(a_position, 0.0, 1.0);
    }
`;

// highp matters here: recombining the two bytes at mediump loses the low byte,
// which is most of the resolution in the drizzle end of the scale.
const FRAGMENT_SHADER = `
    #ifdef GL_FRAGMENT_PRECISION_HIGH
    precision highp float;
    #else
    precision mediump float;
    #endif

    varying vec2 v_texCoord;
    uniform sampler2D u_image;
    uniform sampler2D u_ramp;
    uniform float u_maxPrecip;
    uniform float u_logMin;
    uniform float u_logRange;
    // Below zero: the rain frame's 16-bit pair in R and G. Otherwise the index
    // of a spread frame's channel, each a rate on a log scale in one byte.
    uniform float u_logChannel;
    uniform float u_logFloor;
    uniform float u_logDecades;

    float rateOf(vec4 texel) {
        if (u_logChannel < 0.0) {
            float normalized = texel.r * (65280.0 / 65535.0) + texel.g * (255.0 / 65535.0);
            return normalized * u_maxPrecip;
        }
        float channel = u_logChannel < 0.5 ? texel.r
                      : (u_logChannel < 1.5 ? texel.g : texel.b);
        float level = floor(channel * 255.0 + 0.5);
        if (level < 0.5) return 0.0;
        return u_logFloor * pow(10.0, (level - 1.0) / 254.0 * u_logDecades);
    }

    void main() {
        vec4 texel = texture2D(u_image, v_texCoord);
        // No alpha test: frames are opaque, and a dry pixel is one whose rate is
        // zero, which the check below already discards. Frames written before
        // the format went opaque land in the same place by the same route.
        float mmh = rateOf(texel);
        if (mmh < 0.0001) {
            gl_FragColor = vec4(0.0);
            return;
        }

        float p = (log(mmh) - u_logMin) / u_logRange;
        if (!(p > 0.0)) {
            gl_FragColor = vec4(0.0);
            return;
        }
        p = clamp(p, 0.0, 1.0);

        vec3 color = texture2D(u_ramp, vec2(p, 0.5)).rgb;
        // Fade the weakest returns in rather than cutting them off with a hard edge.
        float alpha = smoothstep(0.0, 0.10, p);
        gl_FragColor = vec4(color * alpha, alpha);
    }
`;

function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        throw new Error(`Shader failed to compile: ${gl.getShaderInfoLog(shader)}`);
    }
    return shader;
}

export class RadarRenderer {
    constructor(canvas) {
        this.canvas = canvas;
        this.gl = canvas.getContext('webgl', {
            preserveDrawingBuffer: true,
            premultipliedAlpha: true,
        });
        if (!this.gl) throw new Error('WebGL is not available in this browser.');

        const gl = this.gl;
        const program = gl.createProgram();
        gl.attachShader(program, compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER));
        gl.attachShader(program, compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER));
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
            throw new Error(`Shader program failed to link: ${gl.getProgramInfoLog(program)}`);
        }
        gl.useProgram(program);
        this.program = program;

        const buffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.bufferData(
            gl.ARRAY_BUFFER,
            new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
            gl.STATIC_DRAW
        );
        const posLoc = gl.getAttribLocation(program, 'a_position');
        gl.enableVertexAttribArray(posLoc);
        gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

        this.frameTexture = this._createTexture();
        this.rampTexture = this._createTexture();
        gl.bindTexture(gl.TEXTURE_2D, this.rampTexture);
        gl.texImage2D(
            gl.TEXTURE_2D,
            0,
            gl.RGBA,
            gl.RGBA,
            gl.UNSIGNED_BYTE,
            createRampLookupCanvas()
        );

        gl.uniform1i(gl.getUniformLocation(program, 'u_image'), 0);
        gl.uniform1i(gl.getUniformLocation(program, 'u_ramp'), 1);
        gl.uniform1f(gl.getUniformLocation(program, 'u_logMin'), SHADER_LOG_MIN);
        gl.uniform1f(gl.getUniformLocation(program, 'u_logRange'), SHADER_LOG_RANGE);
        this.maxPrecipLoc = gl.getUniformLocation(program, 'u_maxPrecip');
        this.logChannelLoc = gl.getUniformLocation(program, 'u_logChannel');
        this.logFloorLoc = gl.getUniformLocation(program, 'u_logFloor');
        this.logDecadesLoc = gl.getUniformLocation(program, 'u_logDecades');
        this.setMaxPrecip(100);
        this.readRainFrames();

        gl.activeTexture(gl.TEXTURE1);
        gl.bindTexture(gl.TEXTURE_2D, this.rampTexture);
        gl.activeTexture(gl.TEXTURE0);
    }

    _createTexture() {
        const gl = this.gl;
        const texture = gl.createTexture();
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        return texture;
    }

    setMaxPrecip(maxPrecip) {
        this.gl.uniform1f(this.maxPrecipLoc, maxPrecip);
    }

    /** Draw rain frames: a 16-bit rate split across R and G. */
    readRainFrames() {
        this.gl.uniform1f(this.logChannelLoc, -1);
    }

    /**
     * Draw one channel of a spread frame instead: a rate in a single byte on a
     * log scale, which is how three percentiles fit in one image. `spread` is
     * the manifest's block, so the floor and ceiling come from whoever wrote
     * the frames rather than from a constant that has to be kept in step.
     */
    readSpreadChannel(index, spread) {
        const gl = this.gl;
        gl.uniform1f(this.logChannelLoc, index);
        gl.uniform1f(this.logFloorLoc, spread.floor_mm_h);
        gl.uniform1f(this.logDecadesLoc, Math.log10(spread.max_mm_h / spread.floor_mm_h));
    }

    resize(width, height) {
        if (this.canvas.width === width && this.canvas.height === height) return;
        this.canvas.width = width;
        this.canvas.height = height;
        this.gl.viewport(0, 0, width, height);
    }

    draw(image) {
        const gl = this.gl;
        gl.activeTexture(gl.TEXTURE0);
        gl.bindTexture(gl.TEXTURE_2D, this.frameTexture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
    }

    clear() {
        const gl = this.gl;
        gl.clearColor(0, 0, 0, 0);
        gl.clear(gl.COLOR_BUFFER_BIT);
    }
}

/**
 * Holds the manifest and the decoded frames.
 *
 * Every frame is fetched up front and played from memory. The previous version
 * kicked off a fetch per frame change, so at higher speeds responses could land
 * out of order and the animation would jump backwards.
 */
export class FrameStore {
    constructor() {
        this.manifest = null;
        this.frames = [];
        this.images = new Map(); // file -> HTMLImageElement
        this.failed = new Set(); // files the server would not give us
        // Spread frames are fetched on demand rather than up front: most
        // visitors never ask for uncertainty, and making them all pay a second
        // payload for it is the reason it is a separate file at all.
        this.spreadImages = new Map();
        this.spreadPending = new Set();
        this.onSpreadLoaded = null;
        this._scratch = document.createElement('canvas');
        this._scratch.width = 1;
        this._scratch.height = 1;
        this._scratchCtx = this._scratch.getContext('2d', { willReadFrequently: true });
    }

    get width() {
        return this.manifest?.width ?? 500;
    }

    get height() {
        return this.manifest?.height ?? 500;
    }

    get maxPrecip() {
        return this.manifest?.max_precip_mm_h ?? 100;
    }

    /** Frame corner coordinates as MapLibre wants them: NW, NE, SE, SW. */
    get coordinates() {
        return this.manifest.bounds;
    }

    /** The manifest's spread block, or null when the layer is switched off. */
    get spreadInfo() {
        return this.manifest?.spread ?? null;
    }

    setManifest(manifest) {
        this.manifest = manifest;
        this.frames = manifest.frames.slice().sort((a, b) => a.t - b.t);
        // Drop images for frames that have aged out of the manifest.
        const live = new Set(this.frames.map((f) => f.file));
        for (const file of [...this.images.keys()]) {
            if (!live.has(file)) this.images.delete(file);
        }
        for (const file of [...this.failed]) {
            if (!live.has(file)) this.failed.delete(file);
        }
        const liveSpread = new Set(this.frames.map((f) => f.spread).filter(Boolean));
        for (const file of [...this.spreadImages.keys()]) {
            if (!liveSpread.has(file)) this.spreadImages.delete(file);
        }
    }

    spreadImageFor(frame) {
        return frame?.spread ? this.spreadImages.get(frame.spread) : undefined;
    }

    /**
     * The spread frame for one step, fetching it if this is the first ask.
     *
     * Returns undefined while it is in flight; `onSpreadLoaded` fires when it
     * lands so a caller that drew without it can draw again. Demand-driven on
     * purpose — hovering one frame costs one 40 KB file, not the whole cycle.
     */
    requestSpread(frame) {
        if (!frame?.spread) return undefined;
        const loaded = this.spreadImages.get(frame.spread);
        if (loaded || this.spreadPending.has(frame.spread)) return loaded;

        this.spreadPending.add(frame.spread);
        loadImage(`/api/frames/${frame.spread}`)
            .then((image) => {
                this.spreadImages.set(frame.spread, image);
                this.onSpreadLoaded?.(frame);
            })
            .catch(() => {})
            .finally(() => this.spreadPending.delete(frame.spread));
        return undefined;
    }

    /** Every spread frame, for playback and for a point's whole series. */
    async prefetchSpread(onProgress) {
        const missing = this.frames.filter(
            (frame) => frame.spread && !this.spreadImages.has(frame.spread)
        );
        if (!missing.length) {
            onProgress?.(1);
            return;
        }
        let done = 0;
        await Promise.all(missing.map(async (frame) => {
            try {
                this.spreadImages.set(frame.spread, await loadImage(`/api/frames/${frame.spread}`));
            } catch {
                // A missing spread frame costs the band, not the map.
            } finally {
                onProgress?.(++done / missing.length);
            }
        }));
    }

    isLoaded(frame) {
        return this.images.has(frame.file);
    }

    hasFailed(frame) {
        return !!frame && this.failed.has(frame.file);
    }

    /** Fetch every frame that is not in memory yet, reporting progress 0..1. */
    async prefetch(onProgress) {
        const missing = this.frames.filter((f) => !this.images.has(f.file));
        if (missing.length === 0) {
            onProgress?.(1);
            return;
        }

        let done = 0;
        const failures = [];
        await Promise.all(
            missing.map(async (frame) => {
                try {
                    this.images.set(frame.file, await loadImage(`/api/frames/${frame.file}`));
                } catch (err) {
                    // Remembered so the UI can say "unavailable" rather than
                    // waiting forever on a frame that is never coming.
                    this.failed.add(frame.file);
                    failures.push(frame.file);
                } finally {
                    done++;
                    onProgress?.(done / missing.length);
                }
            })
        );

        if (failures.length === missing.length) {
            throw new Error('No radar frames could be loaded.');
        }
    }

    imageFor(frame) {
        return frame ? this.images.get(frame.file) : undefined;
    }

    /**
     * Rain rate in mm/h at a geographic point for one frame, or null if the
     * point falls outside the frame, the frame is not loaded yet, or no radar
     * measured that pixel.
     *
     * MapLibre stretches the frame linearly across its corner coordinates in
     * Web Mercator, so the vertical lookup has to be mercator too — treating it
     * as linear latitude would drift by kilometres at the edges.
     */
    /** Which pixel a coordinate lands on, or null if it lands off the frame. */
    _pixelFor(lng, lat) {
        if (!this.manifest) return null;
        const [[west, north], [east], , [, south]] = this.manifest.bounds;
        const xFraction = (lng - west) / (east - west);
        const mercNorth = mercatorY(north);
        const mercSouth = mercatorY(south);
        const yFraction = (mercNorth - mercatorY(lat)) / (mercNorth - mercSouth);
        if (xFraction < 0 || xFraction >= 1 || yFraction < 0 || yFraction >= 1) return null;
        return [
            Math.min(this.width - 1, Math.floor(xFraction * this.width)),
            Math.min(this.height - 1, Math.floor(yFraction * this.height)),
        ];
    }

    _readPixel(image, px, py) {
        this._scratchCtx.clearRect(0, 0, 1, 1);
        this._scratchCtx.drawImage(image, px, py, 1, 1, 0, 0, 1, 1);
        return this._scratchCtx.getImageData(0, 0, 1, 1).data;
    }

    sample(frame, lng, lat) {
        const image = this.imageFor(frame);
        if (!image) return null;
        const pixel = this._pixelFor(lng, lat);
        if (!pixel) return null;

        const [r, g, b, a] = this._readPixel(image, pixel[0], pixel[1]);
        // Checked first: an unmeasured pixel is dry-looking too, and reporting
        // it as 0 mm/h is the whole bug this flag exists to fix. It only
        // survives the canvas at all because frames are opaque — a 2D canvas
        // premultiplies, so under alpha zero this used to read (0, 0, 0, 0) and
        // the branch below swallowed every unmeasured pixel as "dry".
        if (b >= NO_DATA_FLAG) return null;
        // Frames from before the format went opaque, still in the browser cache
        // or on the volume until they age out.
        if (a === 0) return 0;
        return ((r * 256 + g) / 65535) * this.maxPrecip;
    }

    /** The rain rate at one point across every loaded frame. */
    series(lng, lat) {
        return this.frames.map((frame) => ({
            t: frame.t,
            kind: frame.kind,
            mmh: this.sample(frame, lng, lat),
        }));
    }

    /**
     * The ensemble band at one point for one step: `{p10, p50, p90}`, or null
     * when there is no spread layer, no frame for it yet, or the point is off
     * the map. The three come back in the order the manifest lists them, which
     * is the order they were packed into R, G and B.
     */
    sampleSpread(frame, lng, lat) {
        const spread = this.spreadInfo;
        const image = this.spreadImageFor(frame);
        if (!spread || !image) return null;
        const pixel = this._pixelFor(lng, lat);
        if (!pixel) return null;

        const channels = this._readPixel(image, pixel[0], pixel[1]);
        const decades = Math.log10(spread.max_mm_h / spread.floor_mm_h);
        const rate = (level) => (level === 0
            ? 0
            : spread.floor_mm_h * 10 ** (((level - 1) / 254) * decades));
        const out = {};
        spread.percentiles.forEach((percentile, index) => {
            out[`p${percentile}`] = rate(channels[index]);
        });
        return out;
    }

    /** The band at one point across every step whose spread frame is loaded. */
    spreadSeries(lng, lat) {
        return this.frames.map((frame) => ({
            t: frame.t,
            kind: frame.kind,
            band: this.sampleSpread(frame, lng, lat),
        }));
    }
}

function mercatorY(lat) {
    const clamped = Math.max(-85.051129, Math.min(85.051129, lat));
    return Math.log(Math.tan(Math.PI / 4 + (clamped * Math.PI) / 360));
}

function loadImage(url) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.crossOrigin = 'anonymous';
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error(`Failed to load ${url}`));
        img.src = url;
    });
}
