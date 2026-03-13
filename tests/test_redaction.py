"""Tests for libs.core.redaction — secret redaction for logging."""

from __future__ import annotations

import logging

from libs.core.redaction import (
    SecretRedactingFilter,
    configure_logging,
    redact_for_log,
    redact_string,
)


class TestRedactForLog:
    def test_redacts_li_at(self):
        result = redact_for_log({"li_at": "secret_cookie", "label": "test"})
        assert result["li_at"] == "[REDACTED]"
        assert result["label"] == "test"

    def test_redacts_jsessionid(self):
        result = redact_for_log({"jsessionid": "ajax:tok123"})
        assert result["jsessionid"] == "[REDACTED]"

    def test_redacts_multiple_keys(self):
        data = {
            "li_at": "secret",
            "jsessionid": "secret",
            "auth_json": "secret",
            "cookie": "secret",
            "password": "secret",
            "token": "secret",
            "api_key": "secret",
            "proxy_url": "http://proxy",
            "safe_key": "visible",
        }
        result = redact_for_log(data)
        for key in ("li_at", "jsessionid", "auth_json", "cookie", "password", "token", "api_key", "proxy_url"):
            assert result[key] == "[REDACTED]", f"{key} should be redacted"
        assert result["safe_key"] == "visible"

    def test_case_insensitive(self):
        result = redact_for_log({"LI_AT": "secret", "Password": "secret"})
        assert result["LI_AT"] == "[REDACTED]"
        assert result["Password"] == "[REDACTED]"

    def test_nested_dict(self):
        result = redact_for_log({"outer": {"li_at": "secret", "ok": True}})
        assert result["outer"]["li_at"] == "[REDACTED]"
        assert result["outer"]["ok"] is True

    def test_nested_list(self):
        result = redact_for_log({"items": [{"li_at": "s1"}, {"li_at": "s2"}]})
        assert result["items"][0]["li_at"] == "[REDACTED]"
        assert result["items"][1]["li_at"] == "[REDACTED]"

    def test_deeply_nested(self):
        data = {"a": {"b": {"c": {"password": "deep_secret", "value": 42}}}}
        result = redact_for_log(data)
        assert result["a"]["b"]["c"]["password"] == "[REDACTED]"
        assert result["a"]["b"]["c"]["value"] == 42

    def test_does_not_mutate_original(self):
        original = {"li_at": "secret", "label": "test"}
        redact_for_log(original)
        assert original["li_at"] == "secret"

    def test_list_input(self):
        result = redact_for_log([{"li_at": "s"}, {"ok": True}])
        assert result[0]["li_at"] == "[REDACTED]"
        assert result[1]["ok"] is True

    def test_non_dict_passthrough(self):
        assert redact_for_log("string") == "string"
        assert redact_for_log(42) == 42
        assert redact_for_log(None) is None

    def test_empty_dict(self):
        assert redact_for_log({}) == {}


class TestRedactString:
    def test_redacts_li_at_equals(self):
        assert "li_at=[REDACTED]" in redact_string("cookie: li_at=SECRET_VALUE; path=/")

    def test_redacts_jsessionid_equals(self):
        result = redact_string("JSESSIONID=ajax:csrf_tok_123")
        assert "ajax:csrf_tok_123" not in result
        assert "JSESSIONID=[REDACTED]" in result

    def test_redacts_colon_format(self):
        result = redact_string("token: bearer_abc123")
        assert "bearer_abc123" not in result
        assert "token: [REDACTED]" in result

    def test_preserves_safe_content(self):
        safe = "account_id=42 label=test-account"
        assert redact_string(safe) == safe

    def test_multiple_secrets_in_one_string(self):
        text = "li_at=SECRET1; JSESSIONID=SECRET2"
        result = redact_string(text)
        assert "SECRET1" not in result
        assert "SECRET2" not in result

    def test_case_insensitive(self):
        result = redact_string("LI_AT=secret_value")
        assert "secret_value" not in result

    def test_empty_string(self):
        assert redact_string("") == ""


class TestSecretRedactingFilter:
    def _make_record(self, msg: str, args: tuple | dict | None = None) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg=msg, args=args, exc_info=None,
        )
        return record

    def test_scrubs_msg_string(self):
        filt = SecretRedactingFilter()
        record = self._make_record("Login with li_at=SUPERSECRET done")
        filt.filter(record)
        assert "SUPERSECRET" not in record.msg
        assert "[REDACTED]" in record.msg

    def test_scrubs_string_args(self):
        filt = SecretRedactingFilter()
        record = self._make_record("Auth: %s", ("li_at=SECRETVAL",))
        filt.filter(record)
        assert "SECRETVAL" not in str(record.args)

    def test_scrubs_dict_args(self):
        filt = SecretRedactingFilter()
        # LogRecord unwraps single-element tuple containing a dict
        record = self._make_record("Data: %s", ({"li_at": "secret"},))
        filt.filter(record)
        # After LogRecord unpacking, args is the dict itself
        assert record.args["li_at"] == "[REDACTED]"

    def test_always_returns_true(self):
        filt = SecretRedactingFilter()
        record = self._make_record("safe message")
        assert filt.filter(record) is True

    def test_non_string_args_passthrough(self):
        filt = SecretRedactingFilter()
        record = self._make_record("count: %d", (42,))
        filt.filter(record)
        assert record.args == (42,)


class TestConfigureLogging:
    def test_adds_filter_to_root(self):
        root = logging.getLogger()
        original_filters = [f for f in root.filters if isinstance(f, SecretRedactingFilter)]
        original_handlers = len(root.handlers)

        configure_logging()

        new_filters = [f for f in root.filters if isinstance(f, SecretRedactingFilter)]
        assert len(new_filters) >= 1

        # Idempotent: calling again should not add a second filter
        configure_logging()
        after_second = [f for f in root.filters if isinstance(f, SecretRedactingFilter)]
        assert len(after_second) == len(new_filters)
