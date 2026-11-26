"""Evidence classes for peer messages (4.3 P27) and, from 4.3 P28, for knowledge entries.

One vocabulary, defined once, because a broadcast and a KB card must not disagree on what "verified"
means. The three classes are exactly the ones the plan fixes:

``local``
    measured on this machine only (a local run, a profiler reading, a static scan);
``paired``
    two sides measured on the same inputs (a same-generation pairing or a paired report);
``online_receipt``
    an official judge receipt (a ``submission_id`` with its score).

A message whose class is not ``online_receipt`` is *unverified*: the recipient reads it behind the
``待验证`` marker, and the collaboration page shows the same label. The module is dependency-free on
purpose, so both ``agents.peer`` and ``knowledge.*`` can import it without a cycle.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

CLASSES: tuple[str, ...] = ("local", "paired", "online_receipt")
DEFAULT_CLASS = "local"
#: Weakest first: a batch of messages is only as verified as its weakest member.
RANK: dict[str, int] = {name: index for index, name in enumerate(CLASSES)}
UNVERIFIED_MARK = "待验证"
REF_MAX = 300


def class_of(value: Any) -> str:
    """The class of ``value`` (a mapping, a bare class name, a legacy string or None)."""
    if isinstance(value, Mapping):
        return str(value.get("class") or "").strip().lower()
    return ""


def is_verified(value: Any) -> bool:
    """Whether this evidence is an official online receipt (the only class that needs no marker)."""
    return class_of(value) == "online_receipt"


def normalize(value: Any, *, required: bool = False) -> dict[str, Any]:
    """``{"class", "ref"}`` from a mapping, a bare class name, a legacy string or None.

    A legacy P14 envelope carried ``evidence`` as one string (``path#anchor``, a run id or a receipt
    id); it keeps its text as ``ref`` and is read as ``local``, which is what the plan asks for
    ("旧信封没有 evidence 时按 local 处理"). ``required`` raises when the sender names no evidence at
    all, which is the P27 send-time contract.
    """
    if isinstance(value, Mapping):
        name = str(value.get("class") or "").strip().lower() or DEFAULT_CLASS
        ref = " ".join(str(value.get("ref") or "").split())
    else:
        text = " ".join(str(value or "").split())
        name = text.lower() if text.lower() in CLASSES else DEFAULT_CLASS
        ref = "" if name == text.lower() and text else text
    if name not in CLASSES:
        raise ValueError(f"evidence class must be one of: {', '.join(CLASSES)}; got {name!r}")
    if len(ref) > REF_MAX:
        raise ValueError(f"evidence ref is limited to {REF_MAX} characters; cite the id or path only")
    if required and not name:
        raise ValueError("evidence is required")
    return {"class": name, "ref": ref}


def require(value: Any) -> dict[str, Any]:
    """``normalize`` for a *sender*: an absent evidence or an empty ref is an error, not ``local``."""
    if value is None or (isinstance(value, str) and not value.strip()) or (
            isinstance(value, Mapping) and not value):
        raise ValueError("evidence is required: {class: local|paired|online_receipt, ref: run id, "
                         "submission id or path}")
    doc = normalize(value)
    if not doc["ref"]:
        raise ValueError("evidence.ref is required: a run id, a submission id or a path#anchor")
    return doc


def weakest(values: Iterable[Any]) -> str:
    """The weakest class of ``values`` (``local`` when the iterable is empty)."""
    names = [class_of(value) for value in values]
    names = [name for name in names if name in RANK]
    return min(names, key=lambda name: RANK[name]) if names else DEFAULT_CLASS


def marker(value: Any) -> str:
    """The ``待验证`` line a reader sees before a body, or ``""`` for an online receipt.

    Tolerant by design: an old envelope (a bare string, a missing field or a class this version does
    not know) reads as ``local`` instead of raising while a recipient renders its queue.
    """
    name = class_of(value) or DEFAULT_CLASS
    if name not in RANK:
        name = DEFAULT_CLASS
    if name == "online_receipt":
        return ""
    return f"{UNVERIFIED_MARK} ({name}) — {name} evidence, not an online judge receipt"


def line(value: Any) -> str:
    """One-line rendering for a message header or a ledger row: ``local:results/x#a``."""
    doc = normalize(value)
    return f"{doc['class']}:{doc['ref']}" if doc["ref"] else doc["class"]


__all__ = [
    "CLASSES",
    "DEFAULT_CLASS",
    "RANK",
    "REF_MAX",
    "UNVERIFIED_MARK",
    "class_of",
    "is_verified",
    "line",
    "marker",
    "normalize",
    "require",
    "weakest",
]
