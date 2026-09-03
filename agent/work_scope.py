"""Profile-local Work user scope helpers.

Work keeps one Hermes profile but isolates WeCom users' mutable skills and
authentication state below ``users/<stable-key>/``.  This module is deliberately
small and import-light so skill discovery can use it without loading the tool
registry or the profile's dynamic Python modules.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "").strip()
    except Exception:
        return str(os.getenv(name, "") or "").strip()


def current_work_identity() -> tuple[str, str, str]:
    """Return ``(platform, user_id, chat_id)`` from the current task scope."""
    return (
        _session_env("HERMES_SESSION_PLATFORM").lower(),
        _session_env("HERMES_SESSION_USER_ID"),
        _session_env("HERMES_SESSION_CHAT_ID"),
    )


def is_work_profile(home: Path | None = None) -> bool:
    """Whether *home* has the profile-local Work user configuration module."""
    root = Path(home) if home is not None else get_hermes_home()
    return (root / "shared" / "user_config.py").is_file()


def work_user_key(*, user_id: str = "", chat_id: str = "") -> str:
    """Return the same opaque key used by ``shared.user_config``."""
    principal = str(user_id or chat_id or "").strip()
    if not principal:
        return ""
    return hashlib.sha256(f"wecom\0{principal}".encode("utf-8")).hexdigest()[:24]


def current_user_skills_dir(home: Path | None = None) -> Path | None:
    """Return the current WeCom user's mutable skills directory, if scoped."""
    root = Path(home) if home is not None else get_hermes_home()
    _platform, user_id, chat_id = current_work_identity()
    # A shell command can locally assign HERMES_SESSION_PLATFORM=, so the
    # child process may see an empty platform even though the gateway-provided
    # user/chat identity is still present. The Work profile marker plus an
    # opaque user identity is sufficient to preserve the user scope here.
    if not is_work_profile(root):
        return None
    key = work_user_key(user_id=user_id, chat_id=chat_id)
    if not key:
        return None
    return root / "users" / key / "skills"


def session_skill_dirs(public_dir: Path | None = None) -> list[Path]:
    """Return mutable user skills followed by the profile public skills."""
    public = Path(public_dir) if public_dir is not None else get_hermes_home() / "skills"
    user = current_user_skills_dir(public.parent)
    result: list[Path] = []
    if user is not None:
        result.append(user)
    result.append(public)
    return result


def writable_skills_dir(public_dir: Path | None = None) -> Path:
    """Return the default write root for skill_manage."""
    roots = session_skill_dirs(public_dir)
    return roots[0]


def is_current_user_skill_path(path: Any, public_dir: Path | None = None) -> bool:
    """Whether *path* is contained by the current user's mutable skill root."""
    user = current_user_skills_dir(
        Path(public_dir).parent if public_dir is not None else None
    )
    if user is None:
        return False
    try:
        Path(path).resolve().relative_to(user.resolve())
        return True
    except (OSError, ValueError):
        return False


__all__ = [
    "current_user_skills_dir",
    "current_work_identity",
    "is_current_user_skill_path",
    "is_work_profile",
    "session_skill_dirs",
    "work_user_key",
    "writable_skills_dir",
]
