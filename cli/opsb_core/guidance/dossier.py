"""P31 the per-problem dossier: ``<worktree>/.parhelion/dossier/``, built by code from existing data.

Files (the worker reads them with ``read``; messages carry only a card and a path):

``index.md``      the gap conclusion and one line per file (<= 1 KB);
``gap.md``        the gap triage of ``guidance.gap``: per-case ratio, points, weighted gains, tiers;
``findings.md``   what this problem's workers found themselves (insights, constraints, open hypotheses,
                  decisions), with source and confidence: most knowledge is searched or probed by the
                  agents, so their own findings come before the knowledge base;
``dead-ends.md``  refuted hypotheses, negative experiments and recorded failures of every seat;
``hardware.md``   up to 10 hardware laws, verified entries and anti-patterns first, evidence level shown;
``prior-art.md``  breakthroughs, community notes and snippets ranked for the focus tier;
``cards.md``      the guide cards of this problem and their outcomes.

The directory holds a ``.gitignore`` of ``*`` so it can never be committed (problem repositories also
ignore ``.parhelion/``), and submissions package only kernel sources. A file is rewritten only when its
text changed. KB text is quoted as data; ``guidance.generic_terms`` maps deployment-specific terms to
generic ones when set.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

DIRNAME = Path(".parhelion") / "dossier"
FILES = ("index.md", "gap.md", "findings.md", "dead-ends.md", "hardware.md", "prior-art.md", "cards.md")
INDEX_MAX = 1024
HARDWARE_MAX = 10
PENDING_MAX = 4
PRIOR_MAX = 8
ROW_CHARS = 260
TIER_CATEGORIES = {
    "algorithm": {"separable-folding", "tiling-regime", "simd-transpose", "vectorization"},
    "datapath": {"dma-coalescing", "async-pipeline", "tiling-regime", "simd-transpose"},
    "tuning": {"hardware-law", "vectorization", "deterministic-sync", "async-pipeline"},
}
DESCRIBE = {
    "gap.md": "per-case ratio to tbest, judge points, weighted gain of a 2x/10x/100x, tiers",
    "findings.md": "what you and your teammates found (insights, constraints, open hypotheses), evidence level",
    "dead-ends.md": "directions this problem's ledger already refuted",
    "hardware.md": "verified hardware laws and anti-patterns, evidence level shown",
    "prior-art.md": "breakthroughs, community notes and snippets ranked for the focus tier",
    "cards.md": "guide cards so far and their outcomes",
}
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def path_of(worktree: Path | str) -> Path:
    return Path(worktree).expanduser() / DIRNAME


def clip(text: Any, limit: int = ROW_CHARS) -> str:
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


def generic(text: str, terms: dict[str, str] | None) -> str:
    for src, dst in sorted((terms or {}).items(), key=lambda kv: -len(kv[0])):
        if src:
            text = re.sub(re.escape(src), dst, text, flags=re.I)
    return text


def keywords(problem: str, rows: Iterable[dict[str, Any]] = ()) -> set[str]:
    """Problem-name words and the tokens of the newest hypotheses and insights (for KB relevance)."""
    from ..knowledge.hints import _tokens
    words = {w.lower() for w in _CAMEL.findall(problem or "") if len(w) >= 3}
    texts = [str(r.get("claim") or "") for r in list(rows)[-30:] if r.get("type") in ("hypothesis", "insight")]
    return words | _tokens(" ".join(texts))


# ---- renderers (pure) ------------------------------------------------------------------------------

def render_gap(problem: str, doc: dict[str, Any], meta: dict[str, Any]) -> str:
    from .gap import fmt_ratio
    stand = meta.get("standing") or {}
    head = [f"# Gap triage · {problem} (weight x{doc.get('weight', 1):g}, {doc.get('n', 0)} judged cases)"]
    src = [f"receipt {str(meta.get('sha') or '-')[:7]}"]
    if meta.get("observed_at"):
        src.append(f"tbest observed {str(meta['observed_at'])[:16]}")
    for key, label in (("board", "board"), ("leader", "leader"), ("gap_weighted", "weighted gap")):
        if isinstance(stand.get(key), (int, float)):
            src.append(f"{label} {stand[key]:.2f}")
    head.append("source: " + " · ".join(src))
    head.append("points per case = 100 / (1 + ln(t/tbest) / ln 1.5); the score is the mean over cases, "
                "times the weight. A 2x is worth most where the ratio is small.")
    if not doc.get("cases"):
        return "\n".join(head + ["", "No judged receipt with per-case times yet."]) + "\n"
    lines = head + ["", "## Tiers", "",
                    "| tier | cases | ratio | headroom | typical | gain | 2x all | 10x all | 100x all |",
                    "|---|---:|---|---:|---:|---:|---:|---:|---:|"]
    for name, t in (doc.get("tiers") or {}).items():
        low, high = t["ratio_range"]
        lines.append(f"| {name} | {len(t['cases'])} | {fmt_ratio(low)}-{fmt_ratio(high)}x | {t['headroom_w']:.1f} | {t['factor']:g}x | "
                     f"+{t['gain_typical_w']:.1f} | +{t['gain2_w']:.1f} | +{t['gain10_w']:.1f} | +{t['gain100_w']:.1f} |")
    lines += ["", "Gains are weighted points if every case of the tier got that much faster.",
              f"Focus tier: **{doc.get('focus')}**. What each tier needs:"]
    for name, t in (doc.get("tiers") or {}).items():
        lines.append(f"- {name}: {t['method']}")
    lines += ["", "## Cases (largest weighted headroom first)", "",
              "| case | ours us | tbest us | ratio | pts | headroom | +2x | +10x | +100x | tier |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for c in doc["cases"]:
        lines.append(f"| {c['case']} | {c['ours_us']:g} | {c['tbest_us']:g} | {c['ratio']:g} | {c['points']:.1f} | "
                     f"{c['headroom_w']:.2f} | +{c['gain2_w']:.2f} | +{c['gain10_w']:.2f} | +{c['gain100_w']:.2f} | "
                     f"{c['tier']} |")
    if doc.get("untimed"):
        lines.append(f"\n{doc['untimed']} case(s) without both times are left out.")
    return "\n".join(lines) + "\n"


def _row_line(row: dict[str, Any], extra: str = "") -> str:
    from ..ledger import view
    src = str(row.get("source") or "worker")
    conf = str(row.get("confidence") or "")
    label = "unverified (teammate)" if src == "peer" else f"{src}/{conf}" if conf else src
    ev = ", ".join(str(e) for e in (row.get("evidence") or [])[:2])
    tail = clip(extra, 140) if extra else ""
    return (f"- `{view.ref(row)}` {str(row.get('ts') or '')[11:16]} [{label}{'; ' + ev if ev else ''}] "
            f"{clip(row.get('claim'))}" + (f" — {tail}" if tail else ""))


def render_findings(problem: str, rows: list[dict[str, Any]]) -> str:
    from ..ledger import view
    cur = view.current(rows)
    order = lambda r: view.order_key(r)  # noqa: E731
    unexploited = sorted(view.unexploited_insights(rows), key=order, reverse=True)[:10]
    seen = {view.ref(r) for r in unexploited}
    constraints = sorted((r for r in cur if r.get("type") == "constraint" and r.get("source") != "platform"
                          and r.get("rule_source") != "seed"), key=order, reverse=True)[:8]
    hyps = view.open_hypotheses(rows)[:8]
    decisions = sorted((r for r in cur if r.get("type") == "decision"), key=order, reverse=True)[:6]
    other = sorted((r for r in cur if r.get("type") == "insight" and view.ref(r) not in seen),
                   key=order, reverse=True)[:6]
    out = [f"# Findings · {problem}",
           "What the workers of this problem searched, probed and measured themselves, newest first. "
           "Teammate rows are unverified until you reproduce them; cite a row by its ref.", ""]
    for title, items, extra in (
            ("Unexploited insights", unexploited, "actionable_next"), ("Constraints found", constraints, "why"),
            ("Open hypotheses", hyps, "test_plan"), ("Decisions", decisions, "reason"),
            ("Other insights", other, "actionable_next")):
        out.append(f"## {title}")
        out += [_row_line(r, str(r.get(extra) or r.get("why") or "")) for r in items] or ["- none recorded"]
        out.append("")
    return "\n".join(out)


def render_dead_ends(problem: str, rows: list[dict[str, Any]], limit: int = 20) -> str:
    from ..ledger import view
    seen: set[str] = set()
    items = []
    for row in sorted([*view.negatives(rows), *view.failures(rows)], key=view.order_key, reverse=True):
        if view.ref(row) in seen:
            continue
        seen.add(view.ref(row))
        why = row.get("why_failed") or row.get("resolution_note") or row.get("why") or ""
        label = row.get("outcome") or row.get("status") or row.get("type")
        items.append(_row_line({**row, "claim": f"{label}: {row.get('claim') or row.get('approach') or ''}"},
                               str(why) + (f" (retry if {row['retry_conditions']})" if row.get("retry_conditions") else "")))
    body = items[:limit] or ["- none recorded"]
    return "\n".join([f"# Dead ends · {problem}", "Refuted or failed directions of every seat, newest first. "
                      "Do not repeat one without a reason that the row did not cover.", "", *body]) + "\n"


def _kb_line(e: dict[str, Any], terms: dict[str, str] | None, *, problem: bool = False) -> str:
    comp = e.get("evidence_composition") or {}
    ev = "/".join(f"{k} {v}" for k, v in comp.items() if v) or "no evidence rows"
    what = "anti-pattern" if e.get("status") == "anti-pattern" else str(e.get("status") or "")
    head = f"- `{e.get('id')}` [{what}; {ev}]" + (f" ({e.get('problem')})" if problem and e.get("problem") else "")
    gain = f" +{e['delta_weighted']:g} w pts" if isinstance(e.get("delta_weighted"), (int, float)) else ""
    text = generic(f"{e.get('title') or ''} — {e.get('summary') or ''}", terms)
    return f"{head}{gain} {e.get('category') or ''}: {clip(text)}"


def _rank(e: dict[str, Any], problem: str, words: set[str], tier: str) -> tuple[Any, ...]:
    from ..knowledge.hints import _entry_blob, _tokens
    same = str(e.get("problem") or "") == problem or problem.lower() in {
        str(p).lower() for p in e.get("applicable_problems") or []}
    hits = len(words & _tokens(_entry_blob(e)))
    fit = str(e.get("category") or "") in TIER_CATEGORIES.get(tier, set())
    return (0 if same else 1, 0 if fit else 1, -hits, 0 if e.get("evidence_strong") else 1,
            -float(e.get("delta_weighted") or 0.0), -float(e.get("confidence") or 0.0), str(e.get("id")))


def rank_hardware(problem: str, entries: list[dict[str, Any]], words: set[str], tier: str
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(verified and anti-patterns, pending)`` hardware entries in dossier order (items #1.. then pending)."""
    pool = [e for e in entries if e.get("pillar") == "hardware" and not e.get("retracted")]
    firm = sorted((e for e in pool if e.get("status") in ("verified", "anti-pattern")),
                  key=lambda e: _rank(e, problem, words, tier))[:HARDWARE_MAX]
    pending = sorted((e for e in pool if e.get("status") not in ("verified", "anti-pattern")),
                     key=lambda e: _rank(e, problem, words, tier))[:PENDING_MAX]
    return firm, pending


def rank_prior(problem: str, entries: list[dict[str, Any]], words: set[str], tier: str
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(this problem's breakthroughs, the rest)`` in dossier order; anti-patterns stay in hardware.md."""
    pool = [e for e in entries if e.get("pillar") in ("breakthrough", "community", "snippets")
            and not e.get("retracted") and e.get("status") != "anti-pattern"]
    own = sorted((e for e in pool if e.get("pillar") == "breakthrough" and e.get("problem") == problem),
                 key=lambda e: _rank(e, problem, words, tier))[:4]
    ids = {e.get("id") for e in own}
    rest = sorted((e for e in pool if e.get("id") not in ids), key=lambda e: _rank(e, problem, words, tier))
    return own, rest[:PRIOR_MAX]


def citable(e: dict[str, Any]) -> bool:
    """A card may cite only verified entries backed by an online receipt (stuck-guidance §5 risks)."""
    return e.get("status") == "verified" and int((e.get("evidence_composition") or {}).get("online_receipt") or 0) > 0


def _numbered(items: list[dict[str, Any]], start: int, terms: dict[str, str] | None, problem: bool) -> list[str]:
    return [f"{start + i}. " + _kb_line(e, terms, problem=problem)[2:] for i, e in enumerate(items)]


def render_hardware(problem: str, ranked: tuple[list[dict[str, Any]], list[dict[str, Any]]],
                    terms: dict[str, str] | None = None) -> str:
    firm, pending = ranked
    out = [f"# Hardware · {problem}", "Verified laws and anti-patterns first; the bracket gives the status and "
           "the evidence rows by level (only online_receipt is verified online). Full entry: "
           "parhelion_kb_query with the id.", "", "## Verified and anti-patterns"]
    out += _numbered(firm, 1, terms, False) or ["- none"]
    out += ["", "## Pending validation (treat as hypotheses)"]
    out += _numbered(pending, len(firm) + 1, terms, False) or ["- none"]
    return "\n".join(out) + "\n"


def render_prior_art(problem: str, ranked: tuple[list[dict[str, Any]], list[dict[str, Any]]], tier: str,
                     terms: dict[str, str] | None = None) -> str:
    own, rest = ranked
    out = [f"# Prior art · {problem}", f"Ranked for the focus tier ({tier or 'unknown'}): category fit, shared "
           "keywords, evidence, weighted gain. Full entry: parhelion_kb_query with the id.", "",
           "## This problem's breakthroughs"]
    out += _numbered(own, 1, terms, False) or ["- none yet"]
    out += ["", "## Other problems, community notes and snippets"]
    out += _numbered(rest, len(own) + 1, terms, True) or ["- none"]
    return "\n".join(out) + "\n"


def render_cards(problem: str, history: list[dict[str, Any]]) -> str:
    out = [f"# Guide cards · {problem}", "One active card per seat; newest first.", ""]
    for card in history[:20]:
        out.append(f"## {card.get('card_id')} · {card.get('class')} · {card.get('status')} · "
                   f"{str(card.get('issued_at') or '')[:16]} · {card.get('agent_id')}")
        if card.get("outcome"):
            out.append(f"outcome: {clip(card['outcome'], 200)}")
        out += ["```", str(card.get("text") or "").strip(), "```", ""]
    if len(out) == 3:
        out.append("No card yet.")
    return "\n".join(out) + "\n"


def render_index(problem: str, gap_line: str, built_at: str) -> str:
    head = f"# {problem} dossier (built by the platform {built_at[:16]} UTC; regenerated, do not edit)"
    files = [f"- {name} — {DESCRIBE[name]}" for name in FILES if name in DESCRIBE]
    fixed = len("\n".join([head, "", "", *files, ""]).encode("utf-8"))
    room = max(40, INDEX_MAX - fixed)
    line = clip(gap_line, room)
    while len(line.encode("utf-8")) > room:  # clip counts characters; the budget is bytes
        line = clip(line, len(line) - 8)
    return "\n".join([head, line, "", *files]) + "\n"


# ---- writing --------------------------------------------------------------------------------------

def write(worktree: Path | str, files: dict[str, str]) -> list[str]:
    """Write the changed files atomically; returns the names rewritten. The directory ignores itself."""
    root = path_of(worktree)
    root.mkdir(parents=True, exist_ok=True)
    changed = []
    for name, text in {".gitignore": "*\n", **files}.items():
        target = root / name
        try:
            if target.read_text(encoding="utf-8") == text:
                continue
        except OSError:
            pass
        tmp = target.with_name(f".{name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        if name != ".gitignore":
            changed.append(name)
    return changed
