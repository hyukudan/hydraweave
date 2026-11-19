"""CPU-only regressions for passive planning, descriptor publication and complete model sizes."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
import hashlib
import json
import os
import struct
import tempfile
import unittest
from unittest import mock

from hydraweave import assets, modelfiles, vramplan
from hydraweave.placement import ds41
from hydraweave.recipes import commands, descriptors, facts, runtime
from hydraweave.recipes.reader import RecipeError
from tests.test_vramplan import toy, write_gguf


class ModelFilesSafetyTests(unittest.TestCase):
    def test_unknown_quantization_cannot_produce_negative_bytes_past_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.gguf"
            write_gguf(model, [("tensor", [32], 0)])
            raw = bytearray(model.read_bytes())
            kind_at = raw.index(b"tensor") + len(b"tensor") + 4 + 8
            raw[kind_at:kind_at + 4] = struct.pack("<I", 999)
            raw[kind_at + 4:kind_at + 12] = struct.pack("<Q", len(raw) + 128)
            model.write_bytes(raw)
            with self.assertRaisesRegex(modelfiles.ModelFileError, "outside the file"):
                modelfiles.read(model)

    def test_missing_split_shard_is_refused_before_sizing_or_caching(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "model-00001-of-00002.gguf"
            write_gguf(first, [("a", [32], 0)])
            for path in (first, root):
                for operation in (modelfiles.read, modelfiles.fingerprint,
                                  descriptors.Store(root / "cache").save):
                    with self.subTest(path=path, operation=operation):
                        with self.assertRaisesRegex(modelfiles.ModelFileError, "incomplete|shard"):
                            operation(path)

    def test_split_indices_must_be_in_the_declared_range(self):
        for index, count in ((0, 1), (1, 0), (2, 1)):
            with self.subTest(index=index, count=count), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"model-{index:05d}-of-{count:05d}.gguf"
                write_gguf(path, [("a", [32], 0)])
                with self.assertRaisesRegex(modelfiles.ModelFileError, "shard"):
                    modelfiles.read(path)

    def test_descriptors_and_fingerprints_use_the_exact_complete_shard_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "model-00001-of-00002.gguf"
            second = root / "model-00002-of-00002.gguf"
            write_gguf(first, [("a", [32], 0)])
            write_gguf(second, [("b", [64], 0)])
            # Neither an unrelated split nor a similar suffix belongs to this model.
            write_gguf(root / "model-00001-of-00003.gguf", [("other", [32], 0)])
            decoy = root / "model-extra-of-00002.gguf"
            write_gguf(decoy, [("decoy", [32], 0)])
            store = descriptors.PlacementStore(root / "cache")
            self.assertEqual(store.save(first)["files"], 2)
            self.assertEqual(modelfiles.fingerprint(first),
                             [(first.name, first.stat().st_size), (second.name, second.stat().st_size)])
            decoy.unlink()
            self.assertEqual([t.name for t in store.load(first)[1]], ["a", "b"])
            second.unlink()
            with self.assertRaises(descriptors.DescriptorError):
                store.load(first)


class PassivePlanningSafetyTests(unittest.TestCase):
    def test_simultaneous_plans_keep_their_own_descriptor_store(self):
        barrier = Barrier(2)
        original = modelfiles.read

        def plan(name):
            store = SimpleNamespace(load=lambda _: ({"store": name}, []))
            with descriptors.saved_only(store):
                barrier.wait(timeout=5)
                value = modelfiles.read("never-open-a-model.gguf")
                barrier.wait(timeout=5)
            return value

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                values = list(pool.map(plan, ("first", "second")))
            self.assertIs(modelfiles.read, original)
            self.assertEqual(values, [({"store": "first"}, []), ({"store": "second"}, [])])
        finally:
            # Keep the regression isolated even when run against the old global monkeypatch.
            modelfiles.read = original

    def test_nested_saved_only_restores_outer_reader_and_never_falls_back(self):
        outer = SimpleNamespace(load=lambda _: ({"store": "outer"}, []))
        missing = mock.Mock(load=mock.Mock(side_effect=descriptors.DescriptorError("missing")))
        with mock.patch.object(modelfiles, "gguf", side_effect=AssertionError("implicit model read")):
            with descriptors.saved_only(outer):
                with self.assertRaisesRegex(descriptors.DescriptorError, "missing"):
                    with descriptors.saved_only(missing):
                        modelfiles.read("model.gguf")
                self.assertEqual(modelfiles.read("model.gguf"), ({"store": "outer"}, []))

    def test_explicit_reads_in_other_threads_do_not_use_an_active_plan_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.gguf"
            write_gguf(model, [("actual", [32], 0)])
            missing = mock.Mock(load=mock.Mock(side_effect=descriptors.DescriptorError("missing")))
            with descriptors.saved_only(missing), ThreadPoolExecutor(max_workers=1) as pool:
                _, tensors = pool.submit(modelfiles.read, model).result(timeout=5)
            self.assertEqual([tensor.name for tensor in tensors], ["actual"])

    def test_concurrent_descriptor_probes_publish_complete_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.gguf"
            write_gguf(model, [("actual", [32], 0)])
            store = descriptors.PlacementStore(root / "cache")
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: store.save(model), range(64)))
            self.assertEqual([tensor.name for tensor in store.load(model)[1]], ["actual"])
            self.assertEqual(list(store.directory.iterdir()), [store._path(model)])


class PlacementCompatibilityTests(unittest.TestCase):
    def test_release_identity_works_without_python311_file_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin" / "engine"
            binary.parent.mkdir()
            raw = b"fixture release executable" * 100000
            binary.write_bytes(raw)
            expected = hashlib.sha256(raw).hexdigest()
            (root / "MANIFEST.json").write_text(json.dumps({
                "engine_commit": "a" * 40, "binaries": {binary.name: expected}}), encoding="utf-8")
            with mock.patch.object(hashlib, "file_digest", None, create=True), \
                    mock.patch.object(assets, "read_manifest", return_value={}):
                result = facts._identities(SimpleNamespace(assets={}), SimpleNamespace(model="ds41", pc="local"),
                                           binary, "a" * 40, [])
            self.assertEqual(result["engine_sha256"], expected)

    def test_launch_checks_release_digest_without_python311_file_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "bin" / "engine"
            binary.parent.mkdir()
            binary.write_bytes(b"verified release")
            build = hashlib.sha256(binary.read_bytes()).hexdigest()
            data = dict(runtime.preset(), engine_sha=runtime.replica_args.AUDITED_REVISION)
            (root / "MANIFEST.json").write_text(json.dumps({
                "engine_commit": data["engine_sha"], "binaries": {binary.name: build}}), encoding="utf-8")
            reference = dict(build_sha256=build, receipts_sha256="b" * 64)
            selected = dict(model="must-not-open-model.gguf")
            cache = mock.Mock(manifests=SimpleNamespace(directory=root))
            cache.record.return_value = dict(reference=reference, selected=selected, manifest_sha256="c" * 64)
            plan = dict(engine=data["engine"], effective_sha256="a" * 64, route="decode",
                        devices=[dict(host="local")], **reference)
            recipe = dict(routes=[dict(id="decode", phase="decode", components=dict(arena=dict(units=1)))])
            admitted = dict(inputs=dict(routes=dict(decode=dict(
                components=reference, selected_content=selected, receipts=str(root / "receipts.json")))),
                policies=dict(decode={}))
            with mock.patch.object(hashlib, "file_digest", None, create=True), \
                    mock.patch.object(runtime, "preset", return_value=data), \
                    mock.patch.object(runtime, "executable", return_value=binary), \
                    mock.patch.object(runtime, "render"), \
                    mock.patch.object(commands, "components_store", return_value=cache), \
                    mock.patch.object(commands, "runtime_dir", return_value=root):
                result = runtime.decision(None, recipe, SimpleNamespace(pc="local"), None, "a" * 64,
                                          dict(decode=plan), admitted)
                self.assertTrue(result["placement"])
                binary.write_bytes(b"different executable")
                with self.assertRaisesRegex(RecipeError, "digest differs"):
                    runtime.decision(None, recipe, SimpleNamespace(pc="local"), None, "a" * 64,
                                     dict(decode=plan), admitted)


class PlanningBudgetSafetyTests(unittest.TestCase):
    def test_nonfinite_host_limit_and_negative_margin_never_bypass_budgets(self):
        for values in ({"host_gib": float("nan")}, {"host_gib": float("inf")}, {"host_gib": -1},
                       {"margin_gib": -1, "margin_fraction": -1}, {"margin_fraction": float("nan")},
                       {"margin_gib": float("nan")}, {"vram_cap_gib": True}):
            with self.subTest(values=values), self.assertRaises(vramplan.PlanError):
                vramplan.plan(toy(), vramplan.Inputs(**dict({"vram_cap_gib": 8}, **values)))

    def test_ds41_rejects_bad_budgets_before_opening_model_headers(self):
        defaults = dict(vram_cap_gib=90, host_gib=74, positions=8192)
        for values in ({"host_gib": float("nan")}, {"margin_gib": -1}, {"vram_cap_gib": True},
                       {"reserve_gib": -1}, {"context_gib": -1}, {"free_at_start_gib": float("inf")},
                       {"slots": 0}, {"positions": 0}, {"host_gib": None}):
            with self.subTest(values=values), \
                    mock.patch.object(modelfiles, "read", side_effect=AssertionError("model opened")), \
                    self.assertRaises(vramplan.PlanError):
                ds41.predict_prefill("must-not-open.gguf", **dict(defaults, **values))


class ClusterFactsConcurrencyTests(unittest.TestCase):
    def test_node_queries_overlap_and_one_failed_node_preserves_other_facts(self):
        for failing in (False, True):
            with self.subTest(failing=failing):
                started = Barrier(2)

                def call(mid, verb):
                    if verb == "gpu":
                        # Sequential probes cannot satisfy this barrier: no timing-based speed assertion.
                        started.wait(timeout=1)
                        return {"checks": {"ram": {"status": "ok", "value": {
                            "total_bytes": 128 * vramplan.GIB, "available_bytes": 100 * vramplan.GIB}}}}
                    if failing and mid == "second":
                        raise OSError("synthetic node failure")
                    return {"checks": {"interfaces": {"status": "ok", "value": [
                        {"state": "up", "negotiated_mbps": 1000}]}}}

                context = SimpleNamespace(pc="local", model="test", nodes=["first", "second"], service=None)
                with mock.patch.object(facts, "node_runtime", return_value={}):
                    _, gathered, availability, _ = facts.gather(
                        SimpleNamespace(assets={}), context, call=call, gpus=lambda: [],
                        memory=lambda: dict(total_gib=128, free_gib=100, free_commit_gib=100))
                self.assertEqual(list(gathered["devices"]), ["first", "second"])
                self.assertTrue(availability["devices"]["first"]["available"])
                self.assertEqual(availability["devices"]["second"]["available"], not failing)
                if failing:
                    self.assertIn("synthetic node failure", availability["devices"]["second"]["why"])
                    self.assertIsNone(availability["hosts"]["second"]["free_ram_gib"])


class RecipeFixtureIsolationTests(unittest.TestCase):
    def test_inherited_product_and_xdg_roots_cannot_escape_the_fixture(self):
        from hydraweave import paths
        from hydraweave.recipes import service
        from tests.recipe_fixtures import isolated

        with tempfile.TemporaryDirectory() as directory:
            inherited = dict(XDG_STATE_HOME=directory, XDG_DATA_HOME=directory, HYDRAWEAVE_HOME=directory)
            with mock.patch.dict(os.environ, inherited):
                with isolated() as environment:
                    for path in (paths.config_dir(), paths.state_dir(), paths.data_dir(), service.runtime_dir()):
                        self.assertTrue(path.is_relative_to(environment.root), str(path))
                    service.receipts_dir().mkdir(parents=True)
                    (service.receipts_dir() / "test-only.json").write_text("{}", encoding="utf-8")
                self.assertEqual({key: os.environ[key] for key in inherited}, inherited)
                self.assertEqual(list(Path(directory).iterdir()), [])
