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


class Done(BaseException):
    """Ends a run from inside it, as a task's stub does when the task is
    done: not an ``Exception``, so agent code's ``except Exception``
    doesn't swallow it."""


class Ledger:
    """A live host object, called through :class:`LedgerStub`."""

    def __init__(self) -> None:
        self.rows: list[Row] = []
        self.reports: list[Report] = []

    def add(self, row: Row) -> int:
        self.rows.append(row)
        return len(self.rows)

    def report(self, value: Report) -> None:
        self.reports.append(value)

    def best(self) -> Report:
        top = max(self.rows, key=lambda r: r.score)
        return Report(best=top.name, total=sum(r.score for r in self.rows))

    def tally(self, *names: str, **weights: int) -> dict:
        return {"names": list(names), "weights": weights}

    def loose(self, value):
        return value

    def wrong(self) -> int:
        return "not an int"  # type: ignore[return-value]


class LedgerStub:
    """What code in the sandbox holds for a :class:`Ledger`."""

    def __init__(self, remote) -> None:
        self._remote = remote

    def add(self, row):
        return self._remote.add(row)

    def finish(self, report):
        self._remote.report(report)
        raise Done

    def best(self):
        return self._remote.best()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._remote, name)
