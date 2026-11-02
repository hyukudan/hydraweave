"""CI workflow hygiene: actions pinned by SHA, no secrets, the public TensorMeld submodule fetched by one script."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
FETCH = ROOT / "tools" / "ci-fetch-tensormeld.sh"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_every_action_is_pinned_by_commit_sha(self):
        uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", self.text, re.M)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")

    def test_checkout_never_persists_credentials(self):
        self.assertEqual(self.text.count("uses: actions/checkout@"), self.text.count("persist-credentials: false"))

    def test_no_secrets_and_submodule_fetched_by_the_script(self):
        self.assertNotIn("secrets.", self.text)
        self.assertEqual(self.text.count("run: bash tools/ci-fetch-tensormeld.sh"), 2)
        self.assertNotIn("git submodule update", self.text)

    def test_fetch_takes_only_the_tensormeld_submodule(self):
        script = FETCH.read_text(encoding="utf-8")
        command = [line for line in script.splitlines() if line.startswith("git ")]
        self.assertEqual(command, ["git submodule update --init --depth 1 third_party/tensormeld"])


if __name__ == "__main__":
    unittest.main()
