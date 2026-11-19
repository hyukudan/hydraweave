"""A slow node must not delay fresh routing facts from independent nodes."""
import threading
import unittest
from unittest.mock import patch

from hydraweave import orchestrate


class RouterNodePollTests(unittest.TestCase):
    def test_slow_node_does_not_block_other_nodes_or_erase_its_last_fact(self):
        slow_started, fast_published, release_slow = threading.Event(), threading.Event(), threading.Event()

        class Published(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                if key == "fast":
                    fast_published.set()

        state = {"machines": {"slow": {"id": "slow"}, "fast": {"id": "fast"}}, "known_hosts": None}
        old = {"free_ram_gib": 4, "at": 1}
        polled = Published(slow=old)

        def call(machine, *args, **kwargs):
            if machine.id == "slow":
                slow_started.set()
                release_slow.wait(5)
                raise TimeoutError("fixture node unavailable")
            return {"ram": 12}

        with patch.object(orchestrate, "_client", side_effect=lambda machine, _: machine), \
                patch.object(orchestrate, "_call", side_effect=call), \
                patch("hydraweave.recipes.launch.node_ram", side_effect=lambda result: result["ram"]):
            worker = threading.Thread(target=orchestrate.poll_nodes_once, args=(state, polled))
            worker.start()
            try:
                self.assertTrue(slow_started.wait(1))
                self.assertTrue(fast_published.wait(1), "a slow node blocked the independent fast node")
                self.assertEqual(polled["fast"]["free_ram_gib"], 12)
                self.assertGreater(polled["fast"]["at"], 1)
                self.assertIs(polled["slow"], old)
            finally:
                release_slow.set()
                worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertIs(polled["slow"], old)

    def test_empty_node_snapshot_needs_no_worker(self):
        with patch.object(orchestrate, "_call") as call:
            orchestrate.poll_nodes_once({"machines": {}, "known_hosts": None}, {})
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
