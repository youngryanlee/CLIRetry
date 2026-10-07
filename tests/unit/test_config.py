import pytest

from cliretry.config import load_config, parse_config
from cliretry.models import RetryError


def test_example_parses():
    c = load_config("config.example.toml")
    assert c.recovery.prompt == "继续"
    assert c.profile.executable_paths == ()


@pytest.mark.parametrize("raw", [
    {"unknown": True}, {"schema_version": True}, {"daemon": {"max_sessions": False}},
    {"daemon": {"poll_interval_s": float("nan")}}, {"daemon": {"max_sessions": 100}},
    {"recovery": {"prompt": "继续\r"}}, {"recovery": {"prompt": "/goal resume"}},
    {"recovery": {"prompt": "继续 "}}, {"recovery": {"prompt": " /model"}},
    {"recovery": {"backoff_cap_s": 1}}, {"recovery": {"jitter_fraction": -1}},
    {"logging": {"capture_screen": True}},
    {"profiles": {"codex-local-v1": {"enabled_categories": ["AUTH"]}}},
    {"profiles": {"codex-local-v1": {"max_rows": 10}}},
])
def test_invalid_config_rejected(raw):
    with pytest.raises(RetryError):
        parse_config(raw)


def test_no_shell_expansion():
    c = parse_config({"daemon": {"state_dir": "/tmp/$(touch NO)-test"}})
    assert "$(touch NO)" in str(c.state_dir)


@pytest.mark.parametrize("pattern", ["(a+)+", "^a.*b.*$", "^a[bc]$", r"^a\1$", "^a|b$"])
def test_unsafe_custom_regex_rejected(pattern):
    with pytest.raises(RetryError):
        parse_config({"profiles": {"codex-local-v1": {"custom_rules": [
            {"id": "proxy", "category": "UPSTREAM_TRANSIENT", "pattern": pattern}
        ]}}})


def test_custom_transient_rule_and_auth_precedence():
    from cliretry.profiles import CodexProfile
    from tests.helpers import frame
    c = parse_config({"profiles": {"codex-local-v1": {"custom_rules": [
        {"id": "proxy", "category": "UPSTREAM_TRANSIENT", "pattern": "^Proxy unavailable: .*$"}
    ]}}})
    profile = CodexProfile(c.profile)
    assert profile.classify(frame("capacity", error="■ Proxy unavailable: temporary outage")).category == "UPSTREAM_TRANSIENT"
    assert profile.classify(frame("capacity", error="■ Proxy unavailable: auth_not_found")).category == "AUTH"
