use std::collections::BTreeMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use serde::Serialize;
use serde_json::{Map, Value};

use crate::constants::PROVIDER_DEBUG_BODY_MAX_BYTES;

pub mod console;

static NEXT_CALL_ID: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "event")]
pub enum ProviderDebugEvent {
    #[serde(rename = "provider.request")]
    Request(ProviderRequestEvent),
    #[serde(rename = "provider.response")]
    Response(ProviderResponseEvent),
    #[serde(rename = "provider.stream.started")]
    StreamStarted(ProviderStreamStartedEvent),
    #[serde(rename = "provider.stream.completed")]
    StreamCompleted(ProviderStreamCompletedEvent),
    #[serde(rename = "provider.error")]
    Error(ProviderErrorEvent),
}

pub trait ProviderDebugHook: Send + Sync {
    fn emit(&self, event: &ProviderDebugEvent);
}

#[derive(Clone, Debug, Serialize)]
pub struct ProviderRequestEvent {
    pub source: &'static str,
    pub call_id: String,
    pub provider: String,
    pub model: String,
    pub stream: bool,
    pub method: String,
    pub url: String,
    pub headers: BTreeMap<String, String>,
    pub body: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body_truncated: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body_original_bytes: Option<usize>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ProviderResponseEvent {
    pub source: &'static str,
    pub call_id: String,
    pub provider: String,
    pub status: u16,
    pub duration_ms: u128,
    pub headers: BTreeMap<String, String>,
    pub body: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body_truncated: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body_original_bytes: Option<usize>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ProviderStreamStartedEvent {
    pub source: &'static str,
    pub call_id: String,
    pub provider: String,
    pub status: u16,
    pub content_type: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ProviderStreamCompletedEvent {
    pub source: &'static str,
    pub call_id: String,
    pub provider: String,
    pub duration_ms: u128,
    pub bytes_received: usize,
    pub frames_received: usize,
    pub events_decoded: usize,
}

#[derive(Clone, Debug, Serialize)]
pub struct ProviderErrorEvent {
    pub source: &'static str,
    pub call_id: String,
    pub provider: String,
    pub duration_ms: u128,
    pub status: Option<u16>,
    pub kind: String,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body: Option<Value>,
}

pub struct BodySnapshot {
    pub body: Value,
    pub body_truncated: Option<bool>,
    pub body_original_bytes: Option<usize>,
}

pub fn next_call_id() -> String {
    format!("call_{:02}", NEXT_CALL_ID.fetch_add(1, Ordering::Relaxed))
}

pub fn request_event(
    call_id: String,
    provider: String,
    model: String,
    stream: bool,
    url: String,
    headers: &[(String, String)],
    body: Value,
) -> ProviderDebugEvent {
    let snapshot = snapshot_json(body);
    ProviderDebugEvent::Request(ProviderRequestEvent {
        source: "litellm-rust",
        call_id,
        provider,
        model,
        stream,
        method: "POST".to_string(),
        url: redact_url(&url),
        headers: redact_headers(headers),
        body: snapshot.body,
        body_truncated: snapshot.body_truncated,
        body_original_bytes: snapshot.body_original_bytes,
    })
}

pub fn response_event(
    call_id: String,
    provider: String,
    status: u16,
    duration_ms: u128,
    headers: &[(String, String)],
    body: ResponseBody,
) -> ProviderDebugEvent {
    let snapshot = body.snapshot();
    ProviderDebugEvent::Response(ProviderResponseEvent {
        source: "litellm-rust",
        call_id,
        provider,
        status,
        duration_ms,
        headers: redact_headers(headers),
        body: snapshot.body,
        body_truncated: snapshot.body_truncated,
        body_original_bytes: snapshot.body_original_bytes,
    })
}

pub fn error_event(
    call_id: String,
    provider: String,
    duration_ms: u128,
    status: Option<u16>,
    kind: String,
    message: String,
    body: Option<Value>,
) -> ProviderDebugEvent {
    ProviderDebugEvent::Error(ProviderErrorEvent {
        source: "litellm-rust",
        call_id,
        provider,
        duration_ms,
        status,
        kind,
        message,
        body: body.map(|value| snapshot_json(value).body),
    })
}

pub fn stream_started(
    call_id: String,
    provider: String,
    status: u16,
    content_type: Option<String>,
) -> ProviderDebugEvent {
    ProviderDebugEvent::StreamStarted(ProviderStreamStartedEvent {
        source: "litellm-rust",
        call_id,
        provider,
        status,
        content_type,
    })
}

pub fn stream_completed(
    call_id: String,
    provider: String,
    duration_ms: u128,
    bytes_received: usize,
    frames_received: usize,
    events_decoded: usize,
) -> ProviderDebugEvent {
    ProviderDebugEvent::StreamCompleted(ProviderStreamCompletedEvent {
        source: "litellm-rust",
        call_id,
        provider,
        duration_ms,
        bytes_received,
        frames_received,
        events_decoded,
    })
}

#[derive(Clone, Debug)]
pub enum ResponseBody {
    Json(Value),
    Binary {
        media_type: Option<String>,
        bytes: usize,
    },
}

impl ResponseBody {
    fn snapshot(self) -> BodySnapshot {
        match self {
            Self::Json(value) => snapshot_json(value),
            Self::Binary { media_type, bytes } => BodySnapshot {
                body: serde_json::json!({"media_type": media_type, "bytes": bytes}),
                body_truncated: None,
                body_original_bytes: None,
            },
        }
    }
}

fn snapshot_json(value: Value) -> BodySnapshot {
    let redacted = redact_value(value);
    let serialized = serde_json::to_vec(&redacted).unwrap_or_default();
    if serialized.len() <= PROVIDER_DEBUG_BODY_MAX_BYTES {
        return BodySnapshot {
            body: redacted,
            body_truncated: None,
            body_original_bytes: None,
        };
    }
    BodySnapshot {
        body: Value::String(
            String::from_utf8_lossy(&serialized[..PROVIDER_DEBUG_BODY_MAX_BYTES]).into_owned(),
        ),
        body_truncated: Some(true),
        body_original_bytes: Some(serialized.len()),
    }
}

fn redact_value(value: Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.into_iter()
                .map(|(key, value)| {
                    if is_secret_key(&key) {
                        (key, Value::String("[REDACTED]".to_string()))
                    } else {
                        (key, redact_value(value))
                    }
                })
                .collect::<Map<_, _>>(),
        ),
        Value::Array(values) => Value::Array(values.into_iter().map(redact_value).collect()),
        other => other,
    }
}

fn is_secret_key(key: &str) -> bool {
    matches!(
        key.to_ascii_lowercase().as_str(),
        "api_key"
            | "apikey"
            | "secret"
            | "password"
            | "token"
            | "access_token"
            | "client_secret"
            | "aws_secret_access_key"
            | "aws_access_key_id"
            | "aws_session_token"
            | "x-amz-security-token"
    )
}

fn redact_headers(headers: &[(String, String)]) -> BTreeMap<String, String> {
    headers
        .iter()
        .map(|(name, value)| {
            let value = if matches!(
                name.to_ascii_lowercase().as_str(),
                "authorization"
                    | "proxy-authorization"
                    | "x-api-key"
                    | "api-key"
                    | "x-amz-security-token"
                    | "cookie"
                    | "set-cookie"
            ) {
                "[REDACTED]".to_string()
            } else {
                value.clone()
            };
            (name.clone(), value)
        })
        .collect()
}

fn redact_url(url: &str) -> String {
    let Ok(mut parsed) = reqwest::Url::parse(url) else {
        return url.to_string();
    };
    let pairs = parsed
        .query_pairs()
        .map(|(key, value)| {
            let value = if matches!(
                key.to_ascii_lowercase().as_str(),
                "x-amz-signature"
                    | "x-amz-credential"
                    | "x-amz-security-token"
                    | "api-key"
                    | "key"
                    | "access_token"
                    | "signature"
            ) {
                "[REDACTED]"
            } else {
                value.as_ref()
            };
            (key.into_owned(), value.to_string())
        })
        .collect::<Vec<_>>();
    parsed.query_pairs_mut().clear().extend_pairs(pairs);
    parsed.to_string()
}

pub type SharedProviderDebugHook = Arc<dyn ProviderDebugHook>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_credentials_recursively() {
        let event = request_event(
            "call_01".to_string(),
            "anthropic".to_string(),
            "claude".to_string(),
            false,
            "https://example.test?signature=secret&x=ok".to_string(),
            &[("Authorization".to_string(), "Bearer secret".to_string())],
            serde_json::json!({"nested": {"token": "secret"}, "prompt": "visible"}),
        );
        let json = serde_json::to_string(&event).expect("serializes");
        assert!(!json.contains("secret"));
        assert!(json.contains("visible"));
        assert!(json.contains("[REDACTED]"));
    }
}
