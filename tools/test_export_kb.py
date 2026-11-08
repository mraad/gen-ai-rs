#!/usr/bin/env python3
"""Tests for the KB exporter. Needs PyYAML.

python3 tools/test_export_kb.py
"""

import sys
import shutil
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from export_kb import export  # noqa: E402


class ExportTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.src, self.dest = root / "src", root / "dest"
        self.skill = self.src / "skills" / "units"
        (self.skill / "references" / "deep").mkdir(parents=True)
        (self.skill / "resources").mkdir()
        (self.skill / "tools").mkdir()
        (self.skill / "scripts").mkdir()
        (self.skill / "SKILL.md").write_text("---\nname: units\ndescription: d\nmetadata:\n  gait-model-invocable: \"true\"\n---\nBody\n", encoding="utf-8")
        (self.skill / "references" / "a.md").write_text("a", encoding="utf-8")
        (self.skill / "references" / "deep" / "b.md").write_text("b", encoding="utf-8")
        (self.skill / "resources" / "c.txt").write_text("c", encoding="utf-8")
        (self.skill / "tools" / "run.py").write_text("x", encoding="utf-8")
        (self.skill / "scripts" / "run.sh").write_text("x", encoding="utf-8")
        (self.skill / "mcp.json").write_text('{"token": "secret"}', encoding="utf-8")
        self.outside = root / "outside.txt"
        self.outside.write_text("outside", encoding="utf-8")

    def exported(self):
        return export(self.src, self.dest)["skills"]["units"]

    def test_copies_reference_data_only(self):
        skill = self.exported()
        self.assertEqual(skill["resources"], ["references/a.md", "references/deep/b.md", "resources/c.txt"])
        bundle = self.dest / "skills" / "units"
        self.assertEqual((bundle / "references" / "deep" / "b.md").read_text(encoding="utf-8"), "b")
        for excluded in ["tools/run.py", "scripts/run.sh", "mcp.json", "SKILL.md"]:
            self.assertFalse((bundle / excluded).exists(), excluded)

    def test_symlinks_are_not_followed_out_of_the_skill(self):
        (self.skill / "references" / "link.md").symlink_to(self.outside)
        skill = self.exported()
        self.assertNotIn("references/link.md", skill["resources"])
        self.assertFalse((self.dest / "skills" / "units" / "references" / "link.md").exists())

    def test_resource_directory_symlink_is_not_followed(self):
        outside = self.outside.parent / "outside"
        outside.mkdir()
        (outside / "private.txt").write_text("private", encoding="utf-8")
        (self.skill / "assets").symlink_to(outside, target_is_directory=True)
        skill = self.exported()
        self.assertFalse(any(path.startswith("assets/") for path in skill["resources"]))

    def test_linked_skills_and_instructions_are_not_exported(self):
        (self.src / "skills" / "linked").symlink_to(self.skill, target_is_directory=True)
        self.assertEqual(list(export(self.src, self.dest)["skills"]), ["units"])
        instructions = self.skill / "SKILL.md"
        instructions.unlink()
        instructions.symlink_to(self.outside)
        self.assertEqual(export(self.src, self.dest)["skills"], {})

    def test_hash_follows_instructions_and_resources(self):
        first = self.exported()["hash"]
        self.assertEqual(self.exported()["hash"], first)
        (self.skill / "references" / "a.md").write_text("changed", encoding="utf-8")
        second = self.exported()["hash"]
        self.assertNotEqual(second, first)
        (self.skill / "SKILL.md").write_text("---\nname: units\ndescription: d\nmetadata:\n  gait-model-invocable: \"true\"\n---\nNew\n", encoding="utf-8")
        self.assertNotEqual(self.exported()["hash"], second)

    def test_removed_resources_leave_the_bundle(self):
        self.exported()
        (self.skill / "references" / "a.md").unlink()
        self.exported()
        self.assertFalse((self.dest / "skills" / "units" / "references" / "a.md").exists())

    def test_removed_skill_removes_generated_files_but_preserves_unrelated_files(self):
        self.exported()
        other = self.dest / "keep.txt"
        other.write_text("keep", encoding="utf-8")
        shutil.rmtree(self.skill)
        self.assertEqual(export(self.src, self.dest)["skills"], {})
        self.assertFalse((self.dest / "skills" / "units").exists())
        self.assertEqual(other.read_text(encoding="utf-8"), "keep")

    def test_catalog_omits_bodies_and_writes_instructions_separately(self):
        skill = self.exported()
        self.assertNotIn("body", skill)
        self.assertEqual((self.dest / "skills" / "units" / "instructions.md").read_text(encoding="utf-8"), "Body")



if __name__ == "__main__":
    unittest.main()
