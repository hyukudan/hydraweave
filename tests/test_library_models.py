"""Model discovery: machine model roots, unit classification (format, quant) and the engine family mapping."""
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from hydraweave import downloads
from hydraweave.config import ConfigError, model_roots_of, parse
from hydraweave.telemetry.store import Store
from tests.test_config import example
from tests.test_library_control import known_homes


NODE = Path(__file__).resolve().parents[1] / "node"
sys.path.insert(0, str(NODE))
spec = importlib.util.spec_from_file_location("node_models_discovery", NODE / "hydraweave_models.py")
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)
sys.path.remove(str(NODE))


def gguf_header(file_type):
    """A minimal GGUF v3 header: architecture string, a string array (a tokenizer) and general.file_type."""
    def string(value):
        data = value.encode("utf-8")
        return struct.pack("<Q", len(data)) + data
    kvs = string("general.architecture") + struct.pack("<I", 8) + string("llama")
    kvs += string("tokenizer.ggml.tokens") + struct.pack("<IIQ", 9, 8, 3) + b"".join(string(t) for t in ("a", "b", "c"))
    kvs += string("general.file_type") + struct.pack("<II", 4, file_type)
    return b"GGUF" + struct.pack("<IQQ", 3, 0, 3) + kvs


class ModelRootsConfigTests(unittest.TestCase):
    def test_default_is_lab_models_and_explicit_key_wins(self):
        obj = example()
        obj["machines"][1]["lab_dir"] = "/home/sergioc/ai-cluster-lab"
        obj["machines"][2].update(lab_dir="/srv/lab", model_roots=["/data/models", "/srv/more"])
        cluster = parse(obj)
        self.assertEqual(model_roots_of(cluster.machines["halo-1"]), ["/home/sergioc/ai-cluster-lab/models"])
        self.assertEqual(model_roots_of(cluster.machines["halo-2"]), ["/data/models", "/srv/more"])
        self.assertEqual(model_roots_of(cluster.machines["workstation"]), [])       # no lab_dir, no key
        obj["machines"][1]["model_roots"] = []                                      # explicitly none
        self.assertEqual(model_roots_of(parse(obj).machines["halo-1"]), [])
        obj["machines"][0]["model_roots"] = ["D:\\models"]
        self.assertEqual(model_roots_of(parse(obj).machines["workstation"]), ["D:\\models"])

    def test_invalid_model_roots_are_refused(self):
        bad = [(1, None), (1, "/data"), (1, ["relative"]), (1, ["/opt/../x"]), (1, ["/opt\\x"]), (1, ["/a", "/a"]),
               (1, [f"/m{i}" for i in range(17)]), (1, ["D:\\models"]), (0, ["/posix/on/windows"]), (1, [3])]
        for index, value in bad:
            obj = example()
            obj["machines"][index]["model_roots"] = value
            with self.subTest(value=value), self.assertRaises(ConfigError):
                parse(obj)


class ModelRootsLibraryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        obj = example()
        obj["machines"][1]["lab_dir"] = "/home/sergioc/ai-cluster-lab"
        obj["machines"][2]["model_roots"] = ["/data/models"]
        self.cluster = parse(obj)
        known_homes(self, self.cluster, home="/home/sergioc")

    def test_model_roots_are_scanned_by_default(self):
        roots = downloads.library_roots(self.cluster)
        self.assertIn({"path": "/home/sergioc/ai-cluster-lab/models", "source": "config"}, roots["halo-1"])
        self.assertIn({"path": "/data/models", "source": "config"}, roots["halo-2"])

    def test_library_roots_accepts_the_lab_models_directory_and_below(self):
        saved = downloads.set_roots(self.cluster, self.store, "halo-1",
                                    ["/home/sergioc/ai-cluster-lab/models", "/home/sergioc/ai-cluster-lab/models/ds41-exl3"])
        self.assertEqual(saved["roots"], ["/home/sergioc/ai-cluster-lab/models",
                                          "/home/sergioc/ai-cluster-lab/models/ds41-exl3"])
        paths = [row["path"] for row in downloads.library_roots(self.cluster, self.store)["halo-1"]]
        self.assertIn("/home/sergioc/ai-cluster-lab/models/ds41-exl3", paths)
        downloads.set_roots(self.cluster, self.store, "halo-2", ["/data/models/sub"])
        for machine, value in (("halo-1", "/home/sergioc/ai-cluster-lab"), ("halo-1", "/home/sergioc/other"),
                               ("halo-1", "/home/sergioc/ai-cluster-lab/models/.hidden"), ("halo-2", "/data")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "inside a configured root"):
                downloads.set_roots(self.cluster, self.store, machine, [value])

    def test_a_model_root_that_is_a_home_is_never_a_base(self):
        obj = example()
        obj["machines"][1]["model_roots"] = ["/home/sergioc"]
        cluster = parse(obj)
        known_homes(self, cluster, home="/home/sergioc")
        self.assertNotIn("/home/sergioc", downloads.configured_bases(cluster, cluster.machines["halo-1"]))
        self.assertEqual([r for r in downloads.library_roots(cluster)["halo-1"] if r["path"] == "/home/sergioc"], [])


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hydraweave-discovery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "models"
        files = {
            "DeepSeek-V4.1-Flash-1017/DeepSeek-V4.1-Flash-1017-DSpark-MXFP4.gguf": b"GGUF",
            "DeepSeek-V4.1-Flash-1017/DeepSeek-V4.1-Flash-1017-Q2.gguf": b"GGUF",
            "DeepSeek-V4.1-Flash-1017/DeepSeek-V4.1-Flash-1017-vision-BF16.gguf": b"GGUF",
            "GLM-5.3-Flash-EXL3-4.05bpw/config.json": json.dumps(
                {"quantization_config": {"quant_method": "exl3", "bits": 4.05}}).encode(),
            "GLM-5.3-Flash-EXL3-4.05bpw/model-00001-of-00019.safetensors": b"x" * 10,
            "GLM-5.3-Flash-EXL3-4.05bpw/model-00002-of-00019.safetensors": b"x" * 20,
            "MiniMax-M3.1-Flash-EXL3-3.06bpw/model-00001-of-00002.safetensors": b"x",
            "Qwen3.8-Flash-Next/Qwen3.8-Flash-Next-UD-Q4_K_XL-merged.gguf": b"GGUF",
            "Qwen3.8-Flash-Next/MTP/mtp-Qwen3.8-Flash-Next-shared-distillG-Q8_0.gguf": b"GGUF",
            "Qwen3.8-Flash-Next-IQ2_XS/Qwen3.8-Flash-Next-IQ2_XS-00001-of-00002.gguf": b"GGUF",
            "Qwen3-0.6B/config.json": json.dumps({"torch_dtype": "bfloat16"}).encode(),
            "Qwen3-0.6B/model.safetensors": b"x" * 7,
            "ds41-exl3/ours-K3/ours-K3.sha256": b"",
            "ds41-exl3/ours-K3/weights.safetensors": b"x",
            "ds41-exl3/ours-K4/ours-K4.sha256": b"",
            "ds41-native-fp8-L0-3/config.json": json.dumps({"quantization_config": {"quant_method": "fp8"}}).encode(),
            "ds41-native-fp8-L0-3/model.safetensors": b"x",
            "h3/notes.md": b"h3",
            "h3-image/minimax_h3_image_pruned_int8_convrot.safetensors": b"x",
            "h3-image/minimax_h3_image_pruned_w4a8.safetensors": b"x",
            "h3-image/minimax_h3_image_vae_fp16.safetensors": b"x",
            "minimax-drafts/README": b"drafts",
            "plain/weights.gguf": gguf_header(15),
            "Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf": b"GGUF" + b"\0" * 20,
            "scan.log": b"log",
            "notes.txt": b"txt",
        }
        for rel, data in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def scan(self):
        reply = node.run("models", {"roots": [str(self.root)]})
        self.assertNotIn("error", reply)
        return {unit["name"]: unit for unit in reply["roots"][0]["units"]}, reply["roots"][0]

    def test_units_report_format_quant_and_size(self):
        units, root = self.scan()
        expected = {
            "DeepSeek-V4.1-Flash-1017": ("gguf", "MXFP4, Q2, BF16", "name"),
            "GLM-5.3-Flash-EXL3-4.05bpw": ("exl3", "exl3 4.05bpw", "config"),
            "MiniMax-M3.1-Flash-EXL3-3.06bpw": ("exl3", "exl3 3.06bpw", "name"),
            "Qwen3.8-Flash-Next": ("gguf", "Q4_K_XL, Q8_0", "name"),
            "Qwen3.8-Flash-Next-IQ2_XS": ("gguf", "IQ2_XS", "name"),
            "Qwen3-0.6B": ("safetensors", "BF16", "config"),
            "ds41-exl3": ("exl3", None, None),
            "ds41-native-fp8-L0-3": ("safetensors", "FP8", "config"),
            "h3": ("other", None, None),
            "h3-image": ("safetensors", "INT8, W4A8, FP16", "name"),
            "minimax-drafts": ("other", None, None),
            "plain": ("gguf", "Q4_K_M", "header"),
            "Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf": ("gguf", "Q4_K_M", "name"),
        }
        self.assertEqual(set(units), set(expected))                 # no *.log / *.txt unit
        self.assertEqual(root["other_files"], 2)
        for name, (fmt, quant, source) in expected.items():
            with self.subTest(unit=name):
                unit = units[name]
                self.assertEqual((unit["format"], unit["quant"], unit["quant_source"]), (fmt, quant, source))
                self.assertEqual(unit["size_bytes"], unit["bytes"])
        self.assertEqual(units["GLM-5.3-Flash-EXL3-4.05bpw"]["size_bytes"], 30 + len(json.dumps(
            {"quantization_config": {"quant_method": "exl3", "bits": 4.05}})))

    def test_header_reads_are_bounded(self):
        with patch.object(node, "HEADER_READS", 0):
            units, _ = self.scan()
        self.assertEqual((units["plain"]["format"], units["plain"]["quant"]), ("gguf", None))

    def test_name_and_header_parsers(self):
        self.assertEqual(node.name_quants("Qwen3.8-Flash-Next-UD-Q4_K_XL-merged.gguf"), ["Q4_K_XL"])
        self.assertEqual(node.name_quants("GLM-5.3-Flash-EXL3-4.05bpw", exl3=True), ["exl3 4.05bpw"])
        self.assertEqual(node.name_quants("Qwen3-0.6B"), [])
        self.assertEqual(node.gguf_file_type(gguf_header(20)), "IQ2_XS")
        self.assertEqual(node.gguf_file_type(gguf_header(38)), "MXFP4")
        self.assertIsNone(node.gguf_file_type(b"GGUF"))
        self.assertIsNone(node.gguf_file_type(gguf_header(15)[:40]))
        self.assertIsNone(node.gguf_file_type(b"NOPE" + b"\0" * 64))


class FamilyMappingTests(unittest.TestCase):
    def test_names_map_to_families_and_presets(self):
        cluster = parse(example())
        halo = cluster.machines["halo-1"]
        cases = {
            "Qwen3.8-Flash-Next": ("qwen-replicas", ["qwen-replicas", "qwen-replicas-w5"], None),
            "DeepSeek-V4.1-Flash-1017": ("ds41-pair", ["ds41-pair", "ds41-pair-long", "ds41-pair-w5"], None),
            "ds41-exl3": ("ds41-pair", ["ds41-pair", "ds41-pair-long", "ds41-pair-w5"], None),
            "MiniMax-M3.1-Flash-EXL3-3.06bpw": ("minimax-pair", ["minimax-pair"], None),
            "minimax-drafts": ("minimax-pair", ["minimax-pair"], None),
            "GLM-5.3-Flash-EXL3-4.05bpw": ("glm", [], "no preset"),
            "Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf": ("gemma", [], "no preset"),
            "h3": ("h3-image", [], "no preset"),
            "h3-image": ("h3-image", [], "no preset"),
            "unrelated": (None, [], "no engine family matches this name"),
        }
        for name, (family, presets, why) in cases.items():
            with self.subTest(unit=name):
                engine = downloads.engine_of(cluster, halo, "/home/sergioc/ai-cluster-lab/models", {"name": name})["engine"]
                self.assertEqual((engine["family"], sorted(engine["presets"]), engine.get("why")),
                                 (family, presets, why))
                self.assertEqual((engine["basis"], engine["assets"]), (downloads.INFERRED, []))

    def test_a_configured_asset_in_the_unit_confirms_the_preset(self):
        cluster = parse(example())
        engine = downloads.engine_of(cluster, cluster.machines["halo-1"], "/opt/assets", {"name": "qwen.gguf"})["engine"]
        self.assertEqual((engine["family"], engine["presets"], engine["basis"], engine["assets"]),
                         ("qwen-replicas", ["qwen-replicas"], downloads.CONFIGURED, ["model"]))

    def test_inventory_and_overview_carry_the_engine(self):
        obj = example()
        obj["machines"][1]["lab_dir"] = "/srv/lab"
        cluster = parse(obj)
        reply = {"roots": [{"path": "/opt/assets", "units": [{"name": "MiniMax-M3.1", "kind": "dir", "format": "exl3",
                                                              "quant": "exl3 3.06bpw", "size_bytes": 1, "bytes": 1}]}]}
        with patch.object(downloads, "_node", return_value=reply):
            inventory = downloads.inventory(cluster, "halo-1", None, now=1)
        unit = inventory["roots"][0]["units"][0]
        self.assertEqual((unit["format"], unit["quant"], unit["engine"]["family"]), ("exl3", "exl3 3.06bpw", "minimax-pair"))


if __name__ == "__main__":
    unittest.main()
