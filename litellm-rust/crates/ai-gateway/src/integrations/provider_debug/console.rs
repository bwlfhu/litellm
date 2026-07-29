use std::io::{IsTerminal, Write};
use std::sync::OnceLock;
use std::sync::{Arc, Mutex};

use colored_json::{ColorMode, Output, ToColoredJson};

use super::{ProviderDebugEvent, ProviderDebugHook};

#[derive(Clone, Copy)]
enum RenderMode {
    Compact,
    Pretty,
}

pub struct ConsoleDebugHook {
    mode: RenderMode,
    output: Mutex<Box<dyn Write + Send>>,
}

impl ConsoleDebugHook {
    pub fn from_env() -> Self {
        Self::with_writer(Box::new(std::io::stderr()))
    }

    pub fn with_writer(writer: Box<dyn Write + Send>) -> Self {
        Self::with_writer_and_mode(writer, matches!(*render_mode(), RenderMode::Pretty))
    }

    pub fn with_writer_and_mode(writer: Box<dyn Write + Send>, pretty: bool) -> Self {
        Self {
            mode: if pretty {
                RenderMode::Pretty
            } else {
                RenderMode::Compact
            },
            output: Mutex::new(writer),
        }
    }
}

pub fn hook_from_env() -> Option<Arc<dyn ProviderDebugHook>> {
    std::env::var("LITELLM_LOG")
        .ok()
        .filter(|value| value.eq_ignore_ascii_case("DEBUG"))
        .map(|_| Arc::new(ConsoleDebugHook::from_env()) as Arc<dyn ProviderDebugHook>)
}

pub fn hook(enabled: bool) -> Option<Arc<dyn ProviderDebugHook>> {
    enabled.then(|| Arc::new(ConsoleDebugHook::from_env()) as Arc<dyn ProviderDebugHook>)
}

fn render_mode() -> &'static RenderMode {
    static MODE: OnceLock<RenderMode> = OnceLock::new();
    MODE.get_or_init(|| {
        if std::env::var("JSON_LOGS")
            .map(|value| value.eq_ignore_ascii_case("true"))
            .unwrap_or(false)
            || !std::io::stderr().is_terminal()
        {
            RenderMode::Compact
        } else {
            RenderMode::Pretty
        }
    })
}

fn header(event: &ProviderDebugEvent) -> String {
    match event {
        ProviderDebugEvent::Request(value) => {
            format!("provider.request {} {}", value.call_id, value.provider)
        }
        ProviderDebugEvent::Response(value) => format!(
            "provider.response {} {} status={} duration_ms={}",
            value.call_id, value.provider, value.status, value.duration_ms
        ),
        ProviderDebugEvent::StreamStarted(value) => format!(
            "provider.stream.started {} {} status={}",
            value.call_id, value.provider, value.status
        ),
        ProviderDebugEvent::StreamCompleted(value) => format!(
            "provider.stream.completed {} {} duration_ms={}",
            value.call_id, value.provider, value.duration_ms
        ),
        ProviderDebugEvent::Error(value) => format!(
            "provider.error {} {}{} duration_ms={}",
            value.call_id,
            value.provider,
            value
                .status
                .map_or(String::new(), |status| format!(" status={status}")),
            value.duration_ms
        ),
    }
}

impl ProviderDebugHook for ConsoleDebugHook {
    fn emit(&self, event: &ProviderDebugEvent) {
        let Ok(mut output) = self.output.lock() else {
            return;
        };
        let Ok(json) = serde_json::to_string(event) else {
            return;
        };
        match self.mode {
            RenderMode::Compact => {
                let _ = writeln!(output, "{json}");
            }
            RenderMode::Pretty => {
                let pretty = serde_json::to_string_pretty(event).unwrap_or(json);
                let _ = writeln!(output, "{}", header(event));
                let rendered = pretty
                    .to_colored_json(ColorMode::Auto(Output::StdErr))
                    .unwrap_or(pretty);
                let _ = writeln!(output, "{rendered}");
                let _ = writeln!(output, "────────────────────────────────────────");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{Arc, Mutex};

    use serde_json::json;

    use super::*;
    use crate::integrations::provider_debug::{RequestEventInput, request_event};

    struct Buffer(Arc<Mutex<Vec<u8>>>);

    impl Write for Buffer {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0.lock().expect("buffer lock").extend_from_slice(bytes);
            Ok(bytes.len())
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn compact_output_is_canonical_json() {
        let buffer = Arc::new(Mutex::new(Vec::new()));
        let hook = ConsoleDebugHook::with_writer_and_mode(Box::new(Buffer(buffer.clone())), false);
        let event = request_event(RequestEventInput {
            call_id: "call_01".to_string(),
            provider: "anthropic".to_string(),
            model: "claude".to_string(),
            stream: false,
            url: "https://example.test".to_string(),
            headers: Vec::new(),
            body: json!({"prompt": "visible"}),
        });
        let expected = serde_json::to_value(&event).expect("event serializes");
        hook.emit(&event);
        let output =
            String::from_utf8(buffer.lock().expect("buffer lock").clone()).expect("output is utf8");
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(output.trim()).expect("output is JSON"),
            expected
        );
    }

    #[test]
    fn pretty_output_has_header_separator_and_indented_payload() {
        let buffer = Arc::new(Mutex::new(Vec::new()));
        let hook = ConsoleDebugHook::with_writer_and_mode(Box::new(Buffer(buffer.clone())), true);
        let event = request_event(RequestEventInput {
            call_id: "call_01".to_string(),
            provider: "anthropic".to_string(),
            model: "claude".to_string(),
            stream: false,
            url: "https://example.test".to_string(),
            headers: Vec::new(),
            body: json!({"prompt": "visible"}),
        });
        hook.emit(&event);
        let output =
            String::from_utf8(buffer.lock().expect("buffer lock").clone()).expect("output is utf8");
        assert!(output.contains("provider.request call_01 anthropic"));
        assert!(output.contains("────────────────"));
        assert!(output.contains("\n  \"event\""));
    }
}
