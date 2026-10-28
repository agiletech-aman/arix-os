"""Policy file (TOML) -> Policy object.

Decision rule: the STRICTEST level wins among
  - the operation's level ([operations] table, else `default`)
  - every [[rules]] entry whose operations AND paths match.
So a rule can only tighten, never loosen. deny > confirm > allow.
"""

import fnmatch
import posixpath
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from aios.policy.levels import Level

# Built-in default, used when no policy file is given.
DEFAULT_POLICY_TOML = """
default = "deny"

[operations]
read       = "allow"
list_dir   = "allow"
list_trash = "allow"
create     = "allow"
update     = "allow"
mkdir      = "allow"
restore    = "allow"
delete     = "confirm"

[[rules]]
paths      = [".env", "*.env", "*.key", "*.pem", "secrets", "secrets/*"]
operations = ["*"]
level      = "deny"
reason     = "protected path"

[limits]
max_content_bytes = 1048576
"""


class PolicyError(ValueError):
    pass


def norm_path(path: str | None) -> str | None:
    if path is None:
        return None
    # normpath collapses "a/../b" so a rule cannot be dodged with "x/../secrets/k".
    p = posixpath.normpath(str(path).replace("\\", "/"))
    return p.lstrip("/") or "."


@dataclass(frozen=True)
class Rule:
    level: Level
    operations: tuple[str, ...] = ("*",)
    paths: tuple[str, ...] = ()  # empty = any path (incl. path-less ops)
    reason: str = ""

    def matches(self, operation: str, path: str | None) -> bool:
        if not any(fnmatch.fnmatchcase(operation, o) for o in self.operations):
            return False
        if not self.paths:
            return True
        if path is None:
            return False
        name = PurePosixPath(path).name
        return any(fnmatch.fnmatchcase(path, g) or fnmatch.fnmatchcase(name, g) for g in self.paths)


@dataclass
class Policy:
    default: Level = Level.DENY
    operations: dict[str, Level] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    max_content_bytes: int | None = None

    def decide(self, operation: str, path: str | None = None) -> tuple[Level, str]:
        """Return (level, reason)."""
        path = norm_path(path)
        if operation in self.operations:
            level, reason = self.operations[operation], f"operation:{operation}"
        else:
            level, reason = self.default, "default"
        for i, rule in enumerate(self.rules):
            if rule.level > level and rule.matches(operation, path):
                level, reason = rule.level, rule.reason or f"rule:{i}"
        return level, reason

    @classmethod
    def from_dict(cls, raw: dict) -> "Policy":
        try:
            ops = {k: Level.parse(v) for k, v in (raw.get("operations") or {}).items()}
            rules = []
            for r in raw.get("rules") or []:
                unknown = set(r) - {"level", "operations", "paths", "reason"}
                if unknown:
                    raise PolicyError(f"unknown rule keys: {sorted(unknown)}")
                rules.append(Rule(
                    level=Level.parse(r["level"]),
                    operations=tuple(r.get("operations") or ["*"]),
                    paths=tuple(r.get("paths") or []),
                    reason=str(r.get("reason", "")),
                ))
            limits = raw.get("limits") or {}
            mcb = limits.get("max_content_bytes")
            return cls(
                default=Level.parse(raw.get("default", "deny")),
                operations=ops,
                rules=rules,
                max_content_bytes=int(mcb) if mcb is not None else None,
            )
        except PolicyError:
            raise
        except (KeyError, TypeError, ValueError) as e:
            raise PolicyError(f"invalid policy: {e}") from None


def load_policy(path: str | Path | None = None) -> Policy:
    if path is None:
        return Policy.from_dict(tomllib.loads(DEFAULT_POLICY_TOML))
    p = Path(path).expanduser()
    try:
        raw = tomllib.loads(p.read_text())
    except FileNotFoundError:
        raise PolicyError(f"policy file not found: {p}") from None
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(f"bad TOML in {p}: {e}") from None
    return Policy.from_dict(raw)
