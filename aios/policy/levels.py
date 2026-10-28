"""Permission levels, ordered from least to most restrictive."""

from enum import IntEnum


class Level(IntEnum):
    ALLOW = 0
    CONFIRM = 1
    DENY = 2

    @classmethod
    def parse(cls, value: str) -> "Level":
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            raise ValueError(f"unknown level {value!r} (use allow / confirm / deny)") from None

    def __str__(self) -> str:
        return self.name.lower()
