"""FileTool: sandboxed file CRUD.

Safety rules enforced here (the Policy Engine adds more in Phase 2):
  * every path is resolved and must stay inside the sandbox root; `..`,
    absolute paths elsewhere and symlinks that point outside are denied
  * delete never unlinks: it moves the item into <root>/.trash and can be
    restored
  * .trash is not reachable through normal file operations
  * reads and writes are size-capped; file contents are returned as data only
"""

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aios.tools.base import ToolResult

TRASH_DIR = ".trash"


class FileToolError(Exception):
    code = "error"


class PathDenied(FileToolError):
    code = "path_denied"


class NotFound(FileToolError):
    code = "not_found"


class AlreadyExists(FileToolError):
    code = "already_exists"


class WrongType(FileToolError):
    code = "wrong_type"


class TooLarge(FileToolError):
    code = "too_large"


class NotText(FileToolError):
    code = "not_text"


class FileTool:
    def __init__(
        self,
        root: str | Path,
        max_read_bytes: int = 1_000_000,
        max_write_bytes: int = 5_000_000,
    ):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_read_bytes = max_read_bytes
        self.max_write_bytes = max_write_bytes
        self._trash = self.root / TRASH_DIR

    # ---- path safety -------------------------------------------------

    def _resolve(self, path: str, *, allow_root: bool = False) -> Path:
        if not isinstance(path, str) or "\x00" in path:
            raise PathDenied("Invalid path")
        candidate = (self.root / path).resolve()
        try:
            rel = candidate.relative_to(self.root)
        except ValueError:
            raise PathDenied(f"Path is outside the sandbox: {path}") from None
        if rel.parts and rel.parts[0] == TRASH_DIR:
            raise PathDenied("The trash is not accessible as a normal path")
        if not rel.parts and not allow_root:
            raise PathDenied("The sandbox root itself cannot be used here")
        return candidate

    def _rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)) or "."

    def _check_size(self, content: str) -> None:
        if len(content.encode("utf-8")) > self.max_write_bytes:
            raise TooLarge(f"Content exceeds {self.max_write_bytes} bytes")

    # ---- operations --------------------------------------------------

    def create(self, path: str, content: str = "") -> dict:
        """Create a new file. Fails if it already exists."""
        target = self._resolve(path)
        self._check_size(content)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise AlreadyExists(f"Already exists: {path}") from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        return {"path": self._rel(target), "bytes": target.stat().st_size}

    def read(self, path: str) -> dict:
        target = self._resolve(path)
        if not target.exists():
            raise NotFound(f"Not found: {path}")
        if not target.is_file():
            raise WrongType(f"Not a file: {path}")
        size = target.stat().st_size
        if size > self.max_read_bytes:
            raise TooLarge(f"File is {size} bytes, limit is {self.max_read_bytes}")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise NotText(f"Not a UTF-8 text file: {path}") from None
        return {"path": self._rel(target), "content": text, "bytes": size}

    def update(self, path: str, content: str, append: bool = False) -> dict:
        """Overwrite (atomically) or append to an existing file."""
        target = self._resolve(path)
        if not target.exists():
            raise NotFound(f"Not found: {path}")
        if not target.is_file():
            raise WrongType(f"Not a file: {path}")
        self._check_size(content)
        if append:
            with open(target, "a", encoding="utf-8") as fh:
                fh.write(content)
        else:
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(content)
                os.chmod(tmp, target.stat().st_mode & 0o777)
                os.replace(tmp, target)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        return {"path": self._rel(target), "bytes": target.stat().st_size}

    def delete(self, path: str) -> dict:
        """Move a file or folder to the trash. Returns a trash_id for restore."""
        target = self._resolve(path)
        if not target.exists() and not target.is_symlink():
            raise NotFound(f"Not found: {path}")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        trash_id = f"{stamp}-{uuid.uuid4().hex[:8]}"
        slot = self._trash / trash_id
        slot.mkdir(parents=True, mode=0o700)
        original = self._rel(target)
        shutil.move(str(target), str(slot / "item"))
        (slot / "meta.json").write_text(
            json.dumps({"original_path": original, "deleted_at": stamp})
        )
        return {"path": original, "trash_id": trash_id}

    def restore(self, trash_id: str) -> dict:
        if not trash_id or "/" in trash_id or trash_id in {".", ".."}:
            raise PathDenied("Invalid trash id")
        slot = self._trash / trash_id
        if not (slot / "meta.json").is_file():
            raise NotFound(f"No such trash item: {trash_id}")
        original = json.loads((slot / "meta.json").read_text())["original_path"]
        dest = self._resolve(original)
        if dest.exists():
            raise AlreadyExists(f"Cannot restore, already exists: {original}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(slot / "item"), str(dest))
        shutil.rmtree(slot)
        return {"path": original, "trash_id": trash_id}

    def mkdir(self, path: str) -> dict:
        target = self._resolve(path)
        if target.exists():
            raise AlreadyExists(f"Already exists: {path}")
        target.mkdir(parents=True)
        return {"path": self._rel(target)}

    def list_dir(self, path: str = ".") -> dict:
        target = self._resolve(path, allow_root=True)
        if not target.exists():
            raise NotFound(f"Not found: {path}")
        if not target.is_dir():
            raise WrongType(f"Not a folder: {path}")
        entries = []
        for child in sorted(target.iterdir(), key=lambda c: c.name):
            if target == self.root and child.name == TRASH_DIR:
                continue
            is_dir = child.is_dir()
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if is_dir else "file",
                    "bytes": None if is_dir else child.stat().st_size,
                }
            )
        return {"path": self._rel(target), "entries": entries}

    def list_trash(self) -> dict:
        items = []
        if self._trash.is_dir():
            for slot in sorted(self._trash.iterdir()):
                meta = slot / "meta.json"
                if meta.is_file():
                    info = json.loads(meta.read_text())
                    items.append({"trash_id": slot.name, **info})
        return {"items": items}

    # ---- dispatcher --------------------------------------------------

    OPERATIONS = (
        "create", "read", "update", "delete", "restore", "mkdir",
        "list_dir", "list_trash",
    )

    def call(self, operation: str, **args) -> ToolResult:
        """Single entry point for the agent and (later) the Policy Engine."""
        if operation not in self.OPERATIONS:
            return ToolResult(False, error=f"Unknown operation: {operation}",
                              error_code="unknown_operation")
        try:
            return ToolResult(True, data=getattr(self, operation)(**args))
        except FileToolError as exc:
            return ToolResult(False, error=str(exc), error_code=exc.code)
        except TypeError as exc:
            return ToolResult(False, error=f"Bad arguments: {exc}",
                              error_code="bad_arguments")
        except OSError as exc:
            return ToolResult(False, error=f"OS error: {exc.strerror}",
                              error_code="os_error")
