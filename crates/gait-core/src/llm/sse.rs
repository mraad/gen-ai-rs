//! Incremental SSE lines; accepts LF, CRLF, and CR without rescanning buffered bytes.
#[derive(Default)]
pub struct SseParser {
    line: Vec<u8>,
    data: Vec<String>,
    skip_lf: bool,
    started: bool,
}

impl SseParser {
    pub fn feed(&mut self, chunk: &[u8]) -> Vec<String> {
        let mut events = Vec::new();
        for &byte in chunk {
            if self.skip_lf {
                self.skip_lf = false;
                if byte == b'\n' {
                    continue;
                }
            }
            if byte != b'\r' && byte != b'\n' {
                self.line.push(byte);
                continue;
            }
            self.skip_lf = byte == b'\r';
            let line = String::from_utf8_lossy(&self.line);
            let line = if self.started {
                &line
            } else {
                line.strip_prefix('\u{feff}').unwrap_or(&line)
            };
            self.started = true;
            if line.is_empty() {
                if !self.data.is_empty() {
                    events.push(self.data.join("\n"));
                    self.data.clear();
                }
            } else if let Some(value) = line.strip_prefix("data:") {
                self.data
                    .push(value.strip_prefix(' ').unwrap_or(value).to_owned());
            }
            self.line.clear();
        }
        events
    }

    /// Accept the final data event even when the provider omits its terminating blank line.
    pub fn finish(&mut self) -> Option<String> {
        self.feed(b"\n\n").pop()
    }
}
