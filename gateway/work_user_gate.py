"""Profile-local WeCom user-account gate for the YBM work assistant."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_INIT_COMMANDS = {"/work-init", "/account-init", "/ybm100-init"}
_INIT_TEXT = "账号初始化"
_MAX_INIT_MESSAGE_CHARS = 16 * 1024
_LOGIN_TIMEOUT_SECONDS = 180.0
_LOGIN_SCRIPTS = (
    ("admin", "login.py"),
    ("pop_admin", "login_pop.py"),
)
# Probe uses the same scripts but in --probe mode (headless, no state saved).
_PROBE_SCRIPTS = (
    ("admin", "login.py"),
    ("pop_admin", "login_pop.py"),
)
_APPROVE_TEXTS = {"同意", "允许", "批准"}
_REJECT_TEXTS = {"拒绝", "不同意", "驳回"}
_APPROVE_COMMANDS = {"/work-approve", "/work-allow"}
_REJECT_COMMANDS = {"/work-reject", "/work-deny"}
_REQUEST_ID_RE = re.compile(
    r"(?:申请编号|request(?:\s*id)?)\s*[：:]?\s*(WA-[0-9A-F]{8})",
    re.IGNORECASE,
)
_BARE_REQUEST_ID_RE = re.compile(r"\bWA-[0-9A-F]{8}\b", re.IGNORECASE)
_MODULE_CACHE: dict[str, Any] = {}


@dataclass(frozen=True)
class WorkUserIdentity:
    user_id: str = ""
    chat_id: str = ""
    chat_type: str = ""


def _profile_user_module():
    """Load the active profile's shared user config without a source import."""
    home = get_hermes_home().resolve()
    module_path = home / "shared" / "user_config.py"
    if not module_path.is_file():
        return None
    cache_key = str(module_path)
    cached = _MODULE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    module_name = f"_hermes_profile_user_config_{abs(hash(cache_key))}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    _MODULE_CACHE[cache_key] = module
    return module


def _identity(source: Any) -> WorkUserIdentity:
    return WorkUserIdentity(
        user_id=str(getattr(source, "user_id", None) or "").strip(),
        chat_id=str(getattr(source, "chat_id", None) or "").strip(),
        chat_type=str(getattr(source, "chat_type", None) or "").strip().lower(),
    )


def is_work_user_gate_enabled(source: Any) -> bool:
    platform = getattr(getattr(source, "platform", None), "value", None)
    return platform == "wecom" and _profile_user_module() is not None


def is_initialization_request(text: str | None) -> bool:
    raw = str(text or "").strip()
    if raw == _INIT_TEXT or raw.startswith(_INIT_TEXT + " "):
        return True
    command = raw.split(maxsplit=1)[0].lower() if raw else ""
    return command in _INIT_COMMANDS


def _initialization_payload(text: str | None) -> tuple[dict[str, str] | None, str | None]:
    raw = str(text or "").strip()
    if len(raw) > _MAX_INIT_MESSAGE_CHARS:
        return None, "账号初始化内容过长，请拆分处理。"

    if raw.startswith(_INIT_TEXT):
        payload_text = raw[len(_INIT_TEXT):].strip()
    else:
        _, _, payload_text = raw.partition(" ")
        payload_text = payload_text.strip()

    if not payload_text:
        return {}, None
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None, (
            "账号初始化格式错误。请在企微私聊中发送：\n"
            "/work-init {\"admin_user\":\"账号\",\"admin_pass\":\"密码\","
            "\"sso_user\":\"账号\",\"sso_pass\":\"密码\"}"
        )
    if not isinstance(payload, dict):
        return None, "账号初始化内容必须是 JSON 对象。"
    values = {str(key): str(value or "") for key, value in payload.items()}
    required = ("admin_user", "admin_pass", "sso_user", "sso_pass")
    missing = [key for key in required if not values.get(key)]
    if missing:
        return None, "账号初始化缺少必要字段，请同时提供 admin 和 SSO 账号密码。"
    return {key: values[key] for key in required}, None


def _initialization_usage() -> str:
    return (
        "账号初始化仅支持企微私聊。请发送：\n"
        "/work-init {\"admin_user\":\"替换为自己后台账号\","
        "\"admin_pass\":\"替换为自己后台密码\","
        "\"sso_user\":\"替换为自己OA账号\","
        "\"sso_pass\":\"替换为自己OA密码\"}\n\n"
        "提交后会校验账号密码，校验通过才通知管理员审批；请勿在群聊中发送。"
    )


def _module_identity(module: Any, identity: WorkUserIdentity) -> Any:
    return module.WeComIdentity(
        user_id=identity.user_id,
        chat_id=identity.chat_id,
    )


async def _stop_login_process(process: Any) -> None:
    if process is None or getattr(process, "returncode", None) is not None:
        return
    try:
        from gateway.status import terminate_pid

        await asyncio.to_thread(terminate_pid, process.pid, force=True)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except (ProcessLookupError, TimeoutError):
        pass


async def _run_login_script(
    *,
    module: Any,
    profile_home: Path,
    identity: WorkUserIdentity,
    system_id: str,
    script_name: str,
) -> bool:
    """Run one fixed login script and require a fresh user auth-state file."""
    skill_dir = profile_home / "skills" / "ybm100-admin-cookie"
    python_path = skill_dir / ".venv" / "Scripts" / "python.exe"
    script_path = skill_dir / "scripts" / script_name
    if not python_path.is_file() or not script_path.is_file():
        logger.error("work user %s login runtime is unavailable", system_id)
        return False

    from hermes_cli._subprocess_compat import windows_hide_flags
    from tools.environments.local import build_subprocess_env

    # This gateway hook runs before normal session binding. Apply the current
    # source identity after sanitization so a concurrent chat cannot leak its
    # process-global session variables into this user's login subprocess.
    env = build_subprocess_env()
    env.update(
        {
            "HERMES_HOME": str(profile_home),
            "HERMES_SESSION_PLATFORM": "wecom",
            "HERMES_SESSION_USER_ID": identity.user_id,
            "HERMES_SESSION_CHAT_ID": identity.chat_id,
            "HERMES_SESSION_CHAT_TYPE": identity.chat_type,
            "PYTHONIOENCODING": "utf-8",
        }
    )
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(python_path),
            str(script_path),
            cwd=str(script_path.parent),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=windows_hide_flags(),
        )
        return_code = await asyncio.wait_for(
            process.wait(), timeout=_LOGIN_TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.warning("work user %s login timed out", system_id)
        await _stop_login_process(process)
        return False
    except asyncio.CancelledError:
        await _stop_login_process(process)
        raise
    except OSError:
        logger.exception("work user %s login process could not start", system_id)
        return False

    if return_code != 0:
        logger.warning("work user %s login exited with code %s", system_id, return_code)
        return False

    try:
        auth_state = module.auth_state_path_for_user(
            system_id,
            identity=_module_identity(module, identity),
        )
        return auth_state.is_file() and auth_state.stat().st_size > 0
    except Exception:
        logger.exception("work user %s login state could not be verified", system_id)
        return False


async def _run_probe_login(
    *,
    profile_home: Path,
    identity: WorkUserIdentity,
    system_id: str,
    script_name: str,
    probe_username: str,
    probe_password: str,
) -> bool:
    """Run one login script in probe mode against submitted credentials.

    Unlike :func:`_run_login_script`, this never writes a user auth-state file
    and ignores any pre-existing one. It injects the prospective credentials as
    YBM_PROBE_* variables and requires a zero exit code, so a wrong account is
    rejected before any persistent configuration is created.
    """
    skill_dir = profile_home / "skills" / "ybm100-admin-cookie"
    python_path = skill_dir / ".venv" / "Scripts" / "python.exe"
    script_path = skill_dir / "scripts" / script_name
    if not python_path.is_file() or not script_path.is_file():
        logger.error("work user %s probe login runtime is unavailable", system_id)
        return False

    from hermes_cli._subprocess_compat import windows_hide_flags
    from tools.environments.local import build_subprocess_env

    env = build_subprocess_env()
    user_var = "YBM_PROBE_ADMIN_USER" if system_id == "admin" else "YBM_PROBE_POP_USER"
    pass_var = "YBM_PROBE_ADMIN_PASS" if system_id == "admin" else "YBM_PROBE_POP_PASS"
    env.update(
        {
            "HERMES_HOME": str(profile_home),
            "HERMES_SESSION_PLATFORM": "wecom",
            "HERMES_SESSION_USER_ID": identity.user_id,
            "HERMES_SESSION_CHAT_ID": identity.chat_id,
            "HERMES_SESSION_CHAT_TYPE": identity.chat_type,
            user_var: probe_username,
            pass_var: probe_password,
            "PYTHONIOENCODING": "utf-8",
        }
    )
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(python_path),
            str(script_path),
            "--probe",
            cwd=str(script_path.parent),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=windows_hide_flags(),
        )
        return_code = await asyncio.wait_for(
            process.wait(), timeout=_LOGIN_TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.warning("work user %s probe login timed out", system_id)
        await _stop_login_process(process)
        return False
    except asyncio.CancelledError:
        await _stop_login_process(process)
        raise
    except OSError:
        logger.exception("work user %s probe login process could not start", system_id)
        return False

    if return_code != 0:
        logger.warning("work user %s probe login exited with code %s", system_id, return_code)
        return False
    return True


async def initialize_work_user(source: Any, text: str | None) -> Optional[str]:
    """Save a private account configuration and create its login states."""
    if not is_work_user_gate_enabled(source):
        return None

    module = _profile_user_module()
    identity = _identity(source)
    if identity.chat_type not in {"dm", "private", "direct"}:
        return "账号初始化必须在企微私聊中进行，群聊不接受账号信息。"

    payload, error = _initialization_payload(text)
    if error:
        return error
    if not payload:
        return _initialization_usage()

    # 双入口都先探测账号正确性，通过后才允许存档，避免错账号污染用户配置。
    profile_home = get_hermes_home().resolve()
    probe_results: dict[str, bool] = {}
    for system_id, script_name in _PROBE_SCRIPTS:
        probe_username = payload[
            "admin_user" if system_id == "admin" else "sso_user"
        ]
        probe_password = payload[
            "admin_pass" if system_id == "admin" else "sso_pass"
        ]
        probe_results[system_id] = await _run_probe_login(
            profile_home=profile_home,
            identity=identity,
            system_id=system_id,
            script_name=script_name,
            probe_username=probe_username,
            probe_password=probe_password,
        )

    if not all(probe_results.values()):
        failed = "、".join(
            "admin" if system_id == "admin" else "SSO"
            for system_id, succeeded in probe_results.items()
            if not succeeded
        )
        return (
            f"账号初始化未通过 {failed} 验证：未能在对应系统拿到有效登录态。"
            "若确认账号密码正确，可能是验证码识别未通过，请重新发送一次；"
            "若多次仍失败，请核对后台/OA 账号密码。"
        )

    try:
        module.write_user_config(
            _module_identity(module, identity),
            admin_username=payload["admin_user"],
            admin_password=payload["admin_pass"],
            sso_username=payload["sso_user"],
            sso_password=payload["sso_pass"],
        )
    except Exception:
        logger.exception("work user configuration initialization failed")
        return "账号初始化失败，请检查账号信息后重试。"

    results: dict[str, bool] = {}
    for system_id, script_name in _LOGIN_SCRIPTS:
        results[system_id] = await _run_login_script(
            module=module,
            profile_home=profile_home,
            identity=identity,
            system_id=system_id,
            script_name=script_name,
        )

    if all(results.values()):
        return "账号配置和自动登录已完成。admin、SSO 登录态已生成，现在可以直接查询。"

    failed = "、".join(
        "admin" if system_id == "admin" else "SSO"
        for system_id, succeeded in results.items()
        if not succeeded
    )
    return (
        f"账号配置已保存，但 {failed} 自动登录未完成。"
        "请稍后重新发送账号初始化重试。"
    )


def _approval_action(text: str | None) -> tuple[str | None, str]:
    raw = str(text or "").strip()
    if not raw:
        return None, ""
    normalized = raw.lower()
    if normalized in _APPROVE_TEXTS:
        return "approve", ""
    if normalized in _REJECT_TEXTS:
        return "reject", ""
    parts = normalized.split(maxsplit=1)
    command = parts[0]
    if command in _APPROVE_COMMANDS:
        return "approve", parts[1].strip() if len(parts) > 1 else ""
    if command in _REJECT_COMMANDS:
        return "reject", parts[1].strip() if len(parts) > 1 else ""
    return None, ""


def is_work_access_control_request(text: str | None) -> bool:
    return _approval_action(text)[0] is not None


def _request_id_from_text(*texts: str | None) -> str:
    for text in texts:
        raw = str(text or "")
        match = _REQUEST_ID_RE.search(raw)
        if match:
            return match.group(1).upper()
        bare_match = _BARE_REQUEST_ID_RE.search(raw)
        if bare_match:
            return bare_match.group(0).upper()
    return ""


def _work_access_request_notice(metadata: dict[str, Any]) -> str:
    request_id = str(metadata.get("request_id") or "").strip()
    applicant = str(metadata.get("sso_user") or "").strip()
    if not applicant:
        applicant = str(metadata.get("user_name") or "").strip()
    applicant = applicant or "企微用户"
    return (
        "【线上问题知识库账号接入申请】\n"
        f"申请人：{applicant}\n"
        f"申请编号：{request_id}\n\n"
        "请回复“同意”或“拒绝”。有多条申请时，请引用本消息回复。"
    )


def _pending_request_for_identity(
    module: Any, identity: WorkUserIdentity
) -> dict[str, Any] | None:
    for pending in module.list_pending_user_requests():
        if (
            identity.user_id
            and identity.user_id == str(pending.get("user_id") or "").strip()
        ) or (
            identity.chat_id
            and identity.chat_id == str(pending.get("chat_id") or "").strip()
        ):
            return pending
    return None


async def _send_work_access_message(send_message: Any, chat_id: str, text: str) -> bool:
    if not chat_id:
        return False
    try:
        result = await send_message(chat_id, text)
    except Exception:
        logger.exception("work access notification delivery failed")
        return False
    return result is not False and getattr(result, "success", True) is not False


async def _complete_approved_work_access(
    *,
    module: Any,
    metadata: dict[str, Any],
    profile_home: Path,
    manager_chat_id: str,
    send_message: Any,
    grant_access: Any,
) -> None:
    identity = WorkUserIdentity(
        user_id=str(metadata.get("user_id") or "").strip(),
        chat_id=str(metadata.get("chat_id") or "").strip(),
        chat_type="dm",
    )
    results: dict[str, bool] = {}
    for system_id, script_name in _LOGIN_SCRIPTS:
        results[system_id] = await _run_login_script(
            module=module,
            profile_home=profile_home,
            identity=identity,
            system_id=system_id,
            script_name=script_name,
        )

    granted = False
    if all(results.values()):
        try:
            granted = bool(
                grant_access(
                    identity.user_id or identity.chat_id,
                    str(metadata.get("user_name") or "").strip(),
                )
            )
        except Exception:
            logger.exception("work access grant failed")

    if granted:
        applicant_text = (
            "管理员已同意你的账号接入申请，admin 和 SSO 登录态已生成，"
            "现在可以直接查询。"
        )
        manager_text = (
            f"账号接入申请 {metadata.get('request_id', '')} 已完成，"
            "申请人现在可以使用线上问题知识库。"
        )
    else:
        applicant_text = (
            "管理员已同意你的账号接入申请，但自动登录未完成。"
            "请重新发送账号初始化，系统会重新提交申请。"
        )
        manager_text = (
            f"账号接入申请 {metadata.get('request_id', '')} 已同意，"
            "但自动登录未完成，暂未开放查询权限。"
        )

    applicant_chat_id = str(metadata.get("chat_id") or metadata.get("user_id") or "").strip()
    await _send_work_access_message(send_message, applicant_chat_id, applicant_text)
    await _send_work_access_message(send_message, manager_chat_id, manager_text)


async def handle_work_access_request(
    source: Any,
    text: str | None,
    reply_to_text: str | None,
    *,
    authorized: bool,
    manager_user_id: str,
    manager_chat_id: str,
    send_message: Any,
    grant_access: Any,
    schedule_task: Any = None,
) -> Optional[str]:
    """Handle work-profile access requests before the generic pairing gate."""
    if not is_work_user_gate_enabled(source):
        return None

    module = _profile_user_module()
    identity = _identity(source)
    manager_user_id = str(manager_user_id or "").strip()
    manager_chat_id = str(manager_chat_id or manager_user_id).strip()
    is_manager = bool(manager_user_id and identity.user_id == manager_user_id)
    action, action_arg = _approval_action(text)

    if action:
        if not is_manager:
            if authorized:
                return None
            if identity.chat_type in {"dm", "private", "direct"}:
                return "当前企微用户尚未授权，请先在私聊中发送‘账号初始化’。"
            return None
        if identity.chat_type not in {"dm", "private", "direct"}:
            return "管理员审批只支持企微私聊。"

        pending = module.list_pending_user_requests()
        request_id = _request_id_from_text(action_arg, reply_to_text)
        if not request_id:
            if len(pending) == 1:
                request_id = str(pending[0].get("request_id") or "").strip().upper()
            elif not pending:
                return "当前没有待审批的账号接入申请。"
            else:
                return "当前有多个待审批申请，请引用申请通知回复“同意”或“拒绝”。"

        try:
            if action == "reject":
                metadata = module.reject_pending_user_request(request_id)
                if metadata is None:
                    return "该账号接入申请不存在或已过期。"
                applicant_chat_id = str(
                    metadata.get("chat_id") or metadata.get("user_id") or ""
                ).strip()
                await _send_work_access_message(
                    send_message,
                    applicant_chat_id,
                    "管理员已拒绝你的账号接入申请。",
                )
                return f"已拒绝账号接入申请 {request_id}。"

            metadata = module.approve_pending_user_request(request_id)
        except Exception:
            logger.exception("work access approval failed")
            return "账号接入审批处理失败，请稍后重试。"
        if metadata is None:
            return "该账号接入申请不存在或已过期。"

        completion = _complete_approved_work_access(
            module=module,
            metadata=metadata,
            profile_home=get_hermes_home().resolve(),
            manager_chat_id=manager_chat_id,
            send_message=send_message,
            grant_access=grant_access,
        )
        if callable(schedule_task):
            schedule_task(completion)
            return f"已同意账号接入申请 {request_id}，正在自动生成登录态，完成后会通知申请人。"
        await completion
        return f"账号接入申请 {request_id} 已处理。"

    if authorized or is_manager:
        return None

    if is_initialization_request(text):
        if identity.chat_type not in {"dm", "private", "direct"}:
            return "账号初始化必须在企微私聊中进行，群聊不接受账号信息。"
        if not manager_user_id or not manager_chat_id:
            return "账号审批管理员尚未配置，请联系系统管理员。"
        payload, error = _initialization_payload(text)
        if error:
            return error
        if not payload:
            return _initialization_usage()

        # 先探测账号正确性，通过后才允许存档。错误的账号不会留下任何持久化信息。
        profile_home = get_hermes_home().resolve()
        probe_results: dict[str, bool] = {}
        for system_id, script_name in _PROBE_SCRIPTS:
            probe_username = payload[
                "admin_user" if system_id == "admin" else "sso_user"
            ]
            probe_password = payload[
                "admin_pass" if system_id == "admin" else "sso_pass"
            ]
            probe_results[system_id] = await _run_probe_login(
                profile_home=profile_home,
                identity=identity,
                system_id=system_id,
                script_name=script_name,
                probe_username=probe_username,
                probe_password=probe_password,
            )

        if not all(probe_results.values()):
            failed = "、".join(
                "admin" if system_id == "admin" else "SSO"
                for system_id, succeeded in probe_results.items()
                if not succeeded
            )
            # Probe only passes when the real login state (admin cookie /
            # SSO TGC+session) is obtained. A failure here means either the
            # credential is wrong OR (headless) the captcha was misread — so
            # invite a retry rather than asserting the account is wrong.
            return (
                f"账号初始化未通过 {failed} 验证：未能在对应系统拿到有效登录态。"
                "若确认账号密码正确，可能是验证码识别未通过，请重新发送一次；"
                "若多次仍失败，请核对后台/OA 账号密码。"
            )

        try:
            metadata = module.create_pending_user_request(
                _module_identity(module, identity),
                user_name=str(getattr(source, "user_name", None) or "").strip(),
                admin_username=payload["admin_user"],
                admin_password=payload["admin_pass"],
                sso_username=payload["sso_user"],
                sso_password=payload["sso_pass"],
            )
        except Exception:
            logger.exception("work access request initialization failed")
            return "账号接入申请失败，请检查账号信息后重试。"

        notice_sent = await _send_work_access_message(
            send_message,
            manager_chat_id,
            _work_access_request_notice(metadata),
        )
        if not notice_sent:
            return "账号信息已安全保存，但管理员通知发送失败，请稍后重新发送账号初始化。"
        return "账号接入申请已提交，请等待管理员同意；审批完成后会自动通知你。"

    if identity.chat_type in {"dm", "private", "direct"}:
        if _pending_request_for_identity(module, identity) is not None:
            return "账号接入申请正在等待管理员审批，审批完成后会自动通知你。"
        return "当前企微用户尚未授权，请先在私聊中发送‘账号初始化’。"
    return None


def check_work_user(source: Any, text: str | None) -> Optional[str]:
    """Return a direct response when a non-initialization request is blocked.

    ``None`` means the message may continue through the normal gateway/agent
    pipeline. A string is a user-facing response and stops the agent turn.
    """
    if not is_work_user_gate_enabled(source):
        return None

    module = _profile_user_module()
    identity = _identity(source)
    if is_initialization_request(text):
        return "账号初始化正在处理，请稍候。"

    try:
        config_path, _ = module.find_user_config(
            _module_identity(module, identity)
        )
        module.credentials_for_user(
            "admin",
            identity=_module_identity(module, identity),
        )
        module.credentials_for_user(
            "scm",
            identity=_module_identity(module, identity),
        )
        return None
    except module.UserConfigAmbiguousError:
        return "当前企微身份匹配到多个用户配置，请联系管理员清理重复绑定。"
    except module.UserConfigError:
        logger.info(
            "Work WeCom user is not initialized (user_id_present=%s, chat_id_present=%s)",
            bool(identity.user_id),
            bool(identity.chat_id),
        )
        return "当前企微用户尚未初始化账号，请在私聊中发送‘账号初始化’。"
