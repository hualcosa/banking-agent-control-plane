"""Two tools that read this repository, and nothing else.

Both are deliberately offline and deliberately dull. A demo whose tools call a
weather API proves that the agent can call an API; a demo whose tools read the
repository the agent is running inside proves the loop, the rail and the gates
while also answering the question a first-time reader actually has, which is
"what is this thing".

They are also the reason the output guardrail has something true to check
against: ``known_identifiers`` is built from the same files these tools read,
so "did the model invent that setting" is a set membership test rather than a
judgement call.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

#: Marks the directory the documents live in. Any of these is enough.
_MARKERS = ("README.md", "docker-compose.yml")


def _find_root() -> Path:
    """Where this project's documents are, from wherever the process started.

    Three places are tried, and the order is the fix for a bug worth naming.
    Walking up from ``__file__`` is right in a source checkout and **wrong in
    the image**: there the package is installed under ``site-packages``, whose
    parents contain no README, so a fixed ``parents[2]`` resolves to a
    directory with no documents in it. The tool then answers every question
    with "not documented" — which is a plausible, well-behaved, completely
    useless agent, and nothing in the logs says why.

    So: an explicit override first, then a search for a marker, then the
    working directory, which is ``/app`` in the image and the checkout root
    under ``make chat``.
    """
    override = os.environ.get("TRAIL_DOCS_ROOT")
    if override:
        return Path(override)
    for candidate in Path(__file__).resolve().parents:
        if any((candidate / marker).exists() for marker in _MARKERS):
            return candidate
    return Path.cwd()


ROOT = _find_root()

#: Documents the guide is allowed to quote.
#:
#: The README only, and the narrowness is the point. ``ui/DESIGN.md`` argues
#: for a browser surface currently being rewritten, and an agent that quoted it
#: would describe a design that no longer exists — confidently, with a
#: citation, and in a way no guardrail here can catch. A corpus is a claim
#: about what is true now; adding a document to it is a decision, not a
#: convenience.
DOCS: tuple[str, ...] = ("README.md",)

#: How many passages come back. Enough that a second-choice match is visible,
#: few enough that the model is not handed the whole document to re-read.
_MAX_HITS = 6


def _readable(relative: str) -> str:
    path = ROOT / relative
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """``(line number, paragraph)`` for each blank-line-separated block.

    Paragraphs and not lines, and this is the whole quality of the tool. A
    sentence in markdown is wrapped across three lines, so a line-based AND
    search for two words that belong to one sentence finds nothing — and
    "nothing" is a claim the agent then repeats as "not documented". Searching
    the block a human would read is searching the unit the meaning lives in.
    """
    blocks: list[tuple[int, str]] = []
    start = 1
    buffer: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if not buffer:
                start = number
            buffer.append(line)
            continue
        if buffer:
            blocks.append((start, "\n".join(buffer)))
            buffer = []
    if buffer:
        blocks.append((start, "\n".join(buffer)))
    return blocks


def search_docs(query: str) -> str:
    """Search this repository's documentation and return matching passages.

    Args:
        query: Words to look for. Case-insensitive. Passages containing all of
            them come first; if none does, the best partial matches are
            returned rather than nothing.

    Returns:
        Matching passages with their file and starting line, or a sentence
        saying nothing matched.
    """
    words = [w for w in re.split(r"\W+", query.strip().lower()) if len(w) > 2]
    if not words:
        return "Consulta vazia. Informe ao menos uma palavra com 3 letras ou mais."

    # Ranked rather than filtered. An AND search that misses by one word
    # returns nothing, and nothing is indistinguishable from "this repository
    # does not document that" — which the agent is instructed to say out loud.
    # Scoring lets a near-miss come back as a near-miss.
    scored: list[tuple[int, str]] = []
    for relative in DOCS:
        for line_number, block in _paragraphs(_readable(relative)):
            haystack = block.lower()
            score = sum(1 for word in words if word in haystack)
            if score:
                scored.append((score, f"{relative}:{line_number}\n{block.strip()}"))

    if not scored:
        return (
            f"Nada encontrado para {query!r} em: {', '.join(DOCS)}. "
            "Não há resposta documentada para essa pergunta."
        )

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best = scored[0][0]
    hits = [text for score, text in scored[:_MAX_HITS]]
    prefix = ""
    if best < len(words):
        # Said in the result rather than left to be inferred: the agent has to
        # be able to tell "here is the answer" from "here is the closest thing
        # I found", and only the tool knows which one it is handing over.
        prefix = (
            f"(nenhum trecho contém todos os termos de {query!r}; "
            f"estes são os mais próximos)\n\n"
        )
    return prefix + "\n\n---\n\n".join(hits)


def stack_status() -> str:
    """List the services this project runs under Docker Compose, with their ports.

    Returns:
        One line per service: its name, its image, and the host port it
        publishes if it publishes one.
    """
    services = _compose_services()
    if not services:
        return "Não consegui ler docker-compose.yml."
    lines = [
        f"{name} · {info['image'] or '(build local)'}"
        + (f" · porta {info['port']}" if info["port"] else "")
        for name, info in sorted(services.items())
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The ground truth the output guardrail checks against
# --------------------------------------------------------------------------

_SERVICE_RE = re.compile(r"^  ([a-z][a-z0-9_-]*):\s*$")
_IMAGE_RE = re.compile(r"^\s+image:\s*(\S+)")
_PORT_RE = re.compile(r'^\s+-\s+"?\$?\{?([A-Z_]+:-)?(\d+)\}?:')


@lru_cache(maxsize=1)
def _compose_services() -> dict[str, dict[str, str]]:
    """Service name → image and published host port, parsed from the compose file.

    A hand-rolled parse rather than a YAML dependency: pulling a parser into
    the runtime image to read three fields off a file this regular is a
    dependency that earns nothing.
    """
    text = _readable("docker-compose.yml")
    if not text:
        return {}
    services: dict[str, dict[str, str]] = {}
    current: str | None = None
    in_services = False
    for line in text.splitlines():
        if line.startswith("services:"):
            in_services = True
            continue
        if not in_services:
            continue
        if line and not line.startswith(" ") and not line.startswith("#"):
            break  # a new top-level key: `volumes:`, and we are done
        match = _SERVICE_RE.match(line)
        if match:
            current = match.group(1)
            services[current] = {"image": "", "port": ""}
            continue
        if current is None:
            continue
        if image := _IMAGE_RE.match(line):
            services[current]["image"] = image.group(1)
        elif port := _PORT_RE.match(line):
            services[current].setdefault("port", "")
            if not services[current]["port"]:
                services[current]["port"] = port.group(2)
    return services


@lru_cache(maxsize=1)
def known_identifiers() -> frozenset[str]:
    """Every configuration name and service name this project actually has.

    Built from :class:`~trail.config.Settings`' own fields and from the compose
    file, so it cannot drift from the code the way a hand-maintained list
    would: adding a setting adds it here, and deleting one removes it.

    This is what makes the fabrication guard cheap and honest. "Does
    ``TRAIL_TURBO_MODE`` exist" is a set lookup, not a model call and not a
    judgement.
    """
    from trail.config import Settings

    names = {f"TRAIL_{field.upper()}" for field in Settings.model_fields}
    names.update(_compose_services())
    return frozenset(names)
