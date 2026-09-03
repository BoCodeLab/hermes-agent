import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway import work_user_gate as gate
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class _FakeIdentity:
    def __init__(self, *, user_id="", chat_id=""):
        self.user_id = user_id
        self.chat_id = chat_id


class _FakeUserModule:
    class UserConfigError(Exception):
        pass

    class UserConfigAmbiguousError(UserConfigError):
        pass

    WeComIdentity = _FakeIdentity

    def __init__(self, *, initialized=False):
        self.initialized = initialized
        self.saved = None
        self.pending = []

    def auth_state_path_for_user(self, system_id, *, identity):
        return Path("unused") / f"{system_id}.json"

    def find_user_config(self, identity):
        if not self.initialized:
            raise self.UserConfigError("not initialized")
        return ("config.json", {})

    def credentials_for_user(self, _system_id, *, identity):
        if not self.initialized:
            raise self.UserConfigError("not initialized")
        return ("user", "password")

    def write_user_config(self, identity, **kwargs):
        self.initialized = True
        self.saved = (identity, kwargs)
        return "config.json"

    def create_pending_user_request(self, identity, **kwargs):
        metadata = {
            "request_id": "WA-ABCDEF12",
            "user_id": identity.user_id,
            "chat_id": identity.chat_id,
            "user_name": kwargs.get("user_name", ""),
            "sso_user": kwargs.get("sso_username", ""),
        }
        self.pending = [metadata]
        return metadata

    def list_pending_user_requests(self):
        return list(self.pending)

    def approve_pending_user_request(self, request_id):
        for metadata in self.pending:
            if metadata["request_id"] == request_id:
                self.pending.remove(metadata)
                return metadata
        return None

    def reject_pending_user_request(self, request_id):
        return self.approve_pending_user_request(request_id)


def _source(*, chat_type="dm"):
    return SimpleNamespace(
        platform=Platform.WECOM,
        user_id="wecom-user",
        chat_id="wecom-chat",
        chat_type=chat_type,
    )


def test_unknown_wecom_user_is_directed_to_initialize(monkeypatch):
    module = _FakeUserModule()
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)

    reply = gate.check_work_user(_source(), "查询订单")

    assert "账号初始化" in reply


def test_group_cannot_initialize_account(monkeypatch):
    module = _FakeUserModule()
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)

    reply = asyncio.run(
        gate.initialize_work_user(
            _source(chat_type="group"),
            '/work-init {"admin_user":"a","admin_pass":"secret","sso_user":"s","sso_pass":"secret"}',
        )
    )

    assert "私聊" in reply
    assert module.saved is None


def test_private_initialization_logs_in_and_allows_later_queries(monkeypatch):
    module = _FakeUserModule()
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)
    login_calls = []

    async def fake_login(**kwargs):
        login_calls.append(kwargs["system_id"])
        return True

    monkeypatch.setattr(gate, "_run_login_script", fake_login)
    monkeypatch.setattr(gate, "_run_probe_login", AsyncMock(return_value=True))

    reply = asyncio.run(
        gate.initialize_work_user(
            _source(),
            '/work-init {"admin_user":"a","admin_pass":"secret","sso_user":"s","sso_pass":"secret"}',
        )
    )

    assert "自动登录已完成" in reply
    assert "secret" not in reply
    assert module.saved is not None
    assert login_calls == ["admin", "pop_admin"]
    assert gate.check_work_user(_source(), "查询订单") is None


def test_private_initialization_reports_only_failed_system(monkeypatch):
    module = _FakeUserModule()
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)

    async def fake_login(**kwargs):
        return kwargs["system_id"] == "admin"

    monkeypatch.setattr(gate, "_run_login_script", fake_login)
    monkeypatch.setattr(gate, "_run_probe_login", AsyncMock(return_value=True))

    reply = asyncio.run(
        gate.initialize_work_user(
            _source(),
            '/work-init {"admin_user":"a","admin_pass":"secret","sso_user":"s","sso_pass":"secret"}',
        )
    )

    assert "账号配置已保存" in reply
    assert "SSO" in reply
    assert "secret" not in reply


def test_login_subprocess_uses_current_wecom_identity(monkeypatch, tmp_path):
    module = _FakeUserModule()
    profile_home = tmp_path / "work"
    skill_dir = profile_home / "skills" / "ybm100-admin-cookie"
    script_dir = skill_dir / "scripts"
    python_path = skill_dir / ".venv" / "Scripts" / "python.exe"
    script_dir.mkdir(parents=True)
    python_path.parent.mkdir(parents=True)
    python_path.touch()
    (script_dir / "login.py").touch()
    state_path = tmp_path / "admin_auth.json"

    def auth_state_path_for_user(system_id, *, identity):
        assert system_id == "admin"
        assert identity.user_id == "wecom-user"
        assert identity.chat_id == "wecom-chat"
        return state_path

    module.auth_state_path_for_user = auth_state_path_for_user
    captured = {}

    class _Process:
        async def wait(self):
            return 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        state_path.write_text("{}", encoding="utf-8")
        return _Process()

    monkeypatch.setattr(gate.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    succeeded = asyncio.run(
        gate._run_login_script(
            module=module,
            profile_home=profile_home,
            identity=gate.WorkUserIdentity(
                user_id="wecom-user", chat_id="wecom-chat", chat_type="dm"
            ),
            system_id="admin",
            script_name="login.py",
        )
    )

    assert succeeded is True
    assert captured["env"]["HERMES_HOME"] == str(profile_home)
    assert captured["env"]["HERMES_SESSION_USER_ID"] == "wecom-user"
    assert captured["env"]["HERMES_SESSION_CHAT_ID"] == "wecom-chat"
    assert "secret" not in str(captured["args"])


def test_login_process_tree_is_stopped_on_cancellation(monkeypatch):
    terminate = MagicMock()
    monkeypatch.setattr("gateway.status.terminate_pid", terminate)

    class _Process:
        pid = 4321
        returncode = None

        async def wait(self):
            self.returncode = 1
            return self.returncode

        def kill(self):
            raise AssertionError("tree termination should be used first")

    asyncio.run(gate._stop_login_process(_Process()))

    terminate.assert_called_once_with(4321, force=True)


def test_gateway_awaits_automatic_initialization(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.WECOM: PlatformConfig(enabled=True)}
    )
    runner.adapters = {Platform.WECOM: SimpleNamespace(send=AsyncMock())}
    runner.pairing_store = MagicMock()
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._handle_message_with_agent = AsyncMock(return_value="agent reply")

    initialize = AsyncMock(return_value="automatic login complete")
    monkeypatch.setattr(gate, "is_work_user_gate_enabled", lambda _source: True)
    monkeypatch.setattr(gate, "initialize_work_user", initialize)
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])

    event = MessageEvent(
        text='/work-init {"admin_user":"a","admin_pass":"secret","sso_user":"s","sso_pass":"secret"}',
        source=SessionSource(
            platform=Platform.WECOM,
            user_id="wecom-user",
            chat_id="wecom-chat",
            chat_type="dm",
        ),
    )

    reply = asyncio.run(runner._handle_message(event))

    assert reply == "automatic login complete"
    initialize.assert_awaited_once_with(event.source, event.text)
    runner._handle_message_with_agent.assert_not_awaited()


def test_uninitialized_work_dm_bypasses_pairing_code(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.WECOM: PlatformConfig(
                enabled=True,
                home_channel=HomeChannel(
                    platform=Platform.WECOM,
                    chat_id="manager-chat",
                    user_id="manager-user",
                    name="manager",
                ),
            )
        }
    )
    runner.adapters = {Platform.WECOM: SimpleNamespace(send=AsyncMock())}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    runner._is_user_authorized = MagicMock(return_value=False)
    runner._handle_message_with_agent = AsyncMock(return_value="agent reply")

    monkeypatch.setattr(gate, "is_work_user_gate_enabled", lambda _source: True)
    monkeypatch.setattr(gate, "_profile_user_module", lambda: _FakeUserModule())
    monkeypatch.setattr(gate, "_run_probe_login", AsyncMock(return_value=True))
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])

    event = MessageEvent(
        text='/work-init {"admin_user":"a","admin_pass":"secret","sso_user":"s","sso_pass":"secret"}',
        source=SessionSource(
            platform=Platform.WECOM,
            user_id="new-user",
            chat_id="new-chat",
            chat_type="dm",
        ),
    )

    reply = asyncio.run(runner._handle_message(event))

    assert "申请已提交" in reply
    runner.pairing_store.generate_code.assert_not_called()
    runner._handle_message_with_agent.assert_not_awaited()
    runner.adapters[Platform.WECOM].send.assert_awaited_once_with(
        "manager-chat",
        runner.adapters[Platform.WECOM].send.await_args.args[1],
        metadata={"wecom_force_proactive": True},
    )


def test_uninitialized_user_submits_request_to_manager(monkeypatch):
    module = _FakeUserModule()
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)
    monkeypatch.setattr(gate, "_run_probe_login", AsyncMock(return_value=True))
    sent = []

    async def send_message(chat_id, content):
        sent.append((chat_id, content))
        return True

    reply = asyncio.run(
        gate.handle_work_access_request(
            _source(),
            '/work-init {"admin_user":"a","admin_pass":"secret","sso_user":"s","sso_pass":"secret"}',
            None,
            authorized=False,
            manager_user_id="manager-user",
            manager_chat_id="manager-user",
            send_message=send_message,
            grant_access=lambda *_args: True,
        )
    )

    assert "申请已提交" in reply
    assert module.pending[0]["request_id"] == "WA-ABCDEF12"
    assert sent[0][0] == "manager-user"
    assert "secret" not in sent[0][1]
    assert "同意" in sent[0][1]
    # 审批通知应展示申请人的 SSO 账号，而非底层企微 user_id。
    assert "申请人：s" in sent[0][1]


def test_pending_user_is_told_to_wait_for_approval(monkeypatch):
    module = _FakeUserModule()
    module.pending = [{
        "request_id": "WA-ABCDEF12",
        "user_id": "wecom-user",
        "chat_id": "wecom-chat",
        "user_name": "new user",
    }]
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)

    async def send_message(_chat_id, _content):
        return True

    reply = asyncio.run(
        gate.handle_work_access_request(
            _source(),
            "查询订单",
            None,
            authorized=False,
            manager_user_id="manager-user",
            manager_chat_id="manager-chat",
            send_message=send_message,
            grant_access=lambda *_args: True,
        )
    )

    assert "等待管理员审批" in reply


def test_only_configured_manager_can_approve(monkeypatch):
    module = _FakeUserModule()
    module.pending = [{
        "request_id": "WA-ABCDEF12",
        "user_id": "new-user",
        "chat_id": "new-chat",
        "user_name": "new user",
    }]
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)

    async def send_message(_chat_id, _content):
        return True

    outsider_reply = asyncio.run(
        gate.handle_work_access_request(
            _source(),
            "同意",
            "【线上问题知识库账号接入申请】\n申请编号：WA-ABCDEF12",
            authorized=False,
            manager_user_id="manager-user",
            manager_chat_id="manager-user",
            send_message=send_message,
            grant_access=lambda *_args: True,
        )
    )
    assert "尚未授权" in outsider_reply
    assert module.pending


def test_manager_can_reject_pending_request(monkeypatch):
    module = _FakeUserModule()
    module.pending = [{
        "request_id": "WA-ABCDEF12",
        "user_id": "new-user",
        "chat_id": "new-chat",
        "user_name": "new user",
    }]
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)
    sent = []

    async def send_message(chat_id, content):
        sent.append((chat_id, content))
        return True

    reply = asyncio.run(
        gate.handle_work_access_request(
            SimpleNamespace(
                platform=Platform.WECOM,
                user_id="manager-user",
                chat_id="manager-chat",
                chat_type="dm",
            ),
            "拒绝",
            None,
            authorized=True,
            manager_user_id="manager-user",
            manager_chat_id="manager-chat",
            send_message=send_message,
            grant_access=lambda *_args: False,
        )
    )

    assert "已拒绝" in reply
    assert module.pending == []
    assert sent == [("new-chat", "管理员已拒绝你的账号接入申请。")]


def test_manager_approval_schedules_login_and_grants_access(monkeypatch):
    module = _FakeUserModule()
    module.pending = [{
        "request_id": "WA-ABCDEF12",
        "user_id": "new-user",
        "chat_id": "new-chat",
        "user_name": "new user",
    }]
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)
    monkeypatch.setattr(gate, "_run_login_script", AsyncMock(return_value=True))
    sent = []
    granted = []
    scheduled = []

    async def send_message(chat_id, content):
        sent.append((chat_id, content))
        return True

    def schedule_task(coro):
        scheduled.append(coro)

    reply = asyncio.run(
        gate.handle_work_access_request(
            SimpleNamespace(
                platform=Platform.WECOM,
                user_id="manager-user",
                chat_id="manager-user",
                chat_type="dm",
            ),
            "同意",
            None,
            authorized=False,
            manager_user_id="manager-user",
            manager_chat_id="manager-user",
            send_message=send_message,
            grant_access=lambda user_id, user_name: (granted.append((user_id, user_name)) or True),
            schedule_task=schedule_task,
        )
    )

    assert "已同意" in reply
    assert len(scheduled) == 1
    asyncio.run(scheduled.pop())
    assert granted == [("new-user", "new user")]
    assert any(chat_id == "new-chat" and "现在可以直接查询" in content for chat_id, content in sent)


def test_approval_command_accepts_bare_request_id(monkeypatch):
    module = _FakeUserModule()
    module.pending = [{
        "request_id": "WA-ABCDEF12",
        "user_id": "new-user",
        "chat_id": "new-chat",
        "user_name": "new user",
    }]
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)
    monkeypatch.setattr(gate, "_run_login_script", AsyncMock(return_value=False))

    async def send_message(_chat_id, _content):
        return True

    reply = asyncio.run(
        gate.handle_work_access_request(
            SimpleNamespace(
                platform=Platform.WECOM,
                user_id="manager-user",
                chat_id="manager-chat",
                chat_type="dm",
            ),
            "/work-approve WA-ABCDEF12",
            None,
            authorized=True,
            manager_user_id="manager-user",
            manager_chat_id="manager-chat",
            send_message=send_message,
            grant_access=lambda *_args: True,
        )
    )

    assert "已处理" in reply
    assert module.pending == []


def test_gateway_routes_manager_approval_to_tracked_background_task(monkeypatch):
    from gateway.run import GatewayRunner

    module = _FakeUserModule()
    module.pending = [{
        "request_id": "WA-ABCDEF12",
        "user_id": "new-user",
        "chat_id": "new-chat",
        "user_name": "new user",
    }]
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)
    monkeypatch.setattr(gate, "_run_login_script", AsyncMock(return_value=True))
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_args, **_kwargs: [])

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.WECOM: PlatformConfig(
                enabled=True,
                home_channel=HomeChannel(
                    platform=Platform.WECOM,
                    chat_id="manager-chat",
                    user_id="manager-user",
                    name="manager",
                ),
            )
        }
    )
    adapter = SimpleNamespace(send=AsyncMock(return_value=True))
    runner.adapters = {Platform.WECOM: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._handle_message_with_agent = AsyncMock(return_value="agent reply")

    event = MessageEvent(
        text="同意",
        source=SessionSource(
            platform=Platform.WECOM,
            user_id="manager-user",
            chat_id="manager-chat",
            chat_type="dm",
        ),
    )

    async def exercise():
        reply = await runner._handle_message(event)
        assert len(runner._background_tasks) == 1
        await asyncio.gather(*runner._background_tasks)
        return reply

    reply = asyncio.run(exercise())

    assert "已同意" in reply
    runner.pairing_store.approve_user.assert_called_once_with(
        "wecom", "new-user", "new user"
    )
    assert any(
        call.args[0] == "manager-chat"
        and call.kwargs.get("metadata") == {"wecom_force_proactive": True}
        for call in adapter.send.await_args_list
    )
    runner._handle_message_with_agent.assert_not_awaited()


def test_gateway_redacts_initialization_payload_from_pre_dispatch_hook(monkeypatch):
    from gateway.run import GatewayRunner

    observed = {}

    def capture_hook(name, **kwargs):
        if name == "pre_gateway_dispatch":
            observed["event"] = kwargs["event"]
        return []

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.WECOM: PlatformConfig(
                enabled=True,
                home_channel=HomeChannel(
                    platform=Platform.WECOM,
                    chat_id="manager-user",
                    user_id="manager-user",
                    name="manager",
                ),
            )
        }
    )
    runner.adapters = {Platform.WECOM: SimpleNamespace(send=AsyncMock(return_value=True))}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    runner._is_user_authorized = MagicMock(return_value=False)
    runner._handle_message_with_agent = AsyncMock(return_value="agent reply")

    monkeypatch.setattr(gate, "is_work_user_gate_enabled", lambda _source: True)
    monkeypatch.setattr(gate, "_profile_user_module", lambda: _FakeUserModule())
    monkeypatch.setattr(gate, "_run_probe_login", AsyncMock(return_value=True))
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", capture_hook)

    event = MessageEvent(
        text='/work-init {"admin_user":"a","admin_pass":"secret","sso_user":"s","sso_pass":"secret"}',
        raw_message={"credential": "secret"},
        reply_to_text="secret",
        source=SessionSource(
            platform=Platform.WECOM,
            user_id="new-user",
            chat_id="new-chat",
            chat_type="dm",
        ),
    )

    reply = asyncio.run(runner._handle_message(event))

    assert "申请已提交" in reply
    observed_event = observed["event"]
    assert observed_event.text == "[work account initialization payload redacted]"
    assert observed_event.raw_message is None
    assert observed_event.reply_to_text is None
    assert "secret" not in str(observed_event)


def test_work_init_rejects_wrong_credentials_before_storing(monkeypatch):
    """Submit bad credentials -> probe fails -> no pending request is created."""
    module = _FakeUserModule()
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)
    probe_calls = []
    probe_success = {"admin": False, "pop_admin": False}

    async def fake_probe(**kwargs):
        probe_calls.append(kwargs["system_id"])
        return probe_success[kwargs["system_id"]]

    monkeypatch.setattr(gate, "_run_probe_login", fake_probe)

    async def send_message(_chat_id, _content):
        return True

    reply = asyncio.run(
        gate.handle_work_access_request(
            _source(),
            '/work-init {"admin_user":"a","admin_pass":"bad","sso_user":"s","sso_pass":"bad"}',
            None,
            authorized=False,
            manager_user_id="manager-user",
            manager_chat_id="manager-user",
            send_message=send_message,
            grant_access=lambda *_args: True,
        )
    )

    assert "未通过" in reply
    assert module.pending == []  # nothing persisted on failed probe
    assert set(probe_calls) == {"admin", "pop_admin"}


def test_work_init_probes_admin_and_sso_before_approval(monkeypatch):
    """Valid credentials -> probe passes both systems -> pending created."""
    module = _FakeUserModule()
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)
    monkeypatch.setattr(gate, "_run_probe_login", AsyncMock(return_value=True))
    sent = []

    async def send_message(chat_id, content):
        sent.append((chat_id, content))
        return True

    reply = asyncio.run(
        gate.handle_work_access_request(
            _source(),
            '/work-init {"admin_user":"a","admin_pass":"ok","sso_user":"s","sso_pass":"ok"}',
            None,
            authorized=False,
            manager_user_id="manager-user",
            manager_chat_id="manager-user",
            send_message=send_message,
            grant_access=lambda *_args: True,
        )
    )

    assert "申请已提交" in reply
    assert module.pending and module.pending[0]["request_id"] == "WA-ABCDEF12"
    assert sent[0][0] == "manager-user"


def test_initialize_work_user_rejects_bad_credentials(monkeypatch):
    """The direct-initialize path also refuses to write config on failed probe."""
    module = _FakeUserModule()
    monkeypatch.setattr(gate, "_profile_user_module", lambda: module)
    monkeypatch.setattr(gate, "_run_probe_login", AsyncMock(return_value=False))

    reply = asyncio.run(
        gate.initialize_work_user(
            _source(),
            '/work-init {"admin_user":"a","admin_pass":"bad","sso_user":"s","sso_pass":"bad"}',
        )
    )

    assert "未通过" in reply
    assert module.saved is None  # config not written on failed probe
