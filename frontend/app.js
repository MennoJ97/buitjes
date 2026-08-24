const map = new maplibregl.Map({
    container: 'map',
    style: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
    center: [5.2913, 52.1326],
    zoom: 6
});

const coordinates = [
    [3.0, 53.8],
    [7.5, 53.8],
    [7.5, 50.7],
    [3.0, 50.7]
];

let timestamps = [];
let currentIndex = 0;
let isPlaying = false;
let playInterval = null;

// UI Elements
const slider = document.getElementById('timeline-slider');
const playBtn = document.getElementById('play-pause-btn');
const timeDisplay = document.getElementById('current-time-display');
const refTimeDisplay = document.getElementById('ref-time');
const opacitySlider = document.getElementById('opacity-slider');
const speedSlider = document.getElementById('speed-slider');

// WebGL Setup (re-using the same one)
const canvas = document.getElementById('glcanvas');
const gl = canvas.getContext('webgl', { preserveDrawingBuffer: true });

const vsSource = `
    attribute vec2 a_position;
    varying vec2 v_texCoord;
    void main() {
        v_texCoord = a_position * 0.5 + 0.5;
        v_texCoord.y = 1.0 - v_texCoord.y;
        gl_Position = vec4(a_position, 0.0, 1.0);
    }
`;

const fsSource = `
    precision mediump float;
    varying vec2 v_texCoord;
    uniform sampler2D u_image;
    
    void main() {
        vec4 texel = texture2D(u_image, v_texCoord);
        float normalized_val = texel.r + (texel.g / 255.0);
        
        if (normalized_val < 0.01) {
            gl_FragColor = vec4(0.0, 0.0, 0.0, 0.0);
            return;
        }

        vec3 low = vec3(0.0, 0.8, 1.0);
        vec3 mid = vec3(0.0, 0.0, 1.0);
        vec3 high = vec3(1.0, 0.0, 0.0);
        
        vec3 color;
        if (normalized_val < 0.5) {
            color = mix(low, mid, normalized_val * 2.0);
        } else {
            color = mix(mid, high, (normalized_val - 0.5) * 2.0);
        }
        
        gl_FragColor = vec4(color, 1.0); // opacity handled by maplibre
    }
`;

function createShader(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    return shader;
}

const program = gl.createProgram();
gl.attachShader(program, createShader(gl, gl.VERTEX_SHADER, vsSource));
gl.attachShader(program, createShader(gl, gl.FRAGMENT_SHADER, fsSource));
gl.linkProgram(program);
gl.useProgram(program);

const positionBuffer = gl.createBuffer();
gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([
    -1.0, -1.0,  1.0, -1.0, -1.0,  1.0,
    -1.0,  1.0,  1.0, -1.0,  1.0,  1.0,
]), gl.STATIC_DRAW);
const posLoc = gl.getAttribLocation(program, "a_position");
gl.enableVertexAttribArray(posLoc);
gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

const texture = gl.createTexture();
gl.bindTexture(gl.TEXTURE_2D, texture);
gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);

async function init() {
    // Fetch config
    const res = await fetch('/api/config');
    const config = await res.json();
    timestamps = config.timestamps;
    
    slider.max = timestamps.length - 1;
    slider.value = timestamps.length - 1;
    currentIndex = parseInt(slider.value);
    
    const refDate = new Date(timestamps[timestamps.length-1] * 1000);
    refTimeDisplay.innerText = refDate.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});

    map.on('load', () => {
        map.addSource('radar', {
            type: 'canvas',
            canvas: 'glcanvas',
            coordinates: coordinates,
            animate: true
        });

        map.addLayer({
            id: 'radar-layer',
            type: 'raster',
            source: 'radar',
            paint: {
                'raster-opacity': parseInt(opacitySlider.value) / 100,
                'raster-fade-duration': 0
            }
        });

        updateRadarFrame();
    });
}

function updateRadarFrame() {
    if (timestamps.length === 0) return;
    const ts = timestamps[currentIndex];
    
    // Update UI time
    const date = new Date(ts * 1000);
    timeDisplay.innerText = date.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
    slider.value = currentIndex;
    
    // Fetch WebP
    const img = new Image();
    img.src = \`/api/radar.webp?t=\${ts}\`;
    img.onload = () => {
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img);
        gl.drawArrays(gl.TRIANGLES, 0, 6);
    };
}

slider.addEventListener('input', (e) => {
    currentIndex = parseInt(e.target.value);
    updateRadarFrame();
});

opacitySlider.addEventListener('input', (e) => {
    const val = parseInt(e.target.value);
    document.getElementById('opacity-label').innerText = \`\${val}%\`;
    if (map.getLayer('radar-layer')) {
        map.setPaintProperty('radar-layer', 'raster-opacity', val / 100);
    }
});

speedSlider.addEventListener('input', (e) => {
    document.getElementById('speed-label').innerText = \`\${e.target.value} fps\`;
    if (isPlaying) {
        togglePlay(); 
        togglePlay();
    }
});

function togglePlay() {
    isPlaying = !isPlaying;
    playBtn.innerHTML = isPlaying ? '⏸' : '▶';
    
    if (isPlaying) {
        const fps = parseInt(speedSlider.value);
        playInterval = setInterval(() => {
            currentIndex = (currentIndex + 1) % timestamps.length;
            updateRadarFrame();
        }, 1000 / fps);
    } else {
        clearInterval(playInterval);
    }
}

playBtn.addEventListener('click', togglePlay);

init();
