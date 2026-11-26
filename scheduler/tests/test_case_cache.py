"""Prepared reference cases are generated once per oracle and reused across candidates."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "npu_runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

import case_batch  # noqa: E402

# Stand-in for reference_cases.py: writes one part per case and logs each invocation.
STUB = textwrap.dedent(
    """
    import json, os, sys
    from pathlib import Path
    case = Path(sys.argv[2])
    work = case / "0"
    (work / "input").mkdir(parents=True)
    (work / "output").mkdir()
    (work / "input" / "x.bin").write_bytes(b"input-" + sys.argv[1].encode())
    (work / "case.desc").write_text("desc " + sys.argv[1])
    (work / "y.npy").write_bytes(b"golden-" + sys.argv[1].encode())
    (case / "parts.json").write_text(json.dumps([{"path": str(work), "tolerances": {"y": [0, 0, 0]}}]))
    with open(case.parents[1] / "calls.log", "a") as log:
        log.write(sys.argv[1] + " " + os.environ.get("OPENBLAS_NUM_THREADS", "-") + "\\n")
    """
)


class CaseCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "work" / "repo"
        self.repo.mkdir(parents=True)
        (self.repo.parent / "reference_cases.py").write_text(STUB)
        (self.repo.parent / "schema.json").write_text(json.dumps([[["x", [4], 0, True]], []]))
        (self.repo / "reference.py").write_text("CASES = []\n")
        (self.repo / "sample.py").write_text("OUTPUTS = []\n")
        (self.repo / "main.asc").write_text("kernel v1\n")
        (self.repo / "build-casebench").mkdir()
        (self.repo / "build-casebench" / "noise.py").write_text("changes every build\n")
        self.cfg = {"title": "Demo Op", "timeout": 30, "repeats": 2, "warmup": 1, "timing": "all",
                    "selected": [{"id": f"case{i}", "seed": i} for i in range(4)]}
        self.env = mock.patch.dict(os.environ, {"OPSB_CASE_CACHE_DIR": str(base / "cache")})
        self.env.start()
        self.log = self.repo.parent / "calls.log"
        self.updates: list[dict] = []

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _prepare(self, name: str) -> list[dict]:
        root = self.repo.parent / name
        root.mkdir()
        return case_batch.prepare(self.repo, self.cfg, root, self.updates.append)

    def _calls(self) -> list[str]:
        return self.log.read_text().split("\n")[:-1] if self.log.exists() else []

    def test_second_candidate_reuses_cases_with_its_own_paths(self) -> None:
        first = self._prepare("run1")
        self.assertEqual(len(self._calls()), 4)
        self.assertTrue(all(line.split()[1] != "-" for line in self._calls()))  # BLAS pool bounded
        self.assertEqual(self.updates[-1]["index"], 4)

        (self.repo / "main.asc").write_text("kernel v2\n")  # a new candidate: kernel only
        (self.repo / "build-casebench" / "noise.py").write_text("rebuilt\n")
        second = self._prepare("run2")
        self.assertEqual(len(self._calls()), 4)  # nothing regenerated
        for old, new in zip(first, second):
            self.assertNotIn("error", new)
            self.assertEqual(new["progress"]["index"], old["progress"]["index"])
            part = new["parts"][0]
            base = Path(part["path"])
            self.assertTrue(base.is_relative_to(self.repo.parent / "run2"))
            self.assertEqual(part["tolerances"], {"y": [0, 0, 0]})
            self.assertEqual((base / "case.desc").read_text(), (Path(old["parts"][0]["path"]) / "case.desc").read_text())
            self.assertEqual(len(part["runs"]), 2)
            self.assertEqual(len(part["warm"]), 1)
            self.assertEqual((part["runs"][0] / "input" / "x.bin").read_bytes(),
                             (base / "input" / "x.bin").read_bytes())
            golden = base / "y.npy"
            self.assertGreater(golden.stat().st_nlink, 1)       # linked from the cache, not copied
            self.assertFalse(os.access(golden, os.W_OK))        # shared, so read-only

    def test_oracle_or_case_change_regenerates(self) -> None:
        self._prepare("run1")
        (self.repo / "reference.py").write_text("CASES = [1]\n")
        self._prepare("run2")
        self.assertEqual(len(self._calls()), 8)
        self.cfg["selected"][0]["seed"] = 99
        self._prepare("run3")
        self.assertEqual(len(self._calls()), 9)

    def test_cache_can_be_switched_off(self) -> None:
        self.cfg["case_cache"] = False
        self._prepare("run1")
        self._prepare("run2")
        self.assertEqual(len(self._calls()), 8)

    def test_failed_generation_is_not_cached(self) -> None:
        (self.repo.parent / "reference_cases.py").write_text("import sys; sys.exit('oracle broke')\n")
        plans = self._prepare("run1")
        self.assertTrue(all(p["error"]["status"] == "ERROR" for p in plans))
        (self.repo.parent / "reference_cases.py").write_text(STUB)
        self._prepare("run2")
        self.assertEqual(len(self._calls()), 4)


if __name__ == "__main__":
    unittest.main()
