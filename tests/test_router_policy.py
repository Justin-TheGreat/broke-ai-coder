from __future__ import annotations

from app.router.policy import ModelRoutingStatus, is_allowlisted, model_listing


def test_is_allowlisted(make_config):
    cfg = make_config()
    assert is_allowlisted(cfg, "gemini", "g-3.8")
    assert is_allowlisted(cfg, "openrouter", "or-paid-1")
    assert not is_allowlisted(cfg, "gemini", "g-pro")
    assert not is_allowlisted(cfg, "groq", "g-3.8")


def test_model_listing(make_config):
    cfg = make_config()
    out = model_listing(cfg, "gemini", ["g-pro", "g-3.8", "g-lite", "g-pro"])
    assert [(m.model, m.status, m.rank) for m in out] == [
        ("g-3.8", ModelRoutingStatus.ALLOWED, 0),
        ("g-3.7", ModelRoutingStatus.ALLOWED, 1),
        ("g-lite", ModelRoutingStatus.DISCOVERED_ONLY, None),
        ("g-pro", ModelRoutingStatus.DISCOVERED_ONLY, None),
    ]
    assert out[0].policy_id == "gemini-free"
    assert out[2].policy_id is None
