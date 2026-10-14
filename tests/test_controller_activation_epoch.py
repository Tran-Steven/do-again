"""Fail-closed epoch transition for the sealed browser controller."""
import unittest
from pathlib import Path
from do_again.supervisor.controller_configuration import validate_controller_configuration


class ControllerEpochTransitionTests(unittest.TestCase):
    def test_installed_controller_components_use_one_exact_activation_epoch(self):
        root = Path(__file__).resolve().parents[1] / "src/do_again/supervisor"
        backend = (root / "controller_backend.py").read_text()
        service = (root / "controller_service.py").read_text()
        effects = (root / "controller_native_effects.py").read_text()
        self.assertIn("status['epoch'] == self.controller.scope['epoch'] + 1", backend)
        self.assertIn("status['epoch'] == scope['epoch'] + 1", service)
        self.assertIn("epoch=broker.config['controller_scope']['epoch']+1", effects)
        self.assertIn("status['intent'] == 'active'", backend)
        self.assertIn("status['intent'] == 'active'", service)


if __name__ == "__main__":
    unittest.main()
