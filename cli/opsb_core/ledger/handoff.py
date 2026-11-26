"""Handoff validation (compaction-memory.md section 5.7): what a new session may trust from the old one.

The model writes a narrative of at most 1500 characters under five fixed headings. The platform then

1. appends the code-built ledger summary, so every open hypothesis and unexploited insight is named;
2. marks every SHA, path and test-run id of the narrative that does not exist as ``[unverified]``;
3. carries forward decisions and failures of the previous narrative that this one dropped;
4. checks the headings: one repair round, and a second failure falls back to the ledger-only seed.

The full narrative and the outcome of each check are appended to ``state/handoffs/<agent>.jsonl``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import gitref, seed_min, store, summary
from ..drivers.base import resolve_workspace_state_dir

HEADINGS = ("Where I am", "In the middle of", "Uncommitted changes and paths", "Next experiment",
            "Not yet in the ledger")
NARRATIVE_MAX = 1500
HANDOFF_PROMPT = (
    "Write a structured handoff for the next session, at most 1500 characters in total, under exactly these five "
    "headings, each on its own line:\n" + "\n".join(f"## {h}" for h in HEADINGS) + "\n"
    "Under 'Next experiment' name the ledger id (L-nnnn) you will run next. Under 'Not yet in the ledger' list "
    "conclusions you hold that no ledger entry records. Do not repeat what the ledger lists; cite entries by id. "
    "Name real commit SHAs, file paths and test request ids only.")
REPAIR_PROMPT = ("Your handoff is missing the heading(s): {missing}. Rewrite it in at most 1500 characters with "
                 "all five headings exactly as specified, each on its own line starting with '## '.")
EXTS = "py|cpp|cc|c|h|hpp|cu|cuh|md|json|jsonl|txt|yaml|yml|toml|sh|ini|cfg|ts|js|swift"
HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
SHA = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}\b")
PATH = re.compile(rf"(?<![\w@:/.\-])((?:\.{{1,2}}/|/)?(?:[\w.\-]+/)*[\w.\-]+\.(?:{EXTS}))(?![\w/])")
LEDGER_ID = re.compile(r"\b(?:[A-Za-z0-9._-]+/)?L-\d+\b")
DECISION = re.compile(r"\b(decid\w*|chose|chosen|rejected?|refut\w*|abandon\w*|dead[ -]end|ruled out|fail\w*|"
                      r"wrong answer|WA|no gain|slower|regress\w*|never|avoid|must not)\b", re.I)
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{4,}")
CARRY_MAX_ITEMS = 8
CARRY_ITEM_CHARS = 240
UNVERIFIED = " [unverified]"


def _heading_re(name: str) -> re.Pattern[str]:
    """A line that is the heading: optional ``#``/``*``/``-``/``1.`` markers, the heading, optional ``: rest``."""
    return re.compile(rf"^\s*(?:#{{1,6}}\s*|\*{{1,3}}\s*|-\s+)?(?:\d+[.)]\s*)?\**{re.escape(name)}\**\s*"
                      r"(?::\s*\**\s*(.*))?$", re.I)


_HEADING_RES = {h: _heading_re(h) for h in HEADINGS}


def sections(text: str) -> dict[str, str]:
    """``{heading: body}`` for every fixed heading found."""
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        hit = next(((h, m) for h, r in _HEADING_RES.items() if (m := r.match(line))), None)
        if hit:
            current = hit[0]
            found.setdefault(current, [])
            if hit[1].group(1):
                found[current].append(hit[1].group(1).strip())
        elif current is not None:
            found[current].append(line)
    return {h: "\n".join(body).strip() for h, body in found.items()}


def missing_headings(text: str) -> list[str]:
    have = sections(text)
    return [h for h in HEADINGS if h not in have]


def clip_narrative(text: str, limit: int = NARRATIVE_MAX) -> str:
    """Keep all five headings: an over-long narrative has each section cut to its share of ``limit``."""
    text = text.strip()
    if len(text) <= limit:
        return text
    parts = sections(text)
    share = max(60, (limit - sum(len(h) + 5 for h in HEADINGS)) // len(HEADINGS))
    out = []
    for h in HEADINGS:
        body = parts.get(h, "")
        out.append(f"## {h}\n" + (body if len(body) <= share else body[: share - 1] + "…"))
    return "\n".join(out)


def _registry_ids(ws: Path) -> set[str]:
    from ..services import test_runs
    ids: set[str] = set()
    for path in test_runs.registry_dir(ws).glob("*.json"):
        ids.add(path.stem)
        try:
            run_id = json.loads(path.read_text(encoding="utf-8")).get("run_id")
        except (OSError, ValueError, AttributeError):
            continue
        if run_id:
            ids.add(str(run_id))
    return ids


def verify_refs(text: str, *, worktree: Path, workspace: Path) -> tuple[str, dict[str, list[str]]]:
    """Mark SHAs, paths and test-run ids that do not resolve; returns the text and what was marked."""
    bad: dict[str, list[str]] = {"sha": [], "path": [], "test_run": []}
    registry = _registry_ids(workspace)
    root = worktree.resolve() if worktree.is_dir() else None
    spans: list[tuple[int, int, str, str]] = []
    for m in HEX32.finditer(text):
        spans.append((m.start(), m.end(), "test_run", m.group(0)))
    for m in SHA.finditer(text):
        if len(m.group(0)) != 32 and not any(a <= m.start() < b for a, b, _, _ in spans):
            spans.append((m.start(), m.end(), "sha", m.group(0)))
    for m in PATH.finditer(text):
        if not any(a <= m.start() < b for a, b, _, _ in spans):
            spans.append((m.start(), m.end(), "path", m.group(1)))
    out, pos = [], 0
    for start, end, kind, token in sorted(spans):
        if start < pos:
            continue
        ok = (token in registry if kind == "test_run" else gitref.commit_exists(worktree, token) if kind == "sha"
              else _path_ok(token, root))
        out.append(text[pos:end])
        pos = end
        if not ok:
            out.append(UNVERIFIED)
            if token not in bad[kind]:
                bad[kind].append(token)
    out.append(text[pos:])
    return "".join(out), {k: v for k, v in bad.items() if v}


def _path_ok(token: str, root: Path | None) -> bool:
    if root is None:
        return False
    target = Path(token) if token.startswith("/") else root / token
    try:
        resolved = target.resolve()
    except OSError:
        return False
    return resolved.is_relative_to(root) and resolved.exists()


#: Stems that say "a decision was made" or are plain connectives: they never tell two sentences apart.
GENERIC = frozenset("decide reject abando becaus should would which there their about other after before being "
                    "failed fail never avoid regres ruled wrong answer dead".split())


def _words(text: str) -> set[str]:
    """Six-letter stems of the distinctive words, so ``rejected`` and ``reject`` match."""
    return {w.lower()[:6] for w in WORD.findall(text)} - GENERIC


def carry_forward(previous: str, new: str, ledger_text: str = "") -> list[str]:
    """Decisions and failures stated in ``previous`` that neither ``new`` nor the ledger summary mention."""
    seen = new + "\n" + ledger_text
    seen_words = _words(seen)
    out: list[str] = []
    for raw in re.split(r"\n+|(?<=[.!?])\s+", previous):
        item = raw.strip(" -*•\t#")
        if len(item) < 25 or not DECISION.search(item) or any(item.lower().startswith(h.lower()) for h in HEADINGS):
            continue
        keys = LEDGER_ID.findall(item) + SHA.findall(item)
        words = _words(item)
        covered = (any(k in seen for k in keys) if keys
                   else bool(words) and len(words & seen_words) / len(words) >= 0.6)
        if not covered:
            out.append(item[:CARRY_ITEM_CHARS])
        if len(out) >= CARRY_MAX_ITEMS:
            break
    return out


@dataclass
class Result:
    text: str
    status: str  # ok | repaired | ledger_only
    findings: dict[str, Any] = field(default_factory=dict)


class HandoffSession:
    """Everything one compaction of one worker needs: ledger rows, worktree, the previous narrative."""

    def __init__(self, workspace: Path | str, problem: str, agent_id: str, worktree: Path | str, driver: str = "") -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.problem, self.agent_id, self.driver = str(problem or ""), str(agent_id), driver
        self.worktree = Path(worktree).expanduser()

    # ---- ledger and state files
    def rows(self) -> list[dict[str, Any]]:
        if not self.problem:
            return []
        try:
            return store.Ledger(self.workspace, self.problem, self.agent_id).rows()
        except Exception:
            return []

    def summary_block(self) -> str:
        rows = self.rows()
        block = summary.render(rows, own=self.agent_id)
        missing = summary.ids_missing(block, rows, self.agent_id)
        return block + ("\nAlso open: " + ", ".join(missing) if missing else "")

    @property
    def log_path(self) -> Path:
        return resolve_workspace_state_dir(self.workspace) / "handoffs" / f"{self.agent_id}.jsonl"

    def previous_narrative(self) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("narrative_final"):
                return str(row["narrative_final"])
        return ""

    def record(self, kind: str, narrative: str, final: str, result: Result) -> None:
        row = {"ts": store._now(), "agent_id": self.agent_id, "problem": self.problem, "kind": kind,
               "status": result.status, "findings": result.findings, "narrative": narrative,
               "narrative_final": final, "seed_chars": len(result.text)}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---- the four checks
    def _validated(self, narrative: str, repair: Callable[[str], str] | None) -> tuple[str, str, dict[str, Any]]:
        """``(text, status, findings)``; text is empty when the narrative could not be repaired."""
        status, findings = "ok", {}
        text = (narrative or "").strip()
        missing = missing_headings(text)
        if missing and repair is not None:
            status, findings["repaired_headings"] = "repaired", missing
            try:
                text = (repair(REPAIR_PROMPT.format(missing=", ".join(missing))) or "").strip()
            except Exception as exc:  # noqa: BLE001
                findings["repair_error"] = f"{type(exc).__name__}: {exc}"[:200]
                text = ""
            missing = missing_headings(text)
        if missing:
            findings["missing_headings"] = missing
            return "", "ledger_only", findings
        text = clip_narrative(text)
        text, bad = verify_refs(text, worktree=self.worktree, workspace=self.workspace)
        if bad:
            findings["unverified"] = bad
        return text, status, findings

    # ---- seed schemes (4.2 C #5): legacy = pre-P05, ledger_core = P05, ledger_full = code-assembled
    def _ledger(self) -> Any:
        return store.Ledger(self.workspace, self.problem, self.agent_id)

    def _rules(self) -> list[str]:
        from . import seed as _seed
        return (_seed.rules_text(self.workspace) + _seed.admin_directives(self.workspace, self.agent_id)
                + [_seed.platform_rules()])

    def _pin_rules(self, rules: list[str]) -> None:
        from . import seed as _seed
        try:
            _seed.pin(self._ledger(), rules)
        except Exception:
            pass  # pinning a constraint must never cost the worker its compaction

    def build_seed(self, narrative: str, checkpoint: str, repair: Callable[[str], str] | None = None,
                   kind: str = "acp") -> Result:
        """The full seed text for the new session, and the check outcome."""
        from . import seed as _seed
        text, status, findings = self._validated(narrative, repair)
        scheme = _seed.scheme()
        findings["seed_scheme"] = scheme
        if status == "ledger_only":
            from ..services import state_card
            seed = seed_min.build(card=state_card.build(self.workspace, self.problem, self.agent_id), rows=self.rows(),
                                  worktree=self.worktree, own=self.agent_id, rules=state_card.RULES_LINE)
            result = Result(seed, status, findings)
            self.record(kind, narrative, "", result)
            return result
        if scheme == _seed.LEGACY:  # the pre-P05 seed: the narrative and the platform checkpoint, nothing added
            result = Result("\n".join(p for p in (seed_min.SEED_PREFIX, text, "Last reported result:\n" + checkpoint) if p),
                            status, findings)
            self.record(kind, narrative, text, result)
            return result
        if scheme == _seed.FULL:
            rules = self._rules()
            seed = _seed.build(self.workspace, self.problem, self.agent_id, self.rows(), narrative=text,
                               stage=checkpoint, worktree=self.worktree, rules=rules)
            self._pin_rules(rules)
            result = Result(seed, status, findings)
            self.record(kind, narrative, text, result)
            return result
        block = self.summary_block()
        carried = carry_forward(self.previous_narrative(), text, block)
        if carried:
            findings["carried_forward"] = len(carried)
        next_ref = LEDGER_ID.search(sections(text).get("Next experiment", ""))
        findings["next_experiment_ref"] = bool(next_ref)
        parts = [seed_min.SEED_PREFIX, text]
        if carried:
            parts.append("Carried forward from the previous handoff:\n" + "\n".join(f"- {c}" for c in carried))
        parts += [block, "Last reported result:\n" + checkpoint]
        result = Result("\n".join(p for p in parts if p), status, findings)
        self.record(kind, narrative, text, result)
        return result

    def ledger_seed(self, checkpoint: str, kind: str = "codex") -> Result:
        """Native compaction has no narrative: the code-built seed or the ledger summary, then the checkpoint."""
        from . import seed as _seed
        scheme = _seed.scheme()
        if scheme == _seed.LEGACY:
            result = Result(f"{seed_min.SEED_PREFIX}\nLast reported result:\n{checkpoint}", "ok",
                            {"ledger_only_prefix": False, "seed_scheme": scheme})
            self.record(kind, "", "", result)
            return result
        if scheme == _seed.FULL:
            rules = self._rules()
            text = _seed.build(self.workspace, self.problem, self.agent_id, self.rows(), stage=checkpoint,
                               worktree=self.worktree, rules=rules)
            self._pin_rules(rules)
            result = Result(text, "ok", {"seed_scheme": scheme})
            self.record(kind, "", "", result)
            return result
        block = self.summary_block()
        parts = [seed_min.SEED_PREFIX, block, "Last reported result:\n" + checkpoint]
        result = Result("\n".join(p for p in parts if p), "ok", {"ledger_only_prefix": True, "seed_scheme": scheme})
        self.record(kind, "", "", result)
        return result
