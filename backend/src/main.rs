use axum::{
    routing::get,
    Router,
    response::IntoResponse,
    http::{header, StatusCode},
    extract::Query,
};
use image::{ImageBuffer, Rgba, codecs::webp::WebPEncoder, ExtendedColorType, ImageEncoder};
use tower_http::{services::ServeDir, cors::CorsLayer};
use std::net::SocketAddr;
use std::io::Cursor;
use serde::Deserialize;
use serde_json::json;

#[tokio::main]
async fn main() {
    let app = Router::new()
        .route("/api/radar.webp", get(serve_radar))
        .route("/api/config", get(serve_config))
        .nest_service("/", ServeDir::new("frontend"))
        .layer(CorsLayer::permissive());

    let addr = SocketAddr::from(([0, 0, 0, 0], 3000));
    println!("Server running on http://{}", addr);
    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn serve_config() -> impl IntoResponse {
    // Mock: Generate timestamps for every 5 mins over the past hour
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
    // Round to nearest 5 min (300 sec)
    let current_step = now - (now % 300);
    let mut timestamps = Vec::new();
    for i in 0..12 {
        timestamps.push(current_step - ((11 - i) * 300));
    }
    
    axum::Json(json!({
        "timestamps": timestamps
    }))
}

#[derive(Deserialize)]
struct RadarQuery {
    t: Option<u64>,
}

async fn serve_radar(Query(query): Query<RadarQuery>) -> impl IntoResponse {
    let width = 500;
    let height = 500;
    let mut img = ImageBuffer::new(width, height);

    // Use requested timestamp or current time
    let time = query.t.unwrap_or_else(|| std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs());
    
    // Animate mock data based on the timestamp (simulate movement)
    let cx = width as f32 / 2.0 + ((time as f32) / 300.0 * 0.5).sin() * 150.0;
    let cy = height as f32 / 2.0 + ((time as f32) / 300.0 * 0.5).cos() * 150.0;

    for (x, y, pixel) in img.enumerate_pixels_mut() {
        let dist = ((x as f32 - cx).powi(2) + (y as f32 - cy).powi(2)).sqrt();
        let precip = if dist < 150.0 {
            (150.0 - dist) * 0.3 // mm/h up to 45
        } else {
            0.0
        };

        let max_precip = 100.0;
        let normalized = (precip / max_precip).clamp(0.0, 1.0);
        let val_16bit = (normalized * 65535.0) as u16;
        
        let r = (val_16bit >> 8) as u8;
        let g = (val_16bit & 0xFF) as u8;
        let b = 0;
        let a = if precip > 0.1 { 255 } else { 0 };

        *pixel = Rgba([r, g, b, a]);
    }

    let mut buffer = Cursor::new(Vec::new());
    let encoder = WebPEncoder::new_lossless(&mut buffer);
    encoder.write_image(img.as_raw(), width, height, ExtendedColorType::Rgba8).unwrap();

    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "image/webp"), (header::CACHE_CONTROL, "public, max-age=31536000")],
        buffer.into_inner(),
    )
}
