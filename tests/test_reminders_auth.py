import io
import json
from types import SimpleNamespace as NS

import pytest
import requests
from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloud2FARequiredException, PyiCloudAcceptTermsException, PyiCloudFailedLoginException

from productivity import reminders_backend as rb
from productivity.common import Problem
from productivity.state import State


CFG = {"apple_id":"synthetic@example.invalid", "timezone":"Asia/Shanghai", "china_mainland":False}
VALID = {"authenticated":True, "trusted_session":True, "requires_2fa":False, "requires_2sa":False}


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def state(tmp_path):
    state = State(tmp_path)
    state.path.mkdir(parents=True)
    return state


@pytest.mark.parametrize("has_dsinfo", [False, True])
def test_real_library_pending_mfa_prompts_then_validates(state, monkeypatch, capsys, has_dsinfo):
    api = PyiCloudService(CFG["apple_id"], "SENSITIVE-PASSWORD", authenticate=False,
                         cookie_directory=str(state.path / "synthetic-session"))
    if has_dsinfo:
        api.data = {"dsInfo":{"hsaVersion":2}}

    def challenge():
        raise PyiCloud2FARequiredException(CFG["apple_id"], requests.Response())

    monkeypatch.setattr(api, "_authenticate", challenge)
    api.authenticate()
    # This is the real pyicloud 2.6.5 state that the previous CLI misclassified.
    assert api._requires_mfa is True and api.requires_2fa is has_dsinfo
    assert ("dsInfo" in api.data) is has_dsinfo
    calls = []
    monkeypatch.setattr(api, "request_2fa_code", lambda: calls.append("deliver") or True)

    def verify(code):
        assert code == "123456" and calls == ["deliver"]
        calls.append("verify")
        api._requires_mfa = False
        api.data = {"hsaTrustedBrowser":True, "dsInfo":{"hsaVersion":2}}
        api.session.data["session_token"] = "synthetic-token"
        api.session.cookies.set("X-APPLE-WEBAUTH-TOKEN", "synthetic-cookie")
        return True

    def validate_session():
        assert calls == ["deliver", "verify"]  # Never clear a partial challenge first.
        calls.append("server_validate")
        return api.data

    monkeypatch.setattr(api, "validate_2fa_code", verify)
    monkeypatch.setattr(api, "_validate_token", validate_session)
    monkeypatch.setattr(rb, "icloud_session", lambda *args, **kwargs: api)
    monkeypatch.setattr("sys.stdin", Terminal("123456\n"))
    result = rb.icloud_login(state, CFG, "SENSITIVE-PASSWORD")
    assert result["authenticated"] and result["session_verified"] and result["two_factor_completed"]
    assert calls == ["deliver", "verify", "server_validate"]
    assert state.load("reminders") == CFG
    captured = capsys.readouterr()
    assert "6-digit" in captured.err and captured.out == ""
    assert "SENSITIVE-PASSWORD" not in json.dumps(result)
    assert "123456" not in (state.path / "reminders.json").read_text()


def test_trusted_cached_session_skips_code_but_independently_checks_server(state, monkeypatch):
    calls = []
    api = NS(requires_2fa=False, requires_2sa=False,
             get_auth_status=lambda: calls.append("validate") or VALID)
    monkeypatch.setattr(rb, "icloud_session", lambda *args, **kwargs: api)
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("Trusted sessions need no code"))
    result = rb.icloud_login(state, CFG, "SENSITIVE")
    assert calls == ["validate"] and result["session_verified"] and not result["two_factor_completed"]


def test_authenticate_returning_without_valid_session_never_saves_success(state, monkeypatch):
    api = NS(requires_2fa=False, requires_2sa=False, get_auth_status=lambda: {"authenticated":False})
    monkeypatch.setattr(rb, "icloud_session", lambda *args, **kwargs: api)
    with pytest.raises(Problem) as error:
        rb.icloud_login(state, CFG, "SENSITIVE")
    assert error.value.code == "needs_login" and error.value.details["auth_stage"] == "session_validation"
    assert not (state.path / "reminders.json").exists()


def pending_api(**overrides):
    return NS(requires_2fa=False, requires_2sa=False, _requires_mfa=True,
              two_factor_delivery_method="trusted_device", is_trusted_session=True,
              request_2fa_code=lambda: True, validate_2fa_code=lambda code: False, **overrides)


def test_pending_mfa_in_noninteractive_terminal_does_not_read_input(state, monkeypatch):
    monkeypatch.setattr(rb, "icloud_session", lambda *args, **kwargs: pending_api())
    monkeypatch.setattr("sys.stdin", io.StringIO("123456\n"))
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("No blocking input in an agent process"))
    with pytest.raises(Problem) as error:
        rb.icloud_login(state, CFG, "SENSITIVE")
    assert error.value.code == "interactive_required"
    assert not (state.path / "reminders.json").exists()


@pytest.mark.parametrize("code,expected", [("wrong\n", "invalid_input"), ("123456\n", "auth_failed"), ("", "interactive_required")])
def test_failed_or_missing_code_never_persists_login(state, monkeypatch, code, expected):
    monkeypatch.setattr(rb, "icloud_session", lambda *args, **kwargs: pending_api())
    monkeypatch.setattr("sys.stdin", Terminal(code))
    with pytest.raises(Problem) as error:
        rb.icloud_login(state, CFG, "SENSITIVE")
    assert error.value.code == expected
    assert not (state.path / "reminders.json").exists()


@pytest.mark.parametrize("status", [
    {"authenticated":False}, {"authenticated":True, "trusted_session":False},
    {**VALID, "requires_2fa":True}, {**VALID, "requires_2sa":True},
])
def test_successful_code_is_not_enough_without_server_session_readback(state, monkeypatch, status):
    api = pending_api()
    api.validate_2fa_code = lambda code: True
    api.get_auth_status = lambda: status
    monkeypatch.setattr(rb, "icloud_session", lambda *args, **kwargs: api)
    monkeypatch.setattr("sys.stdin", Terminal("123456\n"))
    with pytest.raises(Problem) as error:
        rb.icloud_login(state, CFG, "SENSITIVE")
    assert error.value.code == "needs_login"
    assert not (state.path / "reminders.json").exists()


def test_noninteractive_business_call_detects_hidden_mfa_before_service_access(state):
    api = pending_api()
    with pytest.raises(Problem) as error:
        rb.IOSReminders(state, CFG, api=api)
    assert error.value.code == "needs_login"


def test_security_key_challenge_is_explicitly_unsupported(state, monkeypatch):
    api = pending_api()
    api.two_factor_delivery_method = "security_key"
    monkeypatch.setattr(rb, "icloud_session", lambda *args, **kwargs: api)
    monkeypatch.setattr("sys.stdin", Terminal("123456\n"))
    with pytest.raises(Problem) as error:
        rb.icloud_login(state, CFG, "SENSITIVE")
    assert error.value.code == "unsupported_auth"
    assert not (state.path / "reminders.json").exists()


def test_auth_diagnostics_preserve_status_without_provider_secrets():
    response = requests.Response()
    response.status_code = 403
    response._content = b"SENSITIVE-RESPONSE"
    cause = requests.HTTPError("SENSITIVE-PASSWORD", response=response)
    wrapped = PyiCloudFailedLoginException("SENSITIVE-COOKIE", cause)

    def fail():
        raise wrapped

    with pytest.raises(Problem) as error:
        rb.auth_call({**CFG, "china_mainland":True}, "authentication", fail)
    assert error.value.details["http_status"] == 403
    assert error.value.details["auth_region"] == "china_mainland"
    assert "HTTPError" in error.value.details["exception_types"]
    assert "SENSITIVE" not in str(error.value) + json.dumps(error.value.details)


@pytest.mark.parametrize("failure,expected", [(requests.Timeout("SENSITIVE"), "network_error"),
                                              (PyiCloudAcceptTermsException("SENSITIVE"), "icloud_action_required")])
def test_auth_errors_have_actionable_categories(failure, expected):
    def fail():
        raise failure

    with pytest.raises(Problem) as error:
        rb.auth_call(CFG, "authentication", fail)
    assert error.value.code == expected and "SENSITIVE" not in str(error.value)
