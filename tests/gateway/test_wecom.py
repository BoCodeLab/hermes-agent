"""Tests for the WeCom platform adapter."""

import asyncio
import base64
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult, _thread_metadata_for_source


class TestWeComRequirements:
    def test_returns_false_without_aiohttp(self, monkeypatch):
        monkeypatch.setattr("plugins.platforms.wecom.adapter.AIOHTTP_AVAILABLE", False)
        monkeypatch.setattr("plugins.platforms.wecom.adapter.HTTPX_AVAILABLE", True)
        from plugins.platforms.wecom.adapter import check_wecom_requirements

        assert check_wecom_requirements() is False


class TestWeComAdapterInit:
    def test_declares_non_editable_message_capability(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        assert WeComAdapter.SUPPORTS_MESSAGE_EDITING is False

    def test_thread_metadata_keeps_wecom_reply_anchor(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        source = adapter.build_source(chat_id="chat-123", chat_type="dm")

        assert _thread_metadata_for_source(source, "msg-1") == {
            "wecom_reply_to_message_id": "msg-1"
        }


class TestWeComConnect:

    @pytest.mark.asyncio
    async def test_connect_records_handshake_failure_details(self, monkeypatch):
        import plugins.platforms.wecom.adapter as wecom_module
        from plugins.platforms.wecom.adapter import WeComAdapter

        class DummyClient:
            async def aclose(self):
                return None

        monkeypatch.setattr(wecom_module, "AIOHTTP_AVAILABLE", True)
        monkeypatch.setattr(wecom_module, "HTTPX_AVAILABLE", True)
        monkeypatch.setattr(
            wecom_module,
            "httpx",
            SimpleNamespace(AsyncClient=lambda **kwargs: DummyClient()),
        )

        adapter = WeComAdapter(
            PlatformConfig(enabled=True, extra={"bot_id": "bot-1", "secret": "secret-1"})
        )
        adapter._open_connection = AsyncMock(side_effect=RuntimeError("invalid secret (errcode=40013)"))

        success = await adapter.connect()

        assert success is False
        assert adapter.has_fatal_error is True
        assert adapter.fatal_error_code == "wecom_connect_error"
        assert "invalid secret" in (adapter.fatal_error_message or "")


class TestWeComQrScan:
    @patch("plugins.platforms.wecom.adapter.time")
    @patch("plugins.platforms.wecom.adapter.json.loads")
    @patch("plugins.platforms.wecom.adapter.logger")
    @patch("urllib.request.urlopen")
    @patch("urllib.request.Request")
    def test_qr_scan_timeout_uses_monotonic_clock(
        self,
        mock_request,
        mock_urlopen,
        _mock_logger,
        mock_json_loads,
        mock_time,
    ):
        from plugins.platforms.wecom.adapter import qr_scan_for_bot_info

        generate_resp = MagicMock()
        generate_resp.read.return_value = b'{"data":{"scode":"abc","auth_url":"https://example.com/qr"}}'
        generate_resp.__enter__.return_value = generate_resp
        generate_resp.__exit__.return_value = False

        poll_resp = MagicMock()
        poll_resp.read.return_value = b'{"data":{"status":"pending"}}'
        poll_resp.__enter__.return_value = poll_resp
        poll_resp.__exit__.return_value = False

        mock_urlopen.side_effect = [generate_resp, poll_resp]
        mock_json_loads.side_effect = [
            {"data": {"scode": "abc", "auth_url": "https://example.com/qr"}},
            {"data": {"status": "pending"}},
        ]
        mock_time.monotonic.side_effect = [1000, 1000.2, 1001.1]
        mock_time.time.side_effect = [1000, 900, 901, 902]
        mock_time.sleep = MagicMock()

        with patch("builtins.print"), patch.dict("sys.modules", {"qrcode": None}):
            result = qr_scan_for_bot_info(timeout_seconds=1)

        assert result is None
        assert mock_urlopen.call_count == 2


class TestWeComReplyMode:

    @pytest.mark.asyncio
    async def test_send_image_file_uses_passive_reply_media_when_reply_context_exists(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._reply_req_ids["msg-1"] = "req-1"
        adapter._prepare_outbound_media = AsyncMock(
            return_value={
                "data": b"image-bytes",
                "content_type": "image/png",
                "file_name": "demo.png",
                "detected_type": "image",
                "final_type": "image",
                "rejected": False,
                "reject_reason": None,
                "downgraded": False,
                "downgrade_note": None,
            }
        )
        adapter._upload_media_bytes = AsyncMock(return_value={"media_id": "media-1", "type": "image"})
        adapter._send_reply_request = AsyncMock(
            return_value={"headers": {"req_id": "req-1"}, "errcode": 0}
        )

        result = await adapter.send_image_file("chat-123", "/tmp/demo.png", reply_to="msg-1")

        assert result.success is True
        adapter._send_reply_request.assert_awaited_once()
        args = adapter._send_reply_request.await_args.args
        assert args[0] == "req-1"
        assert args[1] == {"msgtype": "image", "image": {"media_id": "media-1"}}


class TestExtractText:

    def test_extracts_mixed_text(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        body = {
            "msgtype": "mixed",
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "part1"}},
                    {"msgtype": "image", "image": {"url": "https://example.com/x.png"}},
                    {"msgtype": "text", "text": {"content": "part2"}},
                ]
            },
        }
        text, _reply_text = WeComAdapter._extract_text(body)
        assert text == "part1\npart2"

    def test_extracts_quoted_markdown_for_approval_context(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        body = {
            "msgtype": "text",
            "text": {"content": "同意"},
            "quote": {
                "msgtype": "markdown",
                "markdown": {"content": "申请编号：WA-ABCDEF12"},
            },
        }

        text, reply_text = WeComAdapter._extract_text(body)

        assert text == "同意"
        assert reply_text == "申请编号：WA-ABCDEF12"


class TestCallbackDispatch:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("cmd", ["aibot_msg_callback", "aibot_callback"])
    async def test_dispatch_accepts_new_and_legacy_callback_cmds(self, cmd):
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._on_message = AsyncMock()

        await adapter._dispatch_payload({"cmd": cmd, "headers": {"req_id": "req-1"}, "body": {}})

        adapter._on_message.assert_awaited_once()


class TestPolicyHelpers:

    def test_dm_allowlist_honors_env_only_allowed_users(self, monkeypatch):
        """Env-only setup (WECOM_DM_POLICY + WECOM_ALLOWED_USERS, no config
        ``extra``) must populate the DM allowlist. Otherwise ``dm_policy:
        allowlist`` runs with an empty allowlist and drops every listed user
        at intake — the documented env vars become no-ops."""
        from plugins.platforms.wecom.adapter import WeComAdapter

        monkeypatch.setenv("WECOM_DM_POLICY", "allowlist")
        monkeypatch.setenv("WECOM_ALLOWED_USERS", "user-1, user-2")

        adapter = WeComAdapter(PlatformConfig(enabled=True))

        assert adapter._dm_policy == "allowlist"
        assert adapter._allow_from == ["user-1", "user-2"]
        assert adapter._is_dm_allowed("user-1") is True
        assert adapter._is_dm_allowed("user-2") is True
        assert adapter._is_dm_allowed("stranger") is False


    def test_pairing_group_policy_blocks_without_explicit_group_allow_from(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(
            PlatformConfig(enabled=True, extra={"group_policy": "pairing"})
        )

        assert adapter._is_group_allowed("group-1", "user-1") is False


class TestMediaHelpers:
    def test_detect_wecom_media_type(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        assert WeComAdapter._detect_wecom_media_type("image/png") == "image"
        assert WeComAdapter._detect_wecom_media_type("video/mp4") == "video"
        assert WeComAdapter._detect_wecom_media_type("audio/amr") == "voice"
        assert WeComAdapter._detect_wecom_media_type("application/pdf") == "file"

    def test_voice_non_amr_downgrades_to_file(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        result = WeComAdapter._apply_file_size_limits(128, "voice", "audio/mpeg")

        assert result["final_type"] == "file"
        assert result["downgraded"] is True
        assert "AMR" in (result["downgrade_note"] or "")


class TestMediaUpload:


    @pytest.mark.asyncio
    async def test_download_remote_bytes_blocks_connect_time_rebind(self, monkeypatch):
        import httpcore
        from httpcore._backends.auto import AutoBackend
        from plugins.platforms.wecom.adapter import WeComAdapter
        from tools.url_safety import SSRFConnectionBlocked

        for proxy_var in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            monkeypatch.delenv(proxy_var, raising=False)

        answers = iter(("93.184.216.34", "169.254.169.254"))

        def fake_getaddrinfo(_host, port, *_args, **_kwargs):
            ip = next(answers)
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
            ]

        connect_attempts = []

        async def fake_connect_tcp(
            _self,
            host,
            port,
            timeout=None,
            local_address=None,
            socket_options=None,
        ):
            connect_attempts.append((host, port))
            raise httpcore.ConnectError("stop before network")

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        monkeypatch.setattr(AutoBackend, "connect_tcp", fake_connect_tcp)

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        with pytest.raises(SSRFConnectionBlocked):
            await adapter._download_remote_bytes(
                "http://rebind.example/file.bin", max_bytes=1024
            )

        assert connect_attempts == []


class TestSend:

    @pytest.mark.asyncio
    async def test_typing_stream_is_replaced_by_final_reply(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._reply_req_ids["msg-1"] = "req-1"
        adapter._send_reply_request = AsyncMock(
            side_effect=[
                {"headers": {"req_id": "req-1"}, "errcode": 0},
                {"headers": {"req_id": "req-1"}, "errcode": 0},
            ]
        )

        metadata = {"wecom_reply_to_message_id": "msg-1"}
        await adapter.send_typing("chat-123", metadata=metadata)
        await adapter.send_typing("chat-123", metadata=metadata)
        result = await adapter.send("chat-123", "final answer", reply_to="msg-1")

        assert result.success is True
        assert adapter._send_reply_request.await_count == 2
        first_body = adapter._send_reply_request.await_args_list[0].args[1]
        final_body = adapter._send_reply_request.await_args_list[1].args[1]
        assert first_body == {
            "msgtype": "stream",
            "stream": {
                "id": first_body["stream"]["id"],
                "finish": False,
                "content": "",
            },
        }
        assert final_body == {
            "msgtype": "stream",
            "stream": {
                "id": first_body["stream"]["id"],
                "finish": True,
                "content": "final answer",
            },
        }


    @pytest.mark.asyncio
    async def test_send_voice_sends_caption_and_downgrade_note(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._prepare_outbound_media = AsyncMock(
            return_value={
                "data": b"voice-bytes",
                "content_type": "audio/mpeg",
                "file_name": "voice.mp3",
                "detected_type": "voice",
                "final_type": "file",
                "rejected": False,
                "reject_reason": None,
                "downgraded": True,
                "downgrade_note": "语音格式 audio/mpeg 不支持，企微仅支持 AMR 格式，已转为文件格式发送",
            }
        )
        adapter._upload_media_bytes = AsyncMock(return_value={"media_id": "media-1", "type": "file"})
        adapter._send_media_message = AsyncMock(return_value={"headers": {"req_id": "req-media"}, "errcode": 0})
        adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="msg-1"))

        result = await adapter.send_voice("chat-123", "/tmp/voice.mp3", caption="listen")

        assert result.success is True
        adapter._send_media_message.assert_awaited_once_with("chat-123", "file", "media-1")
        assert adapter.send.await_count == 2
        adapter.send.assert_any_await(chat_id="chat-123", content="listen", reply_to=None)
        adapter.send.assert_any_await(
            chat_id="chat-123",
            content="ℹ️ 语音格式 audio/mpeg 不支持，企微仅支持 AMR 格式，已转为文件格式发送",
            reply_to=None,
        )


class TestInboundMessages:
    @pytest.mark.asyncio
    async def test_on_message_builds_event(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(
            PlatformConfig(
                enabled=True,
                extra={"group_policy": "allowlist", "group_allow_from": ["group-1"]},
            )
        )
        adapter._text_batch_delay_seconds = 0  # disable batching for tests
        adapter.handle_message = AsyncMock()
        adapter._extract_media = AsyncMock(return_value=(["/tmp/test.png"], ["image/png"]))

        payload = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-1"},
            "body": {
                "msgid": "msg-1",
                "chatid": "group-1",
                "chattype": "group",
                "from": {"userid": "user-1"},
                "msgtype": "text",
                "text": {"content": "hello"},
            },
        }

        await adapter._on_message(payload)

        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        assert event.text == "hello"
        assert event.source.chat_id == "group-1"
        assert event.source.user_id == "user-1"
        assert event.media_urls == ["/tmp/test.png"]
        assert event.media_types == ["image/png"]


class TestWeComZombieSessionFix:
    """Tests for PR #11572 — device_id, markdown reply, group req_id fallback."""


    @pytest.mark.asyncio
    async def test_on_message_does_not_cache_blocked_sender_req_id(self):
        """Blocked chats shouldn't populate the proactive-send fallback cache."""
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(
            PlatformConfig(
                enabled=True,
                extra={"group_policy": "allowlist", "group_allow_from": ["group-ok"]},
            )
        )
        adapter.handle_message = AsyncMock()
        adapter._extract_media = AsyncMock(return_value=([], []))

        payload = {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-abc"},
            "body": {
                "msgid": "msg-1",
                "chatid": "group-blocked",
                "chattype": "group",
                "from": {"userid": "user-1"},
                "msgtype": "text",
                "text": {"content": "hi"},
            },
        }

        await adapter._on_message(payload)
        adapter.handle_message.assert_not_awaited()
        assert "group-blocked" not in adapter._last_chat_req_ids

    def test_remember_chat_req_id_is_bounded(self):
        from plugins.platforms.wecom.adapter import DEDUP_MAX_SIZE, WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        for i in range(DEDUP_MAX_SIZE + 50):
            adapter._remember_chat_req_id(f"chat-{i}", f"req-{i}")
        assert len(adapter._last_chat_req_ids) <= DEDUP_MAX_SIZE
        # The most recently remembered chat must still be present.
        latest = f"chat-{DEDUP_MAX_SIZE + 49}"
        assert adapter._last_chat_req_ids[latest] == f"req-{DEDUP_MAX_SIZE + 49}"


    @pytest.mark.asyncio
    async def test_proactive_group_send_falls_back_to_cached_req_id(self):
        """Sending into a group without reply_to should use the last cached
        req_id via APP_CMD_RESPONSE — WeCom AI Bots cannot initiate APP_CMD_SEND
        in group chats (errcode 600039)."""
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._last_chat_req_ids["group-1"] = "inbound-req-42"
        adapter._send_reply_request = AsyncMock(
            return_value={"headers": {"req_id": "inbound-req-42"}, "errcode": 0}
        )
        adapter._send_request = AsyncMock(
            return_value={"headers": {"req_id": "new"}, "errcode": 0}
        )

        result = await adapter.send("group-1", "ping", reply_to=None)

        assert result.success is True
        # Must route through reply (APP_CMD_RESPONSE), not proactive send.
        adapter._send_reply_request.assert_awaited_once()
        adapter._send_request.assert_not_awaited()
        args = adapter._send_reply_request.await_args.args
        assert args[0] == "inbound-req-42"
        assert args[1]["msgtype"] == "markdown"
        assert args[1]["markdown"]["content"] == "ping"

    @pytest.mark.asyncio
    async def test_force_proactive_send_ignores_stale_cached_req_id(self):
        from plugins.platforms.wecom.adapter import APP_CMD_SEND, WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._last_chat_req_ids["manager-chat"] = "stale-reply-token"
        adapter._send_reply_request = AsyncMock()
        adapter._send_request = AsyncMock(
            return_value={"headers": {"req_id": "new-request"}, "errcode": 0}
        )

        result = await adapter.send(
            "manager-chat",
            "approval request",
            metadata={"wecom_force_proactive": True},
        )

        assert result.success is True
        adapter._send_reply_request.assert_not_awaited()
        adapter._send_request.assert_awaited_once_with(
            APP_CMD_SEND,
            {
                "chatid": "manager-chat",
                "msgtype": "markdown",
                "markdown": {"content": "approval request"},
            },
        )

    @pytest.mark.asyncio
    async def test_reply_requests_for_one_inbound_req_id_are_serialized(self):
        """Typing and final reply must not overwrite the same response future."""
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._ws = SimpleNamespace(closed=False)
        sent = []
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def fake_send_json(payload):
            sent.append(payload)
            if len(sent) == 1:
                first_started.set()
                await release_first.wait()
            await adapter._dispatch_payload(
                {
                    "cmd": "aibot_respond_ack",
                    "headers": {"req_id": "inbound-req"},
                    "errcode": 0,
                }
            )

        adapter._send_json = fake_send_json
        first = asyncio.create_task(
            adapter._send_reply_request(
                "inbound-req",
                {"msgtype": "stream", "stream": {"finish": False}},
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            adapter._send_reply_request(
                "inbound-req",
                {"msgtype": "markdown", "markdown": {"content": "final"}},
            )
        )
        await asyncio.sleep(0)
        assert len(sent) == 1

        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)
        assert len(sent) == 2

    @pytest.mark.asyncio
    async def test_late_typing_does_not_recreate_finished_reply(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._last_chat_req_ids["chat-1"] = "req-1"
        adapter._send_reply_request = AsyncMock(
            return_value={"headers": {"req_id": "req-1"}, "errcode": 0}
        )

        result = await adapter.send("chat-1", "final")
        assert result.success is True

        await adapter.send_typing("chat-1")

        adapter._send_reply_request.assert_awaited_once()
        assert "req-1" in adapter._finished_reply_req_ids

    @pytest.mark.asyncio
    async def test_typing_frame_is_skipped_when_final_reply_is_in_flight(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._ws = SimpleNamespace(closed=False)
        adapter._finalizing_reply_req_ids.add("req-1")
        adapter._send_json = AsyncMock()

        response = await adapter._send_reply_request(
            "req-1",
            {"msgtype": "stream", "stream": {"finish": False}},
        )

        assert response["_hermes_typing_skipped"] is True
        adapter._send_json.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fast_final_send_prevents_late_typing_bubble(self):
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._last_chat_req_ids["chat-1"] = "req-1"
        final_started = asyncio.Event()
        release_final = asyncio.Event()

        async def fake_reply_request(req_id, body):
            assert req_id == "req-1"
            assert body["msgtype"] == "markdown"
            final_started.set()
            await release_final.wait()
            return {"headers": {"req_id": req_id}, "errcode": 0}

        adapter._send_reply_request = fake_reply_request
        final_task = asyncio.create_task(adapter.send("chat-1", "final"))
        await final_started.wait()

        await adapter.send_typing("chat-1")
        assert adapter._reply_stream_ids == {}

        release_final.set()
        result = await final_task
        assert result.success is True


class TestTextBatchFlushRace:
    """Regression tests for the cancel-delivery race in _flush_text_batch.

    When asyncio.sleep() fires and Task.cancel() is called before the task
    runs, CPython sets _must_cancel but cannot cancel the already-done sleep
    future.  CancelledError is then delivered at the *next* await
    (handle_message), after the task has already popped the event — the
    superseding task sees an empty batch and silently drops the message.
    The fix adds a synchronous task-registry check between the sleep and
    the pop so a superseded task returns before touching the event.
    """

    @pytest.mark.asyncio
    async def test_superseded_task_does_not_pop_or_process_event(self):
        """A flush task that has been superseded must leave the event in the
        batch dict for the new task to handle."""
        from gateway.platforms.base import MessageEvent, MessageType
        from plugins.platforms.wecom.adapter import WeComAdapter

        adapter = WeComAdapter(PlatformConfig(enabled=True))
        adapter._text_batch_delay_seconds = 0

        key = "test-session"
        event = MessageEvent(text="hello", message_type=MessageType.TEXT)
        adapter._pending_text_batches[key] = event

        handle_calls = []

        async def fake_handle(evt):
            handle_calls.append(evt)

        adapter.handle_message = fake_handle

        # Create T1 and register it.
        t1 = asyncio.create_task(adapter._flush_text_batch(key))
        adapter._pending_text_batch_tasks[key] = t1

        # Simulate T2 superseding T1 before T1 wakes from sleep.
        t2 = asyncio.create_task(asyncio.sleep(0.2))
        adapter._pending_text_batch_tasks[key] = t2

        # Yield long enough for T1's sleep(0) to complete and T1 to run.
        await asyncio.sleep(0.05)

        t2.cancel()
        try:
            await t2
        except asyncio.CancelledError:
            pass

        # T1 must have returned without processing or removing the event.
        assert handle_calls == [], "superseded task must not call handle_message"
        assert adapter._pending_text_batches.get(key) is event, (
            "superseded task must not pop the event"
        )
