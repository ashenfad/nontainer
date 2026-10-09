"""Types for the host object tests, in a module of their own: a worker
process imports them by name, and a guest without this module rebuilds
it from its source, which needs nothing past the standard library."""

import contextvars
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
        self.kept: list[list[int]] = []

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

    def keep(self, scores: list[int]) -> str:
        self.kept.append(scores)
        return type(scores).__name__

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


class BrokenStub:
    """A stub whose construction fails."""

    def __init__(self, remote) -> None:
        raise ValueError("no remote today")


MARK: contextvars.ContextVar[str] = contextvars.ContextVar(
    "host_types_mark", default="unset"
)
"""A context variable the embedder sets before a run, which a host
call should see as the embedder left it."""


class Probe:
    """A host object that reports where its calls run."""

    def read(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def mark(self) -> str:
        return MARK.get()

    def drain(self, items) -> list:
        return list(items)

    def wait(self, seconds: float) -> str:
        import time

        time.sleep(seconds)
        return "waited"


class ProbeStub:
    """What code holds for a :class:`Probe`: every call passed through."""

    def __init__(self, remote) -> None:
        self._remote = remote

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._remote, name)
