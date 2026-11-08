use async_trait::async_trait;
use futures::executor::block_on;
use gait_core::agent::{check_args, ToolOutput};
use gait_core::llm::{Http, HttpResponse};
use gait_core::mcp;
use gait_core::observer::Trace;
use gait_core::scratchpad::Scratchpad;
use gait_core::{Error, Result};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::rc::Rc;

type Sent = (Value, Vec<(String, String)>);

struct Server {
    mode: &'static str,
    sent: RefCell<Vec<Sent>>,
}
impl Server {
    fn new(mode: &'static str) -> Rc<Self> {
        Rc::new(Self {
            mode,
            sent: RefCell::default(),
        })
    }
}
#[async_trait(?Send)]
impl Http for Server {
    async fn get(&self, _: &str) -> Result<HttpResponse> {
        unreachable!()
    }
    async fn post_json(
        &self,
        _: &str,
        headers: &[(String, String)],
        body: String,
    ) -> Result<HttpResponse> {
        let body: Value = serde_json::from_str(&body)?;
        self.sent
            .borrow_mut()
            .push((body.clone(), headers.to_vec()));
        let result = if body["method"] == "tools/list" {
            let mut schema = json!({"type":"object", "properties": {"text": {"type":"string"}}});
            if self.mode == "header" {
                schema["properties"]["text"]["x-mcp-header"] = json!("Text");
            }
            let mut page = json!({"resultType":"complete", "tools":[{"name":"echo", "description":"Echo", "inputSchema":schema}]});
            if self.mode == "pages" && body["params"].get("cursor").is_none() {
                page["nextCursor"] = json!("next");
            }
            if self.mode == "cursor-loop" {
                page["nextCursor"] = json!("same");
            }
            page
        } else {
            match self.mode {
                "http-error" => {
                    return Ok(HttpResponse {
                        status: 503,
                        body: b"failed".to_vec(),
                    })
                }
                "input" => json!({"resultType":"input_required", "inputRequests":{}}),
                "tool-error" => {
                    json!({"resultType":"complete", "isError":true, "content":[{"type":"text","text":"denied"}]})
                }
                _ => json!({"resultType":"complete", "content":[{"type":"text", "text":"ok"}]}),
            }
        };
        let mut reply = json!({"jsonrpc":"2.0", "id":body["id"], "result":result});
        if self.mode == "bad-id" {
            reply["id"] = json!(999);
        }
        if self.mode == "rpc-error" {
            reply.as_object_mut().unwrap().remove("result");
            reply["error"] = json!({"code":-32601,"message":"unknown"});
        }
        let bytes = if self.mode == "sse" {
            format!("data: {{\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}}\r\n\r\ndata: {reply}\r\n\r\n").into_bytes()
        } else {
            reply.to_string().into_bytes()
        };
        Ok(HttpResponse {
            status: 200,
            body: bytes,
        })
    }
}

#[test]
fn discovers_and_calls_tools_without_retries() {
    let server = Server::new("json");
    let tools = block_on(mcp::tools("/mcp".into(), server.clone(), &Trace::noop())).unwrap();
    let args = json!({"text": "hello"});
    check_args(&tools[0].parameters, &args).unwrap();
    let result = block_on((tools[0].func)(args, Scratchpad::new())).unwrap();
    assert_eq!(result, ToolOutput::Text("ok".into()));
    let sent = server.sent.borrow();
    assert_eq!(sent.len(), 2);
    let (body, headers) = &sent[1];
    assert_eq!(
        body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"],
        "2026-07-28"
    );
    assert!(headers.contains(&("Mcp-Method".into(), "tools/call".into())));
    assert!(headers.contains(&("Mcp-Name".into(), "echo".into())));
}

#[test]
fn pagination_and_json_or_sse_work() {
    for mode in ["json", "sse", "pages"] {
        let server = Server::new(mode);
        let tools = block_on(mcp::tools("/mcp".into(), server.clone(), &Trace::noop())).unwrap();
        assert_eq!(tools.len(), if mode == "pages" { 2 } else { 1 });
        assert_eq!(
            block_on((tools[0].func)(json!({}), Scratchpad::new())).unwrap(),
            ToolOutput::Text("ok".into())
        );
        assert_eq!(
            server.sent.borrow().len(),
            if mode == "pages" { 3 } else { 2 }
        );
    }
}

#[test]
fn bad_responses_and_endless_pagination_are_rejected() {
    for mode in ["bad-id", "rpc-error", "cursor-loop"] {
        assert!(
            block_on(mcp::tools("/mcp".into(), Server::new(mode), &Trace::noop())).is_err(),
            "{mode}"
        );
    }
    assert!(block_on(mcp::tools(
        "/mcp".into(),
        Server::new("header"), // x-mcp-header is not supported, so the tool is skipped
        &Trace::noop()
    ))
    .unwrap()
    .is_empty());
}

#[test]
fn tool_errors_input_requests_and_http_failures_are_not_retried() {
    for mode in ["http-error", "tool-error", "input"] {
        let server = Server::new(mode);
        let tools = block_on(mcp::tools("/mcp".into(), server.clone(), &Trace::noop())).unwrap();
        let error = block_on((tools[0].func)(json!({}), Scratchpad::new())).unwrap_err();
        assert!(
            matches!(error, Error::Tool(_) | Error::Http { .. }),
            "{error}"
        );
        assert_eq!(server.sent.borrow().len(), 2, "{mode}");
    }
}
