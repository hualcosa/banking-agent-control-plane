"""TRAIL — Traced Runtime for Agents, Instrumented Locally.

A local scaffold for building agents whose behaviour can be **argued with**.
Not an agent framework: the tool-calling loop, the graph and the persistence
protocol are LangChain's, because writing those by hand is the undifferentiated
heavy lifting this repository exists to remove rather than to demonstrate.

What TRAIL owns is the spine — the part nobody enjoys building and everybody
needs by week three:

:mod:`trail.runtime.middleware.guards`
    Input and output guardrails as middleware, composed by a dial. Turning one
    off is list membership, not a flag, and a gate that is switched off still
    reports itself as skipped — because an absence that renders as nothing is
    indistinguishable from a success.

:mod:`trail.runtime.middleware.trace`
    A stage frame per hook, each with a real measurement behind it, and the OTel
    span attributes that make a model call arrive in Langfuse as a typed
    generation rather than an anonymous span.

:mod:`trail.runtime.checkpointers`
    Where conversation state lives, as a swappable slot: in memory for the unit
    suite, in Postgres for anything a user comes back to. Same agent code.

An example agent — see ``examples/`` — is a system prompt, some plain
functions, and two checks. It imports no framework, which is what makes it
portable to whatever this runtime is built on next.

Code, comments and documentation are in English. The shipped example speaks
Brazilian Portuguese to its users, and that is the example's choice, not the
runtime's.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
