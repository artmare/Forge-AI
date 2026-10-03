import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from httpx import AsyncClient

from app.agent_runtime.contracts import ModelRequest
from app.agent_runtime.providers import MockModelProvider, OpenRouterModelProvider
from app.agent_runtime.routing import EconomicTier, ModelCapability, ModelProfile
from app.agent_runtime.runtime import AgentRuntime
from app.core.config import Settings
from app.development.automatic_verification import AutomaticVerificationRunner
from app.development.completion_manifest import CompletionManifestService
from app.development.contracts import RunnerResponse
from app.development.runner_client import FakeRunnerClient
from app.development.verification_contracts import (
    VerificationKind,
    VerificationPlan,
    VerificationResult,
    VerificationStep,
    VerificationStepStatus,
    VisualFindingSeverity,
    VisualQACaptureReference,
    VisualQADecision,
    VisualQAEvidence,
    VisualQAFinding,
)
from app.development.verification_plan import VerificationPlanService
from app.development.visual_qa import VisualQAService
from app.domain.enums import DevelopmentAction, DevelopmentExecutionStatus, DevelopmentProjectType
from app.infrastructure.database import get_session_factory
from app.tool_system.contracts import ToolExecutionContext
from app.tool_system.errors import ToolSystemError
from app.tool_system.workspace import WorkspaceManager
from tests.helpers import create_agent, create_company, create_project, transition_task


def _task(criteria: list[str], deliverables: list[str]):
    return SimpleNamespace(acceptance_criteria=criteria, input={"deliverables": deliverables})


def _profile(project_type: DevelopmentProjectType, *actions: DevelopmentAction):
    values = set(actions)
    return SimpleNamespace(
        project_type=project_type,
        lint_action=next((a for a in values if "LINT" in a.value), None),
        typecheck_action=next((a for a in values if "TYPECHECK" in a.value), None),
        test_action=next((a for a in values if "TEST" in a.value), None),
        build_action=next((a for a in values if "BUILD" in a.value), None),
    )


def _step_kinds(settings: Settings, task, profile, workspace: Path) -> list[VerificationKind]:
    service = VerificationPlanService(None, settings)  # type: ignore[arg-type]
    return [
        step.kind
        for step in service._steps(
            task, profile.project_type, profile, task.input["deliverables"], workspace
        )
    ]


def test_static_web_plan_is_bounded_and_requires_judgment(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<main></main>")
    task = _task(
        ["`index.html` exists", "responsive layout", "no browser console errors"],
        ["index.html", "styles.css", "app.js"],
    )
    kinds = _step_kinds(
        Settings(forge_browser_enabled=True),
        task,
        _profile(DevelopmentProjectType.STATIC_WEB),
        tmp_path,
    )
    assert kinds == [
        VerificationKind.ARTIFACTS,
        VerificationKind.STATIC_WEB,
        VerificationKind.GIT_STATUS,
        VerificationKind.BROWSER_DESKTOP,
        VerificationKind.BROWSER_MOBILE,
        VerificationKind.VISUAL_QA,
    ]
    assert len(kinds) <= 32


def test_browser_and_visual_qa_are_not_planned_for_backend_or_when_disabled(
    tmp_path: Path,
) -> None:
    backend = _task(["tests pass"], ["service.py"])
    backend_kinds = _step_kinds(
        Settings(forge_browser_enabled=True),
        backend,
        _profile(DevelopmentProjectType.PYTHON, DevelopmentAction.PYTHON_TEST),
        tmp_path,
    )
    disabled_kinds = _step_kinds(
        Settings(forge_browser_enabled=False),
        _task(["responsive layout"], ["index.html"]),
        _profile(DevelopmentProjectType.STATIC_WEB),
        tmp_path,
    )
    assert VerificationKind.BROWSER_DESKTOP not in backend_kinds
    assert VerificationKind.VISUAL_QA not in backend_kinds
    assert VerificationKind.BROWSER_DESKTOP not in disabled_kinds
    assert VerificationKind.VISUAL_QA not in disabled_kinds


def test_node_plan_uses_only_configured_scripts(tmp_path: Path) -> None:
    kinds = _step_kinds(
        Settings(),
        _task(["tests and build pass"], ["src/index.ts"]),
        _profile(
            DevelopmentProjectType.NODE,
            DevelopmentAction.NODE_TEST,
            DevelopmentAction.NODE_BUILD,
            DevelopmentAction.NODE_TYPECHECK,
        ),
        tmp_path,
    )
    assert VerificationKind.NODE_TEST in kinds
    assert VerificationKind.NODE_BUILD in kinds
    assert VerificationKind.NODE_TYPECHECK in kinds
    assert VerificationKind.NODE_LINT not in kinds


def test_python_plan_uses_supported_test_and_lint(tmp_path: Path) -> None:
    kinds = _step_kinds(
        Settings(),
        _task(["tests and lint pass"], ["app.py"]),
        _profile(
            DevelopmentProjectType.PYTHON,
            DevelopmentAction.PYTHON_TEST,
            DevelopmentAction.PYTHON_LINT,
        ),
        tmp_path,
    )
    assert VerificationKind.PYTHON_TEST in kinds
    assert VerificationKind.PYTHON_LINT in kinds
    assert VerificationKind.NODE_BUILD not in kinds


def test_source_generation_changes_without_exposing_file_contents(tmp_path: Path) -> None:
    marker = "OPENROUTER_API_KEY=do-not-leak"
    source = tmp_path / "index.html"
    source.write_text(marker)
    first = VerificationPlanService.source_generation(tmp_path)
    source.write_text("changed")
    second = VerificationPlanService.source_generation(tmp_path)
    assert first != second
    assert marker not in first
    assert len(first) == len(second) == 64


def test_step_fingerprints_invalidate_only_dependent_checks(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<main></main>")
    (tmp_path / "styles.css").write_text("main{}")
    (tmp_path / "src.js").write_text("export {}")
    plan = _contract_plan(tmp_path)
    runner = AutomaticVerificationRunner(None, settings=Settings())  # type: ignore[arg-type]
    static = next(step for step in plan.steps if step.kind == VerificationKind.STATIC_WEB)
    node = VerificationStep(
        key="node-test",
        kind=VerificationKind.NODE_TEST,
        reason="configured",
        mechanism="development.execute:NODE_TEST",
        expected_evidence="execution",
    )
    static_before = runner._step_fingerprint(static, tmp_path, plan)
    node_before = runner._step_fingerprint(node, tmp_path, plan)
    (tmp_path / "styles.css").write_text("main{color:red}")
    assert runner._step_fingerprint(static, tmp_path, plan) != static_before
    assert runner._step_fingerprint(node, tmp_path, plan) == node_before


def test_visual_evidence_references_captures_without_embedding_bytes() -> None:
    evidence = VisualQAEvidence(
        task_id=uuid4(),
        task_run_id=uuid4(),
        source_generation="a" * 64,
        captures=[
            VisualQACaptureReference(
                tool_call_id=uuid4(),
                artifact="browser/capture.png",
                sha256="b" * 64,
                width=1440,
                height=900,
            )
        ],
        reviewer_provider="mock",
        reviewer_model="free-vision",
        decision=VisualQADecision.REVISE,
        findings=[
            VisualQAFinding(
                category="OVERFLOW",
                severity=VisualFindingSeverity.MAJOR,
                finding="Pricing clips at the mobile edge.",
                recommendation="Use a fluid grid.",
                criterion_indices=[1],
            )
        ],
        criteria_addressed=[1],
        summary="Revision required.",
        recorded_at=datetime.now(UTC),
    )
    payload = evidence.model_dump_json()
    assert "capture.png" in payload
    assert "data:image" not in payload
    assert "png;base64" not in payload


def test_openrouter_visual_request_contains_images_but_no_tools() -> None:
    provider = object.__new__(OpenRouterModelProvider)
    request = ModelRequest(
        model="free-vision",
        system_prompt="judge",
        user_prompt="requirements",
        image_data_urls=("data:image/png;base64,AAAA",),
    )
    messages = provider._messages(request)
    assert messages[1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }
    assert request.tools == ()


def test_visual_qa_loads_real_bounded_capture_bytes(tmp_path: Path) -> None:
    settings = Settings(development_runner_queue_root=str(tmp_path / "queue"))
    artifact = tmp_path / "queue/browser/artifacts/capture.png"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    data = artifact.read_bytes()
    capture = VisualQACaptureReference(
        tool_call_id=uuid4(),
        artifact="browser/capture.png",
        sha256=hashlib.sha256(data).hexdigest(),
        width=390,
        height=844,
    )
    encoded = VisualQAService(None, settings)._image_data_urls([capture])  # type: ignore[arg-type]
    assert encoded[0].startswith("data:image/png;base64,")
    artifact.write_bytes(b"replaced")
    try:
        VisualQAService(None, settings)._image_data_urls([capture])  # type: ignore[arg-type]
    except ToolSystemError as exc:
        assert exc.code == "VISUAL_QA_CAPTURE_STALE"
    else:
        raise AssertionError("Changed capture must not reach visual review")
    artifact.unlink()
    try:
        VisualQAService(None, settings)._image_data_urls([capture])  # type: ignore[arg-type]
    except ToolSystemError as exc:
        assert exc.code == "VISUAL_QA_CAPTURE_MISSING"
    else:
        raise AssertionError("Missing capture must not reach visual review")


def test_visual_qa_route_rejects_paid_and_unknown_price_profiles() -> None:
    capabilities = frozenset(
        {ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT, ModelCapability.VISION}
    )
    free = ModelProfile("free", "mock", "free", EconomicTier.FREE, capabilities)
    paid = ModelProfile("paid", "mock", "paid", EconomicTier.PREMIUM, capabilities, paid=True)
    unknown = ModelProfile("unknown", "mock", "unknown", EconomicTier.CHEAP, capabilities)
    assert VisualQAService._free_profiles((paid, unknown, free)) == (free,)


def test_plan_projection_is_smaller_and_contains_no_internal_ids(tmp_path: Path) -> None:
    plan = _contract_plan(tmp_path)
    full = json.dumps(plan.model_dump(mode="json"))
    projection = json.dumps(plan.model_projection())
    assert len(projection) < len(full)
    assert str(plan.task_id) not in projection
    assert str(plan.task_run_id) not in projection


def test_failed_and_missing_planned_results_cannot_be_presented_as_passed(
    tmp_path: Path,
) -> None:
    plan = _contract_plan(tmp_path)
    failed = VerificationResult(
        step_key="static-web",
        kind=VerificationKind.STATIC_WEB,
        status=VerificationStepStatus.FAILED,
        source_generation=plan.source_generation,
        input_fingerprint="b" * 64,
        summary="missing app.js",
        recorded_at=datetime.now(UTC),
    )
    assert failed.status != VerificationStepStatus.PASSED
    assert {item.key for item in plan.steps} - {failed.step_key} == {"artifacts", "git-status"}


async def test_automatic_static_verification_is_durable_reusable_and_zero_model_call(
    client: AsyncClient, tmp_path: Path, monkeypatch
) -> None:
    fixture = await _static_runtime_fixture(client, tmp_path)
    model_calls = fixture["provider"].call_count
    async with get_session_factory()() as session:
        plan = await VerificationPlanService(session, fixture["settings"]).build(
            fixture["task_id"], fixture["run"].task_run_id
        )
        context = ToolExecutionContext(
            company_id=fixture["company_id"],
            project_id=fixture["project_id"],
            task_id=fixture["task_id"],
            task_run_id=fixture["run"].task_run_id,
            agent_id=fixture["agent_id"],
            agent_run_id=fixture["run"].id,
            execution_origin="FORGE_QA",
        )
        first_runner = FakeRunnerClient([_runner_response()])
        first = await AutomaticVerificationRunner(
            session, settings=fixture["settings"], runner=first_runner
        ).run(plan, context)
        second_runner = FakeRunnerClient([])
        second = await AutomaticVerificationRunner(
            session, settings=fixture["settings"], runner=second_runner
        ).run(plan, context)
        manifest = await CompletionManifestService(session, fixture["settings"]).build(
            fixture["task_id"], fixture["run"].task_run_id
        )
    assert all(item.status == VerificationStepStatus.PASSED for item in first.results)
    assert all(item.reused for item in second.results)
    assert len(first_runner.requests) == 1
    assert second_runner.requests == []
    assert fixture["provider"].call_count == model_calls == 4
    assert manifest.verification_plan_version == plan.version
    assert len(manifest.planned_verification) == len(plan.steps) == 3
    assert len(manifest.automatic_verification) == 3
    assert all(
        item.status == VerificationStepStatus.PASSED
        for item in manifest.automatic_verification
    )
    assert "document.body.dataset" not in json.dumps(manifest.bounded_summary())

    import app.api.routes.efficient_runtime as efficient_runtime_route
    import app.development.completion_manifest as completion_manifest_module

    monkeypatch.setattr(completion_manifest_module, "get_settings", lambda: fixture["settings"])
    monkeypatch.setattr(efficient_runtime_route, "get_settings", lambda: fixture["settings"])
    response = await client.get(f"/api/v1/tasks/{fixture['task_id']}/dev-mode")
    assert response.status_code == 200, response.text
    diagnostic = response.json()["verification"]
    assert diagnostic["plan_version"] == plan.version
    assert diagnostic["executed_checks"] == 3
    assert diagnostic["reused_results"] == 3
    assert len(diagnostic["results"]) == 3
    assert diagnostic["efficiency"]["model_calls"] == 4
    assert "document.body.dataset" not in json.dumps(diagnostic)


async def test_blocking_artifact_failure_skips_later_expensive_checks(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _static_runtime_fixture(client, tmp_path)
    (fixture["workspace"] / "app.js").unlink()
    async with get_session_factory()() as session:
        plan = await VerificationPlanService(session, fixture["settings"]).build(
            fixture["task_id"], fixture["run"].task_run_id
        )
        context = ToolExecutionContext(
            company_id=fixture["company_id"],
            project_id=fixture["project_id"],
            task_id=fixture["task_id"],
            task_run_id=fixture["run"].task_run_id,
            agent_id=fixture["agent_id"],
            agent_run_id=fixture["run"].id,
            execution_origin="FORGE_QA",
        )
        runner = FakeRunnerClient([])
        result = await AutomaticVerificationRunner(
            session, settings=fixture["settings"], runner=runner
        ).run(plan, context)
    assert result.results[0].status == VerificationStepStatus.FAILED
    assert all(item.status == VerificationStepStatus.SKIPPED for item in result.results[1:])
    assert runner.requests == []


async def test_reconciliation_adds_tooling_and_preserves_mandatory_checks(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _static_runtime_fixture(client, tmp_path)
    async with get_session_factory()() as session:
        service = VerificationPlanService(session, fixture["settings"])
        first = await service.build(fixture["task_id"], fixture["run"].task_run_id)
        (fixture["workspace"] / "package.json").write_text(
            '{"scripts":{"test":"node --test"}}', encoding="utf-8"
        )
        second = await service.build(fixture["task_id"], fixture["run"].task_run_id)
    first_keys = {item.key for item in first.steps if item.blocking}
    second_keys = {item.key for item in second.steps if item.blocking}
    assert first_keys <= second_keys
    assert VerificationKind.NODE_TEST in {item.kind for item in second.steps}
    assert second.version == first.version + 1
    assert len(second.steps) <= 32


async def test_source_mutation_reuses_unaffected_node_verifier_fingerprint(
    client: AsyncClient, tmp_path: Path
) -> None:
    fixture = await _static_runtime_fixture(client, tmp_path)
    workspace = fixture["workspace"]
    (workspace / "package.json").write_text('{"scripts":{"test":"node --test"}}', encoding="utf-8")
    (workspace / "src.js").write_text("export {}", encoding="utf-8")
    async with get_session_factory()() as session:
        service = VerificationPlanService(session, fixture["settings"])
        plan = await service.build(fixture["task_id"], fixture["run"].task_run_id)
        node_step = next(item for item in plan.steps if item.kind == VerificationKind.NODE_TEST)
        runner = AutomaticVerificationRunner(session, settings=fixture["settings"])
        before = runner._step_fingerprint(node_step, workspace, plan)
        (workspace / "styles.css").write_text("main{color:navy}", encoding="utf-8")
        after = runner._step_fingerprint(node_step, workspace, plan)
    assert before == after


def _contract_plan(workspace: Path) -> VerificationPlan:
    generation = VerificationPlanService.source_generation(workspace)
    return VerificationPlan(
        task_id=uuid4(),
        task_run_id=uuid4(),
        company_id=uuid4(),
        project_id=uuid4(),
        workspace_identity="company/project",
        version=1,
        source_generation=generation,
        profile="STATIC_WEB",
        required_deliverables=["index.html", "styles.css", "app.js"],
        steps=[
            VerificationStep(
                key="artifacts",
                kind=VerificationKind.ARTIFACTS,
                reason="deliverables",
                deliverables=["index.html", "styles.css", "app.js"],
                mechanism="forge.static.artifacts",
                expected_evidence="hashes",
            ),
            VerificationStep(
                key="static-web",
                kind=VerificationKind.STATIC_WEB,
                reason="references",
                mechanism="forge.static_web_verifier",
                dependencies=["artifacts"],
                expected_evidence="reference report",
            ),
            VerificationStep(
                key="git-status",
                kind=VerificationKind.GIT_STATUS,
                reason="review",
                mechanism="development.execute:GIT_STATUS",
                dependencies=["artifacts"],
                expected_evidence="status",
            ),
        ],
        generated_at=datetime.now(UTC),
    )


def _runner_response() -> RunnerResponse:
    return RunnerResponse(
        request_id=uuid4(),
        status=DevelopmentExecutionStatus.SUCCEEDED,
        exit_code=0,
        stdout_excerpt="clean",
        stdout_bytes=5,
        duration_ms=1,
    )


async def _static_runtime_fixture(client: AsyncClient, tmp_path: Path) -> dict[str, object]:
    company = await create_company(client, slug=f"verification-{uuid4().hex[:8]}")
    project = await create_project(client, company["id"])
    agent = await create_agent(
        client,
        company["id"],
        role="LEAD_ENGINEER",
        permissions={
            "filesystem.write": True,
            "development.execute": True,
            "git.read": True,
        },
    )
    response = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": company["id"],
            "project_id": project["id"],
            "assigned_agent_id": agent["id"],
            "type": "IMPLEMENTATION",
            "kind": "DEVELOPMENT",
            "title": "Representative frontend fixture",
            "input": {"deliverables": ["index.html", "styles.css", "app.js"]},
            "acceptance_criteria": [
                "`index.html`, `styles.css`, and `app.js` exist",
                "responsive layout",
                "visible hero",
                "three features",
                "pricing section",
                "no browser console errors",
            ],
            "max_iterations": 4,
        },
    )
    assert response.status_code == 201
    task_id = UUID(response.json()["id"])
    await transition_task(client, str(task_id), "QUEUED")
    settings = Settings(
        model_provider="mock",
        tool_workspace_root=str(tmp_path / "workspaces"),
        development_bootstrap_enabled=False,
        forge_browser_enabled=False,
    )
    provider = MockModelProvider(
        responses=[
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {
                    "path": "index.html",
                    "content": (
                        '<link rel="stylesheet" href="styles.css"><main><h1>Hero</h1>'
                        '<section id="features"></section><section id="pricing"></section>'
                        '</main><script src="app.js"></script>'
                    ),
                },
            },
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {"path": "styles.css", "content": "main{max-width:100%;}"},
            },
            {
                "type": "tool_call",
                "tool_name": "filesystem.write",
                "arguments": {"path": "app.js", "content": "document.body.dataset.ready='1'"},
            },
            {
                "type": "final",
                "result": {
                    "status": "completed",
                    "summary": "Implementation is ready for Forge verification.",
                    "output": {"artifacts": [], "details": [], "execution_claims": []},
                    "notes": [],
                },
            },
        ]
    )
    async with get_session_factory()() as session:
        run = await AgentRuntime(session, settings=settings, provider=provider).execute(
            task_id, defer_review=True
        )
    workspace = WorkspaceManager(settings.tool_workspace_root).project_workspace(
        UUID(company["id"]), UUID(project["id"])
    )
    return {
        "task_id": task_id,
        "company_id": UUID(company["id"]),
        "project_id": UUID(project["id"]),
        "agent_id": UUID(agent["id"]),
        "settings": settings,
        "provider": provider,
        "run": run,
        "workspace": workspace,
    }
