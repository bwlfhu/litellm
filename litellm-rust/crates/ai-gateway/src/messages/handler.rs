use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Instant;

use litellm_core::CoreResult;
use litellm_core::error::CoreError;
use litellm_core::messages::types::AnthropicMessagesResponse;
use serde::Deserialize;
use serde_json::Value;

use super::client::http_client;
use super::common_utils::truncate_error_body;
use super::types::ProviderMessagesRequest;
use crate::constants::ANTHROPIC_MESSAGES_PROVIDER;
use crate::integrations::provider_debug::{
    ProviderDebugHook, ResponseBody, error_event, request_event, response_event, stream_completed,
    stream_started,
};

fn response_debug_value(text: &str, headers: &[(String, String)]) -> Value {
    serde_json::from_str(text).unwrap_or_else(|_| {
        serde_json::json!({
            "media_type": headers.iter().find(|(name, _)| name.eq_ignore_ascii_case("content-type")).map(|(_, value)| value),
            "bytes": text.len(),
        })
    })
}

pub struct StreamCounter {
    pub call_id: String,
    pub provider: String,
    pub hook: Option<Arc<dyn ProviderDebugHook>>,
    pub started: Instant,
    pub bytes_received: AtomicUsize,
    pub frames_received: AtomicUsize,
    pub events_decoded: AtomicUsize,
}

impl StreamCounter {
    #[allow(dead_code)]
    pub fn record(&self, bytes: usize, events: usize) {
        self.bytes_received.fetch_add(bytes, Ordering::Relaxed);
        self.frames_received.fetch_add(1, Ordering::Relaxed);
        self.events_decoded.fetch_add(events, Ordering::Relaxed);
    }

    fn complete(&self) {
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
}

impl Drop for StreamCounter {
    fn drop(&mut self) {
        self.complete();
    }
}

pub(super) async fn execute_messages_provider_call(
    request: ProviderMessagesRequest,
) -> CoreResult<Value> {
    let started = Instant::now();
    let debug = request.debug_hook.clone();
    let call_id = request.call_id.clone();
    let provider = request.provider.clone();
    if let Some(hook) = &debug {
        hook.emit(&request_event(
            call_id.clone(),
            provider.clone(),
            request.model.clone(),
            false,
            request.url.clone(),
            &request.upstream_headers,
            request.body.clone(),
        ));
    }
    let mut request_builder = http_client().post(&request.url).json(&request.body);
    for (key, value) in &request.upstream_headers {
        request_builder = request_builder.header(key, value);
    }
    if let Some(duration) = request.timeout {
        request_builder = request_builder.timeout(duration);
    }

    let response = request_builder.send().await.map_err(|err| {
        if let Some(hook) = &debug {
            hook.emit(&error_event(
                call_id.clone(),
                provider.clone(),
                started.elapsed().as_millis(),
                None,
                "network".to_string(),
                err.to_string(),
                None,
            ));
        }
        CoreError::Network(err.to_string())
    })?;

    let status = response.status();
    let response_headers = response
        .headers()
        .iter()
        .filter_map(|(name, value)| {
            value
                .to_str()
                .ok()
                .map(|value| (name.to_string(), value.to_string()))
        })
        .collect::<Vec<_>>();
    let text = response
        .text()
        .await
        .map_err(|err| CoreError::Network(err.to_string()))?;

    if !status.is_success() {
        if let Some(hook) = &debug {
            hook.emit(&response_event(
                call_id.clone(),
                provider.clone(),
                status.as_u16(),
                started.elapsed().as_millis(),
                &response_headers,
                ResponseBody::Json(response_debug_value(&text, &response_headers)),
            ));
            hook.emit(&error_event(
                call_id.clone(),
                provider.clone(),
                started.elapsed().as_millis(),
                Some(status.as_u16()),
                "http".to_string(),
                format!("provider returned HTTP {}", status.as_u16()),
                Some(response_debug_value(&text, &response_headers)),
            ));
        }
        return Err(CoreError::Http {
            status: status.as_u16(),
            body: truncate_error_body(&text),
        });
    }

    let response = match serde_json::from_str::<Value>(&text) {
        Ok(response) => response,
        Err(err) => {
            if let Some(hook) = &debug {
                hook.emit(&response_event(
                    call_id.clone(),
                    provider.clone(),
                    status.as_u16(),
                    started.elapsed().as_millis(),
                    &response_headers,
                    ResponseBody::Json(response_debug_value(&text, &response_headers)),
                ));
                hook.emit(&error_event(
                    call_id.clone(),
                    provider.clone(),
                    started.elapsed().as_millis(),
                    Some(status.as_u16()),
                    "invalid_json".to_string(),
                    err.to_string(),
                    Some(response_debug_value(&text, &response_headers)),
                ));
            }
            return Err(CoreError::InvalidResponse(format!(
                "invalid messages response JSON: {err}"
            )));
        }
    };
    if let Some(hook) = &debug {
        hook.emit(&response_event(
            call_id,
            provider,
            status.as_u16(),
            started.elapsed().as_millis(),
            &response_headers,
            ResponseBody::Json(response.clone()),
        ));
    }
    let typed_response = AnthropicMessagesResponse::deserialize(&response)
        .map_err(|err| CoreError::InvalidResponse(format!("invalid messages response: {err}")))?;
    let transformed = request
        .config
        .transform_response(&request.model, typed_response)?;
    serde_json::to_value(transformed).map_err(|err| {
        CoreError::InvalidResponse(format!("failed to serialize messages response: {err}"))
    })
}

pub(super) async fn execute_messages_provider_stream(
    request: ProviderMessagesRequest,
) -> CoreResult<(reqwest::Response, Arc<StreamCounter>)> {
    if request.provider != ANTHROPIC_MESSAGES_PROVIDER {
        return Err(CoreError::InvalidRequest(
            "streaming messages is not supported for this provider".to_string(),
        ));
    }

    let started = Instant::now();
    let mut request_builder = http_client().post(&request.url).json(&request.body);
    for (key, value) in &request.upstream_headers {
        request_builder = request_builder.header(key, value);
    }
    if let Some(duration) = request.timeout {
        request_builder = request_builder.timeout(duration);
    }
    if let Some(hook) = &request.debug_hook {
        hook.emit(&request_event(
            request.call_id.clone(),
            request.provider.clone(),
            request.model.clone(),
            true,
            request.url.clone(),
            &request.upstream_headers,
            request.body.clone(),
        ));
    }

    let response = request_builder
        .send()
        .await
        .map_err(|err| CoreError::Network(err.to_string()))?;
    let status = response.status();
    if !status.is_success() {
        let text = response
            .text()
            .await
            .map_err(|err| CoreError::Network(err.to_string()))?;
        if let Some(hook) = &request.debug_hook {
            hook.emit(&error_event(
                request.call_id.clone(),
                request.provider.clone(),
                started.elapsed().as_millis(),
                Some(status.as_u16()),
                "http".to_string(),
                format!("provider returned HTTP {}", status.as_u16()),
                Some(Value::String(text.clone())),
            ));
        }
        return Err(CoreError::Http {
            status: status.as_u16(),
            body: truncate_error_body(&text),
        });
    }
    let counter = Arc::new(StreamCounter {
        call_id: request.call_id.clone(),
        provider: request.provider.clone(),
        hook: request.debug_hook.clone(),
        started,
        bytes_received: AtomicUsize::new(0),
        frames_received: AtomicUsize::new(0),
        events_decoded: AtomicUsize::new(0),
    });
    if let Some(hook) = &request.debug_hook {
        hook.emit(&stream_started(
            request.call_id,
            request.provider,
            status.as_u16(),
            response
                .headers()
                .get(reqwest::header::CONTENT_TYPE)
                .and_then(|value| value.to_str().ok())
                .map(str::to_string),
        ));
    }
    Ok((response, counter))
}
