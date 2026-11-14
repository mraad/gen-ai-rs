use async_trait::async_trait;
use futures::executor::block_on;
use gait_core::agent::{check_args, Agent, Ctx, ToolDef};
use gait_core::cancel::Cancel;
use gait_core::compaction::Compaction;
use gait_core::dialog::{Dialog, Message, ToolCall, ToolFunction};
use gait_core::kb::Kb;
use gait_core::llm::{ChatRequest, ChatResponse, Http, HttpResponse, LlmClient, SseParser};
use gait_core::observer::Trace;
use gait_core::scratchpad::Scratchpad;
use gait_core::{Error, Result};
use serde_json::{json, Value};
use std::cell::RefCell;
use std::collections::{BTreeMap, VecDeque};
use std::rc::Rc;

#[derive(Default)]
struct Files {
    values: RefCell<BTreeMap<String, Vec<u8>>>,
    reads: RefCell<Vec<String>>,
}
#[async_trait(?Send)]
impl Http for Files {
    async fn post_json(&self, _: &str, _: &[(String, String)], _: String) -> Result<HttpResponse> {
        unreachable!()
    }
    async fn get(&self, url: &str) -> Result<HttpResponse> {
        self.reads.borrow_mut().push(url.into());
        let value = self.values.borrow().get(url).cloned();
        Ok(HttpResponse {
            status: if value.is_some() { 200 } else { 404 },
            body: value.unwrap_or_default(),
        })
    }
}
struct Model {
    answers: RefCell<VecDeque<Result<ChatResponse>>>,
    requests: RefCell<Vec<ChatRequest>>,
    cancel: Option<Cancel>,
}
impl Model {
    fn new(answers: Vec<Result<ChatResponse>>) -> Rc<Self> {
        Rc::new(Self {
            answers: RefCell::new(answers.into()),
            requests: RefCell::default(),
            cancel: None,
        })
    }
}
#[async_trait(?Send)]
impl LlmClient for Model {
    fn model(&self) -> &str {
        "stub"
    }
    async fn chat(&self, request: ChatRequest) -> Result<ChatResponse> {
        self.requests.borrow_mut().push(request);
        if let Some(cancel) = &self.cancel {
            cancel.cancel();
        }
        self.answers
            .borrow_mut()
            .pop_front()
            .expect("unexpected model call")
    }
}
fn says(text: &str) -> Result<ChatResponse> {
    Ok(ChatResponse {
        message: Message::assistant(text),
        finish_reason: Some("stop".into()),
        usage: None,
    })
}
fn asks(name: &str, args: Value) -> Result<ChatResponse> {
    let mut message = Message::assistant("");
    message.tool_calls = Some(vec![ToolCall {
        id: "call".into(),
        kind: "function".into(),
        function: ToolFunction {
            name: name.into(),
            arguments: args.to_string(),
        },
    }]);
    Ok(ChatResponse {
        message,
        finish_reason: Some("tool_calls".into()),
        usage: None,
    })
}
fn catalog() -> Value {
    json!({"instructions": "Base instructions", "skills": {"units": {"name": "units", "description": "Convert units", "hash": "new", "resources": ["references/factors.md"]}}})
}
fn files(manifest: Value) -> Rc<Files> {
    Rc::new(Files {
        values: RefCell::new(BTreeMap::from([
            ("kb/kb.json".into(), manifest.to_string().into_bytes()),
            (
                "kb/skills/units/instructions.md".into(),
                b"Use the factor from the resource.".to_vec(),
            ),
            (
                "kb/skills/units/references/factors.md".into(),
                b"1 mile = 1.609 km".to_vec(),
            ),
        ])),
        ..Files::default()
    })
}
fn agent(model: Rc<Model>, http: Rc<Files>, tools: Vec<ToolDef>) -> Agent {
    let kb = block_on(Kb::load("kb", http.as_ref())).unwrap();
    Agent::new(kb, model, tools, http).unwrap()
}
fn ctx() -> Ctx {
    Ctx::new(Scratchpad::new(), Dialog::new(), Trace::noop())
}

#[test]
fn plain_turn_costs_one_call_and_repeated_prompts_are_distinct() {
    let model = Model::new(vec![says("one"), says("two")]);
    let agent = agent(model.clone(), files(catalog()), vec![]);
    let ctx = ctx();
    for expected in ["one", "two"] {
        assert_eq!(
            block_on(agent.run("hello", &ctx)).unwrap().message,
            expected
        );
    }
    assert_eq!(ctx.dialog.len(), 4);
    assert_eq!(model.requests.borrow().len(), 2);
    assert_eq!(
        model.requests.borrow()[1].messages.last().unwrap().text(),
        Some("hello")
    );
}

#[test]
fn skills_load_progressively_and_stay_active_without_tool_history() {
    let http = files(catalog());
    let model = Model::new(vec![
        asks("activate_skill", json!({"name": "units"})),
        asks(
            "read_skill_resource",
            json!({"name": "units", "path": "references/factors.md"}),
        ),
        says("done"),
    ]);
    let agent = agent(model.clone(), http.clone(), vec![]);
    assert_eq!(&*http.reads.borrow(), &["kb/kb.json"]);
    let ctx = ctx();
    let result = block_on(agent.run("convert", &ctx)).unwrap();
    assert_eq!(result.steps, 3);
    assert!(!model.requests.borrow()[0].messages[0]
        .text()
        .unwrap()
        .contains("Use the factor"));
    assert!(model.requests.borrow()[1].messages[0]
        .text()
        .unwrap()
        .contains("Use the factor"));
    assert_eq!(
        model.requests.borrow()[2].messages.last().unwrap().text(),
        Some("1 mile = 1.609 km")
    );
    assert_eq!(ctx.dialog.len(), 2);
    // A host that kept state() starts a new session from it.
    let resumed = Ctx::new(
        Scratchpad::from_json(ctx.scratchpad.to_json()),
        Dialog::from_messages(ctx.dialog.messages()),
        Trace::noop(),
    );
    let model = Model::new(vec![says("resumed")]);
    let resumed_agent = self::agent(model.clone(), http.clone(), vec![]);
    block_on(resumed_agent.run("again", &resumed)).unwrap();
    assert!(model.requests.borrow()[0].messages[0]
        .text()
        .unwrap()
        .contains("Use the factor"));
    assert!(model.requests.borrow()[0].messages[0]
        .text()
        .unwrap()
        .contains(r#"Resources (JSON paths): ["references/factors.md"]"#));
    assert_eq!(
        http.reads
            .borrow()
            .iter()
            .filter(|u| u.ends_with("instructions.md"))
            .count(),
        2
    );
}

#[test]
fn invalid_or_stale_activation_cannot_read_resources() {
    for args in [
        json!({"name":"units", "path":"references/factors.md"}),
        json!({"name":"units", "path":"../../private"}),
    ] {
        let http = files(catalog());
        let model = Model::new(vec![asks("read_skill_resource", args), says("handled")]);
        let agent = agent(model.clone(), http.clone(), vec![]);
        let ctx = ctx();
        ctx.scratchpad
            .set("active_skills", json!([{"name":"units", "hash":"old"}]));
        block_on(agent.run("go", &ctx)).unwrap();
        assert!(model.requests.borrow()[1]
            .messages
            .last()
            .unwrap()
            .text()
            .unwrap()
            .contains("Tool error"));
        assert_eq!(http.reads.borrow().len(), 1);
    }
}

#[test]
fn malformed_skill_names_and_resource_paths_are_rejected() {
    for name in ["../..", "different", "", "a/b", "a\\b"] {
        let mut bad = catalog();
        bad["skills"]["units"]["name"] = json!(name);
        assert!(block_on(Kb::load("kb", files(bad).as_ref())).is_err());
    }
    for path in ["../private", "/private", "references\\private", ""] {
        let mut bad = catalog();
        bad["skills"]["units"]["resources"] = json!([path]);
        assert!(block_on(Kb::load("kb", files(bad).as_ref())).is_err());
    }
}

#[test]
fn cancellation_and_model_errors_leave_both_dialog_and_skills_unchanged() {
    let ctx = ctx();
    ctx.dialog.push_user("earlier");
    let model = Model::new(vec![
        asks("activate_skill", json!({"name":"units"})),
        Err(Error::Http {
            status: 400,
            body: "bad request".into(),
        }),
    ]);
    assert!(block_on(agent(model, files(catalog()), vec![]).run("go", &ctx)).is_err());
    assert_eq!(ctx.dialog.len(), 1);
    assert_eq!(ctx.scratchpad.to_json(), json!({}));
    let model = Rc::new(Model {
        answers: RefCell::new(vec![says("must not commit")].into()),
        requests: RefCell::default(),
        cancel: Some(ctx.cancel.clone()),
    });
    assert!(matches!(
        block_on(agent(model, files(catalog()), vec![]).run("go", &ctx)),
        Err(Error::Cancelled)
    ));
    assert_eq!(ctx.dialog.len(), 1);
}

#[test]
fn bad_tool_arguments_do_not_execute_and_errors_return_to_model() {
    let model = Model::new(vec![
        asks("activate_skill", json!({"name":123})),
        asks("missing", json!({})),
        says("recovered"),
    ]);
    let http = files(catalog());
    block_on(agent(model.clone(), http.clone(), vec![]).run("go", &ctx())).unwrap();
    for index in [1, 2] {
        assert!(model.requests.borrow()[index]
            .messages
            .last()
            .unwrap()
            .text()
            .unwrap()
            .contains("Tool error"));
    }
    assert_eq!(http.reads.borrow().len(), 1);
    assert!(check_args(&json!({"additionalProperties":false}), &json!({"extra":1})).is_err());
    assert!(check_args(
        &json!({"properties":{"n":{"type":"integer"}}}),
        &json!({"n":1.5})
    )
    .is_err());
}

#[test]
fn duplicate_names_including_internal_skill_tools_are_rejected() {
    let tool = |name: &str| ToolDef {
        name: name.into(),
        description: String::new(),
        parameters: json!({}),
        func: Rc::new(|_, _| Box::pin(async { Ok(String::new()) })),
    };
    for tools in [
        vec![tool("duplicate"), tool("duplicate")],
        vec![tool("activate_skill")],
    ] {
        let http = files(catalog());
        let kb = block_on(Kb::load("kb", http.as_ref())).unwrap();
        assert!(Agent::new(kb, Model::new(vec![]), tools, http).is_err());
    }
}

#[test]
fn iteration_limit_and_empty_answers_do_not_commit() {
    for answers in [
        vec![says("")],
        (0..3).map(|_| asks("missing", json!({}))).collect(),
    ] {
        let ctx = ctx();
        let mut agent = agent(Model::new(answers.clone()), files(catalog()), vec![]);
        assert_eq!(agent.max_model_calls, 50);
        agent.max_model_calls = 3;
        let error = block_on(agent.run("go", &ctx)).unwrap_err();
        let expected = if answers.len() == 3 {
            "max_model_turns"
        } else {
            "error"
        };
        assert_eq!(error.stop_reason(), expected);
        assert!(ctx.dialog.is_empty());
    }
}

fn counted_tool(calls: Rc<RefCell<usize>>) -> ToolDef {
    ToolDef {
        name: "write".into(),
        description: "Write something.".into(),
        parameters: json!({"type": "object", "properties": {}}),
        func: Rc::new(move |_, _| {
            *calls.borrow_mut() += 1;
            Box::pin(async { Ok("written".into()) })
        }),
    }
}

#[test]
fn a_tool_call_cut_off_at_the_output_limit_never_runs() {
    let calls = Rc::new(RefCell::new(0));
    let mut cut = asks("write", json!({})).unwrap();
    cut.finish_reason = Some("length".into());
    let ctx = ctx();
    let model = Model::new(vec![Ok(cut)]);
    let error =
        block_on(agent(model, files(catalog()), vec![counted_tool(calls.clone())]).run("go", &ctx))
            .unwrap_err();
    assert_eq!(error.stop_reason(), "max_output_tokens");
    assert_eq!(*calls.borrow(), 0);
    assert!(ctx.dialog.is_empty());

    let mut text = says("a long answer that was cut").unwrap();
    text.finish_reason = Some("length".into());
    let result =
        block_on(agent(Model::new(vec![Ok(text)]), files(catalog()), vec![]).run("go", &ctx))
            .unwrap();
    assert_eq!(result.stop_reason, "max_output_tokens");
    assert_eq!(result.message, "a long answer that was cut");
}

#[test]
fn each_request_in_a_turn_extends_the_one_before_so_the_prompt_cache_holds() {
    let calls = Rc::new(RefCell::new(0));
    let model = Model::new(vec![
        asks("activate_skill", json!({"name": "units"})),
        asks("write", json!({})),
        asks("write", json!({})),
        says("done"),
    ]);
    block_on(agent(model.clone(), files(catalog()), vec![counted_tool(calls)]).run("go", &ctx()))
        .unwrap();
    let requests = model.requests.borrow();
    let json = |r: &ChatRequest| serde_json::to_value(&r.messages[1..]).unwrap();
    for pair in requests.windows(2) {
        let (before, after) = (json(&pair[0]), json(&pair[1]));
        let (before, after) = (before.as_array().unwrap(), after.as_array().unwrap());
        assert_eq!(
            &after[..before.len()],
            &before[..],
            "a later request must start with the earlier one"
        );
        assert_eq!(pair[0].tools, pair[1].tools);
    }
    // The system message changes once, when the skill becomes active, and then stays.
    let system: Vec<_> = requests
        .iter()
        .map(|r| r.messages[0].text().unwrap().to_owned())
        .collect();
    assert_ne!(system[0], system[1]);
    assert!(system[1..].windows(2).all(|w| w[0] == w[1]));
}

#[test]
fn compaction_is_transactional_and_failed_summary_keeps_history() {
    for answers in [
        vec![says("old summary"), says("answer")],
        vec![Err(Error::Llm("summary failed".into()))],
    ] {
        let model = Model::new(answers);
        let mut agent = agent(model, files(catalog()), vec![]);
        agent.compaction = Compaction {
            context_window: 1000,
            reserve_tokens: 200,
            keep_recent_tokens: 200,
        };
        let ctx = ctx();
        for _ in 0..20 {
            ctx.dialog.push_user("long history ".repeat(100));
            ctx.dialog.push(Message::assistant("answer"));
        }
        let before = ctx.dialog.len();
        let outcome = block_on(agent.run("next", &ctx));
        let first = ctx.dialog.messages()[0].clone();
        assert!(if first.role == "system" {
            outcome.is_ok() && ctx.dialog.len() < before
        } else {
            outcome.is_err() && ctx.dialog.len() == before
        });
    }
}

#[test]
fn sse_handles_every_utf8_chunk_boundary() {
    for newline in ["\n", "\r\n", "\r"] {
        let text = format!("\u{feff}data: é{newline}{newline}data: two{newline}data: lines{newline}{newline}data: tail");
        for size in 1..=text.len() {
            let mut parser = SseParser::default();
            let mut out = Vec::new();
            for chunk in text.as_bytes().chunks(size) {
                out.extend(parser.feed(chunk));
            }
            out.extend(parser.finish());
            assert_eq!(out, ["é", "two\nlines", "tail"], "{newline:?}, {size}");
        }
    }
}

#[test]
fn oversized_prompt_is_rejected_before_calling_the_model() {
    let model = Model::new(vec![]);
    let mut agent = agent(model.clone(), files(catalog()), vec![]);
    agent.compaction.context_window = 1024;
    let ctx = ctx();
    assert!(block_on(agent.run(&"x".repeat(10000), &ctx)).is_err());
    assert!(model.requests.borrow().is_empty());
    assert!(ctx.dialog.is_empty());
}

#[test]
fn a_turn_that_grows_too_long_compacts_earlier_turns_once() {
    let fetch = ToolDef {
        name: "fetch".into(),
        description: "Fetch a long result.".into(),
        parameters: json!({"type": "object", "properties": {}}),
        func: Rc::new(|_, _| Box::pin(async { Ok("r".repeat(3000)) })),
    };
    let model = Model::new(vec![
        asks("fetch", json!({})),
        says("short summary"),
        says("final"),
    ]);
    let mut agent = agent(model.clone(), files(catalog()), vec![fetch]);
    agent.compaction = Compaction {
        context_window: 2000,
        reserve_tokens: 200,
        keep_recent_tokens: 200,
    };
    let ctx = ctx();
    ctx.dialog.push_user("earlier ".repeat(500));
    ctx.dialog.push(Message::assistant("earlier answer"));
    let result = block_on(agent.run("now", &ctx)).unwrap();
    assert_eq!(result.message, "final");
    assert_eq!(model.requests.borrow().len(), 3); // the call, the summary, the answer
    let roles: Vec<_> = ctx
        .dialog
        .messages()
        .iter()
        .map(|m| m.role.clone())
        .collect();
    assert_eq!(roles, ["system", "user", "assistant"]);
}

struct Events(RefCell<Vec<String>>);
impl gait_core::observer::Observer for Events {
    fn emit(&self, event: &gait_core::observer::Event) {
        self.0
            .borrow_mut()
            .push(format!("{}: {}", event.category, event.message));
    }
}

#[test]
fn page_tools_run_for_one_turn_and_clashing_names_are_skipped() {
    let calls = Rc::new(RefCell::new(0));
    let events = Rc::new(Events(RefCell::default()));
    let mut ctx = Ctx::new(
        Scratchpad::new(),
        Dialog::new(),
        Trace::new(events.clone(), Rc::new(|| 0.0), "t"),
    );
    let mut page = counted_tool(calls.clone());
    page.name = "page_draft".into();
    let mut clash = counted_tool(calls.clone());
    clash.name = "write".into();
    let mut bad = counted_tool(calls.clone());
    bad.name = "bad name".into();
    ctx.page_tools = vec![page, clash, bad];
    let model = Model::new(vec![asks("page_draft", json!({})), says("done")]);
    let fixed = vec![counted_tool(Rc::new(RefCell::new(0)))];
    let agent = agent(model.clone(), files(catalog()), fixed);
    assert_eq!(block_on(agent.run("go", &ctx)).unwrap().message, "done");
    assert_eq!(*calls.borrow(), 1);
    let names: Vec<String> = model.requests.borrow()[0]
        .tools
        .iter()
        .map(|t| t["function"]["name"].as_str().unwrap().to_owned())
        .collect();
    assert_eq!(names.iter().filter(|n| *n == "write").count(), 1);
    assert!(names.contains(&"page_draft".to_owned()));
    assert!(!names.contains(&"bad name".to_owned()));
    let rejected: Vec<_> = events
        .0
        .borrow()
        .iter()
        .filter(|e| e.starts_with("tool_rejected"))
        .cloned()
        .collect();
    assert_eq!(rejected.len(), 2, "{rejected:?}");

    // The next turn without page tools no longer offers them.
    ctx.page_tools.clear();
    let model = Model::new(vec![says("ok")]);
    block_on(self::agent(model.clone(), files(catalog()), vec![]).run("again", &ctx)).unwrap();
    assert!(model.requests.borrow()[0]
        .tools
        .iter()
        .all(|t| t["function"]["name"] != "page_draft"));
}

#[test]
fn active_skill_resource_paths_round_trip_as_json() {
    let paths = json!(["references/a, b.md", "references/a&b.md"]);
    let mut manifest = catalog();
    manifest["skills"]["units"]["resources"] = paths.clone();
    let model = Model::new(vec![
        asks("activate_skill", json!({"name":"units"})),
        says("done"),
    ]);
    let agent = agent(model.clone(), files(manifest), vec![]);
    block_on(agent.run("activate", &ctx())).unwrap();
    let requests = model.requests.borrow();
    let line = requests[1].messages[0]
        .text()
        .unwrap()
        .lines()
        .find_map(|line| line.strip_prefix("Resources (JSON paths): "))
        .unwrap();
    assert_eq!(serde_json::from_str::<Value>(line).unwrap(), paths);
}
