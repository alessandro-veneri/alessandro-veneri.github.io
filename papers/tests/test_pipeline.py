from __future__ import annotations

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_papers import build
from paperlib import (
    build_chunks,
    build_file_chunks,
    format_bibtex_entry,
    context_from_xml,
    safe_xml_fragment,
    sequence_coverage,
    source_inventory,
    strip_if_false,
    strip_comment_environments,
    strip_tex_comments,
)
from validate_paper import compare_inventory, math_coverage, math_fragments_from_tex
from publish_paper import chunk_output_schema, codex_exec_command


class PreparationTests(unittest.TestCase):
    def test_codex_publisher_is_ephemeral_read_only_and_schema_constrained(self) -> None:
        command = codex_exec_command(
            Path("schema.json"), Path("response.json"), Path("work"), codex_binary="codex-test"
        )
        self.assertEqual(command[:2], ["codex-test", "exec"])
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--output-schema") + 1], "schema.json")
        self.assertEqual(command[command.index("--output-last-message") + 1], "response.json")
        self.assertNotIn("--model", command)
        self.assertEqual(command[-1], "-")

    def test_chunk_schema_pins_identity_and_sentinels(self) -> None:
        chunk = {"id": "chunk-001", "startSentinel": "START", "endSentinel": "END"}
        schema = chunk_output_schema(chunk)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["chunkId"]["enum"], ["chunk-001"])
        self.assertEqual(schema["properties"]["startSentinel"]["enum"], ["START"])
        self.assertEqual(schema["properties"]["endSentinel"]["enum"], ["END"])

    def test_comments_and_literal_false_blocks_are_removed(self) -> None:
        source = r"""
Kept text. % removed comment
Escaped \% remains.
\iffalse
Hidden text.
\else
Visible alternative.
\fi
"""
        cleaned = strip_tex_comments(strip_if_false(source))
        self.assertIn("Kept text.", cleaned)
        self.assertIn(r"Escaped \% remains.", cleaned)
        self.assertIn("Visible alternative.", cleaned)
        self.assertNotIn("Hidden text.", cleaned)
        self.assertNotIn("removed comment", cleaned)

    def test_comment_environment_is_removed(self) -> None:
        source = r"Visible. \begin{comment}Hidden material.\end{comment} Still visible."
        cleaned = strip_comment_environments(source)
        self.assertIn("Visible.", cleaned)
        self.assertIn("Still visible.", cleaned)
        self.assertNotIn("Hidden material", cleaned)

    def test_file_aware_chunks_never_merge_distinct_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "sections").mkdir()
            (root / "main.tex").write_text(
                "\\documentclass{article}\n\\begin{document}\nFront matter.\n"
                "\\input{sections/one.tex}\nBridge text.\n"
                "\\input{sections/two}\n\\end{document}\n",
                encoding="utf-8",
            )
            (root / "sections" / "one.tex").write_text("\\section{One}\nFirst file.", encoding="utf-8")
            (root / "sections" / "two.tex").write_text("\\section{Two}\nSecond file.", encoding="utf-8")
            chunks = build_file_chunks(root, "main.tex")
        paths = [chunk["sourcePath"] for chunk in chunks]
        self.assertEqual(paths, ["main.tex", "sections/one.tex", "main.tex", "sections/two.tex"])
        self.assertTrue(all(len({chunk["sourcePath"]}) == 1 for chunk in chunks))

    def test_commented_false_token_is_not_treated_as_a_conditional(self) -> None:
        source = "Visible. % \\iffalse intentionally unmatched\nStill visible."
        cleaned = strip_if_false(strip_tex_comments(source))
        self.assertIn("Visible.", cleaned)
        self.assertIn("Still visible.", cleaned)

    def test_math_iff_is_not_treated_as_a_conditional(self) -> None:
        source = r"Visible $a \iff b$. \iffalse Hidden. \fi Still visible."
        cleaned = strip_if_false(source)
        self.assertIn(r"$a \iff b$", cleaned)
        self.assertNotIn("Hidden", cleaned)

    def test_oversized_sections_are_split_and_sentineled(self) -> None:
        source = "\\section{Large}\n" + ("A complete paragraph.\n\n" * 5000)
        chunks = build_chunks(source)
        self.assertGreater(len(chunks), 1)
        self.assertEqual([item["id"] for item in chunks], [f"chunk-{i:03d}" for i in range(1, len(chunks) + 1)])
        self.assertTrue(all(item["startSentinel"].startswith("MP-START-") for item in chunks))

    def test_adjacent_small_sections_share_a_free_tier_call(self) -> None:
        source = "\\section{One}\nFirst paragraph.\n\\section{Two}\nSecond paragraph."
        chunks = build_chunks(source)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["segmentIds"], ["one", "two"])

    def test_truncated_model_fragment_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            safe_xml_fragment('<CHUNK id="chunk-001"><P>Truncated', "CHUNK")

    def test_bibliography_is_formatted_deterministically(self) -> None:
        entry = '@article{key, author={Smith, Jane and Jones, John}, year={2026}, title={A Paper}, journal={Economic Journal}}'
        rendered = format_bibtex_entry(entry)
        self.assertEqual(rendered, 'Smith, Jane; Jones, John (2026). A Paper. Economic Journal.')


class CoverageTests(unittest.TestCase):
    def test_missing_run_is_reported(self) -> None:
        source = "one two three four five six seven eight nine ten"
        output = "one two three nine ten"
        result = sequence_coverage(source, output)
        self.assertLess(result["recall"], 1)
        self.assertGreaterEqual(result["longestUnmatchedRun"], 5)
        self.assertIn("four five six", result["missingRuns"][0]["text"])

    def test_math_coverage_detects_missing_formula(self) -> None:
        source = math_fragments_from_tex(r"Text $x+y=z$. Then \[a=b+c\].")
        result = math_coverage(source, [r"x+y=z"])
        self.assertEqual(result["sourceFragments"], 2)
        self.assertLess(result["recall"], 1)

    def test_deliberate_structural_omission_fails_inventory(self) -> None:
        expected = {
            "sections": [{"title": "Model"}],
            "environments": {"THEOREM": 1},
            "displayMath": 2,
            "figureCount": 1,
            "tables": 1,
            "footnotes": 1,
            "citationKeys": ["smith2020"],
            "labels": ["sec:model"],
        }
        actual = {
            "sections": [],
            "environments": {"THEOREM": 0},
            "displayMath": 1,
            "figureCount": 0,
            "tables": 0,
            "footnotes": 0,
            "citationKeys": [],
            "labels": [],
            "references": [],
        }
        failures = []
        compare_inventory(expected, actual, failures)
        checks = {failure["check"] for failure in failures}
        self.assertTrue({"section-order", "environment-theorem", "displayMath", "figureCount", "citations", "labels"} <= checks)

    def test_context_is_derived_only_from_answerable_blocks(self) -> None:
        root = ET.fromstring(
            '<PAPER><TITLE>Ignored title</TITLE><SECTION ref="model" title="Model">'
            '<P>Paragraph text.</P><THEOREM ref="thm1">A result.</THEOREM></SECTION></PAPER>'
        )
        context = context_from_xml(root)
        self.assertEqual(context["blocks"][0], {"anchor": "model", "text": "Paragraph text."})
        self.assertEqual(context["blocks"][1], {"anchor": "thm1", "text": "A result."})


class DeploymentGateTests(unittest.TestCase):
    def test_current_draft_is_excluded_from_production_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            deployed = build(Path(temp), include_drafts=False)
            self.assertEqual(deployed, [])
            self.assertTrue((Path(temp) / "manifest.json").exists())
            self.assertFalse((Path(temp) / "digital-advertising-auctions").exists())


if __name__ == "__main__":
    unittest.main()
