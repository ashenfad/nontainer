"""Curated module-grant presets for :class:`~nontainer.PythonConfig`.

Each preset returns a list of :class:`~nontainer.ModuleGrant` that
splices into ``PythonConfig.modules`` (nested sequences flatten)::

    from nontainer.presets import dataframes, plotting

    PythonConfig(modules=[dataframes(), plotting()])

The exclude lists are ported from agex's registration helpers — the
accumulated knowledge of what trips up sandboxed LLM-written code
(global random state, VFS-bypassing pathlib methods, backend switching)
lives here so embedders don't rediscover it.

The safe-stdlib set (:data:`STDLIB`) is granted by default via
``PythonConfig(stdlib=True)``; presets carry only the optional heavy
libraries. Presets run at config-construction time — host level,
before any sandboxed code — which is exactly when environment side
effects (matplotlib's Agg backend, font-cache warm-up) must happen.
"""

from __future__ import annotations

import base64
import calendar
import collections
import csv
import datetime
import decimal
import fnmatch
import fractions
import glob
import gzip
import hashlib
import io
import itertools
import json
import math
import os
import pathlib
import pickle
import random
import re
import statistics
import string
import tarfile
import textwrap
import time
import traceback
import typing
import uuid
import warnings
import zipfile
import zoneinfo

from .workspace import ModuleGrant

__all__ = ["STDLIB", "dataframes", "plotting"]


# -- the always-on set (PythonConfig.stdlib) ---------------------------------

# random minus process-global state: agents in a shared interpreter
# must not reseed or capture the host's RNG.
_RANDOM_EXCLUDE = ("_*", "*._*", "seed", "getstate", "setstate", "SystemRandom")

STDLIB: tuple[ModuleGrant, ...] = (
    # math & numbers
    ModuleGrant(math),
    ModuleGrant(statistics),
    ModuleGrant(decimal),
    ModuleGrant(fractions),
    ModuleGrant(random, exclude=_RANDOM_EXCLUDE),
    # containers & iteration
    ModuleGrant(collections),
    ModuleGrant(itertools),
    # dates & time
    ModuleGrant(time),
    ModuleGrant(calendar),
    ModuleGrant(datetime),
    ModuleGrant(zoneinfo),
    # text
    ModuleGrant(re),
    ModuleGrant(string),
    ModuleGrant(textwrap),
    # data formats
    ModuleGrant(json),
    ModuleGrant(csv),
    ModuleGrant(pickle),
    ModuleGrant(base64),
    ModuleGrant(uuid),
    ModuleGrant(hashlib),
    # debugging: safe formatters only
    ModuleGrant(
        traceback, include=("format_exc", "format_exception", "print_exc")
    ),
    # typing.io / typing.re: deprecated, removed in 3.13
    ModuleGrant(typing, exclude=("_*", "*._*", "io", "re")),
    # file IO — routed through the workspace VFS by the sandbox
    ModuleGrant(io, include=("BytesIO", "StringIO", "TextIOWrapper")),
    ModuleGrant(
        os,
        include=(
            "listdir", "walk", "remove", "unlink", "mkdir", "makedirs",
            "rename", "stat", "getcwd", "chdir",
        ),
    ),
    ModuleGrant(
        os.path,
        name="os.path",
        include=(
            "exists", "isfile", "isdir", "islink", "lexists", "samefile",
            "realpath", "join", "basename", "dirname", "splitext",
        ),
    ),
    # pathlib is fully VFS-routed (monkeyfs patches it), so no method
    # excludes — agex excluded Path.read_*/write_* because ITS
    # interception layer didn't cover pathlib; ours does.
    ModuleGrant(pathlib),
    ModuleGrant(glob),
    ModuleGrant(fnmatch),
    # archives — all open() under the hood, so VFS-routed
    ModuleGrant(gzip),
    ModuleGrant(zipfile),
    ModuleGrant(tarfile),
)


# -- optional heavy libraries -------------------------------------------------

# sandtrap >= 0.2.2: excludes on a recursive grant propagate to
# submodules (and their classes' instances), and dotted patterns match
# owner-qualified names — so one grant per library suffices.
_NUMPY_EXCLUDE = (
    "_*",
    "*._*",
    # memory-mapped host files
    "memmap",
    "DataSource*",
    # process-global RNG state (numpy.random.seed and friends;
    # also blocks RandomState().seed — an agent-created Generator
    # via default_rng() is unaffected and the better idiom anyway)
    "seed",
    "set_state",
    "get_state",
)

_PANDAS_EXCLUDE = (
    "_*",
    "*._*",
    "eval",  # pd.eval / DataFrame.eval: string-expression evaluation
    "pandas.core*",
    "pandas.plotting*",
    "pandas.testing*",
    "pandas.util*",
)


def dataframes() -> list[ModuleGrant]:
    """numpy + pandas, recursively, with the agex exclude lists.

    Raises ImportError if either library is missing — presets fail
    loudly at config time, not on the agent's first import.
    """
    import numpy
    import pandas

    return [
        ModuleGrant(numpy, recursive=True, exclude=_NUMPY_EXCLUDE),
        ModuleGrant(pandas, recursive=True, exclude=_PANDAS_EXCLUDE),
    ]


_MATPLOTLIB_EXCLUDE = (
    "_*",
    "*._*",
    # interactive-display calls: no-ops under Agg, but invite
    # REPL-style usage from agents
    "show",
    "pause",
    "ion",
    "ioff",
    "isinteractive",
    # backend pinned to Agg below; agents can't flip it mid-session
    "switch_backend",
)

_PLOTLY_EXCLUDE = (
    "_*",
    "*._*",
    "show",
    "plot",  # plotly.offline.plot
    "iplot",
    "kaleido",
    "orca",
    "print_grid",
    "mpl_to_plotly",
    "get_config_*",
    "warning_*",
)


def _warm_font_cache() -> None:
    """Render a tiny figure with text so matplotlib builds its
    ``fontlist.json`` cache now, at host level. Deferred to the first
    sandboxed ``savefig``, the cache build acquires a lock file inside
    matplotlib's package directory — a write the sandbox blocks."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    try:
        ax.text(0.5, 0.5, "warm")
        fig.savefig(io.BytesIO(), format="png")
    finally:
        plt.close(fig)


def plotting(*, plotly: bool | None = None) -> list[ModuleGrant]:
    """matplotlib (Agg-pinned, font cache pre-warmed) — plus plotly.

    Args:
        plotly: ``None`` (default) includes plotly iff installed;
            ``True`` requires it (ImportError if missing); ``False``
            skips it.

    Environment side effects happen here, at call time — host level,
    before any sandboxed execution: the backend is forced to Agg
    (headless environments can't probe a GUI backend) and the font
    cache is warmed (best-effort; warns on failure).
    """
    import matplotlib

    matplotlib.use("Agg")
    try:
        _warm_font_cache()
    except Exception as e:  # pragma: no cover
        warnings.warn(
            f"matplotlib font cache warm-up failed: {e}. First savefig() "
            "inside the sandbox may fail.",
            UserWarning,
            stacklevel=2,
        )

    grants = [
        # excludes propagate to submodules (sandtrap >= 0.2.2), so this
        # covers matplotlib.pyplot's show/ion/switch_backend too
        ModuleGrant(matplotlib, recursive=True, exclude=_MATPLOTLIB_EXCLUDE),
    ]

    if plotly is not False:
        # Alias every import so the `plotly` parameter (bool | None) is
        # never rebound to the module — bare `import plotly.express` /
        # `import plotly.subplots` would both bind the top-level name.
        try:
            import plotly as plotly_mod
            import plotly.express as plotly_express
            import plotly.subplots as _  # force-load before the crawl  # noqa: F401
        except ImportError:
            if plotly is True:
                raise
        else:
            grants.append(
                ModuleGrant(plotly_mod, recursive=True, exclude=_PLOTLY_EXCLUDE)
            )
            grants.append(ModuleGrant(plotly_express, recursive=True))
    return grants
