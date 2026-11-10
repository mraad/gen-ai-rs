use super::{
    parse_json, post_sse, ChatRequest, ChatResponse, Http, LlmClient, LlmConfig, OnDelta, Usage,
};
use crate::dialog::{Message, ToolCall, ToolFunction};
use crate::{Error, Result};
use async_trait::async_trait;
use serde_json::{json, Value};
use std::rc::Rc;

/// Builds one response from streamed chunks.
#[derive(Default)]
struct StreamedAnswer {
    content: String,
    calls: Vec<ToolCall>,
    finish_reason: Option<String>,
    usage: Option<Usage>,
    error: Option<Error>,
}

impl StreamedAnswer {
    fn add(&mut self, data: &str, on_delta: &OnDelta<'_>) {
        if data == "[DONE]" || self.error.is_some() {
            return;
        }
        let value: Value = match serde_json::from_str(data) {
            Ok(value) => value,
            Err(error) => {
                self.error = Some(error.into());
                return;
            }
        };
        if let Some(error) = value.get("error") {
            self.error = Some(Error::Llm(error.to_string()));
            return;
        }
        if let Some(usage) = value.get("usage").filter(|u| !u.is_null()) {
            self.usage = parse_usage(usage);
        }
        let choice = &value["choices"][0];
        if let Some(reason) = choice["finish_reason"].as_str() {
            self.finish_reason = Some(reason.to_owned());
        }
        let delta = &choice["delta"];
        if let Some(text) = delta["content"].as_str().filter(|t| !t.is_empty()) {
            self.content.push_str(text);
            on_delta(text);
        }
        for part in delta["tool_calls"].as_array().into_iter().flatten() {
            let index = part["index"].as_u64().unwrap_or(self.calls.len() as u64);
            if index >= 128 {
                self.error = Some(Error::Llm("streamed tool index exceeds 127".into()));
                return;
            }
            let index = index as usize;
            while self.calls.len() <= index {
                self.calls.push(ToolCall {
                    id: String::new(),
                    kind: "function".to_owned(),
                    function: ToolFunction {
                        name: String::new(),
                        arguments: String::new(),
                    },
                });
            }
            let call = &mut self.calls[index];
            if let Some(id) = part["id"].as_str() {
                call.id = id.to_owned();
            }
            if let Some(name) = part["function"]["name"].as_str() {
                call.function.name.push_str(name);
            }
            if let Some(arguments) = part["function"]["arguments"].as_str() {
                call.function.arguments.push_str(arguments);
            }
        }
    }

    fn finish(self) -> Result<ChatResponse> {
        if let Some(error) = self.error {
            return Err(error);
        }
        // A cut connection must not pass for a finished answer.
        if self.finish_reason.is_none() {
            return Err(Error::Transport(
                "the stream ended before the answer was complete".into(),
            ));
        }
        let mut message = Message::assistant(self.content);
        if message.text() == Some("") {
            message.content = Value::Null;
        }
        message.tool_calls = (!self.calls.is_empty()).then_some(self.calls);
        Ok(ChatResponse {
            message,
            finish_reason: self.finish_reason,
            usage: self.usage,
        })
    }
}

/// One wire format for OpenAI, Azure, Ollama, LM Studio, OpenRouter, and a LiteLLM proxy.
pub struct OpenAiCompat {
    model: String,
    chat_url: String,
    headers: Vec<(String, String)>,
    http: Rc<dyn Http>,
}

fn parse_usage(value: &Value) -> Option<Usage> {
    let mut usage: Usage = serde_json::from_value(value.clone()).ok()?;
    usage.cached_tokens = value["prompt_tokens_details"]["cached_tokens"]
        .as_u64()
        .unwrap_or(usage.cached_tokens); // some compatible servers report it at the top level
    Some(usage)
}

fn bearer(api_key: Option<String>) -> Vec<(String, String)> {
    let mut headers = vec![("content-type".to_owned(), "application/json".to_owned())];
    if let Some(key) = api_key {
        headers.push(("authorization".to_owned(), format!("Bearer {key}")));
    }
    headers
}

impl OpenAiCompat {
    pub(super) fn with_default_base(
        model: &str,
        config: LlmConfig,
        http: Rc<dyn Http>,
        base: &str,
    ) -> Self {
        let base = config
            .api_base
            .as_deref()
            .unwrap_or(base)
            .trim_end_matches('/')
            .to_owned();
        Self {
            model: model.to_owned(),
            chat_url: format!("{base}/chat/completions"),
            headers: bearer(config.api_key),
            http,
        }
    }

    pub(super) fn azure(deployment: &str, config: LlmConfig, http: Rc<dyn Http>) -> Result<Self> {
        let base = config
            .api_base
            .ok_or(Error::Llm("azure requires api_base".into()))?;
        let version = config
            .api_version
            .ok_or(Error::Llm("azure requires api_version".into()))?;
        let root = format!(
            "{}/openai/deployments/{deployment}",
            base.trim_end_matches('/')
        );
        let mut headers = vec![("content-type".to_owned(), "application/json".to_owned())];
        if let Some(key) = config.api_key {
            headers.push(("api-key".to_owned(), key));
        }
        Ok(Self {
            model: deployment.to_owned(),
            chat_url: format!("{root}/chat/completions?api-version={version}"),
            headers,
            http,
        })
    }

    fn body(&self, request: ChatRequest) -> Value {
        let mut body = json!({"model": self.model, "messages": request.messages});
        if !request.tools.is_empty() {
            body["tools"] = Value::Array(request.tools);
        }
        body
    }
}

#[async_trait(?Send)]
impl LlmClient for OpenAiCompat {
    fn model(&self) -> &str {
        &self.model
    }

    async fn chat(&self, request: ChatRequest) -> Result<ChatResponse> {
        let body = self.body(request).to_string();
        let response = self
            .http
            .post_json(&self.chat_url, &self.headers, body)
            .await?;
        let mut value = parse_json(&response)?;
        let choice = value
            .get_mut("choices")
            .and_then(|c| c.get_mut(0))
            .map(Value::take)
            .ok_or(Error::Llm("response has no choices".into()))?;
        let message: Message =
            serde_json::from_value(choice.get("message").cloned().unwrap_or(Value::Null))?;
        Ok(ChatResponse {
            message,
            finish_reason: choice
                .get("finish_reason")
                .and_then(Value::as_str)
                .map(str::to_owned),
            usage: value.get("usage").and_then(parse_usage),
        })
    }

    async fn chat_stream(
        &self,
        request: ChatRequest,
        on_delta: &OnDelta<'_>,
    ) -> Result<ChatResponse> {
        let mut body = self.body(request);
        body["stream"] = json!(true);
        body["stream_options"] = json!({ "include_usage": true });
        let mut answer = StreamedAnswer::default();
        post_sse(
            self.http.as_ref(),
            &self.chat_url,
            &self.headers,
            body.to_string(),
            &mut |data| answer.add(data, on_delta),
        )
        .await?;
        answer.finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn invalid_tool_index_cannot_allocate_unbounded_memory() {
        let mut answer = StreamedAnswer::default();
        answer.add(
            r#"{"choices":[{"delta":{"tool_calls":[{"index":4294967295}]}}]}"#,
            &|_| {},
        );
        assert!(answer.finish().is_err());
    }
}
