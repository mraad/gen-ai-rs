//! One Streamable HTTP MCP endpoint, revision 2026-07-28. No retries or protocol sessions.
use crate::agent::ToolDef;
use crate::llm::{parse_json, Http, SseParser};
use crate::observer::Trace;
use crate::{Error, Result};
use serde_json::{json, Value};
use std::cell::Cell;
use std::collections::HashSet;
use std::rc::Rc;

const VERSION: &str = "2026-07-28";

struct Mcp {
    url: String,
    http: Rc<dyn Http>,
    next_id: Cell<u64>,
}

impl Mcp {
    async fn request(&self, method: &str, mut params: Value) -> Result<Value> {
        let id = self.next_id.get();
        self.next_id.set(id + 1);
        params["_meta"] = json!({
            "io.modelcontextprotocol/protocolVersion": VERSION,
            "io.modelcontextprotocol/clientInfo": {"name": "gait", "version": env!("CARGO_PKG_VERSION")},
            "io.modelcontextprotocol/clientCapabilities": {}
        });
        let mut headers = vec![
            ("content-type".into(), "application/json".into()),
            (
                "accept".into(),
                "application/json, text/event-stream".into(),
            ),
            ("MCP-Protocol-Version".into(), VERSION.into()),
            ("Mcp-Method".into(), method.into()),
        ];
        // Tool names are checked to be plain ASCII, so they fit a header unchanged.
        if let Some(name) = params["name"].as_str() {
            headers.push(("Mcp-Name".into(), name.into()));
        }
        let response = self
            .http
            .post_json(
                &self.url,
                &headers,
                json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}).to_string(),
            )
            .await?;
        let value = if response.body.iter().find(|b| !b.is_ascii_whitespace()) == Some(&b'{') {
            parse_json(&response)?
        } else {
            crate::llm::ensure_ok(&response)?;
            let mut parser = SseParser::default();
            let mut events = parser.feed(&response.body);
            events.extend(parser.finish());
            let mut found = None;
            for event in events {
                let value: Value = serde_json::from_str(&event)?;
                if value.get("id").is_some() {
                    if found.is_some() {
                        return Err(Error::Tool("MCP returned multiple responses".into()));
                    }
                    found = Some(value);
                }
            }
            found.ok_or_else(|| Error::Tool("MCP stream ended without a response".into()))?
        };
        if value["jsonrpc"] != "2.0" || value["id"] != id {
            return Err(Error::Tool("MCP response ID or version mismatch".into()));
        }
        if let Some(error) = value.get("error") {
            return Err(Error::Tool(format!("MCP: {error}")));
        }
        let result = value
            .get("result")
            .filter(|r| r.is_object())
            .ok_or_else(|| Error::Tool("MCP response has no result".into()))?;
        if result["resultType"] != "complete" {
            return Err(Error::Tool("MCP result requires unsupported client input; complete authorization on the server".into()));
        }
        Ok(result.clone())
    }
}

/// Plain ASCII names, so a name fits a header unchanged.
pub fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
}

/// Text for the model from an MCP tool result; `isError` becomes a tool error.
pub fn result_text(result: &Value) -> Result<String> {
    let text = if let Some(value) = result.get("structuredContent") {
        value.to_string()
    } else {
        let content = result["content"]
            .as_array()
            .ok_or_else(|| Error::Tool("MCP tool result has no content".into()))?;
        content
            .iter()
            .map(|part| {
                part["text"]
                    .as_str()
                    .unwrap_or("[non-text MCP content omitted]")
            })
            .collect::<Vec<_>>()
            .join("\n")
    };
    if result["isError"] == true {
        return Err(Error::Tool(text));
    }
    Ok(text)
}

pub async fn tools(url: String, http: Rc<dyn Http>, trace: &Trace) -> Result<Vec<ToolDef>> {
    let mcp = Rc::new(Mcp {
        url,
        http,
        next_id: Cell::new(1),
    });
    let mut tools = Vec::new();
    let mut cursor = Value::Null;
    let mut seen = HashSet::new();
    loop {
        let mut params = json!({});
        if !cursor.is_null() {
            params["cursor"] = cursor;
        }
        let page = mcp.request("tools/list", params).await?;
        let list = page["tools"]
            .as_array()
            .ok_or_else(|| Error::Tool("MCP tools/list has no tools array".into()))?;
        for spec in list {
            let name = spec["name"]
                .as_str()
                .filter(|name| valid_name(name))
                .ok_or_else(|| Error::Tool("MCP tool has an invalid name".into()))?
                .to_owned();
            let schema = spec["inputSchema"].clone();
            if !schema.is_object() {
                return Err(Error::Tool(format!(
                    "MCP tool '{name}' has no input schema"
                )));
            }
            // shortcut: x-mcp-header mirroring is not implemented; such a tool is skipped.
            if schema.to_string().contains("\"x-mcp-header\"") {
                trace.event(
                    "tool_rejected",
                    format!("{name}: x-mcp-header is not supported"),
                );
                continue;
            }
            let remote = mcp.clone();
            let remote_name = name.clone();
            tools.push(ToolDef {
                name,
                description: spec["description"].as_str().unwrap_or_default().into(),
                parameters: schema,
                func: Rc::new(move |args, _| {
                    let (remote, name) = (remote.clone(), remote_name.clone());
                    Box::pin(async move {
                        let result = remote
                            .request("tools/call", json!({"name": name, "arguments": args}))
                            .await?;
                        result_text(&result)
                    })
                }),
            });
        }
        cursor = page.get("nextCursor").cloned().unwrap_or(Value::Null);
        if cursor.is_null() {
            break;
        }
        let next = cursor
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| Error::Tool("invalid MCP pagination cursor".into()))?;
        if !seen.insert(next.to_owned()) || seen.len() >= 100 {
            return Err(Error::Tool("MCP pagination did not finish".into()));
        }
    }
    Ok(tools)
}
