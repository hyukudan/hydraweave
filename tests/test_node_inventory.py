from pathlib import Path
import json
import subprocess

import pytest

from hydraweave import config, device_policy, node_agent


def test_inventory_reads_amd_sysfs_and_nvidia_smi(tmp_path):
    device = tmp_path / "card0" / "device"
    device.mkdir(parents=True)
    (device / "vendor").write_text("0x1002")
    (device / "product_name").write_text("Example AMD")
    (device / "mem_info_vram_total").write_text("8589934592")

    def runner(args, **kwargs):
        assert kwargs["timeout"] <= 5
        return subprocess.CompletedProcess(args, 0, "Example NVIDIA, 16384, 555.1\n", "")

    result = node_agent.inventory(sysfs=tmp_path, runner=runner, system="Linux", arch="x86_64")
    assert result["os"] == "linux" and result["arch"] == "x86_64"
    assert [row["id"] for row in result["devices"]] == ["gpu0", "gpu1"]
    assert result["devices"][0]["memory_bytes"] == 8589934592
    assert result["devices"][1]["memory_bytes"] == 16384 * 1024 * 1024


def test_inventory_falls_back_to_cpu_when_probes_fail(tmp_path):
    def runner(*args, **kwargs):
        raise OSError("missing utility")
    assert node_agent.inventory(sysfs=tmp_path, runner=runner, system="Windows", arch="AMD64")["devices"] == [
        {"id": "cpu", "type": "cpu", "vendor": "", "name": "CPU", "memory_bytes": 0, "driver": ""}]


def test_device_type_enum_and_default_policy():
    obj = json.loads((Path(__file__).resolve().parents[1] / "examples" / "cluster.example.json").read_text())
    obj["machines"][0]["devices"] = [{"id": "gpu", "type": "igpu"}]
    with pytest.raises(config.ConfigError, match="amd, apple, cpu, intel, nvidia, other"):
        config.parse(obj)
    assert config.DEFAULT_DEVICE_POLICIES == ()
    assert not device_policy.resolve({"name": "RTX 4090"}, []) ["excluded"]
