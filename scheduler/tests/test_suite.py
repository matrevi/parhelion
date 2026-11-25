import json

import pytest

from scheduler import cli_bridge
from scheduler import suite as suites


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("PARHELION_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PARHELION_MCP_ROLE", raising=False)
    root = tmp_path / "ws"
    root.mkdir()
    return root


def cases():
    return [
        {"id": "01", "source": "official", "perf": False, "buckets": ["official"]},
        {"id": "02", "source": "official", "perf": True, "buckets": ["official", "performance"]},
        {"id": "local/a", "source": "local", "perf": False, "tier": "standard", "buckets": ["local"]},
        {"id": "local/b", "source": "local", "perf": False, "buckets": ["local"]},
    ]


def ids(selected):
    return [c["id"] for c in selected]


def test_legacy_profile_converts_and_migrates(ws):
    legacy = suites.legacy_path(ws, "Op")
    legacy.parent.mkdir(parents=True)
    profile = {"version": 1, "variants": [{"id": "local/v", "base": "01", "seed": 3}],
               "buckets": {"pair": ["01", "local/v"]}, "repeats": 4, "case_timeout": 900}
    legacy.write_text(json.dumps(profile))
    suite = suites.load_suite(ws, "Op")
    assert suite["suite_version"] == 0 and suite["version"] == 2
    assert suite["variants"][0]["tier"] == "final"
    assert suite["repeats"] == 4 and suite["warmup"] == 2 and suite["case_timeout"] == 600
    assert set(suite) == suites.SUITE_FIELDS
    written = suites.migrate(ws, "Op")
    assert written["suite_version"] == 1 and written["updated_at"]
    assert suites.load_suite(ws, "Op")["suite_version"] == 1
    assert json.loads(legacy.read_text()) == profile
    with pytest.raises(ValueError, match="already exists"):
        suites.migrate(ws, "Op")


def test_empty_suite_has_every_field(ws):
    suite = suites.load_suite(ws, "Op")
    assert suite["suite_version"] == 0 and suite["variants"] == [] and suite["strict_fp32"] is False
    (ws / "opsb.workspace.json").write_text(json.dumps({"bench_strict_fp32": True}))
    assert suites.load_suite(ws, "Op")["strict_fp32"] is True
    profile = suites.to_profile(suite)
    assert set(profile) == set(suites.PROFILE_FIELDS) and profile["version"] == 1


def test_levels_follow_the_table():
    suite = suites.normalize({"version": 2, "case_timeout": 30, "repeats": 5})
    standard, label, formal = suites.select(cases(), suite, "standard")
    assert ids(standard) == ["01", "02", "local/a"] and label == "standard" and formal
    final, label, formal = suites.select(cases(), suite, "final")
    assert ids(final) == ["01", "02", "local/a", "local/b"] and formal
    smoke, label, formal = suites.select(cases(), suite, "smoke")
    assert ids(smoke) == ["01", "02"] and label == "anchor_and_performance" and not formal
    explicit, label, formal = suites.select(cases(), suite, "final", ids=["local/b"])
    assert ids(explicit) == ["local/b"] and label == "explicit" and not formal
    with pytest.raises(ValueError, match="Unknown case"):
        suites.select(cases(), suite, "standard", ids=["09"])
    assert suites.protocol(suite, "smoke") == {"warmup": 1, "repeats": 3, "timeout": 8, "tier": 2}
    assert suites.protocol(suite, "standard") == {"warmup": 2, "repeats": 5, "timeout": 30, "tier": 1}
    assert suites.protocol(suite, "final")["repeats"] == 7
    assert suites.protocol({**suite, "repeats": 9}, "final")["repeats"] == 9
    assert suites.requested_level(None, "adaptive") == "smoke"
    assert suites.requested_level(None, "all") == "standard"
    assert suites.requested_level("final", "adaptive") == "final"
    assert suites.requested_level() is None


def test_variant_tier_defaults_to_final():
    suite = suites.normalize({"version": 2, "variants": [{"id": "local/x", "base": "01"}]})
    assert suite["variants"][0]["tier"] == "final"
    with pytest.raises(ValueError, match="tier"):
        suites.normalize({"version": 2, "variants": [{"id": "local/x", "base": "01", "tier": "smoke"}]})


def test_timed_ids_fall_back_to_official_cases():
    suite = suites.normalize({"version": 2, "timed": ["local/a", "missing"]})
    assert suites.timed_ids(cases(), suite) == {"02", "local/a"}
    plain = [{**c, "perf": False} for c in cases()]
    assert suites.timed_ids(plain, suites.normalize({"version": 2})) == {"01", "02"}


@pytest.mark.parametrize("field,value", [("warmup", 11), ("warmup", -1), ("repeats", 0),
                                         ("repeats", 51), ("case_timeout", 601), ("warmup", True)])
def test_out_of_range_values_name_the_field(field, value):
    with pytest.raises(ValueError, match=field):
        suites.normalize({"version": 2, field: value})


def test_unknown_fields_and_versions_are_rejected():
    with pytest.raises(ValueError, match="suite.timing"):
        suites.normalize({"version": 2, "timing": "all"})
    with pytest.raises(ValueError, match="version"):
        suites.normalize({"version": 3})
    with pytest.raises(ValueError, match="weights"):
        suites.normalize({"version": 2, "weights": {"01": -1}})


def test_write_and_add_variant_bump_the_version(ws):
    first = suites.write_suite(ws, "Op", suites.load_suite(ws, "Op"))
    assert first["suite_version"] == 1
    doc = suites.manage(ws, "Op", "add-variant", json.dumps({"id": "local/n", "base": "01", "tier": "standard"}))
    assert doc["suite"]["suite_version"] == 2
    assert suites.load_suite(ws, "Op")["variants"][0]["id"] == "local/n"
    with pytest.raises(ValueError, match="already exists"):
        suites.manage(ws, "Op", "add-variant", json.dumps({"id": "local/n", "base": "01"}))
    with pytest.raises(ValueError, match="variant field"):
        suites.manage(ws, "Op", "add-variant", json.dumps({"id": "local/m", "base": "01", "bogus": 1}))
    shown = suites.manage(ws, "Op", "show")
    assert shown["source"] == "suite" and shown["suite"]["suite_version"] == 2


def test_workers_cannot_manage_the_suite(ws, monkeypatch, capsys):
    monkeypatch.setenv("PARHELION_MCP_ROLE", "worker")
    with pytest.raises(PermissionError):
        suites.manage(ws, "Op", "show")
    code = cli_bridge.main(["suite", "migrate", "--title", "Op", "--workspace", str(ws)])
    assert code == 2 and "platform owns" in capsys.readouterr().err
    assert not suites.suite_path(ws, "Op").exists()
