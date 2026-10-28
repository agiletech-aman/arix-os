"""Shared result type for every AI-OS tool.

Tools never raise to the caller of `call()`; they return a ToolResult. The
Policy Engine (Phase 2) will sit in front of `call()` and see the same shape.
"""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    error_code: str | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)
