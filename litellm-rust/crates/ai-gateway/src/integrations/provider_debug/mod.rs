use std::collections::BTreeMap;
use std::fmt::Display;
use std::sync::Arc;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::{AtomicU64, Ordering};

use bytes::Bytes;
use futures_util::Stream;
use futures_util::StreamExt;
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

pub struct RequestEventInput {
    pub call_id: String,
    pub provider: String,
    pub model: String,
    pub stream: bool,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Value,
}

pub struct ResponseEventInput {
    pub call_id: String,
    pub provider: String,
    pub status: u16,
    pub duration_ms: u128,
    pub headers: Vec<(String, String)>,
    pub body: ResponseBody,
}

pub struct ErrorEventInput {
    pub call_id: String,
    pub provider: String,
    pub duration_ms: u128,
    pub status: Option<u16>,
    pub kind: &'static str,
    pub message: String,
    pub body: Option<ResponseBody>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ProviderRequestEvent {
    pub source: &'static str,
    pub call_id: String,
    pub provider: String,
    pub model: String,
    pub stream: bool,
    pub method: &'static str,
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
    pub kind: &'static str,
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

pub fn request_event(input: RequestEventInput) -> ProviderDebugEvent {
    let snapshot = snapshot_json(input.body);
    ProviderDebugEvent::Request(ProviderRequestEvent {
        source: "litellm-rust",
        call_id: input.call_id,
        provider: input.provider,
        model: input.model,
        stream: input.stream,
        method: "POST",
        url: redact_url(&input.url),
        headers: redact_headers(&input.headers),
        body: snapshot.body,
        body_truncated: snapshot.body_truncated,
        body_original_bytes: snapshot.body_original_bytes,
    })
}

pub fn response_event(input: ResponseEventInput) -> ProviderDebugEvent {
    let snapshot = input.body.snapshot();
    ProviderDebugEvent::Response(ProviderResponseEvent {
        source: "litellm-rust",
        call_id: input.call_id,
        provider: input.provider,
        status: input.status,
        duration_ms: input.duration_ms,
        headers: redact_headers(&input.headers),
        body: snapshot.body,
        body_truncated: snapshot.body_truncated,
        body_original_bytes: snapshot.body_original_bytes,
    })
}

pub fn error_event(input: ErrorEventInput) -> ProviderDebugEvent {
    ProviderDebugEvent::Error(ProviderErrorEvent {
        source: "litellm-rust",
        call_id: input.call_id,
        provider: input.provider,
        duration_ms: input.duration_ms,
        status: input.status,
        kind: input.kind,
        message: input.message,
        body: input.body.map(|body| body.snapshot().body),
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

pub struct StreamCounter {
    pub call_id: String,
    pub provider: String,
    pub hook: Option<Arc<dyn ProviderDebugHook>>,
    pub started: std::time::Instant,
    pub bytes_received: AtomicUsize,
    pub frames_received: AtomicUsize,
    pub events_decoded: AtomicUsize,
}

impl StreamCounter {
    pub fn new(
        call_id: String,
        provider: String,
        hook: Option<Arc<dyn ProviderDebugHook>>,
        started: std::time::Instant,
    ) -> Self {
        Self {
            call_id,
            provider,
            hook,
            started,
            bytes_received: AtomicUsize::new(0),
            frames_received: AtomicUsize::new(0),
            events_decoded: AtomicUsize::new(0),
        }
    }

    pub fn record(&self, bytes: usize, events: usize) {
        self.bytes_received.fetch_add(bytes, Ordering::Relaxed);
        self.frames_received.fetch_add(1, Ordering::Relaxed);
        self.events_decoded.fetch_add(events, Ordering::Relaxed);
    }

    fn completed(&self) {
        if let Some(hook) = &self.hook {
            hook.emit(&stream_completed(
                self.call_id.clone(),
                self.provider.clone(),
                self.started.elapsed().as_millis(),
                self.bytes_received.load(Ordering::Relaxed),
                self.frames_received.load(Ordering::Relaxed),
                self.events_decoded.load(Ordering::Relaxed),
            ));
        }
    }

    fn failed(&self, error: impl Display) {
        if let Some(hook) = &self.hook {
            hook.emit(&error_event(ErrorEventInput {
                call_id: self.call_id.clone(),
                provider: self.provider.clone(),
                duration_ms: self.started.elapsed().as_millis(),
                status: None,
                kind: "stream_error",
                message: error.to_string(),
                body: None,
            }));
        }
    }
}

pub fn count_forwarded_stream<S, E>(
    stream: S,
    counter: Arc<StreamCounter>,
) -> impl Stream<Item = Result<Bytes, E>>
where
    S: Stream<Item = Result<Bytes, E>> + Send + 'static,
    E: Display + Send + 'static,
{
    futures_util::stream::unfold(
        (Box::pin(stream), counter, None::<u8>, false),
        |(mut stream, counter, trailing, failed)| async move {
            match stream.next().await {
                None => {
                    if !failed {
                        counter.completed();
                    }
                    None
                }
                Some(Ok(bytes)) => {
                    let events = bytes.windows(2).filter(|window| *window == b"\n\n").count()
                        + usize::from(trailing == Some(b'\n') && bytes.first() == Some(&b'\n'));
                    let trailing = bytes.last().copied();
                    counter.record(bytes.len(), events);
                    Some((Ok(bytes), (stream, counter, trailing, failed)))
                }
                Some(Err(error)) => {
                    counter.failed(&error);
                    Some((Err(error), (stream, counter, trailing, true)))
                }
            }
        },
    )
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
    let Some(_) = parsed.query() else {
        return parsed.to_string();
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

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use bytes::Bytes;
    use futures_util::{StreamExt, stream};

    use super::*;

    #[derive(Clone, Default)]
    struct RecordingHook(Arc<Mutex<Vec<ProviderDebugEvent>>>);

    impl ProviderDebugHook for RecordingHook {
        fn emit(&self, event: &ProviderDebugEvent) {
            self.0.lock().expect("recording lock").push(event.clone());
        }
    }

    #[test]
    fn redacts_credentials_recursively() {
        let event = request_event(RequestEventInput {
            call_id: "call_01".to_string(),
            provider: "anthropic".to_string(),
            model: "claude".to_string(),
            stream: false,
            url: "https://example.test?signature=secret&x=ok".to_string(),
            headers: vec![("Authorization".to_string(), "Bearer secret".to_string())],
            body: serde_json::json!({"nested": {"token": "secret"}, "prompt": "visible"}),
        });
        let json = serde_json::to_string(&event).expect("serializes");
        assert!(!json.contains("secret"));
        assert!(json.contains("visible"));
        assert!(json.contains("[REDACTED]"));
    }

    #[test]
    fn redact_url_preserves_queryless_encoded_paths() {
        assert_eq!(
            redact_url("https://example.test/v1%3A0/invoke"),
            "https://example.test/v1%3A0/invoke"
        );
    }

    #[test]
    fn redact_url_preserves_non_secret_query_params() {
        let redacted = redact_url(
            "https://example.test/invoke?X-Amz-Signature=sig&X-Amz-Credential=cred&foo=bar",
        );
        assert!(redacted.contains("X-Amz-Signature=%5BREDACTED%5D"));
        assert!(redacted.contains("X-Amz-Credential=%5BREDACTED%5D"));
        assert!(redacted.contains("foo=bar"));
    }

    #[tokio::test]
    async fn counting_stream_forwards_chunks_and_counts_split_delimiters() {
        let hook = RecordingHook::default();
        let counter = Arc::new(StreamCounter::new(
            "call_01".to_string(),
            "anthropic".to_string(),
            Some(Arc::new(hook.clone())),
            std::time::Instant::now(),
        ));
        let input = stream::iter(vec![
            Ok::<_, std::io::Error>(Bytes::from_static(b"data: one\n")),
            Ok(Bytes::from_static(b"\ndata: two\n\n")),
        ]);
        let output = count_forwarded_stream(input, counter)
            .collect::<Vec<_>>()
            .await;
        assert_eq!(
            output
                .iter()
                .map(|item| item.as_ref().expect("chunk").as_ref())
                .collect::<Vec<_>>(),
            vec![b"data: one\n".as_slice(), b"\ndata: two\n\n".as_slice()]
        );
        let events = hook.0.lock().expect("recording lock");
        assert!(matches!(
            events.last(),
            Some(ProviderDebugEvent::StreamCompleted(event))
                if event.bytes_received == 22
                    && event.frames_received == 2
                    && event.events_decoded == 2
        ));
    }

    #[tokio::test]
    async fn counting_stream_reports_stream_error_without_completion() {
        let hook = RecordingHook::default();
        let counter = Arc::new(StreamCounter::new(
            "call_02".to_string(),
            "anthropic".to_string(),
            Some(Arc::new(hook.clone())),
            std::time::Instant::now(),
        ));
        let input = stream::iter(vec![
            Ok::<_, std::io::Error>(Bytes::from_static(b"data: one\n")),
            Err(std::io::Error::other("broken stream")),
        ]);
        let output = count_forwarded_stream(input, counter)
            .collect::<Vec<_>>()
            .await;
        assert!(output[1].is_err());
        let events = hook.0.lock().expect("recording lock");
        assert!(matches!(
            events.last(),
            Some(ProviderDebugEvent::Error(event)) if event.kind == "stream_error"
        ));
        assert!(
            !events
                .iter()
                .any(|event| matches!(event, ProviderDebugEvent::StreamCompleted(_)))
        );
    }
}
