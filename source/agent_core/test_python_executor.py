from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .python_executor import PythonExecutionRequest, PythonExecutor
from .security_context import SecurityContext
from .smartagent_protocol import TOOL_ENVELOPE_SCHEMAS


class PythonExecutorRegressionTests(unittest.TestCase):
    def test_executes_existing_workspace_script_with_argv(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp).resolve()
            script = workspace / "echo_args.py"
            script.write_text(
                "import sys\nprint('|'.join(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            context = SecurityContext.for_workspace(workspace)
            result = PythonExecutor().execute(
                PythonExecutionRequest(script="echo_args.py", args=("alpha", "beta"), timeout=10),
                security_context=context,
            )
            self.assertEqual(result["exit_code"], 0, result)
            self.assertFalse(result["timed_out"])
            self.assertEqual(result["stdout"], "alpha|beta")

    def test_rejects_non_python_script(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp).resolve()
            target = workspace / "payload.txt"
            target.write_text("print('no')\n", encoding="utf-8")
            context = SecurityContext.for_workspace(workspace)
            with self.assertRaisesRegex(ValueError, "python_script_extension_required"):
                PythonExecutor().execute(
                    PythonExecutionRequest(script="payload.txt"),
                    security_context=context,
                )

    def test_rejects_script_outside_workspace(self):
        with tempfile.TemporaryDirectory() as temp, tempfile.TemporaryDirectory() as other:
            workspace = Path(temp).resolve()
            outside = Path(other).resolve() / "outside.py"
            outside.write_text("print('no')\n", encoding="utf-8")
            context = SecurityContext.for_workspace(workspace)
            with self.assertRaises(ValueError):
                PythonExecutor().execute(
                    PythonExecutionRequest(script=outside),
                    security_context=context,
                )

    def test_rejects_arbitrary_interpreter(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp).resolve()
            script = workspace / "ok.py"
            script.write_text("print('ok')\n", encoding="utf-8")
            context = SecurityContext.for_workspace(workspace)
            with self.assertRaisesRegex(ValueError, "python_interpreter_not_allowed"):
                PythonExecutor().execute(
                    PythonExecutionRequest(script=script, interpreter="powershell"),
                    security_context=context,
                )

    def test_run_python_is_not_a_tool_surface(self):
        self.assertNotIn("run_python", TOOL_ENVELOPE_SCHEMAS)


if __name__ == "__main__":
    unittest.main()
