#![cfg(target_arch = "wasm32")]

use gait_core::llm::Http;
use gait_core::Error;
use gait_wasm::FetchHttp;
use wasm_bindgen_test::*;

wasm_bindgen_test_configure!(run_in_browser);

#[wasm_bindgen_test]
async fn fetch_reads_bytes_and_status() {
    let response = FetchHttp::default()
        .get("data:application/octet-stream;base64,AAECAw==")
        .await
        .unwrap();
    assert_eq!(response.status, 200);
    assert_eq!(response.body, vec![0, 1, 2, 3]);
}

#[wasm_bindgen_test]
async fn unreachable_host_is_a_transport_error() {
    let error = FetchHttp::default()
        .get("http://127.0.0.1:9/nothing")
        .await
        .unwrap_err();
    assert!(
        matches!(error, Error::Transport(ref m) if m.contains("127.0.0.1")),
        "{error}"
    );
}

#[wasm_bindgen_test]
fn trace_events_work_on_wasm() {
    let trace = gait_core::observer::Trace::noop();
    trace.event("check", "ok");
}
