"""Small deterministic verifier for vanilla HTML/CSS/JavaScript deliverables."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.development.completion_contracts import (
    ArtifactEvidence,
    ManifestEvidenceStatus,
    VerificationEvidence,
)


class _StaticReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "script" and values.get("src"):
            self.references.append(str(values["src"]))
        if tag.lower() == "link" and values.get("href"):
            rel = str(values.get("rel", "")).lower().split()
            if "stylesheet" in rel:
                self.references.append(str(values["href"]))


class StaticWebVerifier:
    async def verify(
        self, workspace: Path, artifacts: list[ArtifactEvidence]
    ) -> list[VerificationEvidence]:
        html = [item for item in artifacts if Path(item.path).suffix.lower() in {".html", ".htm"}]
        output: list[VerificationEvidence] = []
        for artifact in html[:20]:
            if not artifact.exists:
                continue
            candidate = workspace / artifact.path
            try:
                size = candidate.stat(follow_symlinks=False).st_size
                if size <= 0 or size > 2_000_000:
                    raise ValueError("HTML is empty or exceeds the static verification limit")
                source = await asyncio.to_thread(candidate.read_text, encoding="utf-8")
                parser = _StaticReferences()
                parser.feed(source)
                missing = self._missing_local_references(workspace, candidate, parser.references)
            except (OSError, UnicodeError, ValueError) as exc:
                output.append(
                    VerificationEvidence(
                        kind="STATIC_WEB",
                        reference=artifact.path,
                        status=ManifestEvidenceStatus.FAILED,
                        summary=f"Static HTML verification failed: {str(exc)[:400]}",
                        origin="FORGE_STATIC_WEB",
                        task_run_id=artifact.originating_task_run_id,
                        recorded_at=datetime.now(UTC),
                    )
                )
                continue
            if missing:
                summary = "Missing statically referenced local assets: " + ", ".join(missing[:20])
                status = ManifestEvidenceStatus.FAILED
            else:
                summary = (
                    f"Non-empty HTML and {len(parser.references)} statically resolvable local "
                    "stylesheet/script reference(s) were checked."
                )
                status = ManifestEvidenceStatus.VERIFIED
            output.append(
                VerificationEvidence(
                    kind="STATIC_WEB",
                    reference=artifact.path,
                    status=status,
                    summary=summary[:500],
                    origin="FORGE_STATIC_WEB",
                    task_run_id=artifact.originating_task_run_id,
                    recorded_at=datetime.now(UTC),
                )
            )
        return output

    @staticmethod
    def _missing_local_references(
        workspace: Path, html_path: Path, references: list[str]
    ) -> list[str]:
        missing: list[str] = []
        for raw in references[:100]:
            parsed = urlsplit(raw)
            if parsed.scheme or parsed.netloc or raw.startswith(("//", "data:", "#")):
                continue
            value = unquote(parsed.path).replace("\\", "/")
            if not value:
                continue
            target = (
                workspace / value.lstrip("/")
                if value.startswith("/")
                else html_path.parent / value
            )
            try:
                relative_target = target.relative_to(workspace)
                current = workspace
                for part in relative_target.parts:
                    current = current / part
                    if current.is_symlink():
                        raise OSError
                resolved = target.resolve(strict=True)
                resolved.relative_to(workspace)
                if not resolved.is_file():
                    raise OSError
            except (OSError, ValueError):
                missing.append(raw[:512])
        return missing
