from __future__ import annotations

import unittest

from .skill_registry import SkillDescriptor, SkillRegistry
from .smartagent_protocol import TOOL_ENVELOPE_SCHEMAS


class SkillRegistryRegressionTests(unittest.TestCase):
    def test_register_resolve_and_sorted_names(self):
        registry = SkillRegistry([
            SkillDescriptor("zeta-skill", "skills/zeta.py"),
            SkillDescriptor("alpha_skill", "skills/alpha.py"),
        ])
        self.assertEqual(registry.names(), ("alpha_skill", "zeta_skill"))
        descriptor = registry.resolve("ZETA-SKILL")
        self.assertEqual(descriptor.name, "zeta_skill")
        self.assertEqual(descriptor.script, "skills/zeta.py")
        self.assertEqual(descriptor.kind, "python")

    def test_duplicate_canonical_name_is_rejected(self):
        registry = SkillRegistry([SkillDescriptor("image_compare", "skills/one.py")])
        with self.assertRaisesRegex(ValueError, "skill_already_registered:image_compare"):
            registry.register(SkillDescriptor("image-compare", "skills/two.py"))

    def test_descriptor_rejects_unsafe_or_non_python_script_paths(self):
        invalid = (
            ("C:/outside.py", "skill_script_must_be_relative"),
            ("../outside.py", "skill_script_parent_traversal_forbidden"),
            ("skills/*.py", "skill_script_glob_forbidden"),
            ("skills/readme.txt", "skill_script_extension_required"),
        )
        for script, message in invalid:
            with self.subTest(script=script):
                with self.assertRaisesRegex(ValueError, message):
                    SkillDescriptor("sample", script)

    def test_unknown_skill_cannot_produce_execution_request(self):
        registry = SkillRegistry()
        with self.assertRaisesRegex(KeyError, "unknown_skill:missing"):
            registry.execution_request("missing")

    def test_execution_request_uses_registered_fixed_script(self):
        registry = SkillRegistry([
            SkillDescriptor("image_compare", "skills/image_compare.py", "Compare images"),
        ])
        request = registry.execution_request(
            "image-compare",
            args=("left.png", "right.png"),
            timeout=12,
        )
        self.assertEqual(request.script, "skills/image_compare.py")
        self.assertEqual(request.args, ("left.png", "right.png"))
        self.assertEqual(request.timeout, 12)
        self.assertEqual(request.interpreter, "python")

    def test_run_python_is_not_a_tool_surface(self):
        self.assertNotIn("run_python", TOOL_ENVELOPE_SCHEMAS)


if __name__ == "__main__":
    unittest.main()
