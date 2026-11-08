//! One model/tool loop. Only completed turns update the shared conversation.
use crate::cancel::Cancel;
use crate::compaction::Compaction;
use crate::dialog::{message_tokens, Dialog, Message};
use crate::llm::{ChatRequest, DeltaSink, Http, LlmClient, Usage};
use crate::observer::Trace;
use crate::scratchpad::Scratchpad;
use crate::skills::SkillSet;
use crate::{Error, Result};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::future::Future;
use std::pin::Pin;
use std::rc::Rc;

type ToolFuture = Pin<Box<dyn Future<Output = Result<ToolOutput>>>>;
type ToolFn = dyn Fn(Value, Scratchpad) -> ToolFuture;

#[derive(Clone, Debug, PartialEq)]
pub enum ToolOutput {
    Text(String),
    Json(Value),
}

#[derive(Clone)]
pub struct ToolDef {
    pub name: String,
    pub description: String,
    pub parameters: Value,
    pub func: Rc<ToolFn>,
}

impl ToolDef {
    fn schema(&self) -> Value {
        json!({"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}})
    }
}

#[derive(Clone)]
pub struct Ctx {
    pub dialog: Dialog,
    pub scratchpad: Scratchpad,
    pub cancel: Cancel,
    pub trace: Trace,
    pub on_delta: Option<DeltaSink>,
}

impl Ctx {
    pub fn new(scratchpad: Scratchpad, dialog: Dialog, trace: Trace) -> Self {
        Self {
            scratchpad,
            dialog,
            trace,
            cancel: Cancel::default(),
            on_delta: None,
        }
    }
}

pub struct Agent {
    instructions: String,
    llm: Rc<dyn LlmClient>,
    skills: SkillSet,
    tools: Vec<ToolDef>,
    pub compaction: Compaction,
}

#[derive(Debug)]
pub struct AgentResponse {
    pub message: String,
    pub usage: Usage,
    pub steps: usize,
    /// `end_turn`, or `max_output_tokens` when the answer was cut at the output limit.
    pub stop_reason: &'static str,
}

const MAX_MODEL_CALLS: usize = 10;
/// Checks arguments against the parts of a tool's JSON schema that models get wrong, so the
/// model can correct itself. shortcut: no nested schemas; use a schema crate if tools need them.
pub fn check_args(schema: &Value, args: &Value) -> Result<()> {
    if !args.is_object() {
        return Err(Error::Tool("Arguments must be a JSON object.".into()));
    }
    for key in schema["required"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
    {
        if args.get(key).is_none() {
            return Err(Error::Tool(format!("Missing required argument '{key}'.")));
        }
    }
    let properties = schema["properties"].as_object();
    for (key, value) in args.as_object().into_iter().flatten() {
        let Some(property) = properties.and_then(|p| p.get(key)) else {
            if schema["additionalProperties"] == json!(false) {
                return Err(Error::Tool(format!("Unknown argument '{key}'.")));
            }
            continue;
        };
        let fits = |kind: &Value| match kind.as_str() {
            Some("string") => value.is_string(),
            Some("number") => value.is_number(),
            Some("integer") => value.is_i64() || value.is_u64(),
            Some("boolean") => value.is_boolean(),
            Some("array") => value.is_array(),
            Some("object") => value.is_object(),
            Some("null") => value.is_null(),
            _ => true,
        };
        let matches = match &property["type"] {
            Value::Array(kinds) => kinds.iter().any(fits),
            Value::Null => true,
            kind => fits(kind),
        };
        if !matches {
            return Err(Error::Tool(format!(
                "Argument '{key}' must be of type {}.",
                property["type"]
            )));
        }
        if let Some(allowed) = property["enum"].as_array() {
            if !allowed.contains(value) {
                return Err(Error::Tool(format!(
                    "Argument '{key}' must be one of {}.",
                    property["enum"]
                )));
            }
        }
    }
    Ok(())
}

pub(crate) fn truncate_middle(text: &mut String, limit: usize) {
    let total = text.chars().count();
    if total <= limit {
        return;
    }
    let tail_chars = limit / 4;
    let head_end = text
        .char_indices()
        .nth(limit - tail_chars)
        .map_or(text.len(), |(i, _)| i);
    let tail_start = text
        .char_indices()
        .nth(total - tail_chars)
        .map_or(text.len(), |(i, _)| i);
    *text = format!(
        "{}\n[... {} characters removed ...]\n{}",
        &text[..head_end],
        total - limit,
        &text[tail_start..]
    );
}

impl Agent {
    pub fn new(
        kb: crate::kb::Kb,
        llm: Rc<dyn LlmClient>,
        mut tools: Vec<ToolDef>,
        http: Rc<dyn Http>,
    ) -> Result<Self> {
        let instructions = kb.instructions.clone();
        let skills = SkillSet::new(kb, http.clone());
        tools.extend(skills.tools());
        let mut names = HashSet::new();
        for tool in &tools {
            if tool.name.is_empty() || !names.insert(&tool.name) {
                return Err(Error::Tool(format!(
                    "empty or duplicate tool name '{}'",
                    tool.name
                )));
            }
        }
        Ok(Self {
            instructions,
            llm,
            skills,
            tools,
            compaction: Compaction::default(),
        })
    }

    pub async fn run(&self, prompt: &str, ctx: &Ctx) -> Result<AgentResponse> {
        ctx.trace.event("turn_start", prompt);
        let result = self.turn(prompt, ctx).await;
        if let Err(error) = &result {
            ctx.trace.event("error", error.to_string());
        }
        let stop_reason = match &result {
            Ok(response) => response.stop_reason,
            Err(error) => error.stop_reason(),
        };
        ctx.trace.event_with(
            "turn_end",
            if result.is_ok() {
                "completed"
            } else {
                "stopped"
            },
            json!({ "stop_reason": stop_reason }),
        );
        result
    }

    /// One turn: compact when the estimate is over budget (at most once), call the model, run
    /// the tools it asks for, repeat. Only a final answer changes the shared conversation.
    async fn turn(&self, prompt: &str, ctx: &Ctx) -> Result<AgentResponse> {
        let work = Ctx {
            dialog: ctx.dialog.fork(),
            scratchpad: Scratchpad::from_json(ctx.scratchpad.to_json()),
            ..ctx.clone()
        };
        let tools: Vec<Value> = self.tools.iter().map(ToolDef::schema).collect();
        let tool_tokens = serde_json::to_string(&tools)?.len() / 4;
        let budget = self
            .compaction
            .context_window
            .saturating_sub(self.compaction.reserve_tokens);
        work.dialog.push_user(prompt);
        let mut usage = Usage::default();
        let mut compacted = false;
        for steps in 1..=MAX_MODEL_CALLS {
            let system = work
                .cancel
                .run(self.skills.compose(&self.instructions, &work.scratchpad))
                .await??;
            let system = Message::system(system);
            if !compacted {
                let mut compaction = self.compaction;
                compaction.context_window = compaction
                    .context_window
                    .saturating_sub(tool_tokens + message_tokens(&system));
                if let Some(summary) = compaction
                    .compact_if_needed(&work.dialog, self.llm.as_ref(), &work)
                    .await?
                {
                    usage += summary;
                    compacted = true;
                }
            }
            let mut messages = vec![system];
            messages.extend(work.dialog.messages());
            if tool_tokens + messages.iter().map(message_tokens).sum::<usize>() > budget {
                return Err(Error::Llm(
                    "agent context budget exhausted; start a new session or use a smaller task"
                        .into(),
                ));
            }
            let request = ChatRequest {
                messages,
                tools: tools.clone(),
                ..ChatRequest::default()
            };
            work.trace
                .event_with("model_start", self.llm.model(), json!({"step": steps}));
            let answer = work
                .cancel
                .run(async {
                    match &work.on_delta {
                        Some(sink) => self.llm.chat_stream(request, sink.as_ref()).await,
                        None => self.llm.chat(request).await,
                    }
                })
                .await??;
            if let Some(u) = answer.usage.clone() {
                usage += u;
            }
            work.trace.event_with(
                "model_end",
                answer.message.text().unwrap_or_default(),
                json!({"usage": usage}),
            );
            let calls = answer.message.tool_calls.clone().unwrap_or_default();
            let cut_off = answer.finish_reason.as_deref() == Some("length");
            if cut_off && !calls.is_empty() {
                // A call cut off at the output limit may be incomplete, so it never runs.
                return Err(Error::Stopped {
                    reason: "max_output_tokens",
                    message: "the model reached its output limit inside a tool call; the call did not run".into(),
                });
            }
            if calls.is_empty() {
                let message = answer
                    .message
                    .text()
                    .filter(|text| !text.trim().is_empty())
                    .ok_or_else(|| {
                        Error::Llm(format!(
                            "the model returned no answer (finish reason: {:?})",
                            answer.finish_reason
                        ))
                    })?
                    .to_owned();
                if work.cancel.is_cancelled() {
                    return Err(Error::Cancelled);
                }
                // The history before this prompt, compacted or not, becomes the shared history.
                let mut history = work.dialog.messages();
                history.truncate(history.iter().rposition(|m| m.role == "user").unwrap_or(0));
                ctx.dialog.replace(history);
                ctx.dialog.push_user(prompt);
                ctx.dialog.push(Message::assistant(&message));
                ctx.scratchpad.replace(work.scratchpad.to_json());
                return Ok(AgentResponse {
                    message,
                    usage,
                    steps,
                    stop_reason: if cut_off {
                        "max_output_tokens"
                    } else {
                        "end_turn"
                    },
                });
            }
            work.dialog.push(answer.message);
            for call in calls {
                let name = &call.function.name;
                work.trace.event_with(
                    "tool_start",
                    name,
                    json!({"arguments": call.function.arguments}),
                );
                let output = work
                    .cancel
                    .run(async {
                        let tool = self
                            .tools
                            .iter()
                            .find(|tool| &tool.name == name)
                            .ok_or_else(|| Error::Tool(format!("unknown tool '{name}'")))?;
                        let args = serde_json::from_str(&call.function.arguments)?;
                        check_args(&tool.parameters, &args)?;
                        (tool.func)(args, work.scratchpad.clone()).await
                    })
                    .await?;
                let mut text = match output {
                    Ok(ToolOutput::Text(text)) => text,
                    Ok(ToolOutput::Json(value)) => value.to_string(),
                    Err(error) => format!("Tool error: {error}"),
                };
                truncate_middle(&mut text, 30_000);
                work.trace.event("tool_end", name);
                work.dialog.push(Message::tool(text, name, &call.id));
            }
        }
        Err(Error::Stopped {
            reason: "max_model_turns",
            message: format!("agent exceeded {MAX_MODEL_CALLS} model calls without answering"),
        })
    }
}
