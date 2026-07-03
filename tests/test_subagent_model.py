import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from two_b import orchestrator


class SubModel(unittest.TestCase):
    def setUp(self):
        self._orig_resolve = orchestrator.registry.resolve

    def tearDown(self):
        os.environ.pop("TWOB_SUBAGENT_MODEL", None)
        orchestrator.registry.resolve = self._orig_resolve

    def test_unset_uses_parent(self):
        self.assertEqual(orchestrator._resolve_subagent_model({}, "PARENT_P", "parent-m"), ("PARENT_P", "parent-m"))

    def test_set_and_resolvable_uses_sub(self):
        os.environ["TWOB_SUBAGENT_MODEL"] = "cheap"
        fake_reg = {}
        orchestrator.registry.resolve = lambda reg, name: ("CHEAP_P", "cheap-m") if name == "cheap" else None
        self.assertEqual(orchestrator._resolve_subagent_model(fake_reg, "PARENT_P", "parent-m"), ("CHEAP_P", "cheap-m"))

    def test_set_but_unresolvable_falls_back(self):
        os.environ["TWOB_SUBAGENT_MODEL"] = "nope"
        orchestrator.registry.resolve = lambda reg, name: None
        self.assertEqual(orchestrator._resolve_subagent_model({}, "PARENT_P", "parent-m"), ("PARENT_P", "parent-m"))


if __name__ == "__main__":
    unittest.main()
