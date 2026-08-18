#!/usr/bin/env python3
"""Validate extraction completeness and publication safety for a paper bundle."""

from __future__ import annotations

import argparse
import hashlib
import random
import re
import sys
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from paperlib import (
    PAPERS_ROOT,
    bundle_path,
    context_from_xml,
    detex_text,
    find_paper,
    load_manifest,
    normalize_text,
    normalized_tokens,
    paragraph_list,
    read_json,
    sequence_coverage,
    sha256_file,
    source_segments,
    utc_now,
    write_json,
    xml_inventory,
    xml_text,
)


HARD = "hard"
SOFT = "soft"


def failure_id(check: str, message: str) -> str:
    return f"{check}:{hashlib.sha256(message.encode('utf-8')).hexdigest()[:12]}"


def add_failure(
    failures: list[dict[str, Any]],
    check: str,
    message: str,
    *,
    severity: str = HARD,
    details: Any = None,
) -> None:
    item: dict[str, Any] = {
        "id": failure_id(check, message),
        "check": check,
        "severity": severity,
        "overridable": severity == SOFT,
        "message": message,
    }
    if details is not None:
        item["details"] = details
    failures.append(item)


def normalized_title(value: str) -> str:
    return " ".join(normalized_tokens(value))


MATH_RE = re.compile(
    r"\\begin\s*\{(?P<env>equation\*?|align\*?|gather\*?|alignat\*?|multline\*?)\}"
    r"(?P<environment>[\s\S]*?)\\end\s*\{(?P=env)\}"
    r"|\\\[(?P<bracket>[\s\S]*?)\\\]"
    r"|(?<!\\)\$(?!\$)(?P<inline>[\s\S]*?)(?<!\\)\$"
)


def math_fragments_from_tex(tex: str) -> list[str]:
    fragments: list[str] = []
    for match in MATH_RE.finditer(tex):
        value = match.group("environment") or match.group("bracket") or match.group("inline") or ""
        value = re.sub(r"\\label\s*\{[^}]+\}", "", value)
        fragments.append(value)
    return fragments


def math_tokens(value: str) -> list[str]:
    value = value.replace("\\left", "").replace("\\right", "")
    value = value.replace("\\Bar", "\\bar")
    value = re.sub(r"\s+", "", value)
    return re.findall(r"\\[A-Za-z@]+|[A-Za-z0-9]+|[^A-Za-z0-9\s]", value)


def math_coverage(source_fragments: list[str], output_fragments: list[str]) -> dict[str, Any]:
    source = [token for fragment in source_fragments for token in math_tokens(fragment)]
    output = [token for fragment in output_fragments for token in math_tokens(fragment)]
    matcher = SequenceMatcher(a=source, b=output, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return {
        "sourceFragments": len(source_fragments),
        "outputFragments": len(output_fragments),
        "sourceTokens": len(source),
        "outputTokens": len(output),
        "matchedTokens": matched,
        "recall": round(matched / len(source), 6) if source else 1.0,
    }


def element_text_without_nested_sections(element: ET.Element) -> str:
    pieces: list[str] = []
    nested = {"SECTION", "SUBSECTION", "SUBSUBSECTION"}

    def visit(node: ET.Element, root: bool = False) -> None:
        if not root and node.tag.upper() in nested:
            return
        if node.text:
            pieces.append(node.text)
        for child in node:
            visit(child)
            if child.tail:
                pieces.append(child.tail)

    visit(element, root=True)
    return " ".join(pieces)


def sentinel_matches(source: str, output: str, *, at_end: bool) -> bool:
    paragraphs = paragraph_list(source)
    if not paragraphs:
        return True
    paragraph = paragraphs[-1] if at_end else paragraphs[0]
    tokens = normalized_tokens(paragraph)
    if not tokens:
        return True
    probe = tokens[-20:] if at_end else tokens[:20]
    output_tokens = normalized_tokens(output)
    width = len(probe)
    return any(output_tokens[index : index + width] == probe for index in range(max(0, len(output_tokens) - width + 1)))


def contains_token_probe(output_tokens: list[str], probe: list[str]) -> bool:
    if not probe:
        return True
    width = len(probe)
    return any(output_tokens[index : index + width] == probe for index in range(max(0, len(output_tokens) - width + 1)))


def compare_inventory(
    expected: dict[str, Any],
    actual: dict[str, Any],
    failures: list[dict[str, Any]],
) -> None:
    expected_titles = [normalized_title(section["title"]) for section in expected.get("sections", [])]
    actual_titles = [normalized_title(section["title"]) for section in actual.get("sections", [])]
    if expected_titles != actual_titles:
        add_failure(
            failures,
            "section-order",
            "section/subsection titles or order differ from the pruned source",
            details={"expected": expected_titles, "actual": actual_titles},
        )

    for tag, count in expected.get("environments", {}).items():
        actual_count = actual.get("environments", {}).get(tag, 0)
        if actual_count != count:
            add_failure(
                failures,
                f"environment-{tag.casefold()}",
                f"{tag} count differs: expected {count}, found {actual_count}",
            )

    for expected_key, actual_key, label in (
        ("displayMath", "displayMath", "display-math blocks"),
        ("figureCount", "figureCount", "figures"),
        ("tables", "tables", "tables"),
        ("footnotes", "footnotes", "footnotes"),
    ):
        wanted = expected.get(expected_key, 0)
        found = actual.get(actual_key, 0)
        if wanted != found:
            add_failure(failures, expected_key, f"{label} differ: expected {wanted}, found {found}")

    expected_citations = set(expected.get("citationKeys", []))
    actual_citations = set(actual.get("citationKeys", []))
    if expected_citations != actual_citations:
        add_failure(
            failures,
            "citations",
            "citation-key inventory differs",
            details={
                "missing": sorted(expected_citations - actual_citations),
                "unexpected": sorted(actual_citations - expected_citations),
            },
        )

    expected_labels = set(expected.get("labels", []))
    missing_source_targets = sorted(set(expected.get("references", [])) - expected_labels)
    if missing_source_targets:
        add_failure(
            failures,
            "source-cross-references",
            "pruned source contains references to missing labels",
            details=missing_source_targets,
        )
    actual_labels = set(actual.get("labels", []))
    missing_labels = sorted(expected_labels - actual_labels)
    if missing_labels:
        add_failure(failures, "labels", "source labels are missing from XML", details=missing_labels)

    broken = sorted(set(actual.get("references", [])) - actual_labels)
    if broken:
        add_failure(failures, "cross-references", "XML contains broken cross-references", details=broken)


def human_samples(pruned: str, commit: str) -> dict[str, Any]:
    rng = random.Random(commit)
    paragraph_samples: list[dict[str, Any]] = []
    equation_samples: list[dict[str, Any]] = []
    for segment in source_segments(pruned):
        plain_paragraphs = paragraph_list(detex_text(segment.tex))
        candidates = [paragraph for paragraph in plain_paragraphs if len(normalized_tokens(paragraph)) >= 20]
        if candidates:
            for paragraph in rng.sample(candidates, min(2, len(candidates))):
                paragraph_samples.append(
                    {"section": segment.title, "text": paragraph[:500], "reviewed": False}
                )
        equations = re.findall(
            r"\\begin\s*\{(?:equation|align|gather)\*?\}([\s\S]*?)\\end\s*\{(?:equation|align|gather)\*?\}",
            segment.tex,
        )
        if equations:
            for equation in rng.sample(equations, min(2, len(equations))):
                equation_samples.append(
                    {"section": segment.title, "tex": normalize_text(equation)[:800], "reviewed": False}
                )
    return {"seed": commit, "paragraphs": paragraph_samples, "equations": equation_samples}


def applied_exceptions(bundle: Path, commit: str) -> set[str]:
    path = bundle / "validation-exceptions.json"
    if not path.exists():
        return set()
    data = read_json(path)
    approved: set[str] = set()
    for item in data.get("exceptions", []):
        if (
            item.get("sourceCommit") == commit
            and item.get("failureId")
            and item.get("reason")
            and item.get("approvedBy")
            and item.get("approvedAt")
        ):
            approved.add(item["failureId"])
    return approved


def validate_slug(slug: str, *, write: bool = True) -> dict[str, Any]:
    manifest = load_manifest()
    paper = find_paper(manifest, slug)
    bundle = bundle_path(paper)
    failures: list[dict[str, Any]] = []
    warnings: list[str] = []
    required = [
        bundle / "paper.pdf",
        bundle / "paper.xml",
        bundle / "paper-context.json",
        bundle / "conversion.json",
        bundle / "source-inventory.json",
        bundle / "source" / "pruned.tex",
        bundle / "source" / "source-baseline.txt",
        bundle / "source" / "pdf-baseline.txt",
    ]
    for path in required:
        if not path.exists():
            add_failure(failures, "required-file", f"missing required artifact: {path.relative_to(bundle)}")

    report: dict[str, Any] = {
        "schemaVersion": 1,
        "paperId": paper["id"],
        "slug": slug,
        "generatedAt": utc_now(),
        "thresholds": {
            "overallProseRecall": 0.99,
            "sectionProseRecall": 0.97,
            "maximumUnmatchedRun": 24,
        },
        "failures": failures,
        "warnings": warnings,
    }

    if any(not path.exists() for path in required):
        report["passed"] = False
        report["unapprovedFailures"] = [item["id"] for item in failures]
        if write:
            bundle.mkdir(parents=True, exist_ok=True)
            write_json(bundle / "validation-report.json", report)
        return report

    conversion = read_json(bundle / "conversion.json")
    inventory = read_json(bundle / "source-inventory.json")
    commit = conversion.get("source", {}).get("commit", "")
    if not commit or commit != paper.get("source", {}).get("commit"):
        add_failure(failures, "source-commit", "manifest and conversion source commits do not match")
    if conversion.get("source", {}).get("trackedDirty"):
        add_failure(failures, "source-cleanliness", "paper was prepared from uncommitted tracked source changes")
    publisher = conversion.get("publisher", {})
    if (
        publisher.get("provider") != "codex-cli"
        or publisher.get("authentication") != "local-codex-login"
        or publisher.get("ephemeral") is not True
        or publisher.get("sandbox") != "read-only"
    ):
        add_failure(failures, "publisher-policy", "conversion did not use the constrained Codex publisher policy")
    conversion_chunks = conversion.get("chunks", [])
    chunk_ids = [chunk.get("id") for chunk in conversion_chunks]
    if len(chunk_ids) != len(set(chunk_ids)):
        add_failure(failures, "chunks", "conversion metadata contains duplicate chunk ids")
    incomplete = [chunk.get("id") for chunk in conversion_chunks if not chunk.get("completed")]
    if incomplete:
        add_failure(failures, "chunks", "conversion chunks are incomplete", details=incomplete)
    missing_bib = sorted({key for chunk in conversion_chunks for key in chunk.get("missingBibKeys", [])})
    if missing_bib:
        add_failure(failures, "bibliography", "cited BibTeX keys are missing", details=missing_bib)

    try:
        root = ET.parse(bundle / "paper.xml").getroot()
        if root.tag.upper() != "PAPER":
            raise ET.ParseError("root element is not PAPER")
    except ET.ParseError as exc:
        add_failure(failures, "xml", f"paper.xml is malformed: {exc}")
        root = None

    coverage: dict[str, Any] = {}
    section_results: list[dict[str, Any]] = []
    if root is not None:
        actual_inventory = xml_inventory(root)
        compare_inventory(inventory, actual_inventory, failures)
        xml_refs = [element.attrib["ref"] for element in root.iter() if element.attrib.get("ref")]
        duplicate_refs = sorted({ref for ref in xml_refs if xml_refs.count(ref) > 1})
        if duplicate_refs:
            add_failure(failures, "duplicate-labels", "XML contains duplicate ref anchors", details=duplicate_refs)
        source_baseline = (bundle / "source" / "source-baseline.txt").read_text(encoding="utf-8")
        pdf_baseline = (bundle / "source" / "pdf-baseline.txt").read_text(encoding="utf-8")
        output_text = xml_text(root)
        output_tokens = normalized_tokens(output_text)
        for chunk in conversion_chunks:
            if not contains_token_probe(output_tokens, chunk.get("openingProbe", [])):
                add_failure(
                    failures,
                    "chunk-boundary",
                    f"opening text for {chunk.get('id')} is missing",
                    severity=SOFT,
                )
            if not contains_token_probe(output_tokens, chunk.get("closingProbe", [])):
                add_failure(
                    failures,
                    "chunk-boundary",
                    f"closing text for {chunk.get('id')} is missing",
                    severity=SOFT,
                )
        source_result = sequence_coverage(source_baseline, output_text)
        pdf_result = sequence_coverage(pdf_baseline, output_text)
        coverage = {"source": source_result, "pdfAdvisory": pdf_result}
        if source_result["recall"] < 0.99:
            add_failure(
                failures,
                "overall-prose-recall",
                f"overall source prose recall {source_result['recall']:.3%} is below 99%",
                severity=SOFT,
                details=source_result["missingRuns"][:10],
            )
        if source_result["longestUnmatchedRun"] >= 25:
            add_failure(
                failures,
                "unmatched-run",
                f"longest unmatched source run is {source_result['longestUnmatchedRun']} tokens",
                severity=SOFT,
                details=source_result["missingRuns"][:10],
            )

        pruned = (bundle / "source" / "pruned.tex").read_text(encoding="utf-8")
        source_math = math_fragments_from_tex(pruned)
        output_math = [
            element.text or ""
            for element in root.iter()
            if element.tag.upper() in {"MATH", "EQUATION"}
        ]
        math_result = math_coverage(source_math, output_math)
        coverage["mathematics"] = math_result
        if math_result["recall"] < 0.98:
            add_failure(
                failures,
                "mathematics",
                f"normalized mathematical-token recall {math_result['recall']:.3%} is below 98%",
                details=math_result,
            )
        xml_sections: dict[str, ET.Element] = {}
        for element in root.iter():
            if element.tag.upper() in {"SECTION", "SUBSECTION", "SUBSUBSECTION"}:
                xml_sections.setdefault(normalized_title(element.attrib.get("title", "")), element)
        for segment in source_segments(pruned):
            if segment.level == "front":
                continue
            key = normalized_title(segment.title)
            element = xml_sections.get(key)
            if element is None:
                section_results.append({"title": segment.title, "found": False, "recall": 0.0})
                continue
            source_plain = detex_text(segment.tex)
            output_plain = element_text_without_nested_sections(element)
            result = sequence_coverage(source_plain, output_plain)
            opening = sentinel_matches(source_plain, output_plain, at_end=False)
            closing = sentinel_matches(source_plain, output_plain, at_end=True)
            section_results.append(
                {
                    "title": segment.title,
                    "found": True,
                    "recall": result["recall"],
                    "longestUnmatchedRun": result["longestUnmatchedRun"],
                    "openingMatches": opening,
                    "closingMatches": closing,
                    "missingRuns": result["missingRuns"][:5],
                }
            )
            if result["recall"] < 0.97:
                add_failure(
                    failures,
                    "section-prose-recall",
                    f"section {segment.title!r} recall {result['recall']:.3%} is below 97%",
                    severity=SOFT,
                    details=result["missingRuns"][:5],
                )
            if not opening or not closing:
                add_failure(
                    failures,
                    "section-boundary",
                    f"section {segment.title!r} does not preserve its opening and closing text",
                    severity=SOFT,
                )

        generated_context = context_from_xml(root)
        stored_context = read_json(bundle / "paper-context.json")
        if generated_context != stored_context:
            add_failure(failures, "context", "paper-context.json is not the canonical derivative of paper.xml")

    for figure in inventory.get("figures", []):
        asset = figure.get("asset")
        if not asset:
            add_failure(
                failures,
                "figure-asset",
                f"figure {figure.get('id')} requires reviewed extraction ({figure.get('source')})",
            )
        elif not (bundle / asset).exists():
            add_failure(failures, "figure-asset", f"missing figure asset: {asset}")

    pruned_path = bundle / "source" / "pruned.tex"
    report["coverage"] = coverage
    report["sections"] = section_results
    report["structuralInventory"] = inventory
    report["humanReview"] = human_samples(pruned_path.read_text(encoding="utf-8"), commit) if pruned_path.exists() else {}
    exceptions = applied_exceptions(bundle, commit)
    unapproved = [
        item for item in failures if item["severity"] == HARD or item["id"] not in exceptions
    ]
    report["appliedExceptions"] = sorted(exceptions)
    report["unapprovedFailures"] = [item["id"] for item in unapproved]
    report["passed"] = not unapproved
    if root is not None:
        report["artifactHashes"] = {
            "paperXmlSha256": sha256_file(bundle / "paper.xml"),
            "paperPdfSha256": sha256_file(bundle / "paper.pdf"),
            "contextSha256": sha256_file(bundle / "paper-context.json"),
            "sourceCommit": commit,
        }
    if write:
        write_json(bundle / "validation-report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    args = parser.parse_args()
    report = validate_slug(args.slug, write=True)
    print(f"{args.slug}: {'PASS' if report['passed'] else 'FAIL'}")
    for failure in report["failures"]:
        marker = "overridable" if failure["overridable"] else "hard"
        print(f"- [{marker}] {failure['id']}: {failure['message']}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
