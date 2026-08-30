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

mod point;

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
    /// Ad-hoc point forecasts, so a phone asking every quarter of an hour about
    /// the same kilometre does not re-decode a cycle of frames to be told the
    /// same thing.
    documents: point::DocumentCache,
    /// The published extent, cached from the manifest. See `served_domain`.
    domain: Arc<Mutex<Option<(Instant, Domain)>>>,
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
    budget: Arc<TokenBucket>,
    /// Whether the budget was spent the last time anyone asked, so running out
    /// — and recovering — is logged once each rather than on every request.
    /// Same reasoning as `AppState::was_stale`.
    budget_spent: Arc<AtomicBool>,
}

/// Why a conditions fetch produced no body. The two want different answers:
/// a spent budget is this server declining to spend more, and is worth a
/// Retry-After; an upstream failure is Open-Meteo's problem and is a 502.
enum ConditionsError {
    Budget,
    Upstream(String),
}

/// A ceiling on how often this server calls Open-Meteo, across every caller.
///
/// The per-IP rate limit in front of `/api/conditions` bounds any one client.
/// This bounds the *sum* of them, which is the number Open-Meteo's free tier
/// actually counts. Without it, a sweep spread over enough source addresses —
/// or one page bug that refetches on every mouse move — spends the day's quota
/// in an afternoon, and click-anywhere then fails for everyone until midnight.
///
/// Deliberately not per-IP: a quota is a shared resource, so the thing being
/// protected is shared too. Only an *uncached* coordinate takes a token.
struct TokenBucket {
    /// Tokens and the instant they were last computed, under one lock so the
    /// two cannot drift apart when several requests refill concurrently.
    state: Mutex<(f64, Instant)>,
    capacity: f64,
    per_second: f64,
}

impl TokenBucket {
    fn new(capacity: f64, per_second: f64) -> Self {
        Self {
            state: Mutex::new((capacity, Instant::now())),
            capacity,
            per_second,
        }
    }

    fn take(&self) -> bool {
        self.take_at(Instant::now())
    }

    /// Split from `take` so refill can be asserted without sleeping.
    fn take_at(&self, now: Instant) -> bool {
        // A poisoned lock means some other request panicked mid-refill. Fail
        // closed: declining to spend quota is the safe direction to be wrong in.
        let Ok(mut state) = self.state.lock() else {
            return false;
        };
        let (tokens, refilled) = *state;
        let elapsed = now.saturating_duration_since(refilled).as_secs_f64();
        let tokens = (tokens + elapsed * self.per_second).min(self.capacity);
        if tokens < 1.0 {
            *state = (tokens, now);
            return false;
        }
        *state = (tokens - 1.0, now);
        true
    }
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
            // Sized against Open-Meteo's free tier (order 10k calls/day, which
            // the ingestor barely touches: one call per configured location per
            // refresh). 3/minute sustained is ~4.3k/day, comfortably under it,
            // and the burst of 60 means a person clicking around the map never
            // notices the cap exists — only a sweep does.
            budget: Arc::new(TokenBucket::new(
                env_number("CONDITIONS_UPSTREAM_BURST", 60) as f64,
                env_number("CONDITIONS_UPSTREAM_PER_MINUTE", 3) as f64 / 60.0,
            )),
            budget_spent: Arc::new(AtomicBool::new(false)),
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

    async fn fetch(&self, latitude: f64, longitude: f64) -> Result<Arc<Vec<u8>>, ConditionsError> {
        let key = Self::key(latitude, longitude);
        if let Some(body) = self.cached(key) {
            return Ok(body);
        }

        // Charged here rather than in the handler: a cache hit costs Open-Meteo
        // nothing, so it should not cost a token either.
        //
        // Running out — and recovering — is logged on the edge from here for
        // the same reason. Only a call that actually reaches for a token can
        // tell the two states apart: with the bucket empty, a cached
        // coordinate still answers 200, so a handler watching for a successful
        // response would call that a recovery and flap on every cache hit.
        if !self.budget.take() {
            if !self.budget_spent.swap(true, Ordering::Relaxed) {
                warn!("conditions budget is spent - declining upstream fetches until it refills");
            }
            return Err(ConditionsError::Budget);
        }
        if self.budget_spent.swap(false, Ordering::Relaxed) {
            info!("conditions budget has refilled");
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
            .map_err(|error| ConditionsError::Upstream(error.to_string()))?;

        if !response.status().is_success() {
            return Err(ConditionsError::Upstream(format!(
                "upstream returned {}",
                response.status()
            )));
        }

        let body = Arc::new(
            response
                .bytes()
                .await
                .map_err(|error| ConditionsError::Upstream(error.to_string()))?
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
        documents: point::DocumentCache::new(),
        domain: Arc::new(Mutex::new(None)),
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
        .route("/api/point", get(serve_point_at))
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

    // Both probes sit outside that trace on purpose. /healthz answers 503 when
    // the *data* is stale, which is not a failed request, and the container
    // health check asks every 30 seconds — inside, one KNMI outage would be a
    // WARN every half minute saying nothing the staleness edge has not said
    // once. They are also outside `require_api_key`, which is layered onto
    // `served` alone: no setting of API_KEY_PROTECTED_PREFIXES can close the
    // endpoints you need most when something is wrong.
    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/livez", get(livez))
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

/// Is the process serving?
///
/// Deliberately says nothing about how fresh the data is. This is what the
/// container healthcheck reads, and an unhealthy container is dropped from
/// Traefik's routing table — so anything that answers 503 here takes the whole
/// site down. An upstream publishing gap must degrade the map, not delete it.
/// Freshness is /healthz, which the browser reads.
///
/// The one thing worth failing on is a frame directory that cannot be read at
/// all: the mount is broken and no request can be answered correctly anyway.
/// Keep it to this single stat — a probe that does real work against a volume
/// that might block would reintroduce the very failure this endpoint removes.
async fn livez(State(state): State<AppState>) -> impl IntoResponse {
    let readable = tokio::fs::metadata(&state.frame_dir).await.is_ok();
    let (status, detail) = liveness(readable);
    (
        status,
        [(header::CACHE_CONTROL, "no-store")],
        axum::Json(json!({
            "status": if readable { "alive" } else { "unavailable" },
            "detail": detail,
        })),
    )
}

/// Split from the handler so the verdict can be asserted without a router.
fn liveness(frame_dir_readable: bool) -> (StatusCode, &'static str) {
    if frame_dir_readable {
        (StatusCode::OK, "serving")
    } else {
        (
            StatusCode::SERVICE_UNAVAILABLE,
            "frame directory is not readable",
        )
    }
}

/// Reports the freshness of the ingestor's output, for the browser's staleness
/// banner and for anyone asking by hand.
///
/// Explicitly *not* the container's liveness probe. A 503 here means the data
/// is old, not that the process is broken, and wiring it into a container
/// health check deletes the router in front of a server that is working — see
/// the warning in the README. That job belongs to /livez.
async fn healthz(State(state): State<AppState>) -> impl IntoResponse {
    let age = forecast_age(&state).await;
    let (status, ok, detail) = freshness(age, state.max_manifest_age);

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

/// Split from the handler so the stale case can be asserted without a router.
///
/// It is also the guard against the two endpoints quietly collapsing back into
/// one answer: this one is allowed to say 503, /livez is not.
fn freshness(age: Option<u64>, max_age: u64) -> (StatusCode, bool, &'static str) {
    match age {
        None => (
            StatusCode::SERVICE_UNAVAILABLE,
            false,
            "no manifest yet - the ingestor has not published a cycle",
        ),
        Some(seconds) if seconds > max_age => (
            StatusCode::SERVICE_UNAVAILABLE,
            false,
            "the newest forecast is stale - the ingestor is not keeping up",
        ),
        Some(_) => (StatusCode::OK, true, "ok"),
    }
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
        // "p" is the rain field, "s" the ensemble spread that accompanies it;
        // both are named for the cycle and the timestep, so both prune together.
        "p" | "s" => match rest.split_once('_') {
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
                // This body is only served to a request that presented a key,
                // and it carries the location's coordinates - so a shared cache
                // must not be free to hand it to the next caller who presents
                // none. Same reasoning as /api/config, which has always said
                // this; these two were the pair that did not.
                //
                // Only on the 200: the unauthorised answer is a 401 from
                // require_api_key and the not-found answer below are both
                // no-store, and a response that is never stored has nothing to
                // key on.
                (header::VARY, "x-api-key"),
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

/// The two coordinates every ad-hoc endpoint takes, validated once.
fn coordinates(params: &HashMap<String, String>) -> Result<(f64, f64), Response> {
    let bad_request = |detail: &'static [u8]| -> Response {
        (
            StatusCode::BAD_REQUEST,
            [(header::CONTENT_TYPE, "application/json")],
            detail.to_vec(),
        )
            .into_response()
    };

    let latitude = params.get("lat").and_then(|v| v.parse::<f64>().ok());
    let longitude = params.get("lon").and_then(|v| v.parse::<f64>().ok());
    let (Some(latitude), Some(longitude)) = (latitude, longitude) else {
        return Err(bad_request(br#"{"error":"lat and lon are required"}"#));
    };
    if !(-90.0..=90.0).contains(&latitude) || !(-180.0..=180.0).contains(&longitude) {
        return Err(bad_request(br#"{"error":"lat or lon is out of range"}"#));
    }
    Ok((latitude, longitude))
}

/// A point forecast for a coordinate nobody configured.
///
/// The named-location endpoint above serves a file the ingestor wrote while
/// KNMI's members were in memory. This one assembles the same document shape
/// on demand, for callers that cannot be a configured location — a phone
/// widget, a background worker asking whether to raise a notification.
///
/// Two sources, fetched together because neither waits on the other: the rain
/// series is sampled out of the published frames, and everything else comes
/// from the ensemble proxy. Either may fail on its own without emptying the
/// answer — outside the domain there are no frames but the conditions still
/// hold, and with Open-Meteo unreachable the next six hours of rain are still
/// the thing that was actually asked for.
async fn serve_point_at(
    State(state): State<AppState>,
    axum::extract::Query(params): axum::extract::Query<HashMap<String, String>>,
) -> Response {
    let (latitude, longitude) = match coordinates(&params) {
        Ok(pair) => pair,
        Err(response) => return response,
    };

    let Ok(raw) = tokio::fs::read(state.frame_dir.join(MANIFEST_NAME)).await else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-store"),
                (header::RETRY_AFTER, "10"),
            ],
            br#"{"status":"warming_up","error":"no forecast has been published yet"}"#.to_vec(),
        )
            .into_response();
    };
    let Ok(manifest) = serde_json::from_slice::<serde_json::Value>(&raw) else {
        error!("manifest is not valid JSON");
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-store"),
            ],
            br#"{"error":"the published manifest could not be read"}"#.to_vec(),
        )
            .into_response();
    };

    // The manifest is rewritten whenever the frame list changes, so its
    // `generated_at` is exactly the token that says whether a cached answer is
    // still the current one.
    let published = manifest
        .get("generated_at")
        .and_then(serde_json::Value::as_i64)
        .unwrap_or_default();
    if let Some(body) = state.documents.get(latitude, longitude, published) {
        return point_response(body.to_vec());
    }

    // Sampling reads and decodes images, so it goes to a blocking thread while
    // the conditions request is in flight.
    let sampling = {
        let frame_dir = state.frame_dir.clone();
        let manifest = manifest.clone();
        tokio::task::spawn_blocking(move || {
            let Some(grid) = point::Grid::from_manifest(&manifest) else {
                warn!("manifest carries no usable grid");
                return Vec::new();
            };
            let frames = manifest
                .get("frames")
                .and_then(serde_json::Value::as_array)
                .cloned()
                .unwrap_or_default();
            // `None` when the ingestor publishes no spread layer, which costs
            // the band and nothing else: the series is still sampled and the
            // document still says what it is drawn from.
            let spread = point::Spread::from_manifest(&manifest);
            point::sample_series(&frame_dir, &grid, spread.as_ref(), &frames, latitude, longitude)
        })
    };

    let (sampled, conditions) =
        tokio::join!(sampling, state.conditions.fetch(latitude, longitude));

    let rain = sampled.unwrap_or_else(|error| {
        error!("sampling the frames panicked: {error}");
        Vec::new()
    });
    let conditions = match conditions {
        Ok(body) => serde_json::from_slice::<serde_json::Value>(&body).ok(),
        // Worth a line, not worth a failed request: the rain series is the
        // part a caller at this endpoint actually came for. A spent budget is
        // named as such rather than logged as an upstream failure — the two
        // send whoever reads this to different places.
        Err(ConditionsError::Budget) => {
            warn!("conditions skipped for an ad-hoc point: the Open-Meteo budget is spent");
            None
        }
        Err(ConditionsError::Upstream(error)) => {
            warn!("conditions unavailable for an ad-hoc point: {error}");
            None
        }
    };

    if rain.is_empty() && conditions.is_none() {
        return (
            StatusCode::BAD_GATEWAY,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-store"),
            ],
            br#"{"error":"no forecast could be assembled for that point"}"#.to_vec(),
        )
            .into_response();
    }

    let generated_at = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|since| since.as_secs() as i64)
        .unwrap_or_default();
    let document = point::assemble(
        &manifest,
        latitude,
        longitude,
        rain,
        conditions.as_ref(),
        generated_at,
    );
    let body = serde_json::to_vec(&document).unwrap_or_default();

    // Only worth remembering an answer that was assembled from both halves. A
    // document built while Open-Meteo was down would otherwise be repeated for
    // the rest of the cycle, long after the outage ended.
    if conditions.is_some() {
        state
            .documents
            .insert(latitude, longitude, published, Arc::new(body.clone()));
    }
    point_response(body)
}

fn point_response(body: Vec<u8>) -> Response {
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "application/json"),
            // Matches the named-location endpoint: the cycle underneath only
            // moves every five minutes.
            (header::CACHE_CONTROL, "public, max-age=120"),
        ],
        body,
    )
        .into_response()
}

/// The rectangle this server publishes frames for.
#[derive(Clone, Copy, Debug, PartialEq)]
struct Domain {
    west: f64,
    east: f64,
    south: f64,
    north: f64,
}

impl Domain {
    /// Inclusive, so a click on the very edge of the rendered image counts.
    /// A NaN coordinate compares false against everything and so is rejected
    /// here rather than needing a check of its own — `"NaN".parse::<f64>()`
    /// succeeds, so one does reach this.
    fn contains(&self, latitude: f64, longitude: f64) -> bool {
        (self.south..=self.north).contains(&latitude)
            && (self.west..=self.east).contains(&longitude)
    }
}

/// How long a read extent is reused before the manifest is consulted again.
/// The bounds only change when the ingestor's crop configuration does, which is
/// a restart-level event; this is just so such a change is eventually noticed.
const DOMAIN_REFRESH: Duration = Duration::from_secs(300);

/// Pull the extent out of the manifest's `bounds` polygon.
///
/// Takes the extremes of the corners rather than assuming which corner comes
/// first: the winding order is the ingestor's business, and a reader that
/// silently depends on it is a reader that breaks quietly when it changes.
fn domain_from_manifest(raw: &[u8]) -> Option<Domain> {
    let manifest: serde_json::Value = serde_json::from_slice(raw).ok()?;
    let corners = manifest.get("bounds")?.as_array()?;
    if corners.is_empty() {
        return None;
    }

    let (mut longitudes, mut latitudes) = (Vec::new(), Vec::new());
    for corner in corners {
        let pair = corner.as_array()?;
        longitudes.push(pair.first()?.as_f64()?);
        latitudes.push(pair.get(1)?.as_f64()?);
    }

    Some(Domain {
        west: longitudes.iter().copied().fold(f64::INFINITY, f64::min),
        east: longitudes.iter().copied().fold(f64::NEG_INFINITY, f64::max),
        south: latitudes.iter().copied().fold(f64::INFINITY, f64::min),
        north: latitudes.iter().copied().fold(f64::NEG_INFINITY, f64::max),
    })
}

/// The served extent, read from the manifest and cached.
///
/// A failed re-read keeps the extent already in hand rather than discarding it:
/// the manifest is rewritten every cycle, and catching it mid-write must not
/// turn into either an open proxy or a dead click-anywhere feature. Only the
/// very first read can come up empty, and at that point nothing else works
/// either — there are no frames to click on yet.
async fn served_domain(state: &AppState) -> Option<Domain> {
    if let Ok(cached) = state.domain.lock() {
        if let Some((read, domain)) = *cached {
            if read.elapsed() < DOMAIN_REFRESH {
                return Some(domain);
            }
        }
    }

    let fresh = tokio::fs::read(state.frame_dir.join(MANIFEST_NAME))
        .await
        .ok()
        .as_deref()
        .and_then(domain_from_manifest);

    match state.domain.lock() {
        Ok(mut cached) => match fresh {
            Some(domain) => {
                *cached = Some((Instant::now(), domain));
                Some(domain)
            }
            None => (*cached).map(|(_, domain)| domain),
        },
        Err(_) => fresh,
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
    let (latitude, longitude) = match coordinates(&params) {
        Ok(pair) => pair,
        Err(response) => return response,
    };

    // The whole globe used to be in range here, which made the set of distinct
    // cache keys — and so the set of possible outbound calls — effectively
    // unbounded. The map can only show what the ingestor publishes frames for,
    // so anything outside that rectangle is a typo or a sweep either way.
    //
    // Rejected rather than clamped on purpose: clamping would answer a click on
    // London with Amsterdam's weather and label it London, which is a worse
    // failure than saying no. The frontend already treats a failed conditions
    // fetch as "no extra conditions" and still draws the radar series.
    let Some(domain) = served_domain(&state).await else {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-store"),
                (header::RETRY_AFTER, "10"),
            ],
            br#"{"status":"warming_up","error":"the served area is not known yet"}"#.to_vec(),
        )
            .into_response();
    };

    if !domain.contains(latitude, longitude) {
        return (
            StatusCode::BAD_REQUEST,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-store"),
            ],
            br#"{"error":"lat and lon must be inside the area this server publishes"}"#.to_vec(),
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
        // Not the caller's fault and not Open-Meteo's: this server is declining
        // to spend more of its quota. Retry-After rather than a bare failure.
        Err(ConditionsError::Budget) => (
            StatusCode::SERVICE_UNAVAILABLE,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-store"),
                (header::RETRY_AFTER, "60"),
            ],
            br#"{"error":"this server is out of on-demand forecast budget; try again shortly"}"#
                .to_vec(),
        )
            .into_response(),
        Err(ConditionsError::Upstream(error)) => {
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
    use super::{
        domain_from_manifest, freshness, is_valid_frame_name, is_valid_point_name, liveness,
        Duration, Instant, StatusCode, TokenBucket,
    };

    /// The real shape the ingestor publishes: a four-corner polygon, and not in
    /// an order this code is allowed to assume.
    const BOUNDS: &[u8] = br#"{"bounds":[[-0.0145,56.011],[11.2955,56.011],[11.2955,48.991],[-0.0145,48.991]]}"#;

    /// The two endpoints answer different questions, and the whole point of
    /// /livez is that no state of the *data* can make it fail. If someone ever
    /// wires freshness back into liveness, these are what should stop them.
    #[test]
    fn liveness_fails_only_on_an_unreadable_frame_dir() {
        assert_eq!(liveness(true).0, StatusCode::OK);
        assert_eq!(liveness(false).0, StatusCode::SERVICE_UNAVAILABLE);
    }

    #[test]
    fn freshness_still_degrades_on_a_stale_manifest() {
        let max = 1200;
        assert_eq!(freshness(Some(0), max).0, StatusCode::OK);
        assert_eq!(freshness(Some(max), max).0, StatusCode::OK);
        assert!(freshness(Some(max), max).1);

        for age in [Some(max + 1), None] {
            let (status, ok, _) = freshness(age, max);
            assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "age {age:?}");
            assert!(!ok, "age {age:?}");
        }
    }

    #[test]
    fn accepts_generated_names() {
        assert!(is_valid_frame_name("p_1787583900_1787584200.webp"));
        assert!(is_valid_frame_name("s_1787583900_1787584200.webp"));
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
            "s_123.webp",
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
    fn reads_the_extent_from_the_manifest_polygon() {
        let domain = domain_from_manifest(BOUNDS).expect("bounds should parse");
        assert_eq!(domain.west, -0.0145);
        assert_eq!(domain.east, 11.2955);
        assert_eq!(domain.south, 48.991);
        assert_eq!(domain.north, 56.011);

        for junk in [
            &b"not json"[..],
            br#"{}"#,
            br#"{"bounds":[]}"#,
            br#"{"bounds":[["west","north"]]}"#,
        ] {
            assert!(domain_from_manifest(junk).is_none(), "should reject {junk:?}");
        }
    }

    /// The point of the extent check is that the set of coordinates that can
    /// cause an outbound call is finite. A globe-wide check does not do that.
    #[test]
    fn the_extent_admits_only_what_is_published() {
        let domain = domain_from_manifest(BOUNDS).unwrap();

        assert!(domain.contains(52.08, 4.31), "Den Haag is on the map");
        assert!(domain.contains(48.991, -0.0145), "the corner counts as inside");

        for (latitude, longitude) in [
            (35.68, 139.69),          // Tokyo
            (51.51, -0.13),           // London: just west of the domain
            (56.02, 5.0),             // just north
            (f64::NAN, 4.31),         // "NaN".parse::<f64>() succeeds
            (52.08, f64::NAN),
        ] {
            assert!(
                !domain.contains(latitude, longitude),
                "should reject {latitude},{longitude}"
            );
        }
    }

    #[test]
    fn the_budget_stops_at_its_capacity_and_refills_over_time() {
        // Three per minute, holding at most two.
        let bucket = TokenBucket::new(2.0, 3.0 / 60.0);
        let start = Instant::now();

        assert!(bucket.take_at(start));
        assert!(bucket.take_at(start));
        assert!(!bucket.take_at(start), "capacity is two");

        // Twenty seconds buys exactly one token back, and no more.
        let later = start + Duration::from_secs(20);
        assert!(bucket.take_at(later));
        assert!(!bucket.take_at(later));

        // An idle hour cannot bank more than the bucket holds.
        let much_later = start + Duration::from_secs(3600);
        assert!(bucket.take_at(much_later));
        assert!(bucket.take_at(much_later));
        assert!(!bucket.take_at(much_later), "capacity still caps the refill");
    }

    #[test]
    fn rejects_point_name_traversal() {
        for name in ["", "../manifest", "Home", "a/b", "a.json", "x".repeat(33).as_str()] {
            assert!(!is_valid_point_name(name), "should reject {name:?}");
        }
    }
}
