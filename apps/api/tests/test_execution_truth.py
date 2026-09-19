from uuid import uuid4

from app.agent_runtime.builders import ContextRecord
from app.agent_runtime.contracts import BaseAgentResult
from app.agent_runtime.execution_truth import EvidenceKind, ExecutionTruthValidator
from app.tool_system.contracts import ToolObservation


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

    decision = ExecutionTruthValidator.validate(
        _context(), _result(["hello.txt"]), [observation]
    )

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
    result.output["execution_claims"] = [
        {"kind": "FILE_MUTATION", "reference": "hello.txt"}
    ]

    decision = ExecutionTruthValidator.validate(_context(), result, [observation])

    assert not decision.accepted


def test_successful_forge_mutation_is_authoritative_evidence() -> None:
    observation = ToolObservation(
        tool_call_id=uuid4(),
        tool="filesystem.write",
        status="success",
        result={"path": "hello.txt"},
    )

    decision = ExecutionTruthValidator.validate(
        _context(), _result(["hello.txt"]), [observation]
    )

    assert decision.accepted
    assert decision.evidence[0].kind == EvidenceKind.FILE_MUTATION


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
