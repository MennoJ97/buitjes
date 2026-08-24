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
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tower_http::{cors::CorsLayer, services::ServeDir};

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

    let from_header = request
        .headers()
        .get("x-api-key")
        .and_then(|value| value.to_str().ok())
        .map(str::to_string);
    let from_query = request.uri().query().and_then(|query| {
        query.split('&').find_map(|pair| {
            pair.strip_prefix("key=").map(|value| value.to_string())
        })
    });

    let presented = from_header.or(from_query).unwrap_or_default();
    let accepted = state
        .api_keys
        .iter()
        .any(|key| constant_time_eq(key, &presented));

    if accepted {
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

/// Cross-origin policy for the API.
///
/// The homepage widget is served from a different origin, so *some* CORS is
/// required. `CORS_ALLOWED_ORIGINS` is a comma-separated allowlist; the literal
/// `*` keeps the old permissive behaviour for anyone who wants it. Unset means
/// same-origin only, which is the right default for a server on the public
/// internet — the widget's origin has to be named deliberately.
fn cors_layer() -> CorsLayer {
    let configured = std::env::var("CORS_ALLOWED_ORIGINS").unwrap_or_default();
    let configured = configured.trim();

    if configured == "*" {
        println!("CORS: allowing any origin");
        return CorsLayer::permissive();
    }
    if configured.is_empty() {
        println!("CORS: same-origin only (set CORS_ALLOWED_ORIGINS to allow a widget host)");
        return CorsLayer::new();
    }

    let origins: Vec<HeaderValue> = configured
        .split(',')
        .filter_map(|origin| {
            let origin = origin.trim();
            match origin.parse::<HeaderValue>() {
                Ok(value) => Some(value),
                Err(_) => {
                    eprintln!("CORS: ignoring unparseable origin {origin:?}");
                    None
                }
            }
        })
        .collect();

    println!("CORS: allowing {} origin(s)", origins.len());
    CorsLayer::new()
        .allow_origin(origins)
        .allow_methods([Method::GET])
}

#[tokio::main]
async fn main() {
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
    };

    if state.api_keys.is_empty() {
        println!("API keys: disabled (every endpoint is open)");
    } else {
        println!(
            "API keys: {} configured, guarding {}",
            state.api_keys.len(),
            state.protected_prefixes.join(", ")
        );
    }

    let app = Router::new()
        .route("/healthz", get(healthz))
        .route("/api/config", get(serve_manifest))
        .route("/api/frames/:file", get(serve_frame))
        .route("/api/point/:name", get(serve_point))
        .route("/api/current/:name", get(serve_current))
        .fallback_service(ServeDir::new("frontend"))
        .layer(axum::middleware::from_fn_with_state(
            state.clone(),
            require_api_key,
        ))
        .layer(cors_layer())
        .with_state(state.clone());

    let addr = SocketAddr::from(([0, 0, 0, 0], 3000));
    println!(
        "Server running on http://{} (frames from {})",
        addr,
        state.frame_dir.display()
    );
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
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

async fn serve_manifest(State(state): State<AppState>) -> Response {
    match tokio::fs::read(state.frame_dir.join(MANIFEST_NAME)).await {
        Ok(body) => (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CACHE_CONTROL, "no-cache"),
            ],
            body,
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
