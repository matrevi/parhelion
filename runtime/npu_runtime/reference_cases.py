"""Descriptor-driven runner for the official Group/Tensor ASC sample template.

Invoked inside a private checkout of a single commit. NumPy and SDK are remote
dependencies. Case generation and output comparison run outside the device lock.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

GROUP = re.compile(
    r'Group group_(\d+)\(\{Tensor\{\{([\d, ]*)\}, (\d+), (\d+), "(\w+)\.bin"\}\}, (true|false), (true|false)\);'
)
ATTR = re.compile(
    r"(std::array<int64_t,\s*\d+>|const char\*|bool|float|double|u?int(?:32|64)_t) (attr_(\d+))(_data)?\s*(?:=|\{).*?;"
)
CPP = """
#include <unistd.h>
static std::ifstream desc;
static float sample_us = 0;
template<class T> static T field() {
    T x{}; if (!(desc >> x)) throw std::runtime_error("bad case descriptor"); return x;
}
static std::vector<int64_t> integers() {
    size_t n=field<size_t>(); std::vector<int64_t> a(n);
    for (auto &v:a) v=field<int64_t>(); return a;
}
static std::vector<Tensor> tensors(const char* file) {
    if (!field<int>()) return {};
    auto shape=integers(); auto dtype=field<int32_t>(); auto bytes=field<size_t>();
    return {Tensor{shape,dtype,bytes,file}};
}
"""

BATCH_MAIN = r"""
int main(int argc, char** argv) {
    const char* channel = std::getenv("OPSB_BATCH_FD");
    if (!channel) {
        desc.open("case.desc");
        return desc.is_open() ? case_main(argc, argv, nullptr) : smoke_main(argc, argv);
    }
    int fd = std::stoi(channel);
    Session session(std::stoi(argv[1]));
    session.start();
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.size() < 3 || ::chdir(line.substr(2).c_str()) != 0) return 2;
        if (line[0] == '1') ::setenv("OPSB_CASE_TIMING", "1", 1);
        else ::unsetenv("OPSB_CASE_TIMING");
        desc.clear();
        desc.open("case.desc");
        sample_us = 0;
        int rc = desc ? case_main(argc, argv, &session) : 2;
        desc.close();
        ::dprintf(fd, "%d %.9g\n", rc, sample_us);
        if (rc) return rc;
    }
    return 0;
}
"""


def generate(source, sample_source):
    """Keep the original smoke executable path and the official kernel call ABI."""
    signature = "int main(int argc, char** argv)"
    pre, main = source.split(signature)
    call = next(
        n
        for n in ast.walk(ast.parse(sample_source))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and ast.unparse(n.func) == "reference.impl"
    )
    attrs = [k.arg for k in call.keywords if k.arg is not None]
    for kw in call.keywords:
        if kw.arg is None:
            attrs.extend(ast.literal_eval(k) for k in kw.value.keys)
    groups = []

    def group(m):
        idx, dims, dt, _, name, dyn, inp = m.groups()
        groups.append(
            (
                name,
                list(map(int, dims.split(","))) if dims else [],
                int(dt),
                inp == "true",
            )
        )
        return f'Group group_{idx}(tensors("{name}.bin"), {dyn}, {inp});'

    dynamic = GROUP.sub(group, main)
    if (
        not groups
        or dynamic.count("Group group_") != len(groups)
        or len(call.args) != sum(g[3] for g in groups)
    ):
        raise ValueError("unsupported template tensor declarations")
    attr_types = []

    def attr(m):
        typ, name, index, _ = m.groups()
        if int(index) != len(attr_types):
            raise ValueError("unsupported attribute ordering")
        attr_types.append(typ)
        if typ.startswith("std::array"):
            return f"auto {name}_data = integers();"
        if typ == "const char*":
            return f"auto {name}_str = field<std::string>(); auto {name} = {name}_str.c_str();"
        return f"auto {name} = field<{typ}>();"

    dynamic = ATTR.sub(attr, dynamic)
    init = "Session session(device);\n        session.start();"
    if dynamic.count(init) != 1:
        raise ValueError("unsupported ACL session initialization")
    dynamic = dynamic.replace(init, "Session local(device);\n        if (!shared) local.start();"
                              "\n        Session& session = shared ? *shared : local;")
    if len(attrs) != len(attr_types):
        raise ValueError("sample and kernel attribute counts disagree")
    dynamic = re.sub(
        r"(attr_\d+_data)\.data\(\),\s*\d+", r"\1.data(), \1.size()", dynamic
    )
    start = dynamic.index("        run_kernel(")
    end = dynamic.index("        check(aclrtSynchronizeStream(session.stream));", start)
    dynamic = (
        dynamic[:start]
        + """
        const bool timed = std::getenv("OPSB_CASE_TIMING") != nullptr;
        aclrtEvent begin{}, end{};
        if (timed) { check(aclrtCreateEvent(&begin)); check(aclrtCreateEvent(&end));
                     check(aclrtRecordEvent(begin, session.stream)); }
"""
        + dynamic[start:end]
        + """
        if (timed) { check(aclrtRecordEvent(end, session.stream));
                     check(aclrtSynchronizeStream(session.stream)); float ms{};
                     check(aclrtEventElapsedTime(&ms, begin, end));
                     sample_us = ms * 1000.0f;
                     std::cout << "DEVICE_US " << sample_us << std::endl;
                     check(aclrtDestroyEvent(begin)); check(aclrtDestroyEvent(end)); }
"""
        + dynamic[end:]
    )
    code = (
        pre
        + "int smoke_main(int argc, char** argv)"
        + main
        + CPP
        + "int case_main(int argc, char** argv, Session* shared)"
        + dynamic
        + BATCH_MAIN
    )
    return code, groups, list(zip(attrs, attr_types))


def compare(got, want, tolerance):
    import numpy as np

    if got.size != want.size:
        return {
            "pass": False,
            "reason": "size",
            "actual": got.size,
            "expected": want.size,
        }
    got = got.reshape(want.shape)
    rtol, atol, fraction = tolerance
    if want.dtype.kind in "iub" and rtol == atol == 0:
        close = got == want
    else:
        dtype = np.complex128 if want.dtype.kind == "c" else np.float64
        close = np.isclose(
            got.astype(dtype), want.astype(dtype), atol=atol, rtol=rtol, equal_nan=True
        )
    count = int(np.count_nonzero(~close))
    result = {
        "pass": count <= fraction * want.size,
        "mismatches": count,
        "elements": want.size,
    }
    if count:
        idx = np.unravel_index(int(np.argmax(~close)), want.shape)
        result.update(
            index=list(map(int, idx)), actual=str(got[idx]), expected=str(want[idx])
        )
    return result


def compare_output(got, want, tolerance, strict_fp32):
    result = compare(got, want, tolerance)
    if strict_fp32 and want.dtype.name == "float32":
        limit = 6 * 1024 * 1024
        rtol, atol, fraction = tolerance
        limits = min(rtol, 1e-4), min(atol, 2e-6), fraction
        strict = compare(got.reshape(-1)[:limit], want.reshape(-1)[:limit], limits)
        result["strict_fp32"] = dict(strict, rtol=limits[0], atol=limits[1], mismatch_fraction=fraction)
        result["pass"] = result["pass"] and strict["pass"]
    return result


def parts(golden, names, case, defaults):
    if set(golden) == set(names):
        return [({}, golden)]
    # Some template variants encode several kernel invocations in one reference case.
    gen = case.get("params", {}).get("gen")
    if "part" not in defaults or "variant" not in defaults:
        raise ValueError("reference outputs do not match the kernel output contract")
    p = case["params"]
    variants = {
        "layout_pair": ("layout_pair", ["a_{}", "b_{}"]),
        "multi_mode": ("multi_mode", [f"m{i}_{{}}" for i in p.get("modes", [])]),
        "transpose": ("transpose", ["{}_" + v for v in p.get("combos", [])]),
        "k_extreme": ("k_pair", ["{}", "{}_k1"]),
    }
    variant, patterns = variants.get(gen, ("", []))
    if not patterns or {pat.format(n) for pat in patterns for n in names} != set(
        golden
    ):
        raise ValueError("unsupported multipart output mapping")
    result = []
    for i, pat in enumerate(patterns):
        attrs = {"variant": variant, "part": i}
        if gen == "multi_mode":
            attrs["modes"] = p["modes"]
        result.append((attrs, {n: golden[pat.format(n)] for n in names}))
    return result


def prepare_case(root, item, groups, attrs):
    import numpy as np
    import reference
    import sample

    spec = next(c for c in reference.CASES if c["id"] == item["base"])
    # Each invocation is a fresh Python process; derived cases never change the oracle on disk.
    spec.update(seed=item.get("seed", spec.get("seed")))
    spec["params"] = {**spec.get("params", {}), **item.get("params", {})}
    inputs, values = reference.build_case(spec["id"])
    golden = reference.run(spec["id"])
    signature = inspect.signature(reference.impl).parameters
    names = [g[0] for g in groups if not g[3]]
    tolerances = {Path(o[0]).stem: o[4:7] for o in sample.OUTPUTS}
    if set(names) != set(tolerances) or set(inputs) - {g[0] for g in groups if g[3]}:
        raise ValueError("unmapped input or output")
    if set(values) - {name for name, _ in attrs}:
        raise ValueError("unmapped reference attributes")
    manifest = []
    types = {
        "float32": 0,
        "float16": 1,
        "bfloat16": 2,
        "int8": 3,
        "int16": 4,
        "int32": 5,
        "int64": 6,
        "uint8": 7,
        "uint16": 8,
        "uint32": 9,
        "uint64": 10,
        "bool": 11,
        "float64": 12,
    }
    defaults = {
        k: v.default
        for k, v in signature.items()
        if v.default != inspect.Parameter.empty
    }
    for part_index, (overrides, outputs) in enumerate(
        parts(golden, names, spec, defaults)
    ):
        work = root / str(part_index)
        work.mkdir(parents=True)
        (work / "input").mkdir()
        (work / "output").mkdir()
        fields = []
        for name, shape, dt, inp in groups:
            if (
                inp
                and name not in inputs
                and name in defaults
                and defaults[name] is None
            ):
                tensor = np.zeros(
                    shape, dtype=next(k for k, v in types.items() if v == dt)
                )
            else:
                value = (inputs if inp else outputs)[name]
                if value is None:
                    fields.append(0)
                    continue
                tensor = np.asarray(value)
            fields.extend(
                [
                    1,
                    tensor.ndim,
                    *tensor.shape,
                    types[str(tensor.dtype)],
                    tensor.nbytes,
                ]
            )
            if inp:
                tensor.tofile(work / "input" / f"{name}.bin")
        attr_values = {**defaults, **values, **overrides}
        for name, typ in attrs:
            v = attr_values[name]
            if typ.startswith("std::array"):
                fields.extend([len(v), *v])
            elif typ == "const char*" and (not v or any(c.isspace() for c in v)):
                raise ValueError("unsupported whitespace or empty string attribute")
            else:
                fields.append(int(v) if typ == "bool" else v)
        (work / "case.desc").write_text(" ".join(map(str, fields)))
        for name, want in outputs.items():
            if want is not None:
                np.save(work / (name + ".npy"), want, allow_pickle=False)
        manifest.append(dict(path=str(work), tolerances={n: tolerances[n] for n in outputs if outputs[n] is not None}))
    (root / "parts.json").write_text(json.dumps(manifest))


def emit(event, **values):
    print(
        "OPSB_CASE " + json.dumps(dict(event=event, **values), allow_nan=False),
        flush=True,
    )


def main():
    repo = Path.cwd()
    cfg = json.loads((repo.parent / "config.json").read_text())
    sys.path.insert(0, str(repo))
    if len(sys.argv) > 1:
        schema = json.loads((repo.parent / "schema.json").read_text())
        index = int(sys.argv[1])
        item = cfg["selected"][index]
        prepare_case(Path(sys.argv[2]), item, *schema)
        return
    import platform

    import numpy as np

    soc = None
    try:
        mapping = subprocess.run(
            ["npu-smi", "info", "-m"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in mapping.stdout.splitlines():
            columns = line.split(maxsplit=3)
            if (
                len(columns) == 4
                and columns[2] == cfg["device"]
                and columns[0].isdigit()
            ):
                soc = columns[3].replace(" ", "").lower()
                break
    except (OSError, subprocess.TimeoutExpired):
        pass
    emit(
        "environment",
        host=platform.node(),
        platform=platform.machine(),
        python=platform.python_version(),
        numpy=np.__version__,
        sdk=os.path.realpath(
            os.environ.get("TOOLCHAIN_HOME", "/opt/driver/latest")
        ),
        arch=cfg["arch"],
        device=cfg["device"],
        soc=soc,
    )
    source = repo / "main.asc"
    generated, groups, attrs = generate(
        source.read_text(), (repo / "sample.py").read_text()
    )
    source.write_text(generated)
    (repo.parent / "schema.json").write_text(json.dumps([groups, attrs]))
    from lanes import compilation_units, compile_lane
    with compile_lane(cfg, units=1):
        subprocess.run(
            ["cmake", "-S", ".", "-B", "build-casebench", "-DCMAKE_ASC_RUN_MODE=npu",
             "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON", "-DCMAKE_ASC_ARCHITECTURES=" + cfg["arch"]], check=True, timeout=600,
        )
    units = compilation_units(repo / "build-casebench")
    with compile_lane(cfg, units=units) as jobs:
        emit("compiling", units=units, jobs=jobs)
        subprocess.run(
            ["cmake", "--build", "build-casebench", "--parallel", str(jobs)], check=True, timeout=900,
        )
    emit("built")
    from case_batch import run_series
    run_series(repo, cfg, compare_output, emit)


if __name__ == "__main__":
    main()
