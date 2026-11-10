"""While a GPU turn pauses a machine's node reads, old failed readings are not evidence of an unreachable node."""
import types
import unittest
from unittest import mock

from hydraweave.web.features import health


class PausedTests(unittest.TestCase):
    def ctx(self):
        row = {"labels": {"machine": "halo-1"}, "value": 0}
        return types.SimpleNamespace(api=object(), now=1000.0, states=lambda name, codes: [(row, 0)])

    def test_unreachable_clears_while_a_turn_holds_the_machine(self):
        budget = mock.Mock()
        budget.turn.return_value = ("lab-pair-turn", False)
        with mock.patch("hydraweave.pollbudget.for_api", return_value=budget):
            obs = health._unreachable(self.ctx(), {})
        self.assertEqual([(o.subject, o.verdict) for o in obs], [("halo-1", False)])
        self.assertIn("paused", obs[0].detail)

    def test_unreachable_fires_without_a_turn(self):
        budget = mock.Mock()
        budget.turn.return_value = None
        with mock.patch("hydraweave.pollbudget.for_api", return_value=budget):
            obs = health._unreachable(self.ctx(), {})
        self.assertEqual([(o.subject, o.verdict) for o in obs], [("halo-1", True)])

    def test_a_source_of_a_paused_machine_is_not_stale(self):
        budget = mock.Mock()
        budget.turn.return_value = ("lab-pair-turn", False)
        with mock.patch("hydraweave.pollbudget.for_api", return_value=budget):
            obs = health._source_obs(self.ctx(), "halo-1/usb4-live", "halo-1", "USB4 source", None, 990.0, 5.0)
        self.assertIs(obs.verdict, False)
        self.assertIn("paused", obs.detail)


if __name__ == "__main__":
    unittest.main()
