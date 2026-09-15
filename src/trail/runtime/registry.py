"""Which example agent is mounted, resolved from ``TRAIL_AGENT``.

Examples are looked up lazily, by import path, so that adding one is adding a
module rather than editing the runtime. The runtime never imports an example
at module scope: an example is free to depend on things the runtime does not,
and a broken example should fail when it is asked for, not when the service
starts with a different one selected.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from trail.runtime.agent import AgentSpec

#: Example name → the package it lives in. Two modules are looked for inside
#: it, both exposing a zero-argument ``build()``: ``agent`` returns the
#: :class:`AgentSpec` the service mounts, ``golden`` returns the
#: :class:`~trail.evals.cases.GoldenSet` the harness measures it with. Naming
#: the package rather than the module is what keeps those two in step — an
#: example cannot be mounted under one name and evaluated under another.
EXAMPLES: dict[str, str] = {
    "banking": "examples.banking",
    "trail_guide": "examples.trail_guide",
}


def load_spec(name: str) -> AgentSpec:
    """The :class:`AgentSpec` registered as ``name``, from ``<package>.agent``."""
    return _module(name, "agent").build()


def load_golden(name: str) -> Any:
    """The golden set registered as ``name``, from ``<package>.golden``.

    Separate from :func:`load_spec` and imported only by the harness: the
    service has no reason to load a golden set, and an example that ships no
    ``golden`` module still runs — it is simply not measurable, which is a
    different failure from being unmountable and should read as one.
    """
    return _module(name, "golden").build()


def _module(name: str, part: str) -> Any:
    """``<package>.<part>`` for the registered example ``name``.

    A miss names the valid set. The alternative — an ``ImportError`` mentioning
    a module path the operator never typed — is the same information with the
    useful part removed.
    """
    try:
        package = EXAMPLES[name]
    except KeyError:
        valid = ", ".join(sorted(EXAMPLES))
        raise ValueError(
            f"unknown agent {name!r}; TRAIL_AGENT must be one of: {valid}"
        ) from None
    return import_module(f"{package}.{part}")
