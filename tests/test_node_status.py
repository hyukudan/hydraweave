"""The one node status model (hydraweave/web/features/nodestatus.py): reachability with the real time of the last
successful contact, and the freshness of each telemetry source with its reason."""
from types import SimpleNamespace
import unittest

from hydraweave.web.features import machines as fleet
from hydraweave.web.features import nodestatus

NOW = 10_000.0


def ok(value):
    return {"status": "ok", "value": value}


def unavailable(why):
    return {"status": "unavailable", "why": why}


DOCTOR = {"schema": "hydraweave/node-v1", "checks": {"os": ok("Linux")}}
GPU = {"schema": "hydraweave/node-v1", "checks": {"amd": ok([{"id": "card0"}]), "ram": ok({"total_bytes": 1})}}
HOST = {"schema": "hydraweave/node-v1", "checks": {"gpus": ok([{"card": "card0"}]), "memory": ok({"MemTotal": 1}),
                                                   "cpu": ok({"busy_percent": 3.0})}}


def reads(doctor=NOW - 5, gpu=NOW - 5, host=NOW - 5):
    """Each argument is the time of a successful read, or a dict of changes to a read made 5 s ago."""
    over = {k: v for k, v in (("doctor", doctor), ("gpu", gpu), ("host", host)) if isinstance(v, dict)}
    doctor, gpu, host = (NOW - 5 if isinstance(v, dict) else v for v in (doctor, gpu, host))
    out = {"doctor": {"ok_at": doctor, "tried_at": doctor, "value": DOCTOR},
           "gpu": {"ok_at": gpu, "tried_at": gpu, "value": GPU},
           "host": {"ok_at": host, "tried_at": host, "value": HOST}}
    for name, change in over.items():
        out[name] = {**out[name], **change}
    return out


class BuildTests(unittest.TestCase):
    def test_current(self):
        model = nodestatus.build(now=NOW, reads=reads())
        self.assertEqual((model["state"], model["code"], model["level"]), ("reachable", "ok", "ok"))
        self.assertEqual(model["summary"], "Reachable - telemetry current")
        self.assertEqual(model["last_contact"], NOW - 5)
        self.assertTrue(all(s["state"] == "fresh" and not s["missing"] for s in model["sources"].values()))
        self.assertEqual(nodestatus.health(model), "online")
        self.assertEqual(nodestatus.health(model, ["cable warning"]), "degraded")

    def test_paused_during_gpu_slot_keeps_the_real_sample_time(self):
        paused = "paused: a GPU turn holds n2 (job); node reads wait for its end"
        model = nodestatus.build(now=NOW, reads=reads(NOW - 600, NOW - 600, NOW - 600), paused=paused,
                                 turn_at=NOW - 20, until=NOW + 300, next_check_s=60)
        self.assertEqual((model["state"], model["code"]), ("reachable", "paused"))
        self.assertEqual(model["summary"], "Reachable - telemetry paused while the GPU runs a job")
        self.assertEqual(model["last_sample"], NOW - 600)          # never renewed: the samples are 10 minutes old
        self.assertEqual(model["last_contact"], NOW - 20)          # the slot's holder was really seen then
        gpu = model["sources"]["gpu"]
        self.assertEqual((gpu["state"], gpu["at"], gpu["until"], gpu["next_check_s"]), ("paused", NOW - 600, NOW + 300, 60))
        self.assertEqual(model["paused"]["until"], NOW + 300)
        self.assertIn("paused while the GPU runs a job", nodestatus.source_why(model, "gpu"))

    def test_held_node_whose_turn_checks_fail_is_not_reachable(self):
        paused = "paused: a GPU turn holds n2 (job); node reads wait for its end"
        model = nodestatus.build(now=NOW, reads=reads(NOW - 9900, NOW - 9900, NOW - 9900), paused=paused,
                                 turn_at=NOW - 9900, turn_error="TimeoutError: no answer", turn_interval=60)
        self.assertEqual((model["state"], model["code"], model["reachable"]), ("unreachable", "unreachable", False))
        self.assertIn("TimeoutError: no answer", model["summary"])
        self.assertEqual(model["last_contact"], NOW - 9900)          # the failed checks did not renew it
        # The pause of the node's other reads is still reported, apart from reachability.
        self.assertEqual(model["sources"]["gpu"]["state"], "paused")
        self.assertEqual(model["paused"]["why"], paused)
        self.assertEqual(nodestatus.health(model), "offline")
        # A failure right after the holder was seen ends it too: no grace on a known failure.
        recent = nodestatus.build(now=NOW, reads={}, paused=paused, turn_at=NOW - 30, turn_error="refused",
                                  turn_interval=60)
        self.assertEqual(recent["state"], "unreachable")

    def test_expired_turn_reading_no_longer_proves_reachability(self):
        paused = "paused: a GPU turn holds n2 (job); node reads wait for its end"
        for age, state, reachable in ((180, "reachable", True), (181, "stale", None), (9900, "stale", None)):
            model = nodestatus.build(now=NOW, reads={}, paused=paused, turn_at=NOW - age, turn_interval=60)
            self.assertEqual((model["state"], model["reachable"], model["last_contact"]),
                             (state, reachable, NOW - age), age)
        self.assertEqual((model["code"], model["sources"]["doctor"]["state"]), ("stale", "paused"))
        self.assertIn("180 s", model["detail"])

    def test_paused_without_any_contact_is_not_called_reachable(self):
        model = nodestatus.build(now=NOW, reads={}, paused="paused: a GPU turn holds n2")
        self.assertEqual((model["state"], model["reachable"], model["last_contact"]), ("unknown", None, None))

    def test_stale_cached_answers_do_not_renew_last_contact(self):
        model = nodestatus.build(now=NOW, reads=reads(NOW - 900, NOW - 900, NOW - 900))
        self.assertEqual((model["state"], model["code"], model["reachable"]), ("stale", "stale", None))
        self.assertEqual(model["last_contact"], NOW - 900)
        self.assertEqual(model["sources"]["doctor"]["since"], NOW - 900)
        self.assertEqual(nodestatus.health(model), "stale")

    def test_one_stale_source_with_the_others_fresh(self):
        model = nodestatus.build(now=NOW, reads=reads(host=NOW - 900))
        self.assertEqual((model["state"], model["code"]), ("reachable", "stale"))
        self.assertEqual(model["summary"], "Reachable - host telemetry stale")
        self.assertEqual(model["sources"]["host"]["since"], NOW - 900)

    def test_partial_payload_names_the_unreadable_check_and_its_reason(self):
        gpu = {"schema": "hydraweave/node-v1", "checks": {"amd": unavailable("permission denied: mem_info_vram_used"),
                                                          "ram": ok({"total_bytes": 1})}}
        model = nodestatus.build(now=NOW, reads=reads(gpu={"value": gpu}))
        self.assertEqual((model["state"], model["code"], model["level"]), ("reachable", "partial", "warn"))
        self.assertEqual(model["summary"], "Reachable - GPU counters unreadable: permission denied: mem_info_vram_used")
        self.assertEqual(model["sources"]["gpu"]["state"], "fresh")
        self.assertEqual(model["sources"]["gpu"]["missing"][0]["check"], "amd")
        self.assertIn("permission denied", nodestatus.source_why(model, "gpu"))

    def test_successful_envelope_with_every_check_unavailable_is_not_complete(self):
        host = {"schema": "hydraweave/node-v1", "checks": {name: unavailable("unreadable") for name in ("gpus", "memory", "cpu")}}
        model = nodestatus.build(now=NOW, reads=reads(host={"value": host}))
        self.assertEqual(model["code"], "partial")
        self.assertEqual([m["check"] for m in model["sources"]["host"]["missing"]], ["gpus", "memory", "cpu"])

    def test_ignored_check_on_a_machine_with_another_gpu_source(self):
        gpu = {"checks": {"amd": unavailable("no AMD card"), "ram": ok({"total_bytes": 1})}}
        model = nodestatus.build(now=NOW, reads=reads(gpu={"value": gpu}), ignore={"gpu": ("amd",)})
        self.assertEqual(model["code"], "ok")

    def test_failed_source_keeps_its_reason_and_last_good_time(self):
        model = nodestatus.build(now=NOW, reads=reads(gpu={"ok_at": NOW - 40, "tried_at": NOW - 5,
                                                           "error": "sysfs read failed", "value": None}))
        self.assertEqual((model["state"], model["code"]), ("reachable", "failed"))
        self.assertEqual(model["summary"], "Reachable - GPU counters unreadable: sysfs read failed")
        gpu = model["sources"]["gpu"]
        self.assertEqual((gpu["state"], gpu["at"], gpu["since"], gpu["why"]), ("failed", NOW - 40, NOW - 5, "sysfs read failed"))

    def test_version_mismatch(self):
        model = nodestatus.build(now=NOW, reads=reads(), version={"deployed": "a" * 40, "expected": "b" * 40})
        self.assertEqual((model["state"], model["code"], model["level"]), ("reachable", "version", "warn"))
        self.assertEqual(model["summary"], "Node tool version differs: update")
        self.assertEqual(model["action"]["href"], "#/ops")
        self.assertIs(model["version"]["match"], False)
        same = nodestatus.build(now=NOW, reads=reads(), version={"deployed": "a", "expected": "a"})
        self.assertEqual((same["code"], same["version"]["match"]), ("ok", True))

    def test_old_node_tool_that_does_not_answer_a_part(self):
        model = nodestatus.build(now=NOW, reads=reads(host={"ok_at": None, "tried_at": NOW - 5, "value": None,
                                                            "error": "ValueError: the poll did not answer this part"}))
        self.assertEqual((model["state"], model["code"]), ("reachable", "version"))
        self.assertEqual(model["sources"]["host"]["state"], "failed")

    def test_link_warning_is_part_of_the_model(self):
        model = nodestatus.build(now=NOW, reads=reads(), warnings=[{"message": "cable B below 40 Gb/s"}])
        self.assertEqual((model["state"], model["code"], model["level"]), ("reachable", "warning", "warn"))
        self.assertEqual(model["summary"], "Reachable - warning: cable B below 40 Gb/s")
        self.assertEqual(nodestatus.health(model), "degraded")

    def test_unpaired(self):
        error = "PermissionError: pair this node first"
        failed = {name: {"ok_at": None, "tried_at": NOW - 5, "error": error} for name in nodestatus.SOURCES}
        model = nodestatus.build(now=NOW, reads=failed)
        self.assertEqual((model["state"], model["code"], model["reachable"]), ("unpaired", "unpaired", False))
        self.assertEqual(model["summary"], "Not paired with this controller")
        self.assertEqual(model["detail"], error)
        self.assertIsNone(model["last_contact"])
        self.assertEqual(nodestatus.health(model), "unpaired")
        for text in ("ValueError: node has not been paired with this controller", "unknown host key for n2"):
            row = {name: {"tried_at": NOW, "error": text} for name in nodestatus.SOURCES}
            self.assertEqual(nodestatus.build(now=NOW, reads=row)["state"], "unpaired")

    def test_unreachable_keeps_the_last_real_contact(self):
        failed = {name: {"ok_at": NOW - 700, "tried_at": NOW - 5, "error": "timed out"} for name in nodestatus.SOURCES}
        model = nodestatus.build(now=NOW, reads=failed)
        self.assertEqual((model["state"], model["level"], model["reachable"]), ("unreachable", "err", False))
        self.assertEqual(model["summary"], "Unreachable: timed out")
        self.assertEqual(model["last_contact"], NOW - 700)
        self.assertEqual(nodestatus.health(model), "offline")

    def test_never_collected(self):
        model = nodestatus.build(now=NOW, reads={"host": {"never_why": "telemetry store not open"}})
        self.assertEqual((model["state"], model["code"], model["level"]), ("unknown", "never", "off"))
        self.assertTrue(all(s["state"] == "never" and s["why"] for s in model["sources"].values()))
        self.assertEqual(model["sources"]["host"]["why"], "telemetry store not open")

    def test_controller_machine_is_reachable_by_definition(self):
        model = nodestatus.build(now=NOW, reads={}, access="local")
        self.assertEqual((model["state"], model["last_contact"], model["code"]), ("reachable", NOW, "never"))


class Store:
    def __init__(self, entry):
        self.entry = entry

    def get(self, namespace, key):
        return self.entry if namespace == fleet.HOSTS else None


class CollectTests(unittest.TestCase):
    machine = SimpleNamespace(id="n2", access="agent", os="linux")

    def api(self, at):
        return SimpleNamespace(
            _inspections={("n2", "doctor"): {"value": DOCTOR, "error": None, "at": at},
                          ("n2", "gpu"): {"value": GPU, "error": None, "at": at}, ("n2", "paused"): None},
            _contacts={("n2", "doctor"): at, ("n2", "gpu"): at})

    def test_inventory_and_host_sources_share_one_model(self):
        entry = {"ok": True, "ts": NOW - 4, "last_ok": NOW - 4, "host": HOST, "error": None}
        model = nodestatus.collect(self.api(NOW - 8), self.machine, NOW, store=Store(entry))
        self.assertEqual((model["state"], model["last_contact"]), ("reachable", NOW - 4))
        self.assertEqual({k: v["state"] for k, v in model["sources"].items()},
                         {"doctor": "fresh", "gpu": "fresh", "host": "fresh"})
        self.assertEqual({k: v["at"] for k, v in model["sources"].items()},
                         {"doctor": NOW - 8, "gpu": NOW - 8, "host": NOW - 4})

    def test_failed_host_envelope_with_cached_inventory(self):
        entry = {"ok": False, "ts": NOW - 400, "last_ok": NOW - 400, "failed_at": NOW - 3, "host": HOST,
                 "error": "TimeoutError: no answer"}
        model = nodestatus.collect(self.api(NOW - 500), self.machine, NOW, store=Store(entry))
        self.assertEqual(model["state"], "unreachable")
        self.assertEqual(model["last_contact"], NOW - 400)          # the cached inventory did not renew it
        self.assertEqual(model["sources"]["host"]["why"], "TimeoutError: no answer")
        self.assertEqual(model["sources"]["gpu"]["state"], "stale")

    def test_holder_seen_then_failed_turn_polls(self):
        from hydraweave import pollbudget
        from hydraweave.web.features import turns
        api = SimpleNamespace(_inspections={("n2", "paused"): None}, _contacts={}, config_path=None)
        machine = SimpleNamespace(id="n2", access="agent", os="linux", lab_dir="/lab")
        pollbudget.note_holder(api, "n2", "job", now=100)
        try:
            held = nodestatus.collect(api, machine, 130, store=Store(None))
            self.assertEqual((held["state"], held["code"], held["last_contact"]), ("reachable", "paused", 100))
            turns.record(api, machine, None, "TimeoutError: no answer", 9990, 10000)
            model = nodestatus.collect(api, machine, 10000, store=Store(None))
            self.assertEqual((model["reachable"], model["state"], model["last_contact"]), (False, "unreachable", 100))
            self.assertIn("TimeoutError: no answer", model["summary"])
            self.assertEqual(model["sources"]["gpu"]["state"], "paused")       # the budget still holds the reads
            self.assertIsNotNone(pollbudget.for_api(api).turn("n2"))
            # No failure recorded but no answer either (the poller stopped): the old reading expires.
            turns._replies.pop(id(api), None)
            silent = nodestatus.collect(api, machine, 10000, store=Store(None))
            self.assertEqual((silent["reachable"], silent["state"]), (None, "stale"))
        finally:
            turns._replies.pop(id(api), None)

    def test_missing_store_is_named(self):
        api = self.api(NOW - 8)
        entry = None
        model = nodestatus.collect(api, self.machine, NOW, store=Store(entry))
        self.assertEqual(model["sources"]["host"]["state"], "never")
        self.assertIn("no collection for this machine yet", model["sources"]["host"]["why"])
        self.assertEqual(model["summary"].split(":")[0], "Reachable - host telemetry never collected")


if __name__ == "__main__":
    unittest.main()
