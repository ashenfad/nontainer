"""Types for the host object tests, in a module of their own: a worker
process imports them by name, and a guest without this module rebuilds
it from its source, which needs nothing past the standard library."""

import enum
from dataclasses import dataclass


class Grade(enum.Enum):
    PASS = "pass"
    FAIL = "fail"


@dataclass
class Row:
    name: str
    score: int
    grade: Grade


@dataclass
class Report:
    best: str
    total: int


@dataclass
class Card:
    front: str

    def shout(self) -> str:
        return self.front.upper()

    @property
    def size(self) -> int:
        return len(self.front)
