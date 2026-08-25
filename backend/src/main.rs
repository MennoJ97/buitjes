use axum::{
    extract::{Path, State},
    http::{header, HeaderValue, Method, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
    Router,
};
use serde_json::json;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::collections::HashMap;
use std::time::{Duration, Instant};
use std::time::{SystemTime, UNIX_EPOCH};
use tower_http::{
    cors::CorsLayer,
    services::ServeDir,
    trace::{DefaultMakeSpan, DefaultOnFailure, DefaultOnResponse, TraceLayer},
};
use tracing::{error, info, warn, Level};

/// Frames and the manifest are produced by the ingestor sidecar and shared
/// through a volume; this server only hands them out.
const MANIFEST_NAME: &str = "manifest.json";

/// How stale the manifest may get before /healthz starts failing. The ingestor
/// publishes every ~5 minutes, so this tolerates a few missed cycles.
const DEFAULT_MAX_MANIFEST_AGE: u64 = 1200;

#[derive(Clone)]
struct AppState {
    frame_dir: PathBuf,
    max_manifest_age: u64,
    api_keys: Arc<Vec<String>>,
    protected_prefixes: Arc<Vec<String>>,
    conditions: ConditionsProxy,
    /// Whether the newest forecast was stale the last time anyone asked.
    ///
    /// Only so that going stale — and coming back — is logged once each, on the
    /// edge. The container health check polls every 30 seconds, so logging the
    /// state rather than the change would turn one KNMI outage into a wall of
    /// identical lines that buries the moment it began.
    was_stale: Arc<AtomicBool>,
}

/// On-demand conditions for coordinates nobody configured in advance.
///
/// Point forecasts are precomputed per configured location, but the map lets
/// you click anywhere, and a coordinate should give a real forecast rather than
/// silently snapping to the nearest sampled point. Temperature, wind, solar and
/// the rain outlook all come from Open-Meteo, which accepts any coordinate — so
/// this fetches them when asked.
///
/// It stays a deliberately narrow proxy: the URL and parameter set are fixed
/// here and only the two coordinates come from the caller, so it cannot be used
/// to fetch anything else. Responses are cached per rounded coordinate, which
/// both spares Open-Meteo and keeps a page reload instant.
#[derive(Clone)]
struct ConditionsProxy {
    client: reqwest::Client,
    model: String,
    past_hours: u32,
    forecast_hours: u32,
    cache: Arc<Mutex<HashMap<(i32, i32), (Instant, Arc<Vec<u8>>)>>>,
    ttl: Duration,
}

const OPEN_METEO_ENDPOINT: &str = "https://ensemble-api.open-meteo.com/v1/ensemble";
const OPEN_METEO_VARIABLES: &str =
    "temperature_2m,wind_speed_10m,shortwave_radiation,precipitation";

impl ConditionsProxy {
    fn from_env() -> Self {
        Self {
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(20))
                .build()
                .unwrap_or_default(),
            model: std::env::var("CONDITIONS_MODEL")
                .unwrap_or_else(|_| "icon_seamless".to_string()),
            past_hours: env_number("CONDITIONS_PAST_HOURS", 3),
            forecast_hours: env_number("CONDITIONS_FORECAST_HOURS", 48),
            cache: Arc::new(Mutex::new(HashMap::new())),
            ttl: Duration::from_secs(60 * env_number("CONDITIONS_REFRESH_MINUTES", 30) as u64),
        }
    }

    /// Cache key: hundredths of a degree, about a kilometre. Finer than that is
    /// below the resolution of the model being asked.
    fn key(latitude: f64, longitude: f64) -> (i32, i32) {
        ((latitude * 100.0).round() as i32, (longitude * 100.0).round() as i32)
    }

    fn cached(&self, key: (i32, i32)) -> Option<Arc<Vec<u8>>> {
        let cache = self.cache.lock().ok()?;
        let (stored, body) = cache.get(&key)?;
        (stored.elapsed() < self.ttl).then(|| Arc::clone(body))
    }

    async fn fetch(&self, latitude: f64, longitude: f64) -> Result<Arc<Vec<u8>>, String> {
        let key = Self::key(latitude, longitude);
        if let Some(body) = self.cached(key) {
            return Ok(body);
        }

        let response = self
            .client
            .get(OPEN_METEO_ENDPOINT)
            .query(&[
                ("latitude", latitude.to_string()),
                ("longitude", longitude.to_string()),
                ("hourly", OPEN_METEO_VARIABLES.to_string()),
                ("models", self.model.clone()),
                ("past_hours", self.past_hours.to_string()),
                ("forecast_hours", self.forecast_hours.to_string()),
                ("wind_speed_unit", "ms".to_string()),
                ("timezone", "UTC".to_string()),
            ])
            .send()
            .await
            .map_err(|error| error.to_string())?;

        if !response.status().is_success() {
            return Err(format!("upstream returned {}", response.status()));
        }

        let body = Arc::new(
            response
                .bytes()
                .await
                .map_err(|error| error.to_string())?
                .to_vec(),
        );
        if let Ok(mut cache) = self.cache.lock() {
            // Bounded so a scripted sweep of coordinates cannot grow it forever.
            if cache.len() > 512 {
                cache.clear();
            }
            cache.insert(key, (Instant::now(), Arc::clone(&body)));
        }
        Ok(body)
    }
}

fn env_number(name: &str, default: u32) -> u32 {
    std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

/// Paths the API key guards when `API_KEYS` is set.
///
/// Defaults to the widget endpoints only. The map's own frontend needs
/// `/api/config` and `/api/frames/*`, and a key embedded in a public page is
/// readable by anyone viewing source — so gating those would cost the map
/// nothing in security while breaking it. Set the variable to `/api` to lock
/// the whole API down, accepting that the browser map then needs a key too.
const DEFAULT_PROTECTED_PREFIXES: &str = "/api/point,/api/current";

fn split_env(name: &str, default: &str) -> Vec<String> {
    std::env::var(name)
        .unwrap_or_else(|_| default.to_string())
        .split(',')
        .map(|part| part.trim().to_string())
        .filter(|part| !part.is_empty())
        .collect()
}

/// Compare without an early return, so a wrong key cannot be narrowed down by
/// timing. Cheap insurance; these are short strings compared rarely.
fn constant_time_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Whether a request carries one of the configured keys.
///
/// Header or `key=` query parameter, because an `<img>` or a widget cannot
/// always set headers. Shared by the gate below and by `/api/config`, which is
/// not gated outright but does hide part of its answer without a key.
fn presents_valid_key(
    headers: &axum::http::HeaderMap,
    uri: &axum::http::Uri,
    state: &AppState,
) -> bool {
    let from_header = headers
        .get("x-api-key")
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);
    let from_query = uri.query().and_then(|query| {
        query
            .split('&')
            .find_map(|pair| pair.strip_prefix("key=").map(|value| value.to_string()))
    });

    let presented = from_header.or(from_query).unwrap_or_default();
    state
        .api_keys
        .iter()
        .any(|key| constant_time_eq(key, &presented))
}

/// Gate for the widget endpoints.
///
/// The key is read from `X-API-Key` or a `key` query parameter — the latter
/// because an `<img>`/`fetch` from a homepage widget cannot always set headers.
///
/// Worth being honest about what this does and does not do: a key shipped in a
/// public web page is an *identifier*, not a secret. It stops casual scraping
/// and lets a leaked key be rotated; it does not authenticate, and it does
/// nothing against a volumetric flood. Rate limiting in front (see the Traefik
/// labels in docker-compose.yml) is the measure that actually protects the box.
async fn require_api_key(
    State(state): State<AppState>,
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> Response {
    if state.api_keys.is_empty() {
        return next.run(request).await;
    }

    let path = request.uri().path().to_string();
    let guarded = state
        .protected_prefixes
        .iter()
        .any(|prefix| path.starts_with(prefix.as_str()));
    if !guarded {
        return next.run(request).await;
    }

    if presents_valid_key(request.headers(), request.uri(), &state) {
        next.run(request).await
    } else {
        (
            StatusCode::UNAUTHORIZED,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-store"),
            ],
            br#"{"error":"a valid API key is required for this endpoint"}"#.to_vec(),
        )
            .into_response()
    }
}

/// The frontend, served so that a redeploy actually reaches open browsers.
///
/// `ServeDir` alone sends only `Last-Modified`, which is not an instruction but
/// a hint: with no `Cache-Control` a browser applies its own heuristic and may
/// reuse a script for hours without asking. After a deploy that leaves it
/// running the previous JavaScript against the current HTML — a mismatch that
/// fails in whatever way those two versions happen to disagree, far from the
/// change that caused it.
///
/// `no-cache` does not mean "do not store"; it means "revalidate before use",
/// so the file stays cached and the usual request is a conditional one that
/// `ServeDir` answers with an empty 304. The frames keep their own long-lived
/// immutable caching — they are named by timestamp and never change.
fn static_files() -> Router {
    Router::new()
        .fallback_service(ServeDir::new("frontend"))
        .layer(axum::middleware::from_fn(revalidate))
}

async fn revalidate(request: axum::extract::Request, next: axum::middleware::Next) -> Response {
    let mut response = next.run(request).await;
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    response
}

/// Cross-origin policy for the API.
///
/// The homepage widget is served from a different origin, so *some* CORS is
/// required. `CORS_ALLOWED_ORIGINS` is a comma-separated allowlist; the literal
/// `*` keeps the old permissive behaviour for anyone who wants it. Unset means
/// same-origin only, which is the right default for a server on the public
/// internet — the widget's origin has to be named deliberately.
///
/// A widget that fetches server-side (a dashboard template asking
/// `http://buitjes-weather-app:3000` over the shared Docker network) is not a
/// browser request at all, so it needs none of this. Leave the variable unset
/// unless something in a page's JavaScript really does call the API.
fn cors_layer() -> CorsLayer {
    let configured = std::env::var("CORS_ALLOWED_ORIGINS").unwrap_or_default();
    let configured = configured.trim();

    if configured == "*" {
        info!("CORS: allowing any origin");
        return CorsLayer::permissive();
    }
    if configured.is_empty() {
        info!("CORS: same-origin only (set CORS_ALLOWED_ORIGINS to allow a widget host)");
        return CorsLayer::new();
    }

    let origins: Vec<HeaderValue> = configured
        .split(',')
        .filter_map(|origin| {
            let origin = origin.trim();
            match origin.parse::<HeaderValue>() {
                Ok(value) => Some(value),
                Err(_) => {
                    warn!("CORS: ignoring unparseable origin {origin:?}");
                    None
                }
            }
        })
        .collect();

    info!(origins = origins.len(), "CORS: allowlist configured");
    CorsLayer::new()
        .allow_origin(origins)
        .allow_methods([Method::GET])
}

/// Log to stdout, for `docker compose logs` to pick up.
///
/// Local time rather than UTC so these lines interleave sensibly with the
/// ingestor's, which have used the container's own timezone all along — two
/// containers in one `docker compose logs` stamping the same moment two hours
/// apart is a good way to misread an incident. Both read `TZ`.
///
/// Verbosity is `RUST_LOG`, defaulting to our own INFO and nothing from the
/// libraries. Per-request lines live at DEBUG deliberately: Traefik's access
/// log already records every request in front of this, and one map page load
/// is ~85 frame fetches, so repeating them here by default would bury the
/// handful of lines that say something. To see them:
///
///     RUST_LOG=weather_backend=debug,tower_http=debug
fn init_logging() {
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("weather_backend=info,tower_http=warn"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_timer(tracing_subscriber::fmt::time::ChronoLocal::new(
            "%Y-%m-%d %H:%M:%S%.3f".to_string(),
        ))
        .with_target(true)
        .compact()
        .init();
}

#[tokio::main]
async fn main() {
    init_logging();

    let state = AppState {
        frame_dir: PathBuf::from(
            std::env::var("FRAME_DIR").unwrap_or_else(|_| "/data".to_string()),
        ),
        max_manifest_age: std::env::var("MAX_MANIFEST_AGE_SECONDS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(DEFAULT_MAX_MANIFEST_AGE),
        api_keys: Arc::new(split_env("API_KEYS", "")),
        protected_prefixes: Arc::new(split_env(
            "API_KEY_PROTECTED_PREFIXES",
            DEFAULT_PROTECTED_PREFIXES,
        )),
        conditions: ConditionsProxy::from_env(),
        was_stale: Arc::new(AtomicBool::new(false)),
    };

    if state.api_keys.is_empty() {
        info!("API keys: disabled (every endpoint is open)");
    } else {
        info!(
            keys = state.api_keys.len(),
            guarding = %state.protected_prefixes.join(", "),
            "API keys configured"
        );
    }

    let served = Router::new()
        .route("/api/config", get(serve_manifest))
        .route("/api/frames/:file", get(serve_frame))
        .route("/api/point/:name", get(serve_point))
        .route("/api/current/:name", get(serve_current))
        .route("/api/conditions", get(serve_conditions))
        .fallback_service(static_files())
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            require_api_key,
        ))
        .layer(cors_layer())
        // Quiet unless something breaks: spans and per-response lines at DEBUG,
        // and only a 5xx reaches the log at WARN by default.
        .layer(
            TraceLayer::new_for_http()
                .make_span_with(DefaultMakeSpan::new().level(Level::DEBUG))
                .on_response(DefaultOnResponse::new().level(Level::DEBUG))
                .on_failure(DefaultOnFailure::new().level(Level::WARN)),
        )
        .with_state(state.clone());

    // /healthz sits outside that trace on purpose. It answers 503 when the
    // *data* is stale, which is not a failed request, and the container health
    // check asks every 30 seconds — inside, one KNMI outage would be a WARN
    // every half minute saying nothing the staleness edge has not said once.
    let app = Router::new()
        .route("/healthz", get(healthz))
        .with_state(state.clone())
        .merge(served);

    let addr = SocketAddr::from(([0, 0, 0, 0], 3000));
    let listener = match tokio::net::TcpListener::bind(&addr).await {
        Ok(listener) => listener,
        Err(error) => {
            // Nothing useful to serve from, so say which address failed and
            // why, rather than panicking with a bare `Address in use`.
            error!("cannot bind {addr}: {error}");
            std::process::exit(1);
        }
    };
    info!(
        frames = %state.frame_dir.display(),
        "listening on http://{addr}"
    );
    if let Err(error) = axum::serve(listener, app).await {
        error!("server stopped: {error}");
        std::process::exit(1);
    }
}

/// How old the newest forecast is, in seconds.
///
/// Deliberately measured from the manifest's `reference_time` rather than the
/// file's mtime: the ingestor rewrites the manifest on every poll as observed
/// history fills in, so mtime would stay fresh even if KNMI stopped publishing
/// forecasts altogether — exactly the failure this is meant to catch.
async fn forecast_age(state: &AppState) -> Option<u64> {
    let raw = tokio::fs::read(state.frame_dir.join(MANIFEST_NAME)).await.ok()?;
    let manifest: serde_json::Value = serde_json::from_slice(&raw).ok()?;
    let reference = manifest.get("reference_time")?.as_u64()?;
    let now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_secs();
    Some(now.saturating_sub(reference))
}

/// Reports the freshness of the ingestor's output, so Traefik and the Docker
/// healthcheck notice a poller that has died or lost its API key.
async fn healthz(State(state): State<AppState>) -> impl IntoResponse {
    let age = forecast_age(&state).await;
    let (status, ok, detail) = match age {
        None => (
            StatusCode::SERVICE_UNAVAILABLE,
            false,
            "no manifest yet - the ingestor has not published a cycle",
        ),
        Some(seconds) if seconds > state.max_manifest_age => (
            StatusCode::SERVICE_UNAVAILABLE,
            false,
            "the newest forecast is stale - the ingestor is not keeping up",
        ),
        Some(_) => (StatusCode::OK, true, "ok"),
    };

    // Log the transition, not the state. See `AppState::was_stale`.
    if state.was_stale.swap(!ok, Ordering::Relaxed) != !ok {
        if ok {
            info!(age_seconds = age, "forecast is fresh again");
        } else {
            warn!(
                age_seconds = age,
                limit_seconds = state.max_manifest_age,
                "{detail}"
            );
        }
    }

    (
        status,
        [(header::CACHE_CONTROL, "no-store")],
        axum::Json(json!({
            "status": if ok { "ok" } else { "degraded" },
            "detail": detail,
            "forecast_age_seconds": age,
            "max_forecast_age_seconds": state.max_manifest_age,
        })),
    )
}

/// The manifest, with the list of published locations removed for callers who
/// present no key.
///
/// The map itself has to stay open — it needs the bounds and the frame list to
/// draw anything — but *where someone lives* is a different kind of fact from
/// *where it is raining*. `points` carries the names and coordinates the owner
/// configured, so it is served only to a request that presents a key, and the
/// location picker appears only for that reader.
///
/// With no `API_KEYS` configured there is nothing to distinguish callers by, so
/// the manifest is served whole: a single-user setup on a private network should
/// not have to invent a key to see its own locations.
fn manifest_for(body: Vec<u8>, authorised: bool) -> Vec<u8> {
    if authorised {
        return body;
    }
    match serde_json::from_slice::<serde_json::Value>(&body) {
        Ok(mut manifest) => {
            if let Some(object) = manifest.as_object_mut() {
                object.remove("points");
            }
            serde_json::to_vec(&manifest).unwrap_or(body)
        }
        // Unparseable manifest: serving it unchanged is what happened before
        // this function existed, and the frontend already copes with junk here.
        Err(_) => body,
    }
}

async fn serve_manifest(
    State(state): State<AppState>,
    request: axum::extract::Request,
) -> Response {
    let authorised =
        state.api_keys.is_empty() || presents_valid_key(request.headers(), request.uri(), &state);
    match tokio::fs::read(state.frame_dir.join(MANIFEST_NAME)).await {
        Ok(body) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-cache"),
                // Never let a shared cache serve the keyed answer to someone else.
                (header::VARY, "x-api-key"),
            ],
            manifest_for(body, authorised),
        )
            .into_response(),
        // Normal for the first minute after startup: the ingestor has not
        // finished its first cycle. Retry-After tells clients it is worth waiting.
        Err(_) => (
            StatusCode::SERVICE_UNAVAILABLE,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-store"),
                (header::RETRY_AFTER, "10"),
            ],
            br#"{"status":"warming_up","error":"no forecast has been published yet"}"#.to_vec(),
        )
            .into_response(),
    }
}

/// Frame filenames are generated by the ingestor as `p_<reference>_<valid>.webp`
/// for forecasts and `o_<valid>.webp` for observed radar. Validating against
/// those shapes keeps the path join free of traversal tricks.
fn is_valid_frame_name(name: &str) -> bool {
    let Some(stem) = name.strip_suffix(".webp") else {
        return false;
    };
    let Some((prefix, rest)) = stem.split_once('_') else {
        return false;
    };
    let is_number = |part: &str| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit());

    match prefix {
        "p" => match rest.split_once('_') {
            Some((reference, valid)) => is_number(reference) && is_number(valid),
            None => false,
        },
        "o" => is_number(rest),
        _ => false,
    }
}

async fn serve_frame(State(state): State<AppState>, Path(file): Path<String>) -> impl IntoResponse {
    if !is_valid_frame_name(&file) {
        return (
            StatusCode::NOT_FOUND,
            [
                (header::CONTENT_TYPE, "text/plain"),
                (header::CACHE_CONTROL, "no-store"),
            ],
            Vec::new(),
        );
    }

    match tokio::fs::read(state.frame_dir.join(&file)).await {
        // The reference time is in the filename, so a given URL never changes.
        Ok(body) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, "image/webp"),
                (header::CACHE_CONTROL, "public, max-age=31536000, immutable"),
            ],
            body,
        ),
        Err(_) => (
            StatusCode::NOT_FOUND,
            [
                (header::CONTENT_TYPE, "text/plain"),
                (header::CACHE_CONTROL, "no-store"),
            ],
            Vec::new(),
        ),
    }
}

/// Point forecasts are published per configured location as `point_<name>.json`.
fn is_valid_point_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 32
        && name
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-' || b == b'_')
}

/// Per-location forecast for the homepage widget: precipitation, temperature,
/// wind and solar, each with ensemble spread. Cached briefly — the underlying
/// cycle only moves every 5 minutes, and callers should not have to think
/// about that.
async fn serve_point(State(state): State<AppState>, Path(name): Path<String>) -> Response {
    serve_point_document(&state, &name, "point").await
}

/// The same forecast reduced to "right now", for a widget that only wants a
/// headline and should not have to pull ~80 timesteps to find one.
async fn serve_current(State(state): State<AppState>, Path(name): Path<String>) -> Response {
    serve_point_document(&state, &name, "current").await
}

async fn serve_point_document(state: &AppState, name: &str, prefix: &str) -> Response {
    if !is_valid_point_name(name) {
        return (StatusCode::NOT_FOUND, "unknown location").into_response();
    }

    match tokio::fs::read(state.frame_dir.join(format!("{prefix}_{name}.json"))).await {
        Ok(body) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "public, max-age=120"),
            ],
            body,
        )
            .into_response(),
        Err(_) => (
            StatusCode::NOT_FOUND,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-store"),
            ],
            br#"{"error":"no forecast published for that location"}"#.to_vec(),
        )
            .into_response(),
    }
}

/// Conditions for any coordinate inside the served domain.
///
/// Returns Open-Meteo's ensemble response unchanged: the client already knows
/// how to turn members into percentiles for the precomputed locations, so
/// reducing it here would only duplicate that logic in a second language.
async fn serve_conditions(
    State(state): State<AppState>,
    axum::extract::Query(params): axum::extract::Query<HashMap<String, String>>,
) -> Response {
    let latitude = params.get("lat").and_then(|v| v.parse::<f64>().ok());
    let longitude = params.get("lon").and_then(|v| v.parse::<f64>().ok());

    let (Some(latitude), Some(longitude)) = (latitude, longitude) else {
        return (
            StatusCode::BAD_REQUEST,
            [(header::CONTENT_TYPE, "application/json")],
            br#"{"error":"lat and lon are required"}"#.to_vec(),
        )
            .into_response();
    };

    if !(-90.0..=90.0).contains(&latitude) || !(-180.0..=180.0).contains(&longitude) {
        return (
            StatusCode::BAD_REQUEST,
            [(header::CONTENT_TYPE, "application/json")],
            br#"{"error":"lat or lon is out of range"}"#.to_vec(),
        )
            .into_response();
    }

    match state.conditions.fetch(latitude, longitude).await {
        Ok(body) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "public, max-age=600"),
            ],
            body.to_vec(),
        )
            .into_response(),
        Err(error) => {
            error!("conditions fetch failed: {error}");
            (
                StatusCode::BAD_GATEWAY,
                [
                    (header::CONTENT_TYPE, "application/json"),
                    (header::CACHE_CONTROL, "no-store"),
                ],
                br#"{"error":"could not reach the conditions provider"}"#.to_vec(),
            )
                .into_response()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{is_valid_frame_name, is_valid_point_name};

    #[test]
    fn accepts_generated_names() {
        assert!(is_valid_frame_name("p_1787583900_1787584200.webp"));
        assert!(is_valid_frame_name("o_1787584200.webp"));
    }

    #[test]
    fn rejects_traversal_and_junk() {
        for name in [
            "../manifest.json",
            "p_1_2.webp/../../etc/passwd",
            "/etc/passwd",
            "manifest.json",
            "grid.json",
            "p_abc_123.webp",
            "p_123.webp",
            "p__123.webp",
            "x_1_2.webp",
            "p_1_2.png",
            "o_.webp",
            "o_abc.webp",
            "o_1_2.webp",
            "o_../x.webp",
        ] {
            assert!(!is_valid_frame_name(name), "should reject {name}");
        }
    }

    #[test]
    fn accepts_point_names() {
        for name in ["home", "den-haag", "office_2"] {
            assert!(is_valid_point_name(name), "should accept {name}");
        }
    }

    #[test]
    fn rejects_point_name_traversal() {
        for name in ["", "../manifest", "Home", "a/b", "a.json", "x".repeat(33).as_str()] {
            assert!(!is_valid_point_name(name), "should reject {name:?}");
        }
    }
}
