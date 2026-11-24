"""profiler CSV parsing for the template pipeline (real SDK 9.1 and legacy layouts)."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import textwrap
import unittest

SUP_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(SUP_ROOT) not in sys.path:
    sys.path.insert(0, str(SUP_ROOT))


def _runtime():
    spec = importlib.util.spec_from_file_location("npu_test_profiler", SUP_ROOT / "runtime" / "npu-test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


TASK_TIME_CSV = textwrap.dedent(
    """\
    Device_id,kernel_name,kernel_type,stream_id,task_id,task_time(us),task_start(us),task_stop(us)
    0,N/A,PROFILING_ENABLE,48,2,0.020,1,1
    0,neighbor_list_pbc_fixed_k_custom,AI_CORE,48,3,12.500,2,2
    0,neighbor_list_pbc_fixed_k_custom,AI_CORE,48,4,10.000,3,3
    0,neighbor_list_pbc_fixed_k_custom,AI_VECTOR,48,5,11.000,4,4
    0,N/A,PLACE_HOLDER_SQE,48,6,0.020,5,5
    """
)

OP_SUMMARY_CSV = textwrap.dedent(
    """\
    Op Name,Task Type,Task Duration(us),Device ID
    kernel_a,AICORE,20.0,0
    kernel_a,AICORE,30.0,0
    kernel_b,AI_CPU,99.0,0
    """
)


class ProfilerParseTests(unittest.TestCase):
    def _write(self, root: pathlib.Path, name: str, payload: str) -> pathlib.Path:
        target = root / "PROF_1" / "mindstudio_profiler_output" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        return target

    def test_sdk91_task_time_csv_keeps_only_ai_core_rows(self) -> None:
        runtime = _runtime()
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._write(root, "task_time_20260101.csv", TASK_TIME_CSV)
            # parse via the embedded helper source by exec-ing just that function block
            namespace: dict = {"pathlib": pathlib}
            src = runtime.REMOTE_TEMPLATE
            start = src.index("def _parse_task_durations")
            end = src.index("def _profiler_path")
            exec(compile(textwrap.dedent(src[start:end]), "parse", "exec"), namespace)
            durations = sorted(namespace["_parse_task_durations"](root))
        self.assertEqual(durations, [10.0, 11.0, 12.5])

    def test_legacy_op_summary_csv_is_also_accepted(self) -> None:
        runtime = _runtime()
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._write(root, "op_summary_0.csv", OP_SUMMARY_CSV)
            namespace: dict = {"pathlib": pathlib}
            src = runtime.REMOTE_TEMPLATE
            start = src.index("def _parse_task_durations")
            end = src.index("def _profiler_path")
            exec(compile(textwrap.dedent(src[start:end]), "parse", "exec"), namespace)
            durations = sorted(namespace["_parse_task_durations"](root))
        self.assertEqual(durations, [20.0, 30.0])

    def test_empty_output_returns_no_samples(self) -> None:
        runtime = _runtime()
        with tempfile.TemporaryDirectory() as td:
            namespace: dict = {"pathlib": pathlib}
            src = runtime.REMOTE_TEMPLATE
            start = src.index("def _parse_task_durations")
            end = src.index("def _profiler_path")
            exec(compile(textwrap.dedent(src[start:end]), "parse", "exec"), namespace)
            self.assertEqual(namespace["_parse_task_durations"](pathlib.Path(td)), [])


if __name__ == "__main__":
    unittest.main()
