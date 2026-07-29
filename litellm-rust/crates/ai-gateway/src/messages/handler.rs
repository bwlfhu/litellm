use std::sync::Arc;
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
    ErrorEventInput, RequestEventInput, ResponseBody, ResponseEventInput, StreamCounter,
    error_event, request_event, response_event, stream_started,
};

pub(super) async fn execute_messages_provider_call(
    request: ProviderMessagesRequest,
) -> CoreResult<Value> {
    let started = Instant::now();
    let debug = request.debug_hook.clone();
    let call_id = request.call_id.clone();
    let provider = request.provider.clone();
    let upstream_headers = final_upstream_headers(&request.upstream_headers);
    let body_bytes = serde_json::to_vec(&request.body)
        .map_err(|error| CoreError::InvalidRequest(error.to_string()))?;
    if let Some(hook) = &debug {
        hook.emit(&request_event(RequestEventInput {
            call_id: call_id.clone(),
            provider: provider.clone(),
            model: request.model.clone(),
            stream: false,
            url: request.url.clone(),
            headers: upstream_headers.clone(),
            body: request.body,
        }));
    }
    let mut request_builder = http_client().post(&request.url).body(body_bytes);
    for (key, value) in &upstream_headers {
        request_builder = request_builder.header(key, value);
    }
    if let Some(duration) = request.timeout {
        request_builder = request_builder.timeout(duration);
    }

    let response = request_builder.send().await.map_err(|err| {
        if let Some(hook) = &debug {
            hook.emit(&error_event(ErrorEventInput {
                call_id: call_id.clone(),
                provider: provider.clone(),
                duration_ms: started.elapsed().as_millis(),
                status: None,
                kind: "network_error",
                message: err.to_string(),
                body: None,
            }));
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
            hook.emit(&error_event(ErrorEventInput {
                call_id: call_id.clone(),
                provider: provider.clone(),
                duration_ms: started.elapsed().as_millis(),
                status: Some(status.as_u16()),
                kind: "http_error",
                message: format!("provider returned HTTP {}", status.as_u16()),
                body: Some(response_body(&text, &response_headers)),
            }));
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
                hook.emit(&error_event(ErrorEventInput {
                    call_id: call_id.clone(),
                    provider: provider.clone(),
                    duration_ms: started.elapsed().as_millis(),
                    status: Some(status.as_u16()),
                    kind: "invalid_json",
                    message: err.to_string(),
                    body: Some(response_body(&text, &response_headers)),
                }));
            }
            return Err(CoreError::InvalidResponse(format!(
                "invalid messages response JSON: {err}"
            )));
        }
    };
    let typed_response = AnthropicMessagesResponse::deserialize(&response)
        .map_err(|err| CoreError::InvalidResponse(format!("invalid messages response: {err}")))?;
    if let Some(hook) = &debug {
        hook.emit(&response_event(ResponseEventInput {
            call_id,
            provider,
            status: status.as_u16(),
            duration_ms: started.elapsed().as_millis(),
            headers: response_headers,
            body: ResponseBody::Json(response),
        }));
    }
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
    let upstream_headers = final_upstream_headers(&request.upstream_headers);
    let body_bytes = serde_json::to_vec(&request.body)
        .map_err(|error| CoreError::InvalidRequest(error.to_string()))?;
    if let Some(hook) = &request.debug_hook {
        hook.emit(&request_event(RequestEventInput {
            call_id: request.call_id.clone(),
            provider: request.provider.clone(),
            model: request.model.clone(),
            stream: true,
            url: request.url.clone(),
            headers: upstream_headers.clone(),
            body: request.body,
        }));
    }
    let mut request_builder = http_client().post(&request.url).body(body_bytes);
    for (key, value) in &upstream_headers {
        request_builder = request_builder.header(key, value);
    }
    if let Some(duration) = request.timeout {
        request_builder = request_builder.timeout(duration);
    }
    let response = request_builder.send().await.map_err(|err| {
        if let Some(hook) = &request.debug_hook {
            hook.emit(&error_event(ErrorEventInput {
                call_id: request.call_id.clone(),
                provider: request.provider.clone(),
                duration_ms: started.elapsed().as_millis(),
                status: None,
                kind: "network_error",
                message: err.to_string(),
                body: None,
            }));
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
    if !status.is_success() {
        let text = response.text().await.map_err(|err| {
            if let Some(hook) = &request.debug_hook {
                hook.emit(&error_event(ErrorEventInput {
                    call_id: request.call_id.clone(),
                    provider: request.provider.clone(),
                    duration_ms: started.elapsed().as_millis(),
                    status: Some(status.as_u16()),
                    kind: "network_error",
                    message: err.to_string(),
                    body: None,
                }));
            }
            CoreError::Network(err.to_string())
        })?;
        if let Some(hook) = &request.debug_hook {
            hook.emit(&error_event(ErrorEventInput {
                call_id: request.call_id.clone(),
                provider: request.provider.clone(),
                duration_ms: started.elapsed().as_millis(),
                status: Some(status.as_u16()),
                kind: "http_error",
                message: format!("provider returned HTTP {}", status.as_u16()),
                body: Some(response_body(&text, &response_headers)),
            }));
        }
        return Err(CoreError::Http {
            status: status.as_u16(),
            body: truncate_error_body(&text),
        });
    }
    let counter = Arc::new(StreamCounter::new(
        request.call_id.clone(),
        request.provider.clone(),
        request.debug_hook.clone(),
        started,
    ));
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

fn response_body(text: &str, headers: &[(String, String)]) -> ResponseBody {
    let is_json = headers
        .iter()
        .find(|(name, _)| name.eq_ignore_ascii_case("content-type"))
        .is_some_and(|(_, value)| value.to_ascii_lowercase().contains("json"));
    if is_json {
        return serde_json::from_str(text)
            .map(ResponseBody::Json)
            .unwrap_or_else(|_| ResponseBody::Binary {
                media_type: Some("application/json".to_string()),
                bytes: text.len(),
            });
    }
    ResponseBody::Binary {
        media_type: headers
            .iter()
            .find(|(name, _)| name.eq_ignore_ascii_case("content-type"))
            .map(|(_, value)| value.clone()),
        bytes: text.len(),
    }
}

fn final_upstream_headers(headers: &[(String, String)]) -> Vec<(String, String)> {
    headers
        .iter()
        .cloned()
        .chain(
            (!headers
                .iter()
                .any(|(name, _)| name.eq_ignore_ascii_case("content-type")))
            .then(|| ("content-type".to_string(), "application/json".to_string())),
        )
        .collect()
}
