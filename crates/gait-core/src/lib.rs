//! One browser-independent agent loop: model calls, MCP tool calls, compaction, and skills.
pub mod agent;
pub mod cancel;
pub mod compaction;
pub mod dialog;
pub mod error;
pub mod kb;
pub mod llm;
pub mod mcp;
pub mod observer;
pub mod scratchpad;
pub mod skills;

pub use error::{Error, Result};
