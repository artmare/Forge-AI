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
