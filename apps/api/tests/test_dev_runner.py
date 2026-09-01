import asyncio
import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

RUNNER_SOURCE = Path(os.getenv("FORGE_DEV_RUNNER_SOURCE", "/dev-runner/runner.py"))
pytestmark = pytest.mark.skipif(
    not RUNNER_SOURCE.is_file(),
    reason="mount services/dev-runner at /dev-runner to run isolated runner unit tests",
)


def load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("forge_dev_runner_under_test", RUNNER_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner(tmp_path: Path) -> ModuleType:
    module = load_runner()
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    workspace = workspace_root / "11111111-1111-1111-1111-111111111111"
    workspace = workspace / "22222222-2222-2222-2222-222222222222"
    workspace.mkdir(parents=True)
    wrapper = tmp_path / "sandbox-wrapper.py"
    wrapper.write_text(
        "#!/usr/local/bin/python\nimport os, sys\nos.execvp(sys.argv[2], sys.argv[2:])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    module.WORKSPACE_ROOT = workspace_root
    module.QUEUE_ROOT = tmp_path / "queue"
    module.SANDBOX_EXECUTABLE = str(wrapper)
    module.TEST_WORKSPACE = workspace
    return module


async def test_runner_success_failure_timeout_and_output_bound(runner: ModuleType) -> None:
    success = await runner.run_command(
        ["python", "-c", "print('ok')"],
        runner.TEST_WORKSPACE,
        2,
        1024,
        runner.QUEUE_ROOT / "no-cancel",
    )
    assert success[0] == 0 and success[1].text()[0].strip() == "ok"

    failed = await runner.run_command(
        ["python", "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"],
        runner.TEST_WORKSPACE,
        2,
        1024,
        runner.QUEUE_ROOT / "no-cancel",
    )
    assert failed[0] == 7 and "bad" in failed[2].text()[0]

    timed_out = await runner.run_command(
        ["python", "-c", "import time; time.sleep(5)"],
        runner.TEST_WORKSPACE,
        0.1,
        1024,
        runner.QUEUE_ROOT / "no-cancel",
    )
    assert timed_out[3] is True

    bounded = await runner.run_command(
        ["python", "-c", "print('x' * 10000)"],
        runner.TEST_WORKSPACE,
        2,
        1024,
        runner.QUEUE_ROOT / "no-cancel",
    )
    text, truncated = bounded[1].text()
    assert truncated is True and len(text.encode()) < 1100


async def test_runner_cancellation_terminates_process_group(runner: ModuleType) -> None:
    marker = runner.QUEUE_ROOT / "cancel"
    execution = asyncio.create_task(
        runner.run_command(
            ["python", "-c", "import time; time.sleep(10)"],
            runner.TEST_WORKSPACE,
            20,
            1024,
            marker,
        )
    )
    await asyncio.sleep(0.15)
    marker.parent.mkdir(parents=True)
    marker.write_text("cancel", encoding="utf-8")
    result = await asyncio.wait_for(execution, timeout=3)
    assert result[4] is True


def test_runner_rejects_unsafe_scope_actions_and_arguments(runner: ModuleType) -> None:
    relative = "11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222"
    assert runner.safe_workspace(relative) == runner.TEST_WORKSPACE
    for unsafe in ("../../etc", "/etc", "bad", f"{relative}/extra"):
        with pytest.raises(ValueError):
            runner.safe_workspace(unsafe)
    for unsafe_target in ("../../etc/passwd", "tests | id", "$(id)", "/etc/passwd"):
        with pytest.raises(ValueError):
            runner.safe_target({"target": unsafe_target})
    with pytest.raises(ValueError):
        runner.command_for({"action": "SHELL_RUN", "safe_arguments": {}}, runner.TEST_WORKSPACE)
    with pytest.raises(ValueError):
        runner.command_for(
            {"action": "NODE_TEST", "safe_arguments": {"executable": "sh"}},
            runner.TEST_WORKSPACE,
        )
    checkpoint = runner.command_for(
        {
            "action": "GIT_CHECKPOINT",
            "safe_arguments": {},
            "task_id": "55555555-5555-5555-5555-555555555555",
        },
        runner.TEST_WORKSPACE,
    )
    assert checkpoint[0][:4] == ["git", "add", "-A", "--"]
    assert ":(exclude).env" in checkpoint[0]
    assert ":(exclude)**/*.key" in checkpoint[0]
    assert all(
        "remote" not in argument and "push" not in argument
        for command in checkpoint
        for argument in command
    )


def test_workspace_symlink_escape_is_rejected(runner: ModuleType, tmp_path: Path) -> None:
    company = runner.WORKSPACE_ROOT / "33333333-3333-3333-3333-333333333333"
    company.mkdir()
    project = company / "44444444-4444-4444-4444-444444444444"
    try:
        project.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ValueError):
        runner.safe_workspace(
            "33333333-3333-3333-3333-333333333333/44444444-4444-4444-4444-444444444444"
        )


async def test_controlled_git_bootstrap_and_checkpoint_are_real_and_secret_safe(
    runner: ModuleType,
) -> None:
    task_id = "55555555-5555-5555-5555-555555555555"
    (runner.TEST_WORKSPACE / "package.json").write_text(
        '{"scripts":{"test":"node --test","build":"node scripts/build.mjs"}}',
        encoding="utf-8",
    )
    (runner.TEST_WORKSPACE / "manifest.json").write_text("{}", encoding="utf-8")
    (runner.TEST_WORKSPACE / ".env").write_text(
        "OPENAI_API_KEY=must-not-enter-checkpoint", encoding="utf-8"
    )

    init_commands = runner.command_for(
        {"action": "GIT_INIT", "safe_arguments": {}, "task_id": task_id},
        runner.TEST_WORKSPACE,
    )
    for command in init_commands:
        result = await runner.run_command(
            command,
            runner.TEST_WORKSPACE,
            5,
            4096,
            runner.QUEUE_ROOT / "no-cancel",
        )
        assert result[0] == 0, result[2].text()[0]
    assert (runner.TEST_WORKSPACE / ".git").is_dir()

    checkpoint_commands = runner.command_for(
        {"action": "GIT_CHECKPOINT", "safe_arguments": {}, "task_id": task_id},
        runner.TEST_WORKSPACE,
    )
    for command in checkpoint_commands:
        result = await runner.run_command(
            command,
            runner.TEST_WORKSPACE,
            5,
            4096,
            runner.QUEUE_ROOT / "no-cancel",
        )
        assert result[0] == 0, result[2].text()[0]

    committed = await runner.run_command(
        ["git", "ls-files"],
        runner.TEST_WORKSPACE,
        5,
        4096,
        runner.QUEUE_ROOT / "no-cancel",
    )
    assert committed[0] == 0
    tracked = committed[1].text()[0].splitlines()
    assert "package.json" in tracked and "manifest.json" in tracked
    assert ".env" not in tracked

    status_commands = runner.command_for(
        {"action": "GIT_STATUS", "safe_arguments": {}, "task_id": task_id},
        runner.TEST_WORKSPACE,
    )
    status = await runner.run_command(
        status_commands[0],
        runner.TEST_WORKSPACE,
        5,
        4096,
        runner.QUEUE_ROOT / "no-cancel",
    )
    assert status[0] == 0
    assert ".env" in status[1].text()[0]
