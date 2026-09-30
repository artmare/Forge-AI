from uuid import uuid4

import pytest

from app.agent_runtime.builders import ContextRecord
from app.agent_runtime.contracts import BaseAgentResult
from app.agent_runtime.execution_truth import (
    EvidenceKind,
    EvidenceProvenance,
    ExecutionEvidence,
    ExecutionTruthValidator,
)
from app.tool_system.contracts import ToolObservation, ToolObservationProvenance


def _context(*, execution_required: bool = True) -> ContextRecord:
    return ContextRecord(
        company={"id": "company", "name": "Forge", "goal": "Build safely"},
        project={"id": "project", "name": "Forge", "goal": "Self development"},
        task={
            "id": "task",
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Create hello.txt",
            "description": "Create hello.txt containing FORGE_SELF_DEV_OK",
            "input": {"execution_required": execution_required},
            "acceptance_criteria": ["hello.txt exists"],
            "priority": "NORMAL",
            "iteration": 0,
            "execution_iteration": 1,
            "max_iterations": 3,
        },
        agent={"id": "agent", "name": "Lead Engineer", "role": "DEVELOPER"},
    )


def _result(artifacts: list[str], summary: str = "Completed") -> BaseAgentResult:
    return BaseAgentResult(
        status="completed",
        summary=summary,
        output={"artifacts": artifacts, "details": []},
    )


def _git_result(reference: str, tool_call_id: str, *, scope: str = "CURRENT_RUN"):
    result = _result([], "Inspected the repository with Forge Git tools.")
    result.output["execution_claims"] = [
        {
            "kind": "GIT",
            "reference": reference,
            "scope": scope,
            "tool_call_id": tool_call_id,
        }
    ]
    return result


def _git_observation(
    tool: str,
    reference: str,
    *,
    tool_call_id=None,
    status: str = "SUCCEEDED",
    task_id=None,
    task_run_id=None,
    agent_run_id=None,
    provenance=ToolObservationProvenance.EXECUTED_TOOL_CALL,
) -> ToolObservation:
    return ToolObservation(
        tool_call_id=tool_call_id or uuid4(),
        tool=tool,
        status="success",
        result={"action": reference, "status": status},
        provenance=provenance,
        task_id=task_id,
        task_run_id=task_run_id,
        agent_run_id=agent_run_id,
    )


def test_model_claim_without_tool_execution_is_rejected() -> None:
    decision = ExecutionTruthValidator.validate(
        _context(),
        _result([], "Successfully wrote FORGE_SELF_DEV_OK to hello.txt"),
        [],
    )

    assert not decision.accepted
    assert decision.code == "UNVERIFIED_EXECUTION_CLAIM"


def test_artifact_without_matching_mutation_evidence_is_rejected() -> None:
    observation = ToolObservation(
        tool_call_id=uuid4(),
        tool="filesystem.write",
        status="success",
        result={"path": "other.txt"},
    )

    decision = ExecutionTruthValidator.validate(_context(), _result(["hello.txt"]), [observation])

    assert not decision.accepted
    assert "hello.txt" in decision.message


def test_structured_claim_reference_must_match_authoritative_evidence() -> None:
    observation = ToolObservation(
        tool_call_id=uuid4(),
        tool="filesystem.write",
        status="success",
        result={"path": "other.txt"},
    )
    result = _result([])
    result.output["execution_claims"] = [{"kind": "FILE_MUTATION", "reference": "hello.txt"}]

    decision = ExecutionTruthValidator.validate(_context(), result, [observation])

    assert not decision.accepted


def test_successful_forge_mutation_is_authoritative_evidence() -> None:
    observation = ToolObservation(
        tool_call_id=uuid4(),
        tool="filesystem.write",
        status="success",
        result={"path": "hello.txt"},
    )

    decision = ExecutionTruthValidator.validate(_context(), _result(["hello.txt"]), [observation])

    assert decision.accepted
    assert decision.evidence[0].kind == EvidenceKind.FILE_MUTATION


def test_revalidated_historical_evidence_preserves_provenance_and_supports_history() -> None:
    original_run = uuid4()
    original_call = uuid4()
    task_id = uuid4()
    project_id = uuid4()
    historical = ExecutionEvidence(
        kind=EvidenceKind.FILE_MUTATION,
        tool="filesystem.write",
        reference="hello.txt",
        provenance=EvidenceProvenance.RECOVERY_HISTORY,
        task_id=task_id,
        project_id=project_id,
        agent_run_id=original_run,
        tool_call_id=original_call,
        artifact_sha256="a" * 64,
        checkpoint="deadbee",
    )
    result = _result(["hello.txt"], "The previous execution created hello.txt.")
    result.output["execution_claims"] = [
        {
            "kind": "FILE_MUTATION",
            "reference": "hello.txt",
            "scope": "HISTORICAL",
            "tool_call_id": str(original_call),
        }
    ]

    decision = ExecutionTruthValidator.validate(
        _context(), result, [], historical_evidence=(historical,)
    )

    assert decision.accepted
    assert decision.evidence[0].agent_run_id == original_run
    assert decision.evidence[0].tool_call_id == original_call


def test_historical_evidence_cannot_prove_current_run_performed_mutation() -> None:
    historical = ExecutionEvidence(
        kind=EvidenceKind.FILE_MUTATION,
        tool="filesystem.write",
        reference="hello.txt",
        provenance=EvidenceProvenance.RECOVERY_HISTORY,
        tool_call_id=uuid4(),
    )
    result = _result(["hello.txt"], "Created hello.txt in this run.")
    result.output["execution_claims"] = [
        {"kind": "FILE_MUTATION", "reference": "hello.txt", "scope": "CURRENT_RUN"}
    ]

    decision = ExecutionTruthValidator.validate(
        _context(), result, [], historical_evidence=(historical,)
    )

    assert not decision.accepted
    assert decision.failed_claim == {
        "kind": "FILE_MUTATION",
        "reference": "hello.txt",
        "scope": "CURRENT_RUN",
        "tool_call_id": None,
    }


def test_historical_evidence_from_another_task_or_workspace_is_rejected() -> None:
    task_id = uuid4()
    project_id = uuid4()
    historical = ExecutionEvidence(
        kind=EvidenceKind.FILE_MUTATION,
        tool="filesystem.write",
        reference="hello.txt",
        provenance=EvidenceProvenance.RECOVERY_HISTORY,
        task_id=uuid4(),
        project_id=uuid4(),
        tool_call_id=uuid4(),
    )
    result = _result(["hello.txt"], "The previous execution created hello.txt.")

    decision = ExecutionTruthValidator.validate(
        _context(),
        result,
        [],
        historical_evidence=(historical,),
        current_task_id=task_id,
        current_project_id=project_id,
    )

    assert not decision.accepted
    assert decision.evidence == ()


def test_failed_forge_mutation_does_not_verify_a_write_claim() -> None:
    observation = ToolObservation(
        tool_call_id=uuid4(),
        tool="filesystem.write",
        status="error",
        error={"code": "TOOL_EXECUTION_FAILED", "message": "write failed"},
    )

    decision = ExecutionTruthValidator.validate(
        _context(), _result([], "I created hello.txt"), [observation]
    )

    assert not decision.accepted
    assert decision.code == "UNVERIFIED_EXECUTION_CLAIM"


def test_model_claims_tests_passed_without_test_result_is_rejected() -> None:
    write = ToolObservation(
        tool_call_id=uuid4(),
        tool="filesystem.write",
        status="success",
        result={"path": "hello.txt"},
    )

    decision = ExecutionTruthValidator.validate(
        _context(), _result(["hello.txt"], "Tests passed"), [write]
    )

    assert not decision.accepted
    assert decision.code == "UNVERIFIED_EXECUTION_CLAIM"


def test_successful_forge_test_result_verifies_test_success_claim() -> None:
    observation = ToolObservation(
        tool_call_id=uuid4(),
        tool="development.execute",
        status="success",
        result={"action": "PYTHON_TEST", "status": "SUCCEEDED"},
    )

    decision = ExecutionTruthValidator.validate(
        _context(), _result([], "All tests passed"), [observation]
    )

    assert decision.accepted


def test_failed_test_result_does_not_verify_test_success_claim() -> None:
    observation = ToolObservation(
        tool_call_id=uuid4(),
        tool="development.execute",
        status="success",
        result={"action": "PYTHON_TEST", "status": "FAILED"},
    )

    decision = ExecutionTruthValidator.validate(
        _context(), _result([], "All tests passed"), [observation]
    )

    assert not decision.accepted


def test_explicit_read_only_developer_task_can_complete_without_execution() -> None:
    decision = ExecutionTruthValidator.validate(_context(execution_required=False), _result([]), [])

    assert decision.accepted


def test_informational_developer_task_needs_no_artificial_execution() -> None:
    decision = ExecutionTruthValidator.validate(
        _context(execution_required=True),
        _result([], "I inspected the architecture and recommend extracting the adapter."),
        [],
    )

    assert decision.accepted


def test_later_failed_test_supersedes_old_pass() -> None:
    observations = [
        ToolObservation(
            tool_call_id=uuid4(),
            tool="development.execute",
            status="success",
            result={"action": "NODE_TEST", "status": status},
        )
        for status in ("SUCCEEDED", "FAILED")
    ]
    decision = ExecutionTruthValidator.validate(
        _context(), _result([], "Tests passed"), observations
    )
    assert not decision.accepted


def test_failed_git_action_cannot_back_execution_claim() -> None:
    result = _result([], "Committed the changes")
    observations = [
        ToolObservation(
            tool_call_id=uuid4(), tool="git.commit", status="success", result={"status": "FAILED"}
        )
    ]
    assert not ExecutionTruthValidator.validate(_context(), result, observations).accepted


@pytest.mark.parametrize(
    ("tool", "reference"),
    [
        ("git.status", "GIT_STATUS"),
        ("git.diff", "GIT_DIFF"),
        ("git.log", "GIT_LOG"),
        ("git.commit", "GIT_CHECKPOINT"),
    ],
)
def test_git_tools_produce_forge_owned_canonical_evidence(tool: str, reference: str) -> None:
    call_id = uuid4()
    observation = _git_observation(tool, "MODEL_CONTROLLED_VALUE", tool_call_id=call_id)

    evidence = ExecutionTruthValidator.collect([observation])

    assert len(evidence) == 1
    assert evidence[0].kind == EvidenceKind.GIT
    assert evidence[0].reference == reference
    assert evidence[0].tool_call_id == call_id


def test_current_git_claim_requires_matching_reference_and_tool_call_id() -> None:
    call_id = uuid4()
    observation = _git_observation("git.status", "GIT_STATUS", tool_call_id=call_id)

    assert ExecutionTruthValidator.validate(
        _context(), _git_result("GIT_STATUS", str(call_id)), [observation]
    ).accepted
    assert not ExecutionTruthValidator.validate(
        _context(), _git_result("GIT_STATUS", str(uuid4())), [observation]
    ).accepted
    assert not ExecutionTruthValidator.validate(
        _context(), _git_result("GIT_DIFF", str(call_id)), [observation]
    ).accepted


def test_failed_or_reused_git_observation_is_not_current_execution_evidence() -> None:
    failed_id = uuid4()
    failed = _git_observation(
        "git.status", "GIT_STATUS", tool_call_id=failed_id, status="FAILED"
    )
    reused_id = uuid4()
    reused = _git_observation(
        "git.diff",
        "GIT_DIFF",
        tool_call_id=reused_id,
        provenance=ToolObservationProvenance.REUSED_TOOL_CALL_RESULT,
    )

    assert not ExecutionTruthValidator.validate(
        _context(), _git_result("GIT_STATUS", str(failed_id)), [failed]
    ).accepted
    assert not ExecutionTruthValidator.validate(
        _context(), _git_result("GIT_DIFF", str(reused_id)), [reused]
    ).accepted


@pytest.mark.parametrize("foreign_task", [True, False])
def test_git_evidence_is_scoped_to_current_task_run(foreign_task: bool) -> None:
    current_task = uuid4()
    current_task_run = uuid4()
    foreign_call = uuid4()
    foreign = _git_observation(
        "git.status",
        "GIT_STATUS",
        tool_call_id=foreign_call,
        task_id=uuid4() if foreign_task else current_task,
        task_run_id=current_task_run if foreign_task else uuid4(),
        agent_run_id=uuid4(),
    )

    decision = ExecutionTruthValidator.validate(
        _context(),
        _git_result("GIT_STATUS", str(foreign_call)),
        [foreign],
        current_task_id=current_task,
        current_task_run_id=current_task_run,
    )

    assert not decision.accepted
    assert decision.evidence == ()


def test_historical_git_evidence_cannot_satisfy_current_git_claim() -> None:
    call_id = uuid4()
    historical = ExecutionEvidence(
        kind=EvidenceKind.GIT,
        tool="git.status",
        reference="GIT_STATUS",
        provenance=EvidenceProvenance.RECOVERY_HISTORY,
        tool_call_id=call_id,
    )

    decision = ExecutionTruthValidator.validate(
        _context(),
        _git_result("GIT_STATUS", str(call_id)),
        [],
        historical_evidence=(historical,),
    )

    assert not decision.accepted


def test_visual_claim_requires_real_browser_evidence() -> None:
    assert not ExecutionTruthValidator.validate(
        _context(), _result([], "The page looks correct"), []
    ).accepted
