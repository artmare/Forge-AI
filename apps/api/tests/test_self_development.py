import asyncio
import subprocess
from pathlib import Path

from app.development.self_development import SelfDevelopmentWorkspaceManager


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_self_development_uses_isolated_feature_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "forge"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "forge@example.invalid")
    _git(repository, "config", "user.name", "Forge Test")
    (repository / "stable.txt").write_text("stable\n")
    _git(repository, "add", "stable.txt")
    _git(repository, "commit", "-m", "stable")
    stable_head = _git(repository, "rev-parse", "HEAD")

    created = asyncio.run(
        SelfDevelopmentWorkspaceManager(repository, tmp_path / "worktrees").create("task-123")
    )

    assert created.branch == "forge-dev/task-123"
    assert created.base_revision == stable_head
    assert created.path != repository
    assert _git(repository, "branch", "--show-current") == "main"
    assert _git(repository, "rev-parse", "HEAD") == stable_head
    assert (created.path / "stable.txt").read_text() == "stable\n"


def test_worktree_checkpoint_cleanup_and_path_guards(tmp_path: Path) -> None:
    import pytest

    from app.tool_system.errors import ToolSystemError

    repository = tmp_path / "source"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "forge@example.invalid")
    _git(repository, "config", "user.name", "Forge")
    (repository / "a.txt").write_text("base")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    original = _git(repository, "rev-parse", "HEAD")
    manager = SelfDevelopmentWorkspaceManager(repository, tmp_path / "trees")

    async def run():
        workspace = await manager.create("test")
        (workspace.path / "a.txt").write_text("changed")
        with pytest.raises(ToolSystemError, match="Checkpoint"):
            await manager.cleanup(workspace)
        commit = await manager.checkpoint(workspace)
        assert commit != original
        await manager.cleanup(workspace)
        assert not workspace.path.exists()
        assert _git(repository, "rev-parse", "forge-dev/test") == commit
        assert _git(repository, "rev-parse", "main") == original

    asyncio.run(run())
    with pytest.raises(ToolSystemError):
        SelfDevelopmentWorkspaceManager(repository, repository / "nested")
    symlink = tmp_path / "alias"
    symlink.symlink_to(repository, target_is_directory=True)
    with pytest.raises(ToolSystemError):
        SelfDevelopmentWorkspaceManager(symlink, tmp_path / "other")
