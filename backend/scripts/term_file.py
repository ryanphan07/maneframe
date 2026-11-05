"""Eval-only term file for arm s2: parsing, the per-window picker, and the intro text.

File format (plain text): section headers [slang] and [references], one term per line, blank lines and lines starting with #
ignored. A line may be `display text | alias | alias`: the display text is what the model is offered; the display text and
the aliases are what a quip is matched against (a leading "a", "an" or "the" is dropped for matching).
"""
import random
import re
from typing import Dict, List, Sequence, Tuple

Term = Tuple[str, Tuple[str, ...]]  # (display text, match keys)

SECTIONS = ("slang", "references")
N_SLANG = 3
N_REFERENCES = 2
RECENT_WINDOWS = 3
OPTIONS_PREFIX = (
    "Options for this window (use at most one slang term and one reference per line; "
    "use at least one reference this window if it fits what you see): "
)


def _key(text: str) -> str:
    text = text.strip().lower().replace("’", "'")
    return re.sub(r"^(?:a|an|the)\s+", "", text)


def parse_terms(text: str) -> Dict[str, List[Term]]:
    out: Dict[str, List[Term]] = {s: [] for s in SECTIONS}
    section = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section not in SECTIONS:
                raise ValueError(f"line {n}: unknown section {line!r}; use [slang] and [references]")
            continue
        if section is None:
            raise ValueError(f"line {n}: term before any [section] header")
        display, *aliases = [p.strip() for p in line.split("|") if p.strip()]
        out[section].append((display, tuple(dict.fromkeys(_key(p) for p in [display, *aliases]))))
    for s in SECTIONS:
        if not out[s]:
            raise ValueError(f"no terms in [{s}]")
    return out


def used_in(term: Term, quips: Sequence[str]) -> bool:
    """True if any key of `term` appears as whole words in any quip."""
    for key in term[1]:
        rx = re.compile(r"(?<![\w'])" + re.escape(key) + r"(?![\w'])")
        if any(rx.search(q.lower().replace("’", "'")) for q in quips):
            return True
    return False


def pick_terms(rng: random.Random, terms: Dict[str, List[Term]], recent_quips: Sequence[str],
               n_slang: int = N_SLANG, n_refs: int = N_REFERENCES) -> Dict[str, List[str]]:
    """Random display texts per section, skipping terms found in `recent_quips`. If too few unused terms remain,
    the pick is topped up from the used ones (never fewer than the section has)."""
    picked: Dict[str, List[str]] = {}
    for section, n in (("slang", n_slang), ("references", n_refs)):
        fresh = [t for t in terms[section] if not used_in(t, recent_quips)]
        stale = [t for t in terms[section] if t not in fresh]
        chosen = rng.sample(fresh, min(n, len(fresh)))
        if len(chosen) < n:
            chosen += rng.sample(stale, min(n - len(chosen), len(stale)))
        picked[section] = [t[0] for t in chosen]
    return picked


def options_text(picked: Dict[str, List[str]]) -> str:
    return (OPTIONS_PREFIX + "slang: " + ", ".join(picked["slang"]) + ". references: " + ", ".join(picked["references"]) + ".")
