use std::sync::Arc;

use litellm_ai_gateway::integrations::provider_debug::ProviderDebugHook;
use litellm_ai_gateway::integrations::provider_debug::console::ConsoleDebugHook;

pub fn hook(enabled: bool) -> Option<Arc<dyn ProviderDebugHook>> {
    enabled.then(|| Arc::new(ConsoleDebugHook::from_env()) as Arc<dyn ProviderDebugHook>)
}
