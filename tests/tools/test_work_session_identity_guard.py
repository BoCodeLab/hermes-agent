"""Work session must not be downgraded to public auth by shell assignments."""

from tools.terminal_tool import _work_session_identity_override_error


def test_work_session_rejects_identity_assignment(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "wecom")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "wecom-user")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "wecom-chat")

    error = _work_session_identity_override_error(
        "HERMES_SESSION_PLATFORM= check_buy.py --sku-id 1"
    )

    assert error is not None
    assert "cannot override or clear" in error


def test_work_session_rejects_unset_identity(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "wecom")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "wecom-user")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "wecom-chat")

    error = _work_session_identity_override_error("unset HERMES_SESSION_USER_ID")

    assert error is not None


def test_non_work_session_keeps_existing_shell_behavior(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "")
    monkeypatch.delenv("HERMES_SESSION_USER_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)

    assert _work_session_identity_override_error("HERMES_SESSION_PLATFORM=") is None
