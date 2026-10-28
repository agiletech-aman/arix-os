"""PolicyEngine wraps a tool and exposes the same `call()` -> ToolResult contract."""

from typing import Callable

from aios.policy.audit import AuditLog
from aios.policy.levels import Level
from aios.policy.loader import Policy
from aios.tools.base import ToolResult

# confirm(operation, args, reason) -> True to proceed
ConfirmFn = Callable[[str, dict, str], bool]


class PolicyEngine:
    def __init__(self, tool, policy: Policy, *, audit: AuditLog | None = None,
                 confirm: ConfirmFn | None = None, tool_name: str | None = None):
        self.tool = tool
        self.policy = policy
        self.audit = audit
        self.confirm = confirm
        self.tool_name = tool_name or type(tool).__name__
        self.OPERATIONS = tuple(getattr(tool, "OPERATIONS", ()))
        # Operations the user already approved for this session (L1 / ask_once).
        self.session_grants: set[str] = set()

    def revoke_session(self) -> None:
        self.session_grants.clear()

    def _deny(self, op, args, decision, reason, code, msg) -> ToolResult:
        res = ToolResult(ok=False, error=msg, error_code=code,
                         meta={"policy": {"decision": decision, "reason": reason}})
        self._log(op, args, decision, reason, res)
        return res

    def _log(self, op, args, decision, reason, res: ToolResult) -> None:
        if self.audit is not None:
            try:
                self.audit.write(tool=self.tool_name, operation=op, args=args,
                                 decision=decision, reason=reason,
                                 ok=res.ok, error_code=res.error_code)
            except OSError:
                pass  # audit failure must not crash the tool call

    def _decide(self, operation: str, path) -> tuple[Level, str]:
        """Strictest decision over the raw path and its resolved target (symlinks, `..`)."""
        candidates = [path]
        canonical = getattr(self.tool, "canonical_path", None)
        if canonical is not None and isinstance(path, str):
            resolved = canonical(path)
            if resolved is not None:
                candidates.append(resolved)
        best = self.policy.decide(operation, candidates[0])
        for c in candidates[1:]:
            d = self.policy.decide(operation, c)
            if d[0] > best[0]:
                best = d
        return best

    def check(self, operation: str, **args) -> tuple[Level, str]:
        """Dry-run: what would the policy decide? No tool call, no audit."""
        return self._decide(operation, args.get("path"))

    def call(self, operation: str, **args) -> ToolResult:
        if self.OPERATIONS and operation not in self.OPERATIONS:
            return self._deny(operation, args, "deny", "unknown operation",
                              "UNKNOWN_OPERATION", f"unknown operation: {operation}")

        content = args.get("content")
        limit = self.policy.max_content_bytes
        if limit is not None and isinstance(content, str) and len(content.encode()) > limit:
            return self._deny(operation, args, "deny", "content too large",
                              "POLICY_TOO_LARGE", f"content exceeds policy limit of {limit} bytes")

        level, reason = self._decide(operation, args.get("path"))

        if level is Level.DENY:
            return self._deny(operation, args, "deny", reason, "POLICY_DENIED",
                              f"{operation} denied by policy ({reason})")

        decision = "allow"
        if level is Level.ASK_ONCE and operation in self.session_grants:
            decision = "session"
        elif level in (Level.ASK_ONCE, Level.CONFIRM):
            approved = False
            if self.confirm is not None:
                try:
                    approved = bool(self.confirm(operation, dict(args), reason))
                except Exception:
                    approved = False
            if not approved:
                return self._deny(operation, args, "confirm", reason, "CONFIRMATION_REQUIRED",
                                  f"{operation} needs confirmation ({reason})")
            decision = "confirmed"
            if level is Level.ASK_ONCE:
                self.session_grants.add(operation)

        try:
            res = self.tool.call(operation, **args)
        except Exception as e:  # tools should never raise, but be defensive
            res = ToolResult(ok=False, error=str(e), error_code="INTERNAL_ERROR")
        res.meta = {**(res.meta or {}), "policy": {"decision": decision, "reason": reason}}
        self._log(operation, args, decision, reason, res)
        return res
