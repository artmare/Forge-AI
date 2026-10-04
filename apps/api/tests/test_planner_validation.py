import copy
import json
from uuid import UUID

from httpx import AsyncClient

from app.agent_runtime.contracts import ModelRequest, ModelResponse
from app.agent_runtime.providers import MockModelProvider
from app.core.config import Settings
from app.domain.enums import PlanningRunStatus
from app.infrastructure.database import get_session_factory
from app.planning.acceptance import (
    AcceptanceMechanism,
    analyze_acceptance_criterion,
)
from app.planning.contracts import PlanProposal
from app.planning.mission_planner import MissionPlanner
from app.planning.validator import PlanValidator

VIEWPORT_CRITERION = (
    "Viewport criteria validated for 1440x900, 768x1024, and 390x844 with "
    "recorded verification evidence"
)


def planner_proposal(
    criterion: str = VIEWPORT_CRITERION, *, browser_permission: bool = False
) -> dict:
    return {
        "project": {
            "name": "PennyPilot Landing Page",
            "description": "A bounded static frontend fixture.",
        },
        "agents": [
            {
                "key": "developer",
                "name": "Developer",
                "role": "DEVELOPER",
                "description": "Implements the static frontend.",
                "model_alias": "default",
                "requested_permissions": {
                    "filesystem.list": True,
                    "filesystem.read": True,
                    "filesystem.write": True,
                    "development.execute": True,
                    "development.install_dependencies": False,
                    "git.read": True,
                    "git.write": False,
                    "browser.capture": False,
                },
            },
            {
                "key": "qa",
                "name": "QA",
                "role": "QA",
                "description": "Verifies authoritative evidence.",
                "model_alias": "default",
                "requested_permissions": {
                    "filesystem.list": True,
                    "filesystem.read": True,
                    "filesystem.write": True,
                    "development.execute": True,
                    "development.install_dependencies": False,
                    "git.read": True,
                    "git.write": False,
                    "browser.capture": browser_permission,
                },
            },
        ],
        "tasks": [
            {
                "key": "implement_landing_page",
                "title": "Implement landing page",
                "description": "Create the required static files.",
                "assigned_agent_key": "developer",
                "priority": "NORMAL",
                "kind": "DEVELOPMENT",
                "input": {
                    "objective": "Create the static landing page.",
                    "deliverables": ["index.html", "styles.css", "app.js"],
                    "source_files": [],
                    "constraints": ["No external framework"],
                },
                "acceptance_criteria": [
                    "index.html, styles.css, and app.js exist and are correctly linked"
                ],
                "max_iterations": 4,
            },
            {
                "key": "qa_verification",
                "title": "Verify landing page",
                "description": "Record bounded QA evidence.",
                "assigned_agent_key": "qa",
                "priority": "NORMAL",
                "kind": "QA",
                "input": {
                    "objective": "Verify the static landing page.",
                    "deliverables": ["qa_report.md"],
                    "source_files": ["index.html", "styles.css", "app.js"],
                    "constraints": [],
                },
                "acceptance_criteria": [
                    "File qa_report.md exists and documents validation findings",
                    criterion,
                ],
                "max_iterations": 4,
            },
        ],
        "dependencies": [
            {"task": "qa_verification", "depends_on": "implement_landing_page"}
        ],
    }


class ScriptedPlannerProvider(MockModelProvider):
    def __init__(self, outputs: list[object]) -> None:
        super().__init__()
        self.outputs = outputs

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.call_count += 1
        self.requests.append(request)
        return ModelResponse(
            output=self.outputs[min(self.call_count - 1, len(self.outputs) - 1)],
            usage=self.usage,
            provider=self.name,
            model=request.model,
        )


async def create_planner_mission(
    client: AsyncClient, *, browser_access: bool = False
) -> dict:
    response = await client.post(
        "/api/v1/missions",
        json={
            "title": "Planner validation fixture",
            "goal": "Create and verify a bounded static landing page.",
            "constraints": {
                "max_tasks": 8,
                "max_agents": 3,
                "browser_access": browser_access,
                "internet_access": False,
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_acceptance_classifier_recognizes_authoritative_targets_without_accepting_vagueness():
    viewport = analyze_acceptance_criterion(VIEWPORT_CRITERION)
    assert viewport.observable
    assert viewport.browser_required
    assert viewport.mechanisms == {
        AcceptanceMechanism.BROWSER,
        AcceptanceMechanism.VERIFICATION_EVIDENCE,
    }

    artifact = analyze_acceptance_criterion(
        "File qa_report.md exists and documents the validation findings"
    )
    assert AcceptanceMechanism.ARTIFACT in artifact.mechanisms

    command = analyze_acceptance_criterion("npm test exits successfully with status 0")
    assert AcceptanceMechanism.COMMAND in command.mechanisms

    vague = analyze_acceptance_criterion("Make sure the website looks good")
    assert not vague.observable
    vague_result = PlanValidator(Settings()).validate(
        PlanProposal.model_validate(
            planner_proposal("Make sure the website looks good")
        )
    )
    vague_error = next(
        error
        for error in vague_result.errors
        if error.path == "tasks.1.acceptance_criteria.1"
    )
    assert vague_error.code == "UNVERIFIABLE_ACCEPTANCE_CRITERION"

    subjective = analyze_acceptance_criterion(
        "Visual quality looks good at the 1440x900 viewport with recorded evidence"
    )
    assert subjective.requires_judgment
    assert subjective.browser_required


def test_browser_acceptance_obeys_owner_policy_and_available_qa_permission() -> None:
    disabled = PlanValidator(Settings(forge_browser_enabled=True))
    disabled_result = disabled.validate(
        PlanProposal.model_validate(planner_proposal()),
        policy=disabled.policy_for({"browser_access": False}),
    )
    viewport_error = next(
        error
        for error in disabled_result.errors
        if error.path == "tasks.1.acceptance_criteria.1"
    )
    assert viewport_error.code == "CAPABILITY_POLICY_CONFLICT"
    assert "UNVERIFIABLE_ACCEPTANCE_CRITERION" not in {
        error.code for error in disabled_result.errors
    }
    escalated = disabled.validate(
        PlanProposal.model_validate(planner_proposal(browser_permission=True)),
        policy=disabled.policy_for({"browser_access": False}),
    )
    assert any(
        error.code == "CAPABILITY_POLICY_CONFLICT"
        and error.path == "agents.1.requested_permissions.browser.capture"
        for error in escalated.errors
    )

    enabled = PlanValidator(Settings(forge_browser_enabled=True))
    enabled_result = enabled.validate(
        PlanProposal.model_validate(planner_proposal(browser_permission=True)),
        policy=enabled.policy_for({"browser_access": True}),
    )
    assert enabled_result.valid, enabled_result.errors

    unavailable = PlanValidator(Settings(forge_browser_enabled=False))
    unavailable_result = unavailable.validate(
        PlanProposal.model_validate(planner_proposal(browser_permission=True)),
        policy=unavailable.policy_for({"browser_access": True}),
    )
    assert "CAPABILITY_POLICY_CONFLICT" in {
        error.code for error in unavailable_result.errors
    }


def test_artifact_and_command_acceptance_remain_canonically_valid() -> None:
    validator = PlanValidator(Settings())
    for criterion in (
        "File qa_report.md exists and documents validation findings",
        "npm test exits successfully with status 0 and records its result",
    ):
        result = validator.validate(
            PlanProposal.model_validate(planner_proposal(criterion))
        )
        assert result.valid, result.errors


async def test_canonical_repair_receives_exact_policy_error_and_preserves_topology(
    client: AsyncClient,
) -> None:
    mission = await create_planner_mission(client, browser_access=False)
    initial = planner_proposal()
    repaired = copy.deepcopy(initial)
    repaired["tasks"][1]["acceptance_criteria"][1] = (
        "qa_report.md records static verification results for index.html, styles.css, and app.js"
    )
    provider = ScriptedPlannerProvider([initial, repaired])
    settings = Settings(planner_provider_max_attempts=3, planner_retry_base_seconds=0)

    async with get_session_factory()() as session:
        run = await MissionPlanner(session, settings=settings, provider=provider).plan(
            UUID(mission["id"])
        )

    assert run.status == PlanningRunStatus.SUCCEEDED
    assert provider.call_count == 2
    repair = provider.requests[1]
    payload = json.loads(repair.user_prompt)
    assert payload["validation_errors"] == [
        {
            "code": "CAPABILITY_POLICY_CONFLICT",
            "message": (
                "Acceptance criterion requires browser or rendered viewport evidence, but "
                "Mission policy forbids browser access. Use a permitted verification mechanism "
                "or leave the judgment to human review."
            ),
            "path": "tasks.1.acceptance_criteria.1",
        }
    ]
    assert payload["prior_candidate"] == initial
    assert payload["effective_policy"]["runtime_boundaries"]["browser"] is False
    assert "unchanged rejected value" in payload["instruction"].lower()
    assert run.proposal is not None
    assert run.proposal["project"] == initial["project"]
    assert [item["key"] for item in run.proposal["agents"]] == ["developer", "qa"]
    assert [item["key"] for item in run.proposal["tasks"]] == [
        "implement_landing_page",
        "qa_verification",
    ]
    assert run.proposal["dependencies"] == initial["dependencies"]
    assert [item["outcome"] for item in run.retry_history] == [
        "SUCCEEDED",
        "REPAIR_SUCCEEDED",
    ]


async def test_unchanged_canonical_repair_is_bounded_and_preserves_precise_error(
    client: AsyncClient,
) -> None:
    mission = await create_planner_mission(client, browser_access=False)
    invalid = planner_proposal()
    provider = ScriptedPlannerProvider([invalid, invalid])
    settings = Settings(planner_provider_max_attempts=3, planner_retry_base_seconds=0)

    async with get_session_factory()() as session:
        run = await MissionPlanner(session, settings=settings, provider=provider).plan(
            UUID(mission["id"])
        )

    assert run.status == PlanningRunStatus.INVALID
    assert provider.call_count == 2
    assert run.provider_attempts == 2
    assert run.error is not None
    assert run.error["repair_attempted"] is True
    assert run.error["repair_eligible"] is False
    assert run.error["repair_attempts"] == 1
    assert run.error["max_repair_attempts"] == 1
    assert run.error["provider_attempts_remaining"] == 1
    assert run.error["validation_errors"][0] == {
        "code": "CAPABILITY_POLICY_CONFLICT",
        "message": (
            "Acceptance criterion requires browser or rendered viewport evidence, but Mission "
            "policy forbids browser access. Use a permitted verification mechanism or leave the "
            "judgment to human review."
        ),
        "path": "tasks.1.acceptance_criteria.1",
    }
    assert [item["outcome"] for item in run.retry_history] == [
        "SUCCEEDED",
        "REPAIR_FAILED",
    ]

    response = await client.get(f"/api/v1/missions/{mission['id']}/plan")
    assert response.status_code == 200, response.text
    error = response.json()["planning_run"]["error"]
    assert error["validation_errors"] == run.error["validation_errors"]


async def test_targeted_repair_rejects_unrelated_topology_mutation(
    client: AsyncClient,
) -> None:
    mission = await create_planner_mission(client, browser_access=False)
    initial = planner_proposal()
    repaired = copy.deepcopy(initial)
    repaired["tasks"][1]["acceptance_criteria"][1] = (
        "qa_report.md records static verification results for index.html, styles.css, and app.js"
    )
    repaired["dependencies"] = []
    provider = ScriptedPlannerProvider([initial, repaired])

    async with get_session_factory()() as session:
        run = await MissionPlanner(session, provider=provider).plan(UUID(mission["id"]))

    assert run.status == PlanningRunStatus.INVALID
    assert provider.call_count == 2
    scope_error = next(
        item
        for item in run.validation_result["errors"]
        if item["code"] == "REPAIR_SCOPE_VIOLATION"
    )
    assert scope_error["path"] == "dependencies"


async def test_valid_initial_plan_adds_no_repair_call(client: AsyncClient) -> None:
    mission = await create_planner_mission(client, browser_access=False)
    valid = planner_proposal(
        "qa_report.md records static verification results for index.html, styles.css, and app.js"
    )
    provider = ScriptedPlannerProvider([valid])

    async with get_session_factory()() as session:
        run = await MissionPlanner(session, provider=provider).plan(UUID(mission["id"]))

    assert run.status == PlanningRunStatus.SUCCEEDED
    assert provider.call_count == 1
    assert run.provider_attempts == 1
    assert [item["outcome"] for item in run.retry_history] == ["SUCCEEDED"]
    prompt = json.loads(provider.requests[0].user_prompt.split("\n", 1)[1])
    assert prompt["effective_policy"]["runtime_boundaries"]["browser"] is False
    assert prompt["effective_policy"]["limits"] == {
        "max_agents": 3,
        "max_tasks": 8,
        "max_dependencies": Settings().mission_max_dependencies,
    }
