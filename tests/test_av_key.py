"""The /av key (#63): one per installation, never expiring, never in the logs."""
from __future__ import annotations

from custom_components.vimar_intercom import log_redact, runtime

BASE = {"sip_user": "u", "sip_password": "p", "sip_domain": "d"}


def test_a_new_key_is_long_random_and_url_safe():
    keys = {runtime.new_av_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(len(k) == 32 and all(c.isalnum() or c in "-_" for c in k) for k in keys)


def test_configure_takes_the_key_from_the_entry():
    runtime.configure({**BASE, "av_key": "stored-key"})
    assert runtime.AV_KEY == "stored-key"
    assert runtime.av_key_valid("stored-key")
    assert not runtime.av_key_valid("stored-keY")


def test_without_a_stored_key_configure_never_leaves_it_empty():
    """An empty key would make an empty `auth=` valid."""
    runtime.configure(BASE)
    first = runtime.AV_KEY
    runtime.configure(BASE)
    assert first and runtime.AV_KEY and first != runtime.AV_KEY
    assert not runtime.av_key_valid("")


def test_nothing_is_valid_while_the_key_is_empty(monkeypatch):
    monkeypatch.setattr(runtime, "AV_KEY", "")
    assert not runtime.av_key_valid("")
    assert not runtime.av_key_valid("anything")


def test_a_non_string_key_is_not_valid():
    runtime.configure({**BASE, "av_key": "stored-key"})
    assert not runtime.av_key_valid(None)
    assert not runtime.av_key_valid(["stored-key"])


def test_the_key_parameter_is_masked_in_our_logs():
    key = runtime.new_av_key()
    for line in (f"GET /api/vimar_intercom/av?{runtime.AV_KEY_PARAM}={key}&autocall=0",
                 f"http://127.0.0.1:8123/api/vimar_intercom/av?{runtime.AV_KEY_PARAM}={key}",
                 f"{{'av_key': '{key}'}}"):
        assert key not in log_redact.redact(line), line


def test_digest_qop_auth_is_not_taken_for_a_key():
    line = 'WWW-Authenticate: Digest realm="r", nonce="n", qop="auth"'
    assert log_redact.redact(line).endswith('qop="auth"')
