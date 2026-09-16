import os

import pytest

from nezha.workspace import (WorkspaceError, WorkspaceManager, ensure_within,
                             slugify)


def wm(git_repo, tmp_path, **overrides):
    config = {
        "repo": str(git_repo),
        "root": str(tmp_path / "ws"),
        "state_root": str(tmp_path / "state"),
        "base_ref": "HEAD",
        "branch_prefix": "nezha/",
    }
    config.update(overrides)
    return WorkspaceManager(config)


def test_slugify():
    assert slugify("ABC-123") == "ABC-123"
    assert slugify("feat/some thing!").startswith("feat-some-thing-")
    assert len(slugify("x" * 200)) == 80


def test_slugify_is_injective_for_colliding_identifiers():
    """Regression: sanitizing alone is many-to-one, which would let two live
    issues share one worktree, branch and state file."""
    assert slugify("PROJ/123") != slugify("PROJ-123")
    assert slugify("Fix login bug") != slugify("Fix-login-bug")
    long_a, long_b = "x" * 100 + "A", "x" * 100 + "B"
    assert slugify(long_a) != slugify(long_b)


def test_slugify_is_stable():
    assert slugify("PROJ/123") == slugify("PROJ/123")


def test_slugify_rejects_empty():
    with pytest.raises(WorkspaceError):
        slugify("///")


def test_ensure_within_blocks_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    ensure_within(str(root), str(root / "a"))
    with pytest.raises(WorkspaceError, match="escapes workspace root"):
        ensure_within(str(root), str(tmp_path / "elsewhere"))


def test_ensure_creates_worktree_and_branch(git_repo, tmp_path):
    manager = wm(git_repo, tmp_path)
    ws = manager.ensure("T-1", "T-1")
    assert os.path.isdir(ws.path)
    assert os.path.exists(os.path.join(ws.path, "README.md"))
    assert ws.branch == "nezha/T-1"
    assert manager._branch_exists("nezha/T-1")


def test_ensure_is_idempotent_and_preserves_state(git_repo, tmp_path):
    manager = wm(git_repo, tmp_path)
    ws = manager.ensure("T-1", "T-1")
    ws.save_state(attempts=2, marker="keep")
    again = manager.ensure("T-1", "T-1")
    assert again.path == ws.path
    assert again.load_state()["marker"] == "keep"
    assert again.load_state()["attempts"] == 2


def test_state_lives_outside_the_worktree(git_repo, tmp_path):
    manager = wm(git_repo, tmp_path)
    ws = manager.ensure("T-1", "T-1")
    ws.save_state(attempts=1)
    assert not ws.state_path.startswith(ws.path)
    assert os.path.isfile(ws.state_path)


def test_ensure_rejects_foreign_directory(git_repo, tmp_path):
    manager = wm(git_repo, tmp_path)
    stray = os.path.join(manager.root, "T-9")
    os.makedirs(stray)
    with pytest.raises(WorkspaceError, match="not a registered worktree"):
        manager.ensure("T-9", "T-9")


def test_ensure_rejects_worktree_owned_by_another_issue(git_repo, tmp_path):
    """Defence in depth: never hand a worktree to a run it does not belong to."""
    manager = wm(git_repo, tmp_path)
    ws = manager.ensure("ID-A", "SHARED")
    ws.save_state(attempts=1)
    with pytest.raises(WorkspaceError, match="already belongs to issue ID-A"):
        manager.ensure("ID-B", "SHARED")


def test_remove_cleans_worktree_and_state(git_repo, tmp_path):
    manager = wm(git_repo, tmp_path)
    ws = manager.ensure("T-1", "T-1")
    ws.save_state(attempts=1)
    manager.remove(ws)
    assert not os.path.isdir(ws.path)
    assert not os.path.isfile(ws.state_path)
    assert manager.list_states() == []


def test_hooks_run_with_env(git_repo, tmp_path):
    manager = wm(git_repo, tmp_path)
    manager.hooks = {"after_create": 'echo "$NEZHA_ISSUE_IDENTIFIER" > hook.txt'}
    ws = manager.ensure("T-7", "T-7")
    with open(os.path.join(ws.path, "hook.txt"), encoding="utf-8") as fh:
        assert fh.read().strip() == "T-7"


def test_failing_hook_surfaces_error(git_repo, tmp_path):
    manager = wm(git_repo, tmp_path)
    manager.hooks = {"after_create": "exit 3"}
    with pytest.raises(WorkspaceError, match="hook after_create failed"):
        manager.ensure("T-8", "T-8")


def test_verify_repo_rejects_non_repo(tmp_path):
    manager = WorkspaceManager({"repo": str(tmp_path), "root": str(tmp_path / "ws")})
    with pytest.raises(WorkspaceError, match="not a git repository"):
        manager.verify_repo()


def test_list_states(git_repo, tmp_path):
    manager = wm(git_repo, tmp_path)
    for name in ("T-1", "T-2"):
        manager.ensure(name, name).save_state(status="idle")
    assert sorted(r["identifier"] for r in manager.list_states()) == ["T-1", "T-2"]
