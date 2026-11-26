"""Deterministic probes for a compaction cut (no LLM): what the worker needed again after the cut.

Four probe types, each generated only when the answer was already in the pre-cut history:

``idea_repeat``    an idea stated after the cut (commit message, gate note, report summary,
                   knowledge title, todo or reply bullet) that matches one stated before it;
``refuted_retry``  a direction refuted before the cut (local WA or slower test, gate held for
                   low gain, online non-Pass) tried again after it: same SHA or matching idea;
``file_reread``    a file read after the cut that was read before it;
``retest``         a test after the cut of a ``(sha7, level)`` already tested before it.

Each probe carries key points (SHA prefix, file basename or direction keywords), how many
must appear in a seed to count as covered (``need``), and source locations.
"""
from __future__ import annotations

import collections
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from . import actions
from .loader import AgentLog, Cut, Turn, after_turns, before_turns

WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
HEX = re.compile(r"^[0-9a-f]{7,40}$")
STOP = frozenset("""
this that with from have will into than then them they their there these those were what when
which while would could should about after before again also because being both does done each
even every first more most much must only other over same some such very just like make made use
used using uses need needs keep kept still case cases test tests tested testing level standard final
smoke sha commit commits worker workers problem result results pass passed fail failed speedup
baseline parhelion gate submit submitted note summary report kernel code file files time times
local online judge score scores here next last into onto per via vs now new old run runs running
""".split())
MATCH_MIN_SHARED = 3
MATCH_MIN_OVERLAP = 0.6
SHA_TOKEN = re.compile(r"\b(?=[0-9a-f]*[a-f])([0-9a-f]{7,12})\b")
NEGATIVE_GATE = frozenset({"HOLD_LOW_GAIN", "REJECTED_GATE"})
SLOWER_THAN = 0.98
GIT_COMMIT_OUT = re.compile(r"\[[^\]\s]+ ([0-9a-f]{7,40})\] (.+)")
JUDGE_BAD = re.compile(r"Wrong Answer|Runtime Error|Time Limit Exceeded|Compile Error", re.I)
#: A refuted SHA counts as remembered only next to a word saying how it failed.
NEGATIVE_CONTEXT = [r"\b(wa|re|tle|wrong answer|runtime error|slower|slowdown|regress\w*|no[ -]gain|low[ -]gain"
                    r"|hold\w*|held|refut\w*|worse|fail\w*|reject\w*|revert\w*|dead[ -]end|abandon\w*|negative"
                    r"|mismatch\w*|loss|lost|drop\w*)\b", r"[-−]\d+(\.\d+)?\s*%", r"\b0\.\d+x\b"]
#: A tested SHA counts as remembered only next to its outcome (level or an n/m case count).
RESULT_CONTEXT = [r"\b(smoke|standard|final)\b", r"\b\d+/\d+\b", r"\b(pass\w*|geomean|speedup)\b"]


@dataclass
class Idea:
    text: str
    source: str
    turn: int
    seq: int
    sha7: str = ""
    keywords: set[str] = field(default_factory=set)


@dataclass
class Probe:
    """``keys``: at least ``need`` must occur. ``context``: regexes, one of which must share a line
    with the first key (a SHA alone does not tell a new context it was refuted or tested)."""
    id: str
    type: str
    keys: list[str]
    need: int
    agent_id: str
    cut: int
    where: dict[str, Any]
    before: dict[str, Any]
    context: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "keys": self.keys, "need": self.need, "context": self.context,
                "agent_id": self.agent_id, "cut": self.cut, "where": self.where, "before": self.before}


def keywords(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text or "") if w.lower() not in STOP and not HEX.match(w.lower())}


def _bullets(reply: str) -> list[str]:
    return [ln.strip(" -*•\t") for ln in reply.splitlines()
            if re.match(r"^\s*(?:[-*•]|\d+[.)])\s+\S", ln) and len(ln.strip()) >= 40]


def extract_ideas(turns: Iterable[Turn]) -> list[Idea]:
    out: list[Idea] = []
    for t in turns:
        for call in t.tools:
            sha = str(call.args.get("sha") or "")[:7].lower()
            texts: list[tuple[str, str]] = [(m, "commit") for m in actions.commit_messages(call)]
            if call.name == "parhelion_submit_gate":
                texts.append((actions.args_text(call, "note"), "gate_note"))
            elif call.name == "parhelion_agent_report":
                texts.append((actions.args_text(call, "summary"), "report"))
            elif call.name == "parhelion_kb_record":
                texts.append((actions.args_text(call, "title"), "kb_title"))
            elif call.name == "todo_write":
                texts.extend((str(td.get("content") or ""), "todo") for td in call.args.get("todos") or []
                             if isinstance(td, dict))
            for text, src in texts:
                if text.strip():
                    out.append(Idea(text.strip(), src, t.index, call.seq, sha if HEX.match(sha) else ""))
        out.extend(Idea(b, "reply", t.index, t.seq) for b in _bullets(t.reply))
    for idea in out:
        idea.keywords = keywords(idea.text)
    return out


def _informative(ideas: list[Idea]) -> set[str]:
    """Drop keywords present in more than a quarter of this worker's ideas (generic for it)."""
    df = collections.Counter(k for i in ideas for k in i.keywords)
    if len(ideas) < 8:
        return set(df)
    return {k for k, n in df.items() if n <= max(2, len(ideas) // 4)}


def _match(a: set[str], b: set[str]) -> set[str] | None:
    """Shared keywords when two ideas match: >= 3 shared and overlap coefficient >= 0.6.

    The overlap coefficient (shared / smaller set) suits a short commit message against a
    long reply bullet, where Jaccard would stay low however close the ideas are.
    """
    shared = a & b
    if len(shared) >= MATCH_MIN_SHARED and len(shared) / max(1, min(len(a), len(b))) >= MATCH_MIN_OVERLAP:
        return shared
    return None


def _top(keys: Iterable[str], df: collections.Counter, n: int = 3) -> list[str]:
    return sorted(set(keys), key=lambda k: (df[k], -len(k), k))[:n]


def commit_map(turns: Iterable[Turn]) -> dict[str, str]:
    """``sha7 -> message`` from logged ``git commit`` outputs."""
    out: dict[str, str] = {}
    for t in turns:
        for call in t.tools:
            if call.name == "bash" and "commit" in actions.shell_command(call):
                for m in GIT_COMMIT_OUT.finditer(call.result):
                    out[m.group(1)[:7]] = m.group(2).strip()
    return out


def refuted(turns: list[Turn]) -> dict[str, dict[str, Any]]:
    """``sha7 -> {why, turn, seq}`` for candidates shown worse before the cut."""
    out: dict[str, dict[str, Any]] = {}
    for t in turns:
        for call in t.tools:
            body = actions.platform_result(call)
            if call.name in ("parhelion_test", "parhelion_test_wait") and isinstance(body.get("digest"), dict):
                dg, sha = body["digest"], str(body.get("sha7") or "")[:7]
                counts = (dg.get("summary") or {}).get("counts") or {}
                speed = (dg.get("baseline") or {}).get("geomean_speedup")
                why = "local_wa" if any(k != "PASS" and v for k, v in counts.items()) else (
                    "slower" if isinstance(speed, (int, float)) and speed < SLOWER_THAN else "")
                if why and sha:
                    out.setdefault(sha, {"why": why, "turn": t.index, "seq": call.seq})
            elif call.name == "parhelion_submit_gate" and str(body.get("status_tag") or "") in NEGATIVE_GATE:
                sha = str(body.get("short_sha") or call.args.get("sha") or "")[:7]
                if sha:
                    out.setdefault(sha, {"why": "no_gain", "turn": t.index, "seq": call.seq})
        for line in t.prompt.splitlines():
            if JUDGE_BAD.search(line):
                for sha in sorted({m[:7] for m in SHA_TOKEN.findall(line)}):
                    out.setdefault(sha, {"why": "online_fail", "turn": t.index, "seq": t.seq})
    return out


def _loc(log: AgentLog, turn: int, seq: int) -> dict[str, Any]:
    t = log.turns[turn] if 0 <= turn < len(log.turns) else None
    return {"turn": turn, "seq": seq, "ts": t.ts if t else "", "session": t.session_id[:8] if t else ""}


def generate(log: AgentLog, cut: Cut) -> list[Probe]:
    before, after = before_turns(log, cut), after_turns(log, cut)
    cwd = log.sessions[0][1] if log.sessions else ""
    probes: list[Probe] = []

    def add(ptype: str, keys: list[str], need: int, where: dict[str, Any], src: dict[str, Any],
            context: list[str] | None = None) -> None:
        probes.append(Probe(f"{log.agent_id}:{cut.index}:{ptype}:{len(probes)}", ptype, keys, need,
                            log.agent_id, cut.index, where, src, list(context or [])))

    ideas_b, ideas_a = extract_ideas(before), extract_ideas(after)
    allowed = _informative(ideas_b + ideas_a)
    df = collections.Counter(k for i in ideas_b + ideas_a for k in i.keywords)
    for i in ideas_b + ideas_a:
        i.keywords &= allowed
    used: set[int] = set()
    for ia in ideas_a:
        best = max(((j, _match(ia.keywords, ib.keywords)) for j, ib in enumerate(ideas_b) if j not in used),
                   key=lambda x: len(x[1] or ()), default=(None, None))
        if best[1]:
            used.add(best[0])
            ib = ideas_b[best[0]]
            add("idea_repeat", _top(best[1], df), 2, _loc(log, ia.turn, ia.seq) | {"source": ia.source},
                _loc(log, ib.turn, ib.seq) | {"source": ib.source})

    commits = commit_map(before)
    for sha, info in refuted(before).items():
        direction = keywords(commits.get(sha, "") + " " + " ".join(i.text for i in ideas_b if i.sha7 == sha)) & allowed
        hit = next((i for i in ideas_a if i.sha7 == sha), None)
        hit = hit or next((i for i in ideas_a if direction and _match(i.keywords, direction)), None)
        retest = next((c for t in after for c in t.tools if (actions.test_key(c) or ("",))[0] == sha), None)
        if hit is None and retest is None:
            continue
        where = _loc(log, hit.turn, hit.seq) if hit else {"seq": retest.seq, "ts": retest.ts}
        add("refuted_retry", [sha], 1, where | {"direction": _top(direction, df, 3)},
            _loc(log, info["turn"], info["seq"]) | {"why": info["why"]}, NEGATIVE_CONTEXT)

    read_before: dict[str, int] = {}
    for t in before:
        for call in t.tools:
            for p in actions.read_paths(call, cwd):
                read_before.setdefault(p, call.seq)
    seen_files: set[str] = set()
    tested_before = {k: c.seq for t in before for c in t.tools if (k := actions.test_key(c))}
    seen_tests: set[tuple[str, str]] = set()
    for t in after:
        for call in t.tools:
            for p in actions.read_paths(call, cwd):
                if p in read_before and p not in seen_files:
                    seen_files.add(p)
                    add("file_reread", [os.path.basename(p)], 1, _loc(log, t.index, call.seq), {"seq": read_before[p]})
            key = actions.test_key(call)
            if key and key in tested_before and key not in seen_tests:
                seen_tests.add(key)
                add("retest", [key[0]], 1, _loc(log, t.index, call.seq) | {"level": key[1]}, {"seq": tested_before[key]},
                    RESULT_CONTEXT)
    return probes


def generate_all(logs: Iterable[AgentLog]) -> dict[tuple[str, int], list[Probe]]:
    return {(log.agent_id, cut.index): generate(log, cut) for log in logs for cut in log.cuts}
