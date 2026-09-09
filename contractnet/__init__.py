"""
contractnet: client library for the CPSC 370 Contract Net tournament.

    from contractnet import Contractor, Bid

    class MyContractor(Contractor):
        def on_cfp(self, task):
            ...

    MyContractor(name="Team_07", url="wss://...").run()

The networking half of this package is imported lazily, so `tasks` and
`benchmark` can be used on a machine that has no `websockets` installed,
which is how the instructor's answer-key generator runs.
"""

from typing import Any

from .benchmark import calibrate, overall_score, work_units
from .tasks import run_task

__all__ = [
    "Bid",
    "Contractor",
    "Rules",
    "Settlement",
    "Task",
    "calibrate",
    "overall_score",
    "run_task",
    "work_units",
]

_LAZY = {"Bid", "Contractor", "Rules", "Settlement", "Task"}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from . import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
