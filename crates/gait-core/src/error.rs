use std::fmt;

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Clone, Debug, PartialEq)]
pub enum Error {
    /// The server answered with a non-success status.
    Http {
        status: u16,
        body: String,
    },
    /// The request never completed: network, CORS, or timeout.
    Transport(String),
    Json(String),
    Tool(String),
    Kb(String),
    Llm(String),
    Skill(String),
    /// The caller stopped the turn.
    Cancelled,
    /// The turn ended without an answer for a known reason: `max_model_turns` or
    /// `max_output_tokens`.
    Stopped {
        reason: &'static str,
        message: String,
    },
}

impl Error {
    /// How a turn that returned this error ended, for hosts and events.
    pub fn stop_reason(&self) -> &'static str {
        match self {
            Error::Cancelled => "cancelled",
            Error::Stopped { reason, .. } => reason,
            _ => "error",
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Http { status, body } => write!(f, "HTTP {status}: {body}"),
            Error::Transport(m) => write!(f, "transport error: {m}"),
            Error::Json(m) => write!(f, "JSON error: {m}"),
            Error::Tool(m) => write!(f, "{m}"),
            Error::Kb(m) => write!(f, "KB error: {m}"),
            Error::Llm(m) => write!(f, "LLM error: {m}"),
            Error::Skill(m) => write!(f, "skill error: {m}"),
            Error::Cancelled => write!(f, "cancelled"),
            Error::Stopped { message, .. } => write!(f, "{message}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Error::Json(e.to_string())
    }
}
