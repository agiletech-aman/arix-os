"""Permission levels, ordered from least to most restrictive."""

from enum import IntEnum


class Level(IntEnum):
    ALLOW = 0       # L0: runs automatically
    ASK_ONCE = 1    # L1: asked once per session, per operation
    CONFIRM = 2     # L2: asked every single time
    DENY = 3        # L3: blocked

    @classmethod
    def parse(cls, value: str) -> "Level":
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            raise ValueError(f"unknown level {value!r} (use allow / ask_once / confirm / deny)") from None

    def __str__(self) -> str:
        return self.name.lower()
