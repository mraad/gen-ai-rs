//! Keeps a long conversation inside the model's context window, using Pi's approach:
//! when the history nears the limit, an LLM summary replaces the older turns.

use crate::agent::truncate_middle;
use crate::agent::Ctx;
use crate::dialog::{message_tokens, Dialog, Message, CHARS_PER_TOKEN};
use crate::llm::{ChatRequest, LlmClient, Usage};
use crate::Result;
use serde_json::json;

const SUMMARY_PREFIX: &str = "Summary of the earlier conversation:\n";
const SUMMARY_PROMPT: &str = "Summarize this conversation so that work can continue from the summary alone. \
Keep names, numbers, decisions, and open questions. If it starts with an earlier summary, merge that in. \
Return only the summary.";

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Compaction {
    pub context_window: usize,
    /// Room left for the next request and answer. Compaction starts when less than this is free.
    pub reserve_tokens: usize,
    /// How much recent history stays word for word.
    pub keep_recent_tokens: usize,
}

impl Default for Compaction {
    fn default() -> Self {
        Self {
            context_window: 128_000,
            reserve_tokens: 16_384,
            keep_recent_tokens: 20_000,
        }
    }
}

impl Compaction {
    fn limit(&self) -> usize {
        self.context_window.saturating_sub(self.reserve_tokens)
    }

    /// The first message to keep. It is a user message, so a turn is never split, and the running
    /// turn (from the last user message) is always kept word for word.
    fn cut_point(&self, messages: &[Message]) -> Option<usize> {
        // A small window must still leave room for the summary and the next turn.
        let keep = self.keep_recent_tokens.min(self.limit() / 2);
        let mut recent = 0;
        let mut cut = messages.len();
        for (index, message) in messages.iter().enumerate().rev() {
            recent += message_tokens(message);
            if recent > keep {
                break;
            }
            cut = index;
        }
        let last_user = messages.iter().rposition(|m| m.role == "user")?;
        let start = (cut..messages.len())
            .find(|&i| messages[i].role == "user")
            .unwrap_or(last_user)
            .min(last_user);
        (start > 0).then_some(start)
    }

    /// Summarizes older turns when needed, returning the summary call usage.
    ///
    /// A failed summary leaves the history untouched, because losing turns is worse than a
    /// longer request. The caller checks the remaining context budget.
    pub async fn compact_if_needed(
        &self,
        dialog: &Dialog,
        llm: &dyn LlmClient,
        ctx: &Ctx,
    ) -> Result<Option<Usage>> {
        let tokens = dialog.token_estimate();
        if tokens <= self.limit() {
            return Ok(None);
        }
        let messages = dialog.messages();
        let Some(cut) = self.cut_point(&messages) else {
            return Ok(None);
        };
        let mut transcript: String = messages[..cut]
            .iter()
            .map(|m| {
                format!(
                    "{}: {}\n",
                    m.role.to_uppercase(),
                    m.text().unwrap_or_default()
                )
            })
            .collect();
        // The summary request must fit too, or a huge turn could never be compacted.
        truncate_middle(
            &mut transcript,
            self.limit().saturating_mul(CHARS_PER_TOKEN),
        );
        let request = ChatRequest {
            messages: vec![Message::system(SUMMARY_PROMPT), Message::user(transcript)],
            ..ChatRequest::default()
        };
        ctx.trace
            .event_with("model_start", llm.model(), json!({"purpose": "compaction"}));
        let response = match ctx.cancel.run(llm.chat(request)).await? {
            Ok(response) => response,
            Err(error) => {
                ctx.trace.event("compaction_failed", error.to_string());
                return Ok(None);
            }
        };
        ctx.trace.event_with(
            "model_end",
            llm.model(),
            json!({"usage": response.usage, "purpose": "compaction"}),
        );
        let summary = response.message.text().unwrap_or_default().trim();
        if summary.is_empty() {
            ctx.trace
                .event("compaction_failed", "the model returned an empty summary");
            return Ok(None);
        }
        let mut kept = vec![Message::system(format!("{SUMMARY_PREFIX}{summary}"))];
        kept.extend_from_slice(&messages[cut..]);
        dialog.replace(kept);
        ctx.trace.event_with(
            "compaction",
            format!("Summarized {cut} messages."),
            json!({ "tokens_before": tokens, "tokens_after": dialog.token_estimate() }),
        );
        Ok(Some(response.usage.unwrap_or_default()))
    }
}
