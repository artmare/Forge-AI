from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.domain.models import ProductQAResult, Task, TaskRun
from app.tool_system.workspace import WorkspaceManager

VIEWPORT_CONTRACT = [
    {"name": "desktop", "width": 1440, "height": 900},
    {"name": "tablet", "width": 768, "height": 1024},
    {"name": "narrow", "width": 390, "height": 844},
]


class ProductQAService:
    """Deterministic static product checks; it never claims rendered/browser evidence."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.workspaces = WorkspaceManager(self.settings.tool_workspace_root)

    async def evaluate(
        self,
        task: Task,
        task_run: TaskRun,
        *,
        functional_passed: bool,
    ) -> ProductQAResult | None:
        if not self.settings.product_qa_enabled or task.project_id is None:
            return None
        workspace = self.workspaces.existing_project_workspace(task.company_id, task.project_id)
        if workspace is None:
            return None
        sources = self._source_files(workspace)
        if not self._is_frontend(sources):
            return None
        issues = self.inspect_sources(sources)
        blocking = [item for item in issues if item["severity"] in {"BLOCKING", "MAJOR"}]
        dimensions = self._dimensions(issues, functional_passed)
        decision = "FAIL" if blocking else "PASS"
        await self.session.execute(
            delete(ProductQAResult).where(
                ProductQAResult.task_id == task.id,
                ProductQAResult.iteration == task.iteration,
            )
        )
        result = ProductQAResult(
            project_id=task.project_id,
            task_id=task.id,
            task_run_id=task_run.id,
            iteration=task.iteration,
            decision=decision,
            dimensions=dimensions,
            issues=issues,
            viewport_contract=VIEWPORT_CONTRACT,
            evidence={
                "mode": "DETERMINISTIC_STATIC_ANALYSIS",
                "files_inspected": sorted(sources),
                "rendered_browser_check": False,
                "note": (
                    "No browser or screenshot capability was used. Render-dependent findings "
                    "remain UNVERIFIED unless supported by deterministic source evidence."
                ),
            },
            screenshot_references=[],
        )
        self.session.add(result)
        await self.session.flush()
        return result

    @classmethod
    def inspect_sources(cls, sources: dict[str, str]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        html = "\n".join(value for path, value in sources.items() if path.endswith(".html"))
        css_sources = {path: value for path, value in sources.items() if path.endswith(".css")}
        for path, css in css_sources.items():
            for match in re.finditer(r"(?<!max-)width\s*:\s*(\d{3,4})px", css, re.I):
                width = int(match.group(1))
                nearby = css[max(0, match.start() - 100) : match.end() + 150]
                if width >= 390 and not re.search(r"max-width\s*:\s*100%", nearby, re.I):
                    issues.append(
                        cls._issue(
                            "HORIZONTAL_OVERFLOW",
                            "RESPONSIVENESS",
                            "MAJOR",
                            path,
                            f"Fixed width {width}px can exceed the 390px narrow viewport.",
                            "Use fluid sizing with max-width: 100% and verify the narrow viewport.",
                            "Source rule and the 390x844 viewport contract.",
                        )
                    )
            if re.search(
                r"(?:html|body)[^{]*\{[^}]*overflow-x\s*:\s*(auto|scroll)", css, re.I | re.S
            ):
                issues.append(
                    cls._issue(
                        "HORIZONTAL_OVERFLOW",
                        "RESPONSIVENESS",
                        "MAJOR",
                        path,
                        "Root-level horizontal scrolling permits content to escape the viewport.",
                        "Remove the overflow source instead of masking it at the root.",
                        "Deterministic CSS selector inspection.",
                    )
                )
            for match in re.finditer(r"font-size\s*:\s*(\d+(?:\.\d+)?)px", css, re.I):
                if float(match.group(1)) < 12:
                    issues.append(
                        cls._issue(
                            "TINY_TEXT",
                            "ACCESSIBILITY",
                            "MINOR",
                            path,
                            f"Text size {match.group(1)}px is below the 12px product floor.",
                            "Use a readable text token and preserve zoom/reflow behavior.",
                            "Deterministic CSS declaration inspection.",
                        )
                    )
        if html:
            visible_inputs = re.findall(r"<input\b(?![^>]*type=[\"']hidden[\"'])[^>]*>", html, re.I)
            labels = set(re.findall(r"<label[^>]*for=[\"']([^\"']+)", html, re.I))
            for raw in visible_inputs:
                identifier = re.search(r"id=[\"']([^\"']+)", raw, re.I)
                if not identifier or identifier.group(1) not in labels:
                    issues.append(
                        cls._issue(
                            "MISSING_FORM_LABEL",
                            "ACCESSIBILITY",
                            "MAJOR",
                            cls._first_html_path(sources),
                            "A visible input has no deterministic associated label evidence.",
                            "Associate a visible label with the control using for/id.",
                            "Static HTML label/control association check.",
                        )
                    )
            if not re.search(r"<h[1-3]\b", html, re.I):
                issues.append(
                    cls._issue(
                        "VISUAL_HIERARCHY",
                        "VISUAL",
                        "MAJOR",
                        cls._first_html_path(sources),
                        "No primary or sectional heading is present in the user-facing HTML.",
                        "Add a clear heading hierarchy that communicates the primary action.",
                        "Static semantic-heading inspection; rendered appearance is unverified.",
                    )
                )
        return cls._deduplicate(issues)

    @staticmethod
    def _source_files(workspace: Path) -> dict[str, str]:
        sources: dict[str, str] = {}
        allowed = {".html", ".css", ".js", ".mjs", ".jsx", ".ts", ".tsx", ".json"}
        for path in workspace.rglob("*"):
            if not path.is_file() or path.is_symlink() or path.suffix.lower() not in allowed:
                continue
            relative = path.relative_to(workspace)
            if any(part in {".git", "node_modules", "dist", "build"} for part in relative.parts):
                continue
            if path.stat().st_size > 262_144:
                continue
            try:
                sources[relative.as_posix()] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
        return sources

    @staticmethod
    def _is_frontend(sources: dict[str, str]) -> bool:
        return any(path.endswith((".html", ".css", ".jsx", ".tsx")) for path in sources)

    @staticmethod
    def _issue(
        category: str,
        dimension: str,
        severity: str,
        file: str,
        description: str,
        suggested_fix: str,
        evidence: str,
    ) -> dict[str, Any]:
        return {
            "category": category,
            "dimension": dimension,
            "severity": severity,
            "file": file,
            "description": description,
            "suggested_fix": suggested_fix,
            "evidence": evidence,
        }

    @staticmethod
    def _first_html_path(sources: dict[str, str]) -> str:
        return next((path for path in sorted(sources) if path.endswith(".html")), "HTML source")

    @staticmethod
    def _deduplicate(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[tuple[str, str, str], dict[str, Any]] = {}
        for issue in issues:
            key = (issue["category"], issue["file"], issue["description"])
            unique[key] = issue
        return list(unique.values())

    @staticmethod
    def _dimensions(issues: list[dict[str, Any]], functional_passed: bool) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for dimension in (
            "FUNCTIONAL",
            "TESTS",
            "ACCESSIBILITY",
            "RESPONSIVENESS",
            "VISUAL",
            "USABILITY",
            "SECURITY",
        ):
            related = [item for item in issues if item["dimension"] == dimension]
            if related:
                status = "FAIL"
                evidence = f"{len(related)} deterministic source issue(s) recorded."
            elif dimension in {"FUNCTIONAL", "TESTS"}:
                status = "PASS" if functional_passed else "FAIL"
                evidence = "Backed by the deterministic development QA result."
            elif dimension in {"VISUAL", "USABILITY"}:
                status = "UNVERIFIED"
                evidence = "Rendered/browser evidence is unavailable in Phase 09.5."
            else:
                status = "PASS"
                evidence = "No issue was found by the bounded deterministic source checks."
            records.append({"dimension": dimension, "status": status, "evidence": evidence})
        return records
