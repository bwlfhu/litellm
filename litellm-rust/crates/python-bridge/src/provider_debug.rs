use std::sync::Arc;

use litellm_ai_gateway::integrations::provider_debug::ProviderDebugHook;
use litellm_ai_gateway::integrations::provider_debug::console::hook as console_hook;

pub fn hook(enabled: bool) -> Option<Arc<dyn ProviderDebugHook>> {
    console_hook(enabled)
}
