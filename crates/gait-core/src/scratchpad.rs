use serde_json::{Map, Value};
use std::cell::RefCell;
use std::rc::Rc;

/// Session key/value state. `Clone` shares the same storage.
///
/// Unlike Python's scratchpad this holds JSON values only, so a session can be saved as is.
#[derive(Clone, Default, Debug)]
pub struct Scratchpad(Rc<RefCell<Map<String, Value>>>);

impl Scratchpad {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn get(&self, key: &str) -> Option<Value> {
        self.0.borrow().get(key).cloned()
    }

    pub fn set(&self, key: &str, value: impl Into<Value>) {
        self.0.borrow_mut().insert(key.to_owned(), value.into());
    }

    pub fn replace(&self, value: Value) {
        if let Value::Object(map) = value {
            *self.0.borrow_mut() = map;
        }
    }

    pub fn to_json(&self) -> Value {
        Value::Object(self.0.borrow().clone())
    }

    /// Non-object values give an empty scratchpad.
    pub fn from_json(value: Value) -> Self {
        match value {
            Value::Object(map) => Self(Rc::new(RefCell::new(map))),
            _ => Self::default(),
        }
    }
}
