from __future__ import annotations

import io
import logging

import pytest
from conftest import SECRET, secret_env

from app.redaction import REDACTED, RedactingFilter, SecretRedactor, install_redaction


def test_examples():
    r = SecretRedactor()
    assert r.redact("Authorization: Bearer sk-abc") == f"Authorization: Bearer {REDACTED}"
    assert r.redact("{'x-goog-api-key': 'AIza123'}") == "{'x-goog-api-key': '[REDACTED]'}"
    assert r.redact("https://h/v1?key=AIza123&x=1") == "https://h/v1?key=[REDACTED]&x=1"
    assert r.redact("https://h/v1?x=1&api_key=zzz") == "https://h/v1?x=1&api_key=[REDACTED]"
    assert r.redact("nothing here") == "nothing here"


def test_already_redacted_not_doubled():
    r = SecretRedactor()
    assert r.redact(f"Authorization: Bearer {REDACTED}") == f"Authorization: Bearer {REDACTED}"


def test_short_secrets_ignored():
    r = SecretRedactor(["abc", "  ab  "])
    assert r.redact("abc ab") == "abc ab"
    r.add("123456")
    assert r.redact("x 123456 y") == f"x {REDACTED} y"


def test_overlapping_longest_first():
    r = SecretRedactor(["secret-abc", "secret-abcdef"])
    assert r.redact("k=secret-abcdef") == f"k={REDACTED}"
    assert "def" not in r.redact("secret-abcdef")


def test_repr_hides_secret():
    r = SecretRedactor([SECRET])
    assert SECRET not in repr(r)
    assert "1 secrets" in repr(r)


def _logger(name, redactor, install_on_handler=True):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    parent = logging.getLogger(name)
    parent.setLevel(logging.DEBUG)
    parent.addHandler(handler)
    parent.propagate = False
    flt = install_redaction(redactor, logger=parent)
    return parent, handler, stream, flt


def test_filter_redacts_messages_and_exceptions():
    parent, handler, stream, _ = _logger("t.redact.a", SecretRedactor([SECRET]))
    try:
        parent.error("auth %s", f"Bearer {SECRET}")
        try:
            raise RuntimeError(f"bad key {SECRET}")
        except RuntimeError:
            parent.exception("failed")
        out = stream.getvalue()
        assert REDACTED in out
        assert SECRET not in out
        assert "RuntimeError" in out
    finally:
        parent.removeHandler(handler)


def test_filter_applies_to_child_loggers():
    parent, handler, stream, _ = _logger("t.redact.b", SecretRedactor([SECRET]))
    child = logging.getLogger("t.redact.b.child")
    try:
        child.warning("leak %s", SECRET)
        try:
            raise ValueError(SECRET)
        except ValueError:
            child.exception("oops")
        assert SECRET not in stream.getvalue()
        assert REDACTED in stream.getvalue()
    finally:
        parent.removeHandler(handler)


def test_install_twice_adds_one_filter():
    r = SecretRedactor([SECRET])
    parent, handler, _, f1 = _logger("t.redact.c", r)
    try:
        f2 = install_redaction(r, logger=parent)
        assert isinstance(f2, RedactingFilter)
        assert sum(isinstance(f, RedactingFilter) for f in handler.filters) == 1
    finally:
        parent.removeHandler(handler)


def test_filter_survives_bad_args_and_stack_info():
    r = SecretRedactor([SECRET])
    flt = RedactingFilter(r)
    rec = logging.LogRecord("n", logging.INFO, "f", 1, "bad %d fmt", ("x",), None)
    assert flt.filter(rec) is True
    assert rec.args == ()
    rec2 = logging.LogRecord("n", logging.INFO, "f", 1, "m", (), None)
    rec2.stack_info = f"stack {SECRET}"
    rec2.exc_text = f"cached {SECRET}"
    flt.filter(rec2)
    assert SECRET not in rec2.stack_info and SECRET not in rec2.exc_text


def test_from_config(make_config):
    r = SecretRedactor.from_config(make_config(), secret_env())
    for v in secret_env().values():
        assert r.redact(f"x {v} y") == f"x {REDACTED} y"


@pytest.fixture(autouse=True)
def _cleanup_loggers():
    yield
    for name in ("t.redact.a", "t.redact.b", "t.redact.c"):
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
