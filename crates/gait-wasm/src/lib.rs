//! Browser fetch, host events, and a single-session API.
use async_trait::async_trait;
use gait_core::agent::{Agent, Ctx};
use gait_core::cancel::Cancel;
use gait_core::dialog::{Dialog, Message};
use gait_core::kb::Kb;
use gait_core::llm::{
    create_client, ChatRequest, ChatResponse, DeltaSink, Http, HttpResponse, LlmClient, LlmConfig,
    OnChunk,
};
use gait_core::observer::{Event, Observer, Trace};
use gait_core::scratchpad::Scratchpad;
use gait_core::{Error, Result};
use js_sys::{Function, Promise, Reflect, Uint8Array};
use serde_json::{json, Value};
use std::cell::{Cell, RefCell};
use std::rc::Rc;
use wasm_bindgen::prelude::*;
use wasm_bindgen_futures::{future_to_promise, JsFuture};
use web_sys::{
    AbortController, ReadableStreamDefaultReader, Request, RequestInit, RequestRedirect, Response,
    Url,
};

const TIMEOUT_MS: i32 = 300_000;
const MAX_RESPONSE: usize = 16 * 1024 * 1024;

#[wasm_bindgen]
extern "C" {
    #[wasm_bindgen(js_name = setTimeout)]
    fn set_timeout(callback: &Function, ms: i32) -> JsValue;
    #[wasm_bindgen(js_name = clearTimeout)]
    fn clear_timeout(id: &JsValue);
    #[wasm_bindgen(js_name = fetch)]
    fn global_fetch(request: &Request) -> Promise;
}

#[wasm_bindgen(start)]
pub fn start() {
    console_error_panic_hook::set_once();
}

fn js_message(error: &JsValue) -> String {
    error
        .as_string()
        .or_else(|| {
            error
                .dyn_ref::<js_sys::Error>()
                .map(|e| String::from(e.message()))
        })
        .unwrap_or_else(|| format!("{error:?}"))
}
fn to_js(value: &Value) -> JsValue {
    js_sys::JSON::parse(&value.to_string()).unwrap_or(JsValue::NULL)
}
fn from_js(value: &JsValue) -> Result<Value> {
    let text = js_sys::JSON::stringify(value).map_err(|e| Error::Json(js_message(&e)))?;
    Ok(serde_json::from_str(&String::from(text))?)
}
fn error_to_js(error: Error) -> JsValue {
    js_sys::Error::new(&error.to_string()).into()
}
fn prop(object: &JsValue, key: &str) -> JsValue {
    Reflect::get(object, &key.into()).unwrap_or(JsValue::UNDEFINED)
}
fn string(object: &JsValue, key: &str) -> Option<String> {
    prop(object, key).as_string().filter(|s| !s.is_empty())
}
fn function(object: &JsValue, key: &str) -> Option<Function> {
    prop(object, key).dyn_into().ok()
}
fn settle(value: &JsValue) -> JsFuture {
    JsFuture::from(Promise::resolve(value))
}

/// Dropping an in-flight future closes its network request.
struct Abort {
    controller: AbortController,
    timer: JsValue,
}
impl Drop for Abort {
    fn drop(&mut self) {
        clear_timeout(&self.timer);
        self.controller.abort();
    }
}
fn abort_after(controller: &AbortController, ms: i32) -> std::result::Result<JsValue, JsValue> {
    let abort: Function = Reflect::get(controller, &"abort".into())?.dyn_into()?;
    Ok(set_timeout(&abort.bind0(controller), ms))
}

#[derive(Clone, Copy)]
pub struct FetchHttp {
    pub timeout_ms: i32,
}
impl Default for FetchHttp {
    fn default() -> Self {
        Self {
            timeout_ms: TIMEOUT_MS,
        }
    }
}
impl FetchHttp {
    async fn request(
        &self,
        method: &str,
        url: &str,
        headers: &[(String, String)],
        body: Option<&str>,
        mut on_chunk: Option<&mut OnChunk<'_>>,
    ) -> Result<HttpResponse> {
        let result = async {
            let controller = AbortController::new()?;
            let init = RequestInit::new();
            init.set_method(method);
            init.set_signal(Some(&controller.signal()));
            init.set_redirect(RequestRedirect::Error);
            if let Some(body) = body {
                init.set_body(&JsValue::from_str(body));
            }
            let request = Request::new_with_str_and_init(url, &init)?;
            for (name, value) in headers {
                request.headers().set(name, value)?;
            }
            let mut guard = Abort {
                timer: abort_after(&controller, self.timeout_ms)?,
                controller,
            };
            let response: Response = JsFuture::from(global_fetch(&request)).await?.dyn_into()?;
            let mut bytes = Vec::new();
            let mut size = 0;
            if let Some(stream) = response.body() {
                let reader: ReadableStreamDefaultReader = stream.get_reader().dyn_into()?;
                loop {
                    let part = JsFuture::from(reader.read()).await?;
                    if prop(&part, "done").as_bool().unwrap_or(true) {
                        break;
                    }
                    let chunk = Uint8Array::new(&prop(&part, "value")).to_vec();
                    size += chunk.len();
                    if size > MAX_RESPONSE {
                        return Err(js_sys::Error::new("HTTP response exceeds 16 MiB").into());
                    }
                    match on_chunk.as_deref_mut().filter(|_| response.ok()) {
                        Some(add) => add(&chunk),
                        None => bytes.extend_from_slice(&chunk),
                    }
                    clear_timeout(&guard.timer);
                    guard.timer = abort_after(&guard.controller, self.timeout_ms)?;
                }
            }
            Ok::<_, JsValue>(HttpResponse {
                status: response.status(),
                body: bytes,
            })
        }
        .await;
        result.map_err(|e| {
            Error::Transport(format!(
                "{url}: {} (network, CORS, or timeout)",
                js_message(&e)
            ))
        })
    }
}

#[async_trait(?Send)]
impl Http for FetchHttp {
    async fn post_json(
        &self,
        url: &str,
        headers: &[(String, String)],
        body: String,
    ) -> Result<HttpResponse> {
        self.request("POST", url, headers, Some(&body), None).await
    }
    async fn post_stream(
        &self,
        url: &str,
        headers: &[(String, String)],
        body: String,
        on_chunk: &mut OnChunk<'_>,
    ) -> Result<HttpResponse> {
        self.request("POST", url, headers, Some(&body), Some(on_chunk))
            .await
    }
    async fn get(&self, url: &str) -> Result<HttpResponse> {
        self.request("GET", url, &[], None, None).await
    }
}

/// Lets the host supply an in-browser model. The second argument is an AbortSignal.
struct JsLlm {
    model: String,
    chat: Function,
}
#[async_trait(?Send)]
impl LlmClient for JsLlm {
    fn model(&self) -> &str {
        &self.model
    }
    async fn chat(&self, request: ChatRequest) -> Result<ChatResponse> {
        let error = |e: JsValue| Error::Llm(js_message(&e));
        let guard = Abort {
            controller: AbortController::new().map_err(error)?,
            timer: JsValue::UNDEFINED,
        };
        let returned = self
            .chat
            .call2(
                &JsValue::NULL,
                &to_js(&serde_json::to_value(request)?),
                &guard.controller.signal(),
            )
            .map_err(error)?;
        Ok(serde_json::from_value(from_js(
            &settle(&returned).await.map_err(error)?,
        )?)?)
    }
}

struct PageObserver(Option<Function>);
impl Observer for PageObserver {
    fn emit(&self, event: &Event) {
        if let Some(callback) = &self.0 {
            if let Err(error) = callback.call1(
                &JsValue::NULL,
                &to_js(&serde_json::to_value(event).unwrap_or(Value::Null)),
            ) {
                web_sys::console::warn_1(&error);
            }
        }
    }
}

struct Inner {
    agent: Agent,
    dialog: Dialog,
    scratchpad: Scratchpad,
    observer: Rc<PageObserver>,
    on_delta: Option<DeltaSink>,
    busy: Cell<bool>,
    cancel: RefCell<Cancel>,
    turn: Cell<u64>,
}

#[wasm_bindgen]
pub struct GaitSession(Rc<Inner>);

fn same_origin(url: &str) -> Result<String> {
    let location = prop(&js_sys::global(), "location");
    let base = string(&location, "href")
        .ok_or_else(|| Error::Transport("a browser location is required".into()))?;
    let target = Url::new_with_base(url, &base).map_err(|e| Error::Transport(js_message(&e)))?;
    if Some(target.origin()) != string(&location, "origin")
        || !target.username().is_empty()
        || !target.password().is_empty()
        || !target.hash().is_empty()
    {
        return Err(Error::Transport(
            "MCP and skills must use URLs on the page's origin, without credentials or fragments"
                .into(),
        ));
    }
    Ok(target.href())
}

/// A value from `state()`, given back to `create`. A damaged value is an error, never a silent empty session.
fn restore(saved: &JsValue) -> Result<(Dialog, Scratchpad)> {
    if saved.is_undefined() || saved.is_null() {
        return Ok((Dialog::new(), Scratchpad::new()));
    }
    let state = from_js(saved)?;
    let messages: Vec<Message> = serde_json::from_value(state["dialog"].clone())
        .map_err(|e| Error::Json(format!("state.dialog: {e}")))?;
    let scratchpad = state
        .get("scratchpad")
        .cloned()
        .unwrap_or_else(|| json!({}));
    if !scratchpad.is_object() {
        return Err(Error::Json("state.scratchpad must be an object".into()));
    }
    Ok((
        Dialog::from_messages(messages),
        Scratchpad::from_json(scratchpad),
    ))
}

async fn build(config: JsValue) -> Result<GaitSession> {
    // Reject removed security/extension APIs rather than silently ignoring them.
    for key in [
        "hooks",
        "tools",
        "agents",
        "collection",
        "embed",
        "embedModel",
        "reserveTokens",
        "keepRecentTokens",
        "store",
        "sessionId",
    ] {
        if !prop(&config, key).is_undefined() {
            return Err(Error::Tool(format!(
                "'{key}' was removed; tools execute through the same-origin MCP server, and the host saves state() and passes it back as 'state'"
            )));
        }
    }
    let timeout = prop(&config, "timeoutMs");
    let timeout_ms = if timeout.is_undefined() {
        TIMEOUT_MS
    } else {
        let ms = timeout
            .as_f64()
            .filter(|ms| ms.is_finite() && ms.fract() == 0.0 && *ms > 0.0 && *ms <= i32::MAX as f64)
            .ok_or_else(|| {
                Error::Transport("timeoutMs must be a positive 32-bit integer".into())
            })?;
        ms as i32
    };
    let http = Rc::new(FetchHttp { timeout_ms });
    let observer = Rc::new(PageObserver(function(&config, "onEvent")));
    let trace = Trace::new(observer.clone(), Rc::new(js_sys::Date::now), "setup");
    let kb_url = same_origin(string(&config, "kbUrl").as_deref().unwrap_or("kb"))?;
    let kb = Kb::load(&kb_url, http.as_ref()).await?;
    let model =
        string(&config, "model").ok_or_else(|| Error::Llm("config needs 'model'".into()))?;
    let llm: Rc<dyn LlmClient> = match function(&config, "chat") {
        Some(chat) => Rc::new(JsLlm { model, chat }),
        None => create_client(
            &model,
            LlmConfig {
                api_key: string(&config, "apiKey"),
                api_base: string(&config, "apiBase"),
                api_version: string(&config, "apiVersion"),
            },
            http.clone(),
        )?,
    };
    let mcp_url = same_origin(string(&config, "mcpUrl").as_deref().unwrap_or("/mcp"))?;
    let tools = gait_core::mcp::tools(mcp_url, http.clone(), &trace).await?;
    let mut agent = Agent::new(kb, llm, tools, http)?;
    let window = prop(&config, "contextWindow");
    if !window.is_undefined() {
        agent.compaction.context_window = window
            .as_f64()
            .filter(|v| v.fract() == 0.0 && *v >= 1024.0 && *v <= u32::MAX as f64)
            .ok_or_else(|| {
                Error::Llm("contextWindow must be an integer of at least 1024 tokens".into())
            })? as usize;
    }
    agent.compaction.reserve_tokens = agent
        .compaction
        .reserve_tokens
        .min(agent.compaction.context_window / 4);
    agent.compaction.keep_recent_tokens = agent
        .compaction
        .keep_recent_tokens
        .min(agent.compaction.context_window / 4);
    let (dialog, scratchpad) = restore(&prop(&config, "state"))?;
    let on_delta = function(&config, "onDelta").map(|callback| {
        Rc::new(move |text: &str| {
            if let Err(error) = callback.call1(&JsValue::NULL, &text.into()) {
                web_sys::console::warn_1(&error);
            }
        }) as DeltaSink
    });
    Ok(GaitSession(Rc::new(Inner {
        agent,
        dialog,
        scratchpad,
        observer,
        on_delta,
        busy: Cell::new(false),
        cancel: RefCell::default(),
        turn: Cell::new(0),
    })))
}

struct Busy(Rc<Inner>);
impl Drop for Busy {
    fn drop(&mut self) {
        self.0.busy.set(false);
    }
}

#[wasm_bindgen]
impl GaitSession {
    pub fn create(config: JsValue) -> Promise {
        future_to_promise(
            async move { build(config).await.map(JsValue::from).map_err(error_to_js) },
        )
    }
    pub fn send(&self, prompt: String) -> Promise {
        let inner = self.0.clone();
        if inner.busy.replace(true) {
            return Promise::reject(&js_sys::Error::new("a turn or clear is already running"));
        }
        let cancel = Cancel::default();
        *inner.cancel.borrow_mut() = cancel.clone();
        future_to_promise(async move {
            let _busy = Busy(inner.clone());
            let turn = inner.turn.get() + 1;
            inner.turn.set(turn);
            let mut ctx = Ctx::new(
                inner.scratchpad.clone(),
                inner.dialog.clone(),
                Trace::new(
                    inner.observer.clone(),
                    Rc::new(js_sys::Date::now),
                    format!("turn-{turn}"),
                ),
            );
            ctx.cancel = cancel;
            ctx.on_delta = inner.on_delta.clone();
            let value = match inner.agent.run(&prompt, &ctx).await {
                Ok(answer) => {
                    json!({"message": answer.message, "usage": answer.usage, "steps": answer.steps, "stopReason": answer.stop_reason, "cancelled": false, "error": null})
                }
                Err(error) => {
                    json!({"message": null, "usage": null, "steps": 0, "stopReason": error.stop_reason(), "cancelled": error == Error::Cancelled, "error": error.to_string()})
                }
            };
            Ok(to_js(&value))
        })
    }
    pub fn cancel(&self) {
        self.0.cancel.borrow().cancel();
    }
    pub fn state(&self) -> std::result::Result<JsValue, JsValue> {
        Ok(to_js(
            &json!({"dialog": self.0.dialog.to_json().map_err(error_to_js)?, "scratchpad": self.0.scratchpad.to_json()}),
        ))
    }
    pub fn clear(&self) -> Promise {
        let inner = self.0.clone();
        if inner.busy.replace(true) {
            return Promise::reject(&js_sys::Error::new("a turn or clear is already running"));
        }
        future_to_promise(async move {
            let _busy = Busy(inner.clone());
            inner.dialog.replace(Vec::new());
            inner.scratchpad.replace(json!({}));
            inner.observer.emit(&Event {
                ts_ms: js_sys::Date::now(),
                run_id: "session".into(),
                category: "clear".into(),
                message: String::new(),
                attrs: Value::Null,
            });
            Ok(JsValue::UNDEFINED)
        })
    }
}
