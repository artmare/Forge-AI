import json
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.config import Settings
from app.development.contracts import RunnerRequest
from app.development.runner_client import QueueRunnerClient
from app.domain.enums import DevelopmentAction, DevelopmentExecutionStatus

pytestmark = pytest.mark.skipif(
    os.getenv("DEVELOPMENT_RUNNER_QUEUE_ROOT") != "/runner-queue",
    reason="requires the actual isolated Compose development runner",
)


async def run_action(
    client: QueueRunnerClient,
    action: DevelopmentAction,
    company_id: UUID,
    project_id: UUID,
    task_id: UUID,
):
    return await client.execute(
        RunnerRequest(
            request_id=uuid4(),
            action=action,
            workspace_relative=f"{company_id}/{project_id}",
            timeout_seconds=20,
            output_limit_bytes=16_384,
            task_id=task_id,
        )
    )


def node_test(workspace: Path, source: str) -> None:
    (workspace / "package.json").write_text(
        json.dumps({"scripts": {"test": "node attack.mjs"}}), encoding="utf-8"
    )
    (workspace / "attack.mjs").write_text(source, encoding="utf-8")


async def test_project_code_cannot_mutate_git_metadata_but_normal_outputs_and_checkpoint_work(
    tmp_path: Path,
) -> None:
    del tmp_path  # The integration boundary intentionally uses the shared isolated-runner volumes.
    settings = Settings(
        development_runner_mode="queue",
        development_runner_queue_root="/runner-queue",
        tool_workspace_root="/workspaces",
    )
    client = QueueRunnerClient(settings)
    company_id, project_id, task_id = uuid4(), uuid4(), uuid4()
    workspace = Path("/workspaces") / str(company_id) / str(project_id)
    workspace.mkdir(parents=True)
    try:
        initialized = await run_action(
            client, DevelopmentAction.GIT_INIT, company_id, project_id, task_id
        )
        assert initialized.status == DevelopmentExecutionStatus.SUCCEEDED
        config = workspace / ".git/config"
        head = workspace / ".git/HEAD"
        original_config = config.read_bytes()
        original_head = head.read_bytes()

        # A: direct package-script write to .git/config cannot reach authoritative metadata.
        node_test(
            workspace,
            "import {writeFileSync} from 'node:fs'; writeFileSync('.git/config', 'owned');",
        )
        config_attack = await run_action(
            client, DevelopmentAction.NODE_TEST, company_id, project_id, task_id
        )
        assert config_attack.status != DevelopmentExecutionStatus.SUCCEEDED
        assert config.read_bytes() == original_config

        # B: a direct Node process cannot overwrite HEAD.
        node_test(
            workspace,
            "import {writeFileSync} from 'node:fs'; writeFileSync('.git/HEAD', 'owned');",
        )
        head_attack = await run_action(
            client, DevelopmentAction.NODE_TEST, company_id, project_id, task_id
        )
        assert head_attack.status != DevelopmentExecutionStatus.SUCCEEDED
        assert head.read_bytes() == original_head

        # C: creating a hook in the disposable tree is detected and never synchronized.
        node_test(
            workspace,
            "import {mkdirSync,writeFileSync} from 'node:fs';"
            "mkdirSync('.git/hooks',{recursive:true});"
            "writeFileSync('.git/hooks/pre-commit','owned');",
        )
        hook_attack = await run_action(
            client, DevelopmentAction.NODE_TEST, company_id, project_id, task_id
        )
        assert hook_attack.status == DevelopmentExecutionStatus.DENIED
        assert hook_attack.error_code == "DEVELOPMENT_GIT_METADATA_WRITE_DENIED"
        assert not (workspace / ".git/hooks/pre-commit").exists()

        # D: a nested symlink aimed at the real repository remains outside Landlock's tree.
        git_target = (workspace / ".git").as_posix()
        node_test(
            workspace,
            "import {mkdirSync,symlinkSync,writeFileSync} from 'node:fs';"
            "mkdirSync('nested',{recursive:true});"
            f"symlinkSync('{git_target}','nested/.git','dir');"
            "writeFileSync('nested/.git/config','owned');",
        )
        nested_attack = await run_action(
            client, DevelopmentAction.NODE_TEST, company_id, project_id, task_id
        )
        assert nested_attack.status != DevelopmentExecutionStatus.SUCCEEDED
        assert config.read_bytes() == original_config

        # E: normal generated output is synchronized back to the project workspace.
        node_test(
            workspace,
            "import {mkdirSync,writeFileSync} from 'node:fs';"
            "mkdirSync('dist',{recursive:true});"
            "writeFileSync('dist/output.txt','safe output');",
        )
        normal = await run_action(
            client, DevelopmentAction.NODE_TEST, company_id, project_id, task_id
        )
        assert normal.status == DevelopmentExecutionStatus.SUCCEEDED
        assert (workspace / "dist/output.txt").read_text(encoding="utf-8") == "safe output"

        # F: the fixed Forge-owned Git branch still checkpoints the resulting safe files.
        checkpoint = await run_action(
            client, DevelopmentAction.GIT_CHECKPOINT, company_id, project_id, task_id
        )
        assert checkpoint.status == DevelopmentExecutionStatus.SUCCEEDED
        status = await run_action(
            client, DevelopmentAction.GIT_STATUS, company_id, project_id, task_id
        )
        assert status.status == DevelopmentExecutionStatus.SUCCEEDED
        assert "dist/output.txt" not in status.stdout_excerpt
        assert config.read_bytes() == original_config
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
