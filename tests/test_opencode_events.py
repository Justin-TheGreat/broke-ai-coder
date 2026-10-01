from __future__ import annotations

import json

from conftest import NOW

from app.opencode.events import classify_opencode_error, classify_text, parse_event_line
from app.providers.base import ErrorClass


def line(obj) -> str:
    return json.dumps(obj)


def parse(obj):
    return parse_event_line(line(obj), now=NOW)


def test_text_event():
    ev = parse({"type": "text", "sessionID": "ses_1", "part": {"text": "hi"}})
    assert ev is not None and ev.type == "text" and ev.text == "hi" and ev.session_id == "ses_1"


def test_tool_use_file_tool():
    ev = parse(
        {
            "type": "tool_use",
            "part": {
                "tool": "edit",
                "state": {"status": "completed", "input": {"filePath": "a/b.py"}},
            },
        }
    )
    assert ev.tool == "edit" and ev.tool_status == "completed" and ev.file_path == "a/b.py"


def test_tool_use_non_file_tool_has_no_path():
    ev = parse(
        {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"filePath": "x"}}}}
    )
    assert ev.tool == "bash" and ev.file_path is None


def test_step_finish_tokens():
    ev = parse({"type": "step_finish", "part": {"tokens": {"input": 3, "output": 4}}})
    assert (ev.input_tokens, ev.output_tokens) == (3, 4)


def test_bool_and_negative_tokens_rejected():
    ev = parse({"type": "step_finish", "part": {"tokens": {"input": True, "output": -1}}})
    assert ev.input_tokens is None and ev.output_tokens is None


def test_session_id_from_part():
    ev = parse({"type": "step_start", "part": {"sessionID": "ses_p"}})
    assert ev.session_id == "ses_p"


def test_invalid_session_ids_ignored():
    for bad in ("-x", "a b", "x" * 200, "", 5, None):
        ev = parse({"type": "step_start", "sessionID": bad})
        assert ev.session_id is None


def test_non_json_and_non_dict_and_missing_type():
    assert parse_event_line("not json", now=NOW) is None
    assert parse_event_line(b"\xff\xfe garbage", now=NOW) is None
    assert parse_event_line("[]", now=NOW) is None
    assert parse_event_line('"str"', now=NOW) is None
    assert parse_event_line("{}", now=NOW) is None
    assert parse_event_line('{"type": 5}', now=NOW) is None
    assert parse_event_line("", now=NOW) is None


def test_unknown_type_is_parsed_but_inert():
    ev = parse({"type": "weird"})
    assert ev.type == "weird" and ev.error is None


def err(name, **data):
    return classify_opencode_error({"name": name, "data": data}, now=NOW)


def test_auth():
    assert err("ProviderAuthError").error_class == ErrorClass.AUTH_FAILED


def test_429_rate_limited_with_retry_after_case_insensitive():
    e = err("APIError", statusCode=429, responseHeaders={"Retry-After": "12"})
    assert e.error_class == ErrorClass.RATE_LIMITED
    assert e.http_status == 429 and e.retry_after_s == 12


def test_429_quota_text():
    e = err("APIError", statusCode=429, message="You exceeded your current quota")
    assert e.error_class == ErrorClass.QUOTA_EXHAUSTED


def test_504_never_refined():
    e = err("APIError", statusCode=504, message="rate limit hit")
    assert e.error_class == ErrorClass.TIMEOUT_UNKNOWN_OUTCOME


def test_400_context():
    e = err("APIError", statusCode=400, message="maximum context length exceeded")
    assert e.error_class == ErrorClass.CONTEXT_TOO_LARGE


def test_400_plain_stays_invalid():
    assert err("APIError", statusCode=400, message="bad").error_class == ErrorClass.INVALID_REQUEST


def test_no_status_quota_text():
    e = err("UnknownError", message="Resource has been exhausted (e.g. check quota).")
    assert e.error_class == ErrorClass.QUOTA_EXHAUSTED


def test_output_length():
    assert err("MessageOutputLengthError").error_class == ErrorClass.CONTEXT_TOO_LARGE


def test_garbage_is_unknown():
    assert classify_opencode_error("junk", now=NOW).error_class == ErrorClass.UNKNOWN
    assert classify_opencode_error(None, now=NOW).error_class == ErrorClass.UNKNOWN
    assert err("Whatever").error_class == ErrorClass.UNKNOWN


def test_classify_text_order():
    assert classify_text("rate limit and quota") == ErrorClass.QUOTA_EXHAUSTED
    assert classify_text("HTTP 429") == ErrorClass.RATE_LIMITED
    assert classify_text("context window") == ErrorClass.CONTEXT_TOO_LARGE
    assert classify_text("model gpt not found") == ErrorClass.MODEL_UNAVAILABLE
    assert classify_text("boom") == ErrorClass.UNKNOWN


def test_nothing_raises_on_nested_junk():
    junk = [
        {"name": 1, "data": 5},
        {"name": None, "data": {"statusCode": True, "message": 3, "responseHeaders": [1]}},
        {"data": {"statusCode": 10**9, "responseHeaders": {1: 2, "retry-after": 5}}},
        {"data": {"message": ["x"], "responseBody": {"a": 1}}},
        [],
        7,
    ]
    for j in junk:
        classify_opencode_error(j, now=NOW)
        parse({"type": "error", "error": j})
        parse({"type": "tool_use", "part": j})
        parse({"type": "step_finish", "part": {"tokens": j}})
