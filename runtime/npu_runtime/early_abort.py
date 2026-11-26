"""Stop a standard run early when its first timed cases are clearly slower than the baseline (R9).

The platform sends per-case baseline minima (event timer) of the best judged SHA with the run
config. As case results arrive, the run compares each timed case's warm minimum with its
baseline. Only cases whose baseline is at least ``min_us`` count: below that the event timer's
mode jumps (5 versus 25 us) exceed any real difference. Once ``cases`` such cases have been
seen and every one of them is slower than ``factor`` times its baseline, the run stops: the
broker job is cancelled, the device is released, and the remaining cases are reported as SKIP.
``final`` runs never carry this config (AGENTS.md principle 5).
"""


class Watch:
    def __init__(self, cfg):
        cfg = cfg if isinstance(cfg, dict) else {}
        self.baseline = {str(k): float(v) for k, v in (cfg.get("baseline_us") or {}).items() if v}
        self.factor = float(cfg.get("factor") or 1.25)
        self.cases = max(1, int(cfg.get("cases") or 3))
        self.min_us = float(cfg.get("min_us") or 200.0)
        self.base_sha = str(cfg.get("base_sha") or "")
        self.seen = []
        self.aborted = None

    @property
    def active(self):
        return bool(self.baseline) and self.aborted is None

    def observe(self, case_id, samples):
        """Record one timed case's warm samples; return True when the run should stop now."""
        base = self.baseline.get(str(case_id))
        values = [v for v in samples or [] if v and v > 0]
        if not self.active or base is None or base < self.min_us or not values:
            return False
        ratio = min(values) / base
        self.seen.append((str(case_id), round(ratio, 4)))
        if len(self.seen) >= self.cases and all(r > self.factor for _, r in self.seen[:self.cases]):
            self.aborted = {"after_cases": [c for c, _ in self.seen[:self.cases]],
                            "ratios": dict(self.seen[:self.cases]), "factor": self.factor,
                            "min_us": self.min_us, "base_sha": self.base_sha}
            return True
        return False

    def skip_detail(self):
        a = self.aborted or {}
        return (f"aborted early: {len(a.get('after_cases') or [])} timed cases were slower than "
                f"{a.get('factor')}x the baseline {a.get('base_sha', '')[:7]}")
