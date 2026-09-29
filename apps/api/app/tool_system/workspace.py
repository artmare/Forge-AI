import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import UUID

from app.tool_system.errors import ToolSystemError


class WorkspaceManager:
    _RESERVED_MUTATION_COMPONENTS = frozenset({".git"})
    _RESERVED_MUTATION_FILES = frozenset({".gitattributes", ".gitmodules"})

    def __init__(self, root: str | Path) -> None:
        self.configured_root = Path(root)

    def project_workspace(self, company_id: UUID, project_id: UUID) -> Path:
        self._reject_symlink(self.configured_root)
        root = self.configured_root.resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        workspace = root / str(company_id) / str(project_id)
        self._reject_symlink(workspace.parent)
        self._reject_symlink(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        resolved = workspace.resolve(strict=True)
        self._require_within(root, resolved)
        return resolved

    def existing_project_workspace(self, company_id: UUID, project_id: UUID) -> Path | None:
        """Resolve an existing UUID-scoped workspace without creating directories."""
        self._reject_symlink(self.configured_root)
        root = self.configured_root.resolve(strict=False)
        if not root.is_dir():
            return None
        company = root / str(company_id)
        workspace = company / str(project_id)
        if not workspace.exists() and not workspace.is_symlink():
            return None
        self._reject_symlink(company)
        self._reject_symlink(workspace)
        resolved = workspace.resolve(strict=True)
        self._require_within(root, resolved)
        return resolved

    def delete_project_workspace(self, company_id: UUID, project_id: UUID) -> bool:
        """Remove exactly one UUID-scoped Project workspace without following symlinks."""
        self._reject_symlink(self.configured_root)
        root = self.configured_root.resolve(strict=False)
        if not root.exists():
            return False
        company = root / str(company_id)
        workspace = company / str(project_id)
        if not workspace.exists() and not workspace.is_symlink():
            return False
        self._reject_symlink(company)
        self._reject_symlink(workspace)
        resolved_company = company.resolve(strict=True)
        resolved_workspace = workspace.resolve(strict=True)
        self._require_within(root, resolved_company)
        self._require_within(resolved_company, resolved_workspace)
        if resolved_workspace != resolved_company / str(project_id):
            raise ToolSystemError(
                "PATH_OUTSIDE_WORKSPACE", "Project workspace target is not the expected UUID path"
            )
        if (resolved_workspace / ".git").is_file():
            raise ToolSystemError(
                "WORKTREE_CLEANUP_REQUIRED", "Use checkpoint-aware self-development cleanup"
            )
        shutil.rmtree(resolved_workspace)
        try:
            resolved_company.rmdir()
        except OSError:
            pass
        return True

    def resolve(
        self,
        company_id: UUID,
        project_id: UUID,
        requested_path: str,
        *,
        must_exist: bool,
    ) -> tuple[Path, Path]:
        self._validate_relative_path(requested_path)
        workspace = self.project_workspace(company_id, project_id)
        relative = Path(PurePosixPath(requested_path.replace("\\", "/")))
        candidate = workspace.joinpath(relative)
        self._reject_symlink_components(workspace, candidate)
        try:
            resolved = candidate.resolve(strict=must_exist)
        except FileNotFoundError as exc:
            raise ToolSystemError("FILE_NOT_FOUND", "Requested path does not exist") from exc
        self._require_within(workspace, resolved)
        return workspace, resolved

    @classmethod
    def validate_mutation_path(cls, requested_path: str) -> None:
        """Reject model-controlled writes to Forge-owned Git administration state."""
        cls._validate_relative_path(requested_path)
        parts = PurePosixPath(requested_path.replace("\\", "/")).parts
        normalized = tuple(part.rstrip(" .").casefold() for part in parts)
        if any(part in cls._RESERVED_MUTATION_COMPONENTS for part in normalized) or (
            normalized and normalized[-1] in cls._RESERVED_MUTATION_FILES
        ):
            raise ToolSystemError(
                "RESERVED_WORKSPACE_PATH",
                "Model-controlled writes to Git administration paths are not allowed",
            )

    @staticmethod
    def relative(workspace: Path, path: Path) -> str:
        value = path.relative_to(workspace).as_posix()
        return value or "."

    @staticmethod
    def ensure_regular_file(path: Path) -> None:
        try:
            mode = path.stat(follow_symlinks=False).st_mode
        except FileNotFoundError as exc:
            raise ToolSystemError("FILE_NOT_FOUND", "Requested file does not exist") from exc
        if not stat.S_ISREG(mode):
            raise ToolSystemError("UNSUPPORTED_FILE_TYPE", "Requested path is not a regular file")

    @staticmethod
    def atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            WorkspaceManager._reject_symlink(path)
            WorkspaceManager.ensure_regular_file(path)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, path)
        finally:
            if temporary_name is not None and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _validate_relative_path(requested_path: str) -> None:
        if not requested_path or "\x00" in requested_path:
            raise ToolSystemError("INVALID_PATH", "Path must be a non-empty relative path")
        posix = PurePosixPath(requested_path.replace("\\", "/"))
        windows = PureWindowsPath(requested_path)
        if posix.is_absolute() or windows.is_absolute() or windows.drive:
            raise ToolSystemError("PATH_OUTSIDE_WORKSPACE", "Absolute paths are not allowed")
        if ".." in posix.parts or ".." in windows.parts:
            raise ToolSystemError("PATH_OUTSIDE_WORKSPACE", "Path traversal is not allowed")

    @staticmethod
    def _require_within(workspace: Path, candidate: Path) -> None:
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise ToolSystemError(
                "PATH_OUTSIDE_WORKSPACE", "Requested path is outside the project workspace"
            ) from exc

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise ToolSystemError("SYMLINK_NOT_ALLOWED", "Symbolic links are not allowed")

    @staticmethod
    def _reject_symlink_components(workspace: Path, candidate: Path) -> None:
        current = workspace
        WorkspaceManager._reject_symlink(current)
        try:
            relative = candidate.relative_to(workspace)
        except ValueError as exc:
            raise ToolSystemError(
                "PATH_OUTSIDE_WORKSPACE", "Requested path is outside the project workspace"
            ) from exc
        for part in relative.parts:
            current = current / part
            if current.exists() or current.is_symlink():
                WorkspaceManager._reject_symlink(current)
