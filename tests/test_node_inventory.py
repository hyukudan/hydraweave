from pathlib import Path
import json
import subprocess
import tempfile
import unittest

from hydraweave import config, device_policy, node_agent


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_inventory_reads_amd_sysfs_and_nvidia_smi(self):
        device = self.tmp / "card0" / "device"
        device.mkdir(parents=True)
        (device / "vendor").write_text("0x1002")
        (device / "product_name").write_text("Example AMD")
        (device / "mem_info_vram_total").write_text("8589934592")

        def runner(args, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 5)
            return subprocess.CompletedProcess(args, 0, "Example NVIDIA, 16384, 555.1\n", "")

        result = node_agent.inventory(sysfs=self.tmp, runner=runner, system="Linux", arch="x86_64")
        self.assertEqual((result["os"], result["arch"]), ("linux", "x86_64"))
        self.assertEqual([row["id"] for row in result["devices"]], ["gpu0", "gpu1"])
        self.assertEqual(result["devices"][0]["memory_bytes"], 8589934592)
        self.assertEqual(result["devices"][1]["memory_bytes"], 16384 * 1024 * 1024)

    def test_inventory_falls_back_to_cpu_when_probes_fail(self):
        def runner(*args, **kwargs):
            raise OSError("missing utility")
        self.assertEqual(
            node_agent.inventory(sysfs=self.tmp, runner=runner, system="Windows", arch="AMD64")["devices"],
            [{"id": "cpu", "type": "cpu", "vendor": "", "name": "CPU", "memory_bytes": 0, "driver": ""}])

    def test_legacy_device_types_load_and_no_model_is_excluded_by_default(self):
        obj = json.loads((Path(__file__).resolve().parents[1] / "examples" / "cluster.example.json").read_text())
        obj["machines"][0]["devices"] = [{"id": "gpu", "type": "igpu", "vendor": "AMD", "memory_bytes": 1}]
        config.parse(obj)
        obj["machines"][0]["devices"][0]["memory_bytes"] = -1
        with self.assertRaises(config.ConfigError):
            config.parse(obj)
        self.assertEqual(config.DEFAULT_DEVICE_POLICIES, ())
        self.assertFalse(device_policy.resolve({"name": "RTX 4090"}, [])["excluded"])


if __name__ == "__main__":
    unittest.main()
