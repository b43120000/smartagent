from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from .python_executor import PythonExecutor
from .security_context import SecurityContext
from .skill_registry import default_skill_registry
from .smartagent_protocol import TOOL_ENVELOPE_SCHEMAS


class ImageCompareSkillRegressionTests(unittest.TestCase):
    def _workspace(self, temp: str) -> tuple[Path, Path]:
        workspace = Path(temp).resolve()
        source_script = Path(__file__).resolve().parent / "skills" / "image_compare.py"
        script = workspace / "source" / "agent_core" / "skills" / "image_compare.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_script, script)
        return workspace, script

    def test_default_registry_executes_image_compare_end_to_end(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace, _ = self._workspace(temp)
            Image.new("RGB", (2, 2), (10, 20, 30)).save(workspace / "left.png")
            Image.new("RGB", (2, 2), (10, 20, 30)).save(workspace / "right.png")
            registry = default_skill_registry()
            self.assertEqual(registry.names(), ("image_compare",))
            request = registry.execution_request(
                "image_compare",
                args=("left.png", "right.png"),
                timeout=10,
            )
            self.assertEqual(request.script, "source/agent_core/skills/image_compare.py")
            result = PythonExecutor().execute(
                request,
                security_context=SecurityContext.for_workspace(workspace),
            )
            self.assertEqual(result["exit_code"], 0, result)
            payload = json.loads(result["stdout"])
            self.assertEqual(payload["schema"], "SMARTAGENT_IMAGE_COMPARE_V1")
            self.assertEqual(payload["status"], "OK")
            self.assertTrue(payload["equal"])
            self.assertEqual(payload["mae"], [0.0, 0.0, 0.0])
            self.assertEqual(payload["normalized_mean_abs_diff"], 0.0)

    def test_image_compare_reports_pixel_difference(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace, _ = self._workspace(temp)
            Image.new("RGB", (1, 1), (0, 0, 0)).save(workspace / "left.png")
            Image.new("RGB", (1, 1), (255, 0, 0)).save(workspace / "right.png")
            request = default_skill_registry().execution_request(
                "image_compare",
                args=("left.png", "right.png"),
            )
            result = PythonExecutor().execute(
                request,
                security_context=SecurityContext.for_workspace(workspace),
            )
            self.assertEqual(result["exit_code"], 0, result)
            payload = json.loads(result["stdout"])
            self.assertFalse(payload["equal"])
            self.assertEqual(payload["mae"], [255.0, 0.0, 0.0])
            self.assertEqual(payload["max_abs_diff"], [255, 0, 0])
            self.assertEqual(payload["normalized_mean_abs_diff"], 0.33333333)

    def test_image_compare_rejects_absolute_input_path(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace, _ = self._workspace(temp)
            outside = Path(temp).parent / "outside-image-compare.png"
            try:
                Image.new("RGB", (1, 1), (0, 0, 0)).save(outside)
                Image.new("RGB", (1, 1), (0, 0, 0)).save(workspace / "right.png")
                request = default_skill_registry().execution_request(
                    "image_compare",
                    args=(str(outside.resolve()), "right.png"),
                )
                result = PythonExecutor().execute(
                    request,
                    security_context=SecurityContext.for_workspace(workspace),
                )
                self.assertEqual(result["exit_code"], 2, result)
                payload = json.loads(result["stdout"])
                self.assertEqual(payload["status"], "ERROR")
                self.assertEqual(payload["error"], "image_path_must_be_relative")
            finally:
                outside.unlink(missing_ok=True)

    def test_run_python_is_still_not_a_tool_surface(self):
        self.assertNotIn("run_python", TOOL_ENVELOPE_SCHEMAS)


if __name__ == "__main__":
    unittest.main()
