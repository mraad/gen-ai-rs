use crate::Result;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::cell::RefCell;
use std::rc::Rc;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ToolFunction {
    pub name: String,
    pub arguments: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ToolCall {
    pub id: String,
    #[serde(rename = "type", default = "function_type")]
    pub kind: String,
    pub function: ToolFunction,
}

fn function_type() -> String {
    "function".to_owned()
}

/// One chat message, in the same JSON shape as Python's `s_message`, `t_message`, and so on.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Message {
    pub role: String,
    /// String, null, or a list of content parts.
    #[serde(default)]
    pub content: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_calls: Option<Vec<ToolCall>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
}

impl Message {
    fn new(role: &str, content: Value) -> Self {
        Self {
            role: role.to_owned(),
            content,
            tool_calls: None,
            name: None,
            tool_call_id: None,
        }
    }

    pub fn system(content: impl Into<String>) -> Self {
        Self::new("system", Value::String(content.into()))
    }

    pub fn user(content: impl Into<String>) -> Self {
        Self::new("user", Value::String(content.into()))
    }

    pub fn assistant(content: impl Into<String>) -> Self {
        Self::new("assistant", Value::String(content.into()))
    }

    pub fn tool(content: impl Into<String>, name: &str, tool_call_id: &str) -> Self {
        let mut m = Self::new("tool", Value::String(content.into()));
        m.name = Some(name.to_owned());
        m.tool_call_id = Some(tool_call_id.to_owned());
        m
    }

    pub fn text(&self) -> Option<&str> {
        self.content.as_str()
    }
}

/// Ordered chat history. `Clone` shares the same storage; `fork` makes an independent copy.
#[derive(Clone, Default, Debug)]
pub struct Dialog {
    messages: Rc<RefCell<Vec<Message>>>,
}

pub(crate) const CHARS_PER_TOKEN: usize = 4;

/// shortcut: ~4 chars per token instead of LiteLLM's tokenizer; prefer provider `usage` when exact counts matter.
pub fn message_tokens(message: &Message) -> usize {
    serde_json::to_string(message).map_or(0, |s| s.len()) / CHARS_PER_TOKEN
}

impl Dialog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn from_messages(messages: Vec<Message>) -> Self {
        Self {
            messages: Rc::new(RefCell::new(messages)),
        }
    }

    pub fn push(&self, message: Message) {
        let mut messages = self.messages.borrow_mut();
        messages.push(message);
    }

    pub fn push_user(&self, content: impl Into<String>) {
        self.push(Message::user(content));
    }

    pub fn messages(&self) -> Vec<Message> {
        self.messages.borrow().clone()
    }

    pub fn len(&self) -> usize {
        self.messages.borrow().len()
    }

    pub fn is_empty(&self) -> bool {
        self.messages.borrow().is_empty()
    }

    pub fn fork(&self) -> Self {
        Self {
            messages: Rc::new(RefCell::new(self.messages())),
        }
    }

    pub fn token_estimate(&self) -> usize {
        self.messages.borrow().iter().map(message_tokens).sum()
    }

    /// Replaces all messages, as compaction does.
    pub fn replace(&self, messages: Vec<Message>) {
        *self.messages.borrow_mut() = messages;
    }

    pub fn to_json(&self) -> Result<Value> {
        Ok(serde_json::to_value(&*self.messages.borrow())?)
    }
}
