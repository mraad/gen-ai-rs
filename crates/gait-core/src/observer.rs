//! Events are streamed to the host. The harness retains no trace buffer.
use serde::Serialize;
use serde_json::Value;
use std::rc::Rc;

#[derive(Clone, Debug, Serialize)]
pub struct Event {
    pub ts_ms: f64,
    pub run_id: String,
    pub category: String,
    pub message: String,
    pub attrs: Value,
}

pub trait Observer {
    fn emit(&self, event: &Event);
}

pub struct NoopObserver;
impl Observer for NoopObserver {
    fn emit(&self, _: &Event) {}
}

#[derive(Clone)]
pub struct Trace {
    sink: Rc<dyn Observer>,
    clock: Rc<dyn Fn() -> f64>,
    run_id: String,
}

impl Trace {
    pub fn new(
        sink: Rc<dyn Observer>,
        clock: Rc<dyn Fn() -> f64>,
        run_id: impl Into<String>,
    ) -> Self {
        Self {
            sink,
            clock,
            run_id: run_id.into(),
        }
    }
    pub fn noop() -> Self {
        Self::new(Rc::new(NoopObserver), Rc::new(|| 0.0), "noop")
    }
    pub fn event(&self, category: &str, message: impl Into<String>) {
        self.event_with(category, message, Value::Null);
    }
    pub fn event_with(&self, category: &str, message: impl Into<String>, attrs: Value) {
        self.sink.emit(&Event {
            ts_ms: (self.clock)(),
            run_id: self.run_id.clone(),
            category: category.into(),
            message: message.into(),
            attrs,
        });
    }
}
