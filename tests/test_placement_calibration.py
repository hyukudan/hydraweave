"""Current Strix performance evidence uses synthetic identities and CPU-only receipts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
import uuid
from unittest import mock

try:
    import pytest
except ImportError:  # unittest discovery before installation of the optional test extra
    from unittest import SkipTest
    raise SkipTest("requires the test extra (pytest)") from None

from hydraweave import assets
from hydraweave.placement import hetero
from hydraweave.recipes import calibration as c, content_cache, facts, measurements as m, placement, probe, solver
from hydraweave.recipes.reader import RecipeError
from hydraweave.vramplan import PlanError


def evidence_inputs():
    now = time.time()
    device = "GPU-" + str(uuid.uuid4())
    report = {"observed_at": now, "engine_revision": "a" * 40, "engine_sha256": "b" * 64,
              "model_sha256": "c" * 64, "devices": [{"id": "anchor", "uuid": device, "driver": "test-driver",
                                                        "platform": "linux-cuda", "memory_gib": 64}],
              "links": [], "file_store": "not-used", "residency": {"mode": "warm", "state_sha256": "d" * 64}}
    inputs = {"choices": [{"id": "plain", "components": {"schema": "tierstream/components-v1"}}],
              "costs": {"schema": "synthetic-costs", "value": 1}, "curves": {"points": [[1, 2]]}}
    arguments = {"route": "decode", "workload": "default", "context": {"tokens": 8192},
                 "recipe_sha256": "e" * 64}
    receipt = c.receipt(report, inputs, **arguments, measured_at=now - 1, expires_at=now + 60,
                        evidence={"method": "owned synthetic benchmark", "runs": 3, "source_sha256": ["f" * 64]})
    return report, inputs, arguments, receipt


def test_exact_calibration_and_ttl_never_runs_hardware():
    report, inputs, arguments, receipt = evidence_inputs()
    with mock.patch.object(m, "acquire", side_effect=AssertionError("implicit acquisition")):
        assert c.validate(receipt, report, inputs, **arguments)["status"] == "calibrated"
        for now in (receipt["measured_at"] - 1, receipt["expires_at"], float("nan")):
            with pytest.raises(RecipeError, match="expired|future"):
                c.validate(receipt, report, inputs, **arguments, now=now)
    changed = deepcopy(receipt)
    changed["expires_at"] = changed["measured_at"] + c.MAX_AGE_S + 1
    with pytest.raises(RecipeError, match="seven days"):
        c.validate(changed, report, inputs, **arguments)


@pytest.mark.parametrize("field", ["engine_revision", "engine_sha256", "model_sha256", "uuid", "driver",
                                  "memory", "residency", "link", "store", "context", "workload", "route", "settings"])
def test_calibration_refuses_foreign_build_hardware_workload_and_residency(field):
    report, inputs, arguments, receipt = evidence_inputs()
    if field in ("engine_revision", "engine_sha256", "model_sha256"):
        report[field] = "9" * len(report[field])
    elif field in ("uuid", "driver"):
        report["devices"][0][field] = "GPU-" + str(uuid.uuid4()) if field == "uuid" else "changed-driver"
    elif field == "memory":
        report["devices"][0]["memory_gib"] += 1
    elif field == "residency":
        report["residency"]["mode"] = "cold"
    elif field == "link":
        report["links"] = [{"measurement_sha256": "7" * 64, "bytes_per_s": 1e9, "latency_ns": 1000}]
    elif field == "store":
        report["file_store"] = "another-store"
    elif field == "context":
        arguments["context"]["tokens"] += 1
    elif field == "settings":
        arguments["settings"] = {"sets": [["routes.decode.max_rows", 2]], "layers": []}
    else:
        arguments[field] = "different"
    with pytest.raises(RecipeError, match="another build"):
        c.validate(receipt, report, inputs, **arguments)


def test_calibration_rejects_unknown_physical_identity_stale_probe_and_extrapolation():
    report, inputs, arguments, receipt = evidence_inputs()
    for bad in ("unknown", "card0", "AMD-0000", "GPU-00000000-0000-0000-0000-000000000000", "0000:03:00.0"):
        modified = deepcopy(report)
        modified["devices"][0]["uuid"] = bad
        with pytest.raises(RecipeError, match="physical"):
            c.identity(modified, inputs, **arguments)
    modified = deepcopy(report)
    modified["devices"][0]["driver"] = "unknown"
    with pytest.raises(RecipeError, match="driver"):
        c.identity(modified, inputs, **arguments)
    modified = deepcopy(report)
    modified["observed_at"] -= c.MAX_PROBE_AGE_S + 1
    with pytest.raises(RecipeError, match="24 hours"):
        c.validate(receipt, modified, inputs, **arguments)
    modified = deepcopy(receipt)
    modified["extrapolated"] = True
    with pytest.raises(RecipeError, match="extrapolated"):
        c.validate(modified, report, inputs, **arguments)
    inputs["costs"]["value"] += 1
    with pytest.raises(RecipeError, match="documents changed"):
        c.validate(receipt, report, inputs, **arguments)


def test_calibration_link_requires_actual_bandwidth_and_latency_evidence():
    report, inputs, arguments, receipt = evidence_inputs()
    report["links"] = [{"kind": "usb4", "hosts": ["synthetic-a", "synthetic-b"], "gbps": 40}]
    with pytest.raises(RecipeError, match="measured bandwidth"):
        c.identity(report, inputs, **arguments)
    report["links"][0].update(measurement_sha256="6" * 64, bytes_per_s=1e9, latency_ns=3000)
    assert c.identity(report, inputs, **arguments)["links_sha256"]


def test_measurement_cache_rejects_old_future_legacy_and_bad_provenance(tmp_path):
    now = [1000.0]
    cache = m.Cache(tmp_path, clock=lambda: now[0])
    identity = m.key("GPU-" + str(uuid.uuid4()), "test", "a" * 64, "test-store")
    result = m.median([{"h2d_mbps": 1, "d2h_mbps": 2, "h2d_lat_ns": 3}] * 9, m.XFER_METHOD)
    cache.save(identity, "xfer", result)
    assert cache.lookup(identity, "xfer")["status"] == "measured"
    for elapsed in (-1, m.MAX_AGE_S, m.MAX_AGE_S + 1000):
        now[0] = 1000 + elapsed
        assert cache.lookup(identity, "xfer")["status"] == "unpredicted"
    now[0] = 1000
    path = next(cache.directory.glob("*.json"))
    saved = json.loads(path.read_bytes())
    for field in ("schema", "measured_at", "expires_at"):
        modified = deepcopy(saved)
        modified.pop(field)
        content_cache._write(path, modified)
        assert cache.lookup(identity, "xfer")["status"] == "unpredicted"
    for bad in ([{}] * 9, [{"h2d_mbps": True, "d2h_mbps": 1, "h2d_lat_ns": 1}] * 9,
                [{"file_read_mbps": 100}] * 9):
        with pytest.raises(RecipeError):
            m.median(bad, m.XFER_METHOD)
    with pytest.raises(RecipeError):
        cache.save(identity, "invalid", result)


def test_missing_current_xfer_store_or_ambiguous_gpu_never_uses_old_key(tmp_path):
    identity = m.key("GPU-" + str(uuid.uuid4()), "test", "a" * 64, "test-store")
    cache = m.Cache(tmp_path)
    cache.save(identity, "xfer", m.median([{"h2d_mbps": 1, "d2h_mbps": 2, "h2d_lat_ns": 3}] * 9, m.XFER_METHOD))
    report = {"devices": [{"uuid": identity["uuid"], "driver": identity["driver"]}],
              "file_store": identity["store_id"], "xfer_sha256": {"anchor": identity["xfer_sha256"]}}
    assert placement.cached_measurements(report, {"anchor": identity}, cache)["probe_xfer"]
    for field in ("devices", "file_store", "xfer_sha256"):
        changed = deepcopy(report)
        changed.pop(field)
        assert placement.cached_measurements(changed, {"anchor": identity}, cache)["probe_xfer"] == {}
    report["devices"] *= 2
    assert placement.cached_measurements(report, {"anchor": identity}, cache)["probe_xfer"] == {}


def test_malformed_current_probe_and_empty_performance_cannot_claim_calibration(tmp_path):
    for report in (None, [], {"devices": None}):
        with pytest.raises(RecipeError, match="PL_E_SCHEMA"):
            placement.cached_measurements(report, {}, m.Cache(tmp_path))
    report, inputs, arguments, receipt = evidence_inputs()
    with pytest.raises(RecipeError, match="measured cost"):
        c.receipt(report, {}, **arguments, measured_at=receipt["measured_at"], expires_at=receipt["expires_at"],
                  evidence=receipt["evidence"])
    with pytest.raises(RecipeError, match="current probe"):
        c.validate(receipt, None, inputs, **arguments)


def test_real_asset_cache_path_preserves_model_identity_without_model_read(tmp_path, monkeypatch):
    model, binary = tmp_path / "model.gguf", tmp_path / "bin" / "engine"
    binary.parent.mkdir()
    model.write_bytes(b"synthetic-model")
    binary.write_bytes(b"synthetic-engine")
    engine_sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    model_sha = hashlib.sha256(model.read_bytes()).hexdigest()
    content_cache._write(tmp_path / "MANIFEST.json", {"engine_commit": "a" * 40,
                                                     "binaries": {"engine": engine_sha}})
    stat = model.stat()
    entry = dict(bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, inode=stat.st_ino, dev=stat.st_dev,
                 sha256=model_sha, verified_at=time.time())
    monkeypatch.setattr(assets, "manifest_path", lambda: tmp_path / "verified.json")
    assets.write_manifest({"pc": {str(model): entry}})
    cluster = SimpleNamespace(assets={"pc": {"ds41_model": dict(path=str(model), bytes=stat.st_size, sha256=model_sha)}})
    ctx = SimpleNamespace(pc="pc", model="deepseek-v4.1-flash")
    notes = []
    open_original = Path.open
    def guarded_open(path, *args, **kwargs):
        assert path != model, "passive identity opened model contents"
        return open_original(path, *args, **kwargs)
    with mock.patch.object(Path, "open", guarded_open):
        found = facts._identities(cluster, ctx, binary, "a" * 40, notes)
    assert found == dict(engine_revision="a" * 40, engine_sha256=engine_sha, model_sha256=model_sha)
    assert notes == []
    model.write_bytes(b"changed-model!!")
    assert "model_sha256" not in facts._identities(cluster, ctx, binary, "a" * 40, [])


def test_amd_physical_id_missing_or_zero_is_not_card_identity():
    card = {"product_name": {"status": "ok", "value": "Synthetic AMD"},
            "gtt_total_bytes": {"status": "ok", "value": 16 << 30}}
    report = {"checks": {"amd": {"status": "ok", "value": [card]}}}
    for value in (None, "0", "00000000", "unknown"):
        card["unique_id"] = {"status": "ok", "value": value}
        assert probe.node_device(report)["uuid"]["value"] is None
    card["unique_id"]["value"] = "0x12345678"
    card["pci"] = {"status": "ok", "value": "0000:02:01.0"}
    found = probe.node_device(report)
    assert found["uuid"]["value"] == "AMD-12345678"
    assert found["pci"]["value"] == "0000:02:01.0"


def test_solver_accepts_current_receipt_and_refuses_it_after_expiry():
    from tests.test_placement_solver import SolverTests
    helper = SolverTests()
    helper.setUp()
    try:
        report, inputs, arguments, receipt = evidence_inputs()
        bundle = json.loads(helper.bundle.read_bytes())
        bundle["probe"] = report
        bundle["inputs"] = inputs
        bundle["calibration"] = c.receipt(report, inputs, route=helper.rid, workload="default", context=bundle["context"],
            recipe_sha256=hashlib.sha256(helper.recipe.read_bytes()).hexdigest(), measured_at=receipt["measured_at"],
            expires_at=receipt["expires_at"], evidence=receipt["evidence"])
        helper.bundle.write_bytes(json.dumps(bundle).encode())
        result = helper.solve()
        assert result["calibration"]["status"] == "calibrated"
        record = content_cache._read(helper.root / "solver-plans" / (result["selection"] + ".json"), "PL_E_STALE")
        assert record["snapshot"]["request"]["inputs"]["costs"] == inputs["costs"]
        with mock.patch.object(c.time, "time", return_value=receipt["expires_at"] + 1):
            with pytest.raises(RecipeError, match="expired"):
                solver.pick(helper.cluster, result["selection"], "plain", "picked", backend=helper.backend)
        bundle["probe"]["devices"][0]["driver"] = "different"
        helper.bundle.write_bytes(json.dumps(bundle).encode())
        with pytest.raises(RecipeError, match="another build"):
            helper.solve()
    finally:
        helper.doCleanups()


def test_direct_solver_withholds_unbound_figures_without_mutating_input():
    from hydraweave.recipes import core
    backend = core.CoreBackend(core.Core("fake"))
    inputs = {"costs": {"x": 1}, "curves": {"x": 2}, "choices": [{"id": "plain", "costs": {"x": 3},
                                                                       "probe_xfer": {"old": 999}}]}
    with mock.patch.object(backend, "solve") as run:
        placement.solve("unused", route="decode", probe={"probe_xfer": {"old": 999}}, inputs=inputs, backend=backend)
    sent = run.call_args.kwargs
    assert "costs" not in sent["inputs"] and "curves" not in sent["inputs"]
    assert "costs" not in sent["inputs"]["choices"][0]
    assert sent["probe"]["probe_xfer"] == sent["inputs"]["choices"][0]["probe_xfer"] == {}
    assert inputs["costs"] == {"x": 1}


def test_extrapolated_solver_rows_and_cyclic_sources_refuse_save(tmp_path):
    for prediction in ({"tag": "extrapolated"}, {"tag": "estimate", "extrapolated": True}):
        row = solver.rows({"schema": "tierstream/solver-v1", "rows": [{"id": "x", "prediction": prediction}]})[0]
        assert row["saveable"] is False and "extrapolated" in row["save_refused"]
    cycle = tmp_path / "cycle.json"
    cycle.write_text('{"costs":"cycle.json"}')
    with pytest.raises(RecipeError, match="cyclic"):
        solver._materialize(str(cycle), {}, directory=tmp_path)
    with pytest.raises(RecipeError, match="format changed"):
        solver._check({})


def test_historical_hetero_models_do_not_certify_current_hardware():
    assert hetero.pipeline(12).to_dict()["current_hardware"] is False
    for curve in ([[-1, .5]], [[0, True]], [[0, .5], [0, .6]], [None]):
        with pytest.raises(PlanError):
            hetero.hot(1, curve)
    assert hetero.hot(20, [(0, .5), (10, .5)]).extrapolated is True
    for options in ({"vram_cap_gib": True}, {"margin_gib": False}, {"front_k": 41}, {"link": "invalid"}):
        request = {"vram_cap_gib": 90, "curve": [(0, .5), (10, .6)], "front_k": 0, **options}
        with pytest.raises(PlanError):
            hetero.candidates(**request)


def test_disk_identity_distinguishes_replacements_and_subsecond_changes():
    stat = {"device": 1, "inode": 2, "size": 8, "mtime_ns": 1_100_000_000}
    original = probe.DiskCache.key("node", "model.gguf", stat)
    assert probe.DiskCache.key("node", "model.gguf", dict(stat, mtime_ns=1_200_000_000)) != original
    assert probe.DiskCache.key("node", "model.gguf", dict(stat, inode=3)) != original


def test_parallel_disk_cache_writers_keep_each_receipt_and_invalid_dates_are_misses(tmp_path):
    path = tmp_path / "facts.json"
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: facts.Cache(path).put(str(i), {"gbytes_ps": 1 + i, "method": "direct"}, now=1000),
                      range(18)))
    cache = facts.Cache(path)
    for i in range(18):
        assert cache.get(str(i), now=1001)["gbytes_ps"] == 1 + i
        assert cache.get(str(i), now=999) is None
        assert cache.get(str(i), now=1000 + probe.CACHE_TTL_S + 1) is None
    assert not list(tmp_path.glob("*.tmp"))


def test_importing_disk_receipt_preserves_its_measurement_time(tmp_path):
    now = time.time()
    cache = facts.Cache(tmp_path / "cache.json")
    path = tmp_path / "receipt.json"
    receipt = {"disk_read_gbytes_ps": 1, "method": "direct", "host": "node", "path": "model.gguf",
               "measured_at": now - probe.CACHE_TTL_S + 10}
    path.write_text(json.dumps(receipt))
    assert cache.put_evidence("node", "model.gguf", path)["measured_at"] == receipt["measured_at"]
    assert cache.evidence("node", "model.gguf", now=now + 11) is None
    for invalid in (now + 60, now - probe.CACHE_TTL_S - 1, True, None):
        path.write_text(json.dumps(dict(receipt, measured_at=invalid)))
        with pytest.raises(probe.ProbeError, match="measured_at"):
            cache.put_evidence("node", "model.gguf", path)


def test_invalid_disk_probe_result_never_becomes_evidence(tmp_path):
    cache = facts.Cache(tmp_path / "cache.json")
    stat = {"device": 1, "size": 8, "mtime_ns": 1}
    for result in ({"gbytes_ps": 1}, {"method": "direct", "gbytes_ps": True},
                   {"method": "direct", "gbytes_ps": float("inf")}, None):
        assert probe.disk_fact(cache, "node", "model.gguf", stat, True, lambda: result)["value"] is None
