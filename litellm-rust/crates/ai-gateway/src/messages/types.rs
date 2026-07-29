use std::sync::Arc;
use std::time::Duration;

use litellm_core::messages::transformation::AnthropicMessagesProviderConfig;
use serde_json::{Map, Value};

use crate::integrations::provider_debug::ProviderDebugHook;

pub struct MessagesRequest<'a> {
    pub model: &'a str,
    pub body: Value,
    pub api_key: Option<&'a str>,
    pub api_base: Option<&'a str>,
    pub custom_llm_provider: Option<&'a str>,
    pub extra_headers: Option<Map<String, Value>>,
    pub timeout: Option<Duration>,
    pub debug_hook: Option<Arc<dyn ProviderDebugHook>>,
}

pub(crate) struct ProviderMessagesRequest {
    pub(crate) provider: String,
    pub(crate) model: String,
    pub(crate) config: &'static dyn AnthropicMessagesProviderConfig,
    pub(crate) url: String,
    pub(crate) body: Value,
    pub(crate) upstream_headers: Vec<(String, String)>,
    pub(crate) timeout: Option<Duration>,
    pub(crate) debug_hook: Option<Arc<dyn ProviderDebugHook>>,
    pub(crate) call_id: String,
}
