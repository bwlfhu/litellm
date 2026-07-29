use std::io::{IsTerminal, Write};
use std::sync::Mutex;

use colored_json::{ColorMode, ToColoredJson};

use super::{ProviderDebugEvent, ProviderDebugHook};

#[derive(Clone, Copy)]
enum RenderMode {
    Compact,
    Pretty,
}

pub struct ConsoleDebugHook {
    mode: RenderMode,
    output: Mutex<std::io::Stderr>,
}

impl ConsoleDebugHook {
    pub fn from_env() -> Self {
        let compact = std::env::var("JSON_LOGS")
            .map(|value| value.eq_ignore_ascii_case("true"))
            .unwrap_or(false)
            || !std::io::stderr().is_terminal();
        Self {
            mode: if compact {
                RenderMode::Compact
            } else {
                RenderMode::Pretty
            },
            output: Mutex::new(std::io::stderr()),
        }
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
                let rendered = if std::env::var_os("NO_COLOR").is_some() {
                    pretty
                } else {
                    pretty.to_colored_json(ColorMode::On).unwrap_or(pretty)
                };
                let _ = writeln!(output, "{rendered}");
            }
        }
    }
}
