use async_trait::async_trait;
use futures::executor::block_on;
use gait_core::dialog::Message;
use gait_core::llm::*;
use gait_core::{Error, Result};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::rc::Rc;
const SECRET: &str = "sk-secret-value";

#[derive(Clone, Debug)]
struct Sent {
    url: String,
    headers: Vec<(String, String)>,
    body: Value,
}

#[derive(Default)]
struct FakeHttp {
    responses: RefCell<Vec<(u16, String)>>,
    sent: RefCell<Vec<Sent>>,
}

impl FakeHttp {
    fn replying(status: u16, body: Value) -> Rc<Self> {
        let http = Self::default();
        http.responses.borrow_mut().push((status, body.to_string()));
        Rc::new(http)
    }

    fn last(&self) -> Sent {
        self.sent.borrow().last().unwrap().clone()
    }

    fn header(&self, name: &str) -> Option<String> {
        self.last()
            .headers
            .iter()
            .find(|(k, _)| k == name)
            .map(|(_, v)| v.clone())
    }
}

#[async_trait(?Send)]
impl Http for FakeHttp {
    async fn post_json(
        &self,
        url: &str,
        headers: &[(String, String)],
        body: String,
    ) -> Result<HttpResponse> {
        self.sent.borrow_mut().push(Sent {
            url: url.to_owned(),
            headers: headers.to_vec(),
            body: serde_json::from_str(&body).unwrap(),
        });
        let (status, body) = self.responses.borrow_mut().remove(0);
        Ok(HttpResponse {
            status,
            body: body.into_bytes(),
        })
    }

    async fn get(&self, _url: &str) -> Result<HttpResponse> {
        unreachable!()
    }
}

fn config() -> LlmConfig {
    LlmConfig {
        api_key: Some(SECRET.into()),
        api_base: None,
        api_version: None,
    }
}

fn openai_reply() -> Value {
    json!({
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant", "content": null, "refusal": null,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{\"a\":1}"}}]
            }
        }],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    })
}

fn request_with_params() -> ChatRequest {
    ChatRequest {
        messages: vec![Message::system("s"), Message::user("u")],
        tools: vec![
            json!({"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}),
        ],
        params: json!({"temperature": 0.0, "name": "skill-name", "description": "frontmatter"})
            .as_object()
            .unwrap()
            .clone(),
    }
}

#[test]
fn openai_request_and_response() {
    let http = FakeHttp::replying(200, openai_reply());
    let client = create_client("openai/gpt-4.1", config(), http.clone()).unwrap();
    let response = block_on(client.chat(request_with_params())).unwrap();
    let sent = http.last();
    assert_eq!(sent.url, "https://api.openai.com/v1/chat/completions");
    assert_eq!(
        http.header("authorization").unwrap(),
        format!("Bearer {SECRET}")
    );
    assert_eq!(sent.body["model"], "gpt-4.1");
    assert_eq!(sent.body["temperature"], 0.0);
    // Frontmatter keys must not reach the API.
    assert!(sent.body.get("name").is_none());
    assert!(sent.body.get("description").is_none());
    assert_eq!(response.finish_reason.as_deref(), Some("tool_calls"));
    assert_eq!(response.usage.unwrap().total_tokens, 10);
    assert_eq!(
        response.message.tool_calls.unwrap()[0].function.arguments,
        "{\"a\":1}"
    );
}

#[test]
fn reasoning_models_drop_sampling_params() {
    let http = FakeHttp::replying(200, openai_reply());
    let client = create_client("openai/gpt-5.1", config(), http.clone()).unwrap();
    block_on(client.chat(request_with_params())).unwrap();
    assert!(http.last().body.get("temperature").is_none());
}

#[test]
fn azure_uses_deployment_url_and_api_key_header() {
    let http = FakeHttp::replying(200, openai_reply());
    let config = LlmConfig {
        api_key: Some(SECRET.into()),
        api_base: Some("https://gw.example.com/".into()),
        api_version: Some("2024-10-21".into()),
    };
    let client = create_client("azure/gpt-4.1", config, http.clone()).unwrap();
    block_on(client.chat(ChatRequest::default())).unwrap();
    assert_eq!(
        http.last().url,
        "https://gw.example.com/openai/deployments/gpt-4.1/chat/completions?api-version=2024-10-21"
    );
    assert_eq!(http.header("api-key").unwrap(), SECRET);
    assert!(http.header("authorization").is_none());
    assert!(create_client("azure/x", LlmConfig::default(), http).is_err());
}

#[test]
fn proxy_route_and_local_providers_use_the_same_adapter() {
    // The M0 gate: an unreachable gateway is served by pointing at a proxy, with no new code.
    let http = FakeHttp::replying(200, openai_reply());
    let config = LlmConfig {
        api_key: Some("k".into()),
        api_base: Some("https://proxy.example.com/v1".into()),
        api_version: None,
    };
    let client = create_client("litellm_proxy/gpt-4.1", config, http.clone()).unwrap();
    block_on(client.chat(ChatRequest::default())).unwrap();
    assert_eq!(
        http.last().url,
        "https://proxy.example.com/v1/chat/completions"
    );
    assert_eq!(http.last().body["model"], "gpt-4.1");

    let http = FakeHttp::replying(200, openai_reply());
    let client = create_client("ollama/nomic", LlmConfig::default(), http.clone()).unwrap();
    block_on(client.chat(ChatRequest::default())).unwrap();
    assert_eq!(
        http.last().url,
        "http://localhost:11434/v1/chat/completions"
    );
    assert!(http.header("authorization").is_none());

    let Err(error) = create_client("mystery/x", LlmConfig::default(), http) else {
        panic!("expected unknown provider error");
    };
    assert!(error.to_string().contains("unknown provider"));
}

#[test]
fn http_errors_keep_status_and_body() {
    let http = FakeHttp::replying(429, json!({"error": "slow down"}));
    let client = create_client("openai/gpt-4.1", config(), http).unwrap();
    let error = block_on(client.chat(ChatRequest::default())).unwrap_err();
    assert!(matches!(error, Error::Http { status: 429, ref body } if body.contains("slow down")));

    let http = FakeHttp::replying(200, json!({"choices": []}));
    let client = create_client("openai/gpt-4.1", config(), http).unwrap();
    assert!(matches!(
        block_on(client.chat(ChatRequest::default())),
        Err(Error::Llm(_))
    ));
}

#[test]
fn usage_reports_cached_tokens() {
    let mut reply = openai_reply();
    reply["usage"]["prompt_tokens_details"] = json!({"cached_tokens": 4});
    let client = create_client("openai/gpt-4.1", config(), FakeHttp::replying(200, reply)).unwrap();
    let usage = block_on(client.chat(ChatRequest::default()))
        .unwrap()
        .usage
        .unwrap();
    assert_eq!((usage.prompt_tokens, usage.cached_tokens), (7, 4));

    let mut reply = openai_reply();
    reply["usage"]["cached_tokens"] = json!(3); // a compatible server without prompt_tokens_details
    let client = create_client("openai/gpt-4.1", config(), FakeHttp::replying(200, reply)).unwrap();
    let usage = block_on(client.chat(ChatRequest::default()))
        .unwrap()
        .usage
        .unwrap();
    assert_eq!(usage.cached_tokens, 3);
}
