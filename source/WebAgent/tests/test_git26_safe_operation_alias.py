"""Only deterministic, proven-read aliases may be translated into v10 operations."""
from __future__ import annotations
import unittest
from WebAgent.minimal_decision import MinimalDecision
from WebAgent.minimal_admission import (
    canonicalize_safe_observation_alias, admit_minimal_action,
)

class FakeTools:
    interface_name = "webdirect"
    current_request_text = "Read exact requested target"
    def preview_tool_scope(self, action):
        return {"allowed": True}

def call(command, operation):
    return MinimalDecision("tool_call", "run_command",
                           {"command": command, "operation": operation})

class SafeOperationAliasTests(unittest.TestCase):
    def test_canonicalizes_proven_read_without_changing_command(self):
        original = call("Get-Item -LiteralPath 'C:\\Repo'", "READ_ONLY")
        converted, note = canonicalize_safe_observation_alias(original)
        self.assertEqual(converted.arguments["operation"], "INSPECT")
        self.assertEqual(converted.arguments["command"], original.arguments["command"])
        self.assertEqual(original.arguments["operation"], "READ_ONLY")
        self.assertEqual(note["rewrite_changed_fields"], ["operation"])
        self.assertFalse(note["authorizes_action"])
        admitted, reason = admit_minimal_action(converted, FakeTools())
        self.assertTrue(admitted, reason)

    def test_registered_git_read_alias_is_only_git_inspect(self):
        original = call("git ls-remote https://github.com/b43120000/test.git HEAD",
                        "GIT_READ")
        canonical, note = canonicalize_safe_observation_alias(original)
        self.assertEqual(canonical.arguments["operation"], "GIT_INSPECT")
        self.assertEqual(note["rewrite_reason_code"],
                         "PROVEN_READ_ALIAS_TO_CANONICAL_OPERATION")

    def test_unproven_mutation_never_rewrites_or_executes(self):
        original = call("Set-Content -LiteralPath 'C:\\Repo\\s.txt' -Value 'X'",
                        "READ_ONLY")
        rejected, note = canonicalize_safe_observation_alias(original)
        self.assertIs(rejected, original)
        self.assertEqual(note, {})
        action, reason = admit_minimal_action(rejected, FakeTools())
        self.assertIsNone(action)
        self.assertTrue(reason)

    def test_no_verify_or_general_alias_elevation(self):
        original = call("Get-Location", "CHECK")
        converted, note = canonicalize_safe_observation_alias(original)
        self.assertIs(converted, original)
        self.assertFalse(note)

if __name__ == "__main__":
    unittest.main()
