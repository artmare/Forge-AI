"""Read-only live browser-runner diagnostic; does not mark a Mission as verified.

Run inside forge-api, with a completed project ID as argv[1]. Requires the
Compose browser profile. Keeps the application's browser feature flag unchanged.
"""

import asyncio
import json
import sys
from urllib.request import urlopen
from uuid import UUID

from app.core.config import get_settings
from app.tool_system.browser import BrowserInput, BrowserTools
from app.tool_system.contracts import ToolExecutionContext


def get(path):
    with urlopen(f"http://localhost:8000/api/v1/{path}") as response:
        return json.load(response)


async def main():
    project = get(f"projects/{sys.argv[1]}")
    node = get(f"projects/{sys.argv[1]}/graph")["nodes"][0]
    call = get(f"tasks/{node['id']}/tool-calls")[0]
    context = ToolExecutionContext(
        company_id=UUID(project["company_id"]), project_id=UUID(project["id"]),
        task_id=UUID(node["id"]), task_run_id=UUID(call["task_run_id"]),
        agent_id=UUID(call["agent_id"]), agent_run_id=UUID(call["agent_run_id"]),
    )
    result = await BrowserTools(get_settings()).capture(BrowserInput(path="dist/index.html"), context)
    assert result.rendered
    assert result.title == "Forge Todo demo"
    assert result.console_errors == [], result.console_errors
    print(result.model_dump_json(indent=2))


asyncio.run(main())
