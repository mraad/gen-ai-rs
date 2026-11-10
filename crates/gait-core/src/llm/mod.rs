//! Chat and tool-call wire formats, with browser-independent HTTP adapters.

mod openai;
mod sse;

use openai::OpenAiCompat;
pub use sse::SseParser;

use crate::dialog::Message;
use crate::{Error, Result};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::rc::Rc;

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct ChatRequest {
    pub messages: Vec<Message>,
    /// OpenAI-shaped tool definitions. Adapters convert them when needed.
    pub tools: Vec<Value>,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Usage {
    #[serde(default)]
    pub prompt_tokens: u64,
    #[serde(default)]
    pub completion_tokens: u64,
    #[serde(default)]
    pub total_tokens: u64,
    /// Part of `prompt_tokens` that the provider read from its prompt cache.
    #[serde(default)]
    pub cached_tokens: u64,
}

impl std::ops::AddAssign for Usage {
    fn add_assign(&mut self, other: Self) {
        self.prompt_tokens += other.prompt_tokens;
        self.completion_tokens += other.completion_tokens;
        self.total_tokens += other.total_tokens;
        self.cached_tokens += other.cached_tokens;
    }
}

#[derive(Clone, Debug, PartialEq, Deserialize)]
pub struct ChatResponse {
    pub message: Message,
    /// Normalized to OpenAI values: `stop`, `length`, `tool_calls`, `content_filter`.
    pub finish_reason: Option<String>,
    pub usage: Option<Usage>,
}

// Aliases, because `async_trait` breaks the elided lifetime when it sees `dyn Fn(&str)` in a signature.
pub type OnDelta<'a> = dyn Fn(&str) + 'a;
pub type OnChunk<'a> = dyn FnMut(&[u8]) + 'a;
pub type DeltaSink = Rc<dyn Fn(&str)>;

#[async_trait(?Send)]
pub trait LlmClient {
    fn model(&self) -> &str;
    async fn chat(&self, request: ChatRequest) -> Result<ChatResponse>;

    /// Like `chat`, and calls `on_delta` with answer text as it arrives.
    /// A client that cannot stream sends the whole answer as one delta.
    async fn chat_stream(
        &self,
        request: ChatRequest,
        on_delta: &OnDelta<'_>,
    ) -> Result<ChatResponse> {
        let response = self.chat(request).await?;
        if let Some(text) = response.message.text().filter(|t| !t.is_empty()) {
            on_delta(text);
        }
        Ok(response)
    }
}

#[derive(Clone, Debug)]
pub struct HttpResponse {
    pub status: u16,
    pub body: Vec<u8>,
}

impl HttpResponse {
    pub fn is_success(&self) -> bool {
        (200..300).contains(&self.status)
    }
}

/// Keeps adapters testable natively. `gait-wasm` implements it with `fetch`.
#[async_trait(?Send)]
pub trait Http {
    async fn post_json(
        &self,
        url: &str,
        headers: &[(String, String)],
        body: String,
    ) -> Result<HttpResponse>;

    /// Like `post_json`, and passes a successful body to `on_chunk` piece by piece.
    /// The returned `body` holds only an error response.
    async fn post_stream(
        &self,
        url: &str,
        headers: &[(String, String)],
        body: String,
        on_chunk: &mut OnChunk<'_>,
    ) -> Result<HttpResponse> {
        let response = self.post_json(url, headers, body).await?;
        if response.is_success() {
            on_chunk(&response.body);
            return Ok(HttpResponse {
                status: response.status,
                body: Vec::new(),
            });
        }
        Ok(response)
    }

    async fn get(&self, url: &str) -> Result<HttpResponse>;
}

/// Connection settings. There is deliberately no `Debug`, so the key cannot reach a log.
#[derive(Clone, Default)]
pub struct LlmConfig {
    pub api_key: Option<String>,
    pub api_base: Option<String>,
    pub api_version: Option<String>,
}

/// Builds a client from a LiteLLM-style `provider/model` name.
pub fn create_client(
    model: &str,
    config: LlmConfig,
    http: Rc<dyn Http>,
) -> Result<Rc<dyn LlmClient>> {
    let (provider, name) = model.split_once('/').unwrap_or(("openai", model));
    let default_base = match provider {
        "azure" => return Ok(Rc::new(OpenAiCompat::azure(name, config, http)?)),
        "openai" => "https://api.openai.com/v1",
        "openrouter" => "https://openrouter.ai/api/v1",
        "ollama" | "ollama_chat" => "http://localhost:11434/v1",
        "lm_studio" => "http://localhost:1234/v1",
        // `api_base` replaces this.
        _ if config.api_base.is_some() => "",
        _ => {
            return Err(Error::Llm(format!(
                "unknown provider '{provider}': set api_base to use an OpenAI-compatible endpoint"
            )))
        }
    };
    Ok(Rc::new(OpenAiCompat::with_default_base(
        name,
        config,
        http,
        default_base,
    )))
}

pub(crate) fn ensure_ok(response: &HttpResponse) -> Result<()> {
    if response.is_success() {
        return Ok(());
    }
    Err(Error::Http {
        status: response.status,
        body: String::from_utf8_lossy(&response.body).into_owned(),
    })
}

pub(crate) fn parse_json(response: &HttpResponse) -> Result<Value> {
    ensure_ok(response)?;
    Ok(serde_json::from_slice(&response.body)?)
}

/// Posts `body` and passes each server-sent event's data to `add`.
pub(crate) async fn post_sse(
    http: &dyn Http,
    url: &str,
    headers: &[(String, String)],
    body: String,
    add: &mut dyn FnMut(&str),
) -> Result<()> {
    let mut parser = SseParser::default();
    let response = http
        .post_stream(url, headers, body, &mut |chunk| {
            for data in parser.feed(chunk) {
                add(&data);
            }
        })
        .await?;
    ensure_ok(&response)?;
    if let Some(data) = parser.finish() {
        add(&data);
    }
    Ok(())
}
