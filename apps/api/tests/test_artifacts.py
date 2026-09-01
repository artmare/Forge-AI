import os
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.tool_system.workspace import WorkspaceManager
from tests.helpers import create_company, create_project


async def artifact_project(client: AsyncClient) -> tuple[dict[str, object], Path]:
    company = await create_company(client, slug="artifact-viewer-company")
    project = await create_project(client, str(company["id"]))
    manager = WorkspaceManager(get_settings().tool_workspace_root)
    workspace = manager.project_workspace(company["id"], project["id"])  # type: ignore[arg-type]
    return project, workspace


async def test_artifact_list_and_valid_utf8_reads(client: AsyncClient) -> None:
    project, workspace = await artifact_project(client)
    (workspace / "research.md").write_text("# Research\n\nForge works.", encoding="utf-8")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "data.json").write_text('{"ready":true}', encoding="utf-8")
    (workspace / "payload.bin").write_bytes(b"\x00\x01")
    (workspace / ".env").write_text("SECRET=hidden", encoding="utf-8")

    listing = await client.get(f"/api/v1/projects/{project['id']}/artifacts")
    research = await client.get(
        f"/api/v1/projects/{project['id']}/artifacts/content",
        params={"path": "research.md"},
    )
    nested = await client.get(
        f"/api/v1/projects/{project['id']}/artifacts/content",
        params={"path": "nested/data.json"},
    )

    assert listing.status_code == 200
    by_path = {artifact["path"]: artifact for artifact in listing.json()}
    assert set(by_path) == {"nested/data.json", "payload.bin", "research.md"}
    assert by_path["research.md"]["file_type"] == "Markdown"
    assert by_path["research.md"]["previewable"] is True
    assert by_path["payload.bin"]["previewable"] is False
    assert all("workspaces" not in artifact["path"] for artifact in by_path.values())
    assert research.status_code == 200
    assert research.json()["content"] == "# Research\n\nForge works."
    assert research.json()["path"] == "research.md"
    assert nested.status_code == 200
    assert nested.json()["content"] == '{"ready":true}'


@pytest.mark.parametrize(
    "attack",
    [
        "../secret.txt",
        "nested/../../secret.txt",
        "/etc/passwd",
        r"C:\secret.txt",
        "research.md\x00.txt",
    ],
)
async def test_artifact_read_rejects_unsafe_paths(client: AsyncClient, attack: str) -> None:
    project, _workspace = await artifact_project(client)
    response = await client.get(
        f"/api/v1/projects/{project['id']}/artifacts/content",
        params={"path": attack},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ARTIFACT_PATH_REJECTED"


async def test_artifact_read_rejects_symlink_directory_and_missing_file(
    client: AsyncClient, tmp_path: Path
) -> None:
    project, workspace = await artifact_project(client)
    (workspace / "folder").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        os.symlink(outside, workspace / "linked.txt")
    except OSError:
        pytest.skip("Symlink creation is unavailable")

    symlink = await client.get(
        f"/api/v1/projects/{project['id']}/artifacts/content",
        params={"path": "linked.txt"},
    )
    directory = await client.get(
        f"/api/v1/projects/{project['id']}/artifacts/content",
        params={"path": "folder"},
    )
    missing = await client.get(
        f"/api/v1/projects/{project['id']}/artifacts/content",
        params={"path": "missing.txt"},
    )

    assert symlink.status_code == 400
    assert symlink.json()["error"]["code"] == "ARTIFACT_PATH_REJECTED"
    assert directory.status_code == 400
    assert directory.json()["error"]["code"] == "ARTIFACT_NOT_FILE"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"


async def test_artifact_read_rejects_oversized_invalid_utf8_and_hidden(
    client: AsyncClient,
) -> None:
    project, workspace = await artifact_project(client)
    limit = get_settings().tool_file_read_max_bytes
    (workspace / "large.txt").write_bytes(b"x" * (limit + 1))
    (workspace / "invalid.txt").write_bytes(b"\xff\xfe")
    (workspace / ".env").write_text("SECRET=hidden", encoding="utf-8")

    oversized = await client.get(
        f"/api/v1/projects/{project['id']}/artifacts/content",
        params={"path": "large.txt"},
    )
    invalid = await client.get(
        f"/api/v1/projects/{project['id']}/artifacts/content",
        params={"path": "invalid.txt"},
    )
    hidden = await client.get(
        f"/api/v1/projects/{project['id']}/artifacts/content",
        params={"path": ".env"},
    )

    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "ARTIFACT_TOO_LARGE"
    assert invalid.status_code == 415
    assert invalid.json()["error"]["code"] == "ARTIFACT_NOT_UTF8"
    assert hidden.status_code == 403
    assert hidden.json()["error"]["code"] == "ARTIFACT_SENSITIVE_PATH"


async def test_artifact_read_is_project_scoped(client: AsyncClient) -> None:
    company = await create_company(client, slug="artifact-scope-company")
    first = await create_project(client, str(company["id"]))
    second = await create_project(client, str(company["id"]))
    manager = WorkspaceManager(get_settings().tool_workspace_root)
    first_workspace = manager.project_workspace(  # type: ignore[arg-type]
        company["id"], first["id"]
    )
    (first_workspace / "research.md").write_text("first project", encoding="utf-8")

    response = await client.get(
        f"/api/v1/projects/{second['id']}/artifacts/content",
        params={"path": "research.md"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
