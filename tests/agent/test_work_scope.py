"""Work profile user-scope and skill-tier regressions."""

import json
from pathlib import Path


def _write_skill(root: Path, name: str, body: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {body}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def _bind_work_session(monkeypatch, home: Path, user_id: str = "wecom-user") -> Path:
    (home / "shared").mkdir(parents=True, exist_ok=True)
    (home / "shared" / "user_config.py").write_text("# marker\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "wecom")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", user_id)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", user_id)
    from agent.work_scope import current_user_skills_dir

    user_root = current_user_skills_dir(home)
    assert user_root is not None
    return user_root


def test_work_user_skill_root_is_opaque_and_stable(monkeypatch, tmp_path):
    user_root = _bind_work_session(monkeypatch, tmp_path / "work")
    from agent.work_scope import work_user_key

    assert user_root == tmp_path / "work" / "users" / work_user_key(user_id="wecom-user") / "skills"
    assert len(work_user_key(user_id="wecom-user")) == 24


def test_different_work_users_get_distinct_skill_roots(monkeypatch, tmp_path):
    home = tmp_path / "work"
    first = _bind_work_session(monkeypatch, home, user_id="wecom-user-a")
    monkeypatch.setenv("HERMES_SESSION_USER_ID", "wecom-user-b")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "wecom-user-b")

    from agent.work_scope import current_user_skills_dir

    second = current_user_skills_dir(home)
    assert second is not None
    assert first != second
    assert first.parent != second.parent


def test_session_skill_dirs_put_user_before_public(monkeypatch, tmp_path):
    home = tmp_path / "work"
    user_root = _bind_work_session(monkeypatch, home)
    public_root = home / "skills"
    public_root.mkdir()

    from agent.skill_utils import get_session_skills_dirs

    assert get_session_skills_dirs(public_root)[:2] == [user_root, public_root]


def test_skill_listing_and_view_prefer_private_copy(monkeypatch, tmp_path):
    home = tmp_path / "work"
    user_root = _bind_work_session(monkeypatch, home)
    public_root = home / "skills"
    _write_skill(public_root, "shared-check", "public version")
    _write_skill(user_root, "shared-check", "private version")

    import tools.skills_tool as skills_tool

    monkeypatch.setattr(skills_tool, "SKILLS_DIR", public_root)
    skills_tool._SKILLS_CACHE.clear()
    listed = {item["name"]: item for item in skills_tool._find_all_skills()}
    viewed = json.loads(skills_tool.skill_view("shared-check"))

    assert listed["shared-check"]["description"] == "private version"
    assert viewed["success"] is True
    assert "private version" in viewed["content"]
    assert str(user_root) in viewed["skill_dir"]


def test_skill_manage_create_uses_private_root_and_blocks_public_edit(monkeypatch, tmp_path):
    home = tmp_path / "work"
    user_root = _bind_work_session(monkeypatch, home)
    public_root = home / "skills"
    _write_skill(public_root, "public-only", "public version")

    import tools.skill_manager_tool as manager

    monkeypatch.setattr(manager, "SKILLS_DIR", public_root)
    monkeypatch.setattr(manager, "_security_scan_skill", lambda _path: None)
    monkeypatch.setattr(manager, "_attach_lint_findings", lambda _result, _path: None)

    content = "---\nname: private-only\ndescription: private\n---\n\nPrivate.\n"
    result = manager._create_skill("private-only", content)
    assert result["success"] is True
    assert (user_root / "private-only" / "SKILL.md").exists()
    assert not (public_root / "private-only" / "SKILL.md").exists()

    blocked = manager._edit_skill(
        "public-only",
        "---\nname: public-only\ndescription: changed\n---\n\nChanged.\n",
    )
    assert blocked["success"] is False
    assert "public skill" in blocked["error"]
