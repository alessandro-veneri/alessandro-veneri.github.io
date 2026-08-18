#!/usr/bin/env python3
"""Prepare, convert, assemble, and validate a reusable live-paper bundle."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from paperlib import (
    MANIFEST_PATH,
    PAPERS_ROOT,
    PUBLISHER_PROVIDER,
    PROMPT_VERSION,
    bibliography_xml,
    build_file_chunks,
    bundle_path,
    citation_keys,
    compile_fresh_pdf,
    context_from_xml,
    extract_figures,
    find_paper,
    flatten_and_prune,
    git_commit,
    git_source_state,
    load_manifest,
    make_baselines,
    normalized_tokens,
    parse_bibtex,
    pretty_xml,
    safe_xml_fragment,
    detex_text,
    sequence_coverage,
    sha256_file,
    sha256_text,
    source_inventory,
    utc_now,
    write_json,
    xml_text,
)


class CodexUnavailable(RuntimeError):
    pass


CONVERSION_PROMPT = r"""
You are converting one ordered chunk of an academic economics paper into XML.
Transcribe the academic content exactly. Do not correct, rewrite, summarize,
polish, shorten, or add claims. Preserve mathematical notation as TeX. Ignore
pure LaTeX layout commands. Do not use tools or external sources: the supplied
text is the sole authority. Return the requested structured result containing
the XML transcription.

Required wrapper:
<CHUNK id="{chunk_id}" startSentinel="{start_sentinel}" endSentinel="{end_sentinel}">...</CHUNK>

Use this nested vocabulary:
- <ABSTRACT><P>...</P></ABSTRACT>
- <SECTION ref="source-label-or-stable-id" title="Exact title">...</SECTION>
- <SUBSECTION ...> and <SUBSUBSECTION ...>
- <P> for every prose paragraph
- <MATH>TeX</MATH> for inline math
- <EQUATION ref="label-if-any">TeX</EQUATION> for display math
- <THEOREM>, <PROPOSITION>, <LEMMA>, <COROLLARY>, <DEFINITION>,
  <ASSUMPTION>, <REMARK>, and <PROOF>, preserving ref labels when present
- <FOOTNOTE>...</FOOTNOTE>
- <CITATION key="bib-key">visible citation text</CITATION>
- <XREF target="latex-label">visible reference text</XREF>
- <FIGURE ref="label" src="approved asset path"><CAPTION>...</CAPTION></FIGURE>
- <TABLE ref="label"><CAPTION>...</CAPTION>...</TABLE>

Do not include a lay summary or bibliography. Do not invent missing figure
assets, citations, labels, or prose. Copy the wrapper attributes exactly.
Every source paragraph, displayed equation, result, proof, footnote, figure,
table, citation, and section boundary in this chunk must appear once. Do not
stop early or replace any passage with a summary. Before returning, verify that
the first and final substantive source passages are both present.
""".strip()


SUMMARY_PROMPT = """
Write a faithful 200-300 word plain-language interpretation of this academic
paper. Explain the question, approach, principal findings, and significance.
Do not introduce claims absent from the paper. Do not use tools or external
sources. Return the requested structured result containing only summary prose;
do not use headings, XML, or Markdown in the summary.
""".strip()


def codex_version(codex_binary: str = "codex") -> str:
    try:
        result = subprocess.run(
            [codex_binary, "--version"], capture_output=True, text=True, timeout=15, check=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise CodexUnavailable("Codex CLI is not installed or is not runnable") from exc
    return result.stdout.strip() or result.stderr.strip()


def ensure_codex_login(codex_binary: str = "codex") -> None:
    try:
        result = subprocess.run(
            [codex_binary, "login", "status"], capture_output=True, text=True, timeout=20
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise CodexUnavailable("Codex CLI is not installed or is not runnable") from exc
    if result.returncode != 0 or "logged in" not in (result.stdout + result.stderr).lower():
        raise CodexUnavailable("Codex is not logged in; run `codex login` before publishing")


def codex_exec_command(
    schema_path: Path,
    output_path: Path,
    working_directory: Path,
    *,
    codex_binary: str = "codex",
    model: str = "",
) -> list[str]:
    command = [
        codex_binary,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--output-schema",
        str(schema_path),
        "--output-last-message",
        str(output_path),
        "-C",
        str(working_directory),
    ]
    if model:
        command.extend(["--model", model])
    command.append("-")
    return command


def codex_generate(
    prompt: str,
    schema: dict[str, Any],
    output_path: Path,
    *,
    codex_binary: str = "codex",
) -> tuple[dict[str, Any], dict[str, Any]]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path = output_path.with_suffix(".schema.json")
    write_json(schema_path, schema)
    requested_model = os.environ.get("CODEX_PUBLISHER_MODEL", "").strip()
    command = codex_exec_command(
        schema_path, output_path, output_path.parent, codex_binary=codex_binary, model=requested_model
    )
    try:
        result = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired as exc:
        raise CodexUnavailable("Codex timed out; rerun the publisher to resume from its cache") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1600:]
        if any(term in detail.lower() for term in ("usage limit", "rate limit", "not logged in", "authentication")):
            raise CodexUnavailable(f"Codex is temporarily unavailable; rerun to resume. {detail}")
        raise RuntimeError(f"Codex conversion failed ({result.returncode}): {detail}")
    try:
        data = json.loads(output_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex did not return schema-valid JSON") from exc
    diagnostic_output = result.stdout + "\n" + result.stderr
    actual_model = next(
        (line.split(":", 1)[1].strip() for line in diagnostic_output.splitlines() if line.startswith("model: ")),
        None,
    )
    metadata = {
        "provider": PUBLISHER_PROVIDER,
        "cliVersion": codex_version(codex_binary),
        "requestedModel": requested_model or None,
        "actualModel": actual_model,
        "ephemeral": True,
        "sandbox": "read-only",
        "responseSha256": sha256_file(output_path),
    }
    return data, metadata


def source_for_manifest_path(path_value: str) -> Path:
    return (PAPERS_ROOT / path_value).resolve()


def prepare_bundle(source: Path, paper: dict[str, Any], bundle: Path) -> dict[str, Any]:
    root_file = paper["source"]["root"]
    bib_file = paper["source"]["bibliography"]
    if not (source / root_file).exists():
        raise FileNotFoundError(source / root_file)
    if not (source / bib_file).exists():
        raise FileNotFoundError(source / bib_file)

    bundle.mkdir(parents=True, exist_ok=True)
    for generated_name in ("paper.xml", "paper-context.json", "validation-report.json"):
        (bundle / generated_name).unlink(missing_ok=True)
    source_dir = bundle / "source"
    source_dir.mkdir(exist_ok=True)
    assets_dir = bundle / "assets"
    pdf_path = bundle / "paper.pdf"

    print("[1/6] Compiling a fresh PDF in an isolated copy...")
    build = compile_fresh_pdf(source, root_file, pdf_path)
    print("[2/6] Flattening and pruning the compiled LaTeX source...")
    flat, pruned = flatten_and_prune(source, root_file)
    (source_dir / "flattened.tex").write_text(flat, encoding="utf-8")
    (source_dir / "pruned.tex").write_text(pruned, encoding="utf-8")

    print("[3/6] Building source and PDF baselines...")
    baselines = make_baselines(pruned, pdf_path)
    (source_dir / "source-baseline.txt").write_text(baselines["sourcePlain"], encoding="utf-8")
    (source_dir / "pdf-baseline.txt").write_text(baselines["pdfPlain"], encoding="utf-8")

    figures = extract_figures(pruned, source, assets_dir)
    inventory = source_inventory(pruned, figures)
    write_json(bundle / "source-inventory.json", inventory)
    review_path = bundle / "figure-review.json"
    existing_reviews: dict[str, dict[str, Any]] = {}
    if review_path.exists():
        existing = json.loads(review_path.read_text(encoding="utf-8"))
        existing_reviews = {item.get("id", ""): item for item in existing.get("figures", [])}
    review_figures = []
    for item in figures:
        if not item["manualRequired"]:
            continue
        previous = existing_reviews.get(item["id"], {})
        if previous.get("source") != item["source"] or previous.get("sourceStart") != item.get("sourceStart"):
            previous = {}
        review_figures.append(
            {
                "id": item["id"],
                "source": item["source"],
                "sourceStart": item.get("sourceStart"),
                "page": previous.get("page"),
                "x": previous.get("x"),
                "y": previous.get("y"),
                "width": previous.get("width"),
                "height": previous.get("height"),
                "dpi": previous.get("dpi", 180),
            }
        )
    write_json(
        review_path,
        {
            "schemaVersion": 1,
            "instructions": "Fill page/x/y/width/height after reviewing rendered PDF pages, then run extract_figures.py.",
            "figures": review_figures,
        },
    )

    print("[4/6] Building file-aware, resumable conversion chunks...")
    chunks = build_file_chunks(source, root_file)
    bib_entries = parse_bibtex((source / bib_file).read_text(encoding="utf-8", errors="replace"))
    work_root = PAPERS_ROOT / "work" / paper["slug"]
    chunk_dir = work_root / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk_records: list[dict[str, Any]] = []
    for chunk in chunks:
        keys = citation_keys(chunk["tex"])
        selected_bib = "\n\n".join(bib_entries[key] for key in keys if key in bib_entries)
        # The deterministic inventory is small and flattened positions do not
        # map safely back to file-aware chunks. Supplying the complete approved
        # map is safer than guessing which file owns an inline figure.
        figure_map = figures
        input_text = (
            CONVERSION_PROMPT.format(
                chunk_id=chunk["id"],
                start_sentinel=chunk["startSentinel"],
                end_sentinel=chunk["endSentinel"],
            )
            + "\n\nAPPROVED FIGURE MAP:\n"
            + json.dumps(figure_map, ensure_ascii=False)
            + "\n\nREFERENCED BIBTEX ENTRIES:\n"
            + selected_bib
            + "\n\nLATEX CHUNK:\n"
            + chunk["tex"]
        )
        input_path = chunk_dir / f"{chunk['id']}-{chunk['sha256'][:12]}.input.txt"
        input_path.write_text(input_text, encoding="utf-8")
        source_chunk_path = chunk_dir / f"{chunk['id']}-{chunk['sha256'][:12]}.source.tex"
        source_chunk_path.write_text(chunk["tex"], encoding="utf-8")
        record = {key: value for key, value in chunk.items() if key != "tex"}
        record.update(
            {
                "inputPath": str(input_path.relative_to(PAPERS_ROOT)),
                "sourceChunkPath": str(source_chunk_path.relative_to(PAPERS_ROOT)),
                "citationKeys": keys,
                "missingBibKeys": [key for key in keys if key not in bib_entries],
            }
        )
        chunk_records.append(record)

    commit = git_commit(source)
    source_state = git_source_state(source)
    provenance = {
        "schemaVersion": 1,
        "createdAt": utc_now(),
        "stage": "prepared",
        "publisher": {
            "provider": PUBLISHER_PROVIDER,
            "cliVersion": codex_version(),
            "requestedModel": os.environ.get("CODEX_PUBLISHER_MODEL", "").strip() or None,
            "authentication": "local-codex-login",
            "ephemeral": True,
            "sandbox": "read-only",
        },
        "promptVersion": PROMPT_VERSION,
        "source": {
            "path": paper["source"]["path"],
            "root": root_file,
            "bibliography": bib_file,
            "commit": commit,
            "trackedDirty": source_state["trackedDirty"],
            "gitStatus": source_state["status"],
            "rootSha256": sha256_file(source / root_file),
            "bibliographySha256": sha256_file(source / bib_file),
            "flatSha256": sha256_text(flat),
            "prunedSha256": sha256_text(pruned),
        },
        "pdf": build,
        "chunks": chunk_records,
        "inventorySha256": sha256_file(bundle / "source-inventory.json"),
    }
    write_json(bundle / "conversion.json", provenance)
    paper["source"]["commit"] = commit
    manifest = load_manifest()
    manifest_paper = next(item for item in manifest["papers"] if item["slug"] == paper["slug"])
    manifest_paper["source"]["commit"] = commit
    write_json(MANIFEST_PATH, manifest)
    return provenance


def chunk_output_schema(chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "chunkId": {"type": "string", "enum": [chunk["id"]]},
            "startSentinel": {"type": "string", "enum": [chunk["startSentinel"]]},
            "endSentinel": {"type": "string", "enum": [chunk["endSentinel"]]},
            "xml": {"type": "string"},
        },
        "required": ["chunkId", "startSentinel", "endSentinel", "xml"],
    }


SUMMARY_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def contains_probe(tokens: list[str], probe: list[str]) -> bool:
    if not probe:
        return True
    width = len(probe)
    return any(tokens[index : index + width] == probe for index in range(len(tokens) - width + 1))


def chunk_is_complete(chunk: dict[str, Any], root: ET.Element, coverage: dict[str, Any]) -> bool:
    output_tokens = normalized_tokens(xml_text(root))
    return (
        coverage["recall"] >= 0.85
        and coverage["longestUnmatchedRun"] < 25
        and contains_probe(output_tokens, chunk.get("openingProbe", []))
        and contains_probe(output_tokens, chunk.get("closingProbe", []))
    )


def convert_chunks(paper: dict[str, Any], bundle: Path, provenance: dict[str, Any]) -> list[ET.Element]:
    print("[5/6] Converting chunks with Codex...")
    roots: list[ET.Element] = []
    for index, chunk in enumerate(provenance["chunks"], start=1):
        input_path = PAPERS_ROOT / chunk["inputPath"]
        response_path = input_path.with_suffix(".response.xml")
        metadata_path = input_path.with_suffix(".response.json")
        source_chunk_path = PAPERS_ROOT / chunk["sourceChunkPath"]
        if response_path.exists() and metadata_path.exists():
            try:
                cached_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                root = safe_xml_fragment(response_path.read_text(encoding="utf-8"), "CHUNK")
                if (
                    cached_metadata.get("provider") == PUBLISHER_PROVIDER
                    and root.attrib.get("id") == chunk["id"]
                    and root.attrib.get("startSentinel") == chunk["startSentinel"]
                    and root.attrib.get("endSentinel") == chunk["endSentinel"]
                ):
                    coverage = sequence_coverage(
                        detex_text(source_chunk_path.read_text(encoding="utf-8")), xml_text(root)
                    )
                    if chunk_is_complete(chunk, root, coverage):
                        chunk["completed"] = True
                        chunk["responseSha256"] = sha256_file(response_path)
                        chunk["chunkCoverage"] = coverage
                        roots.append(root)
                        print(f"  [{index}/{len(provenance['chunks'])}] {chunk['id']} cached")
                        continue
            except (json.JSONDecodeError, ValueError, ET.ParseError):
                pass
        prompt = input_path.read_text(encoding="utf-8")
        print(f"  [{index}/{len(provenance['chunks'])}] {chunk['id']} requesting Codex")
        codex_output_path = input_path.with_suffix(".codex.json")
        response, metadata = codex_generate(prompt, chunk_output_schema(chunk), codex_output_path)
        root = safe_xml_fragment(response["xml"], "CHUNK")
        if root.attrib.get("id") != chunk["id"]:
            raise RuntimeError(f"Codex returned the wrong chunk id for {chunk['id']}")
        if root.attrib.get("startSentinel") != chunk["startSentinel"] or root.attrib.get("endSentinel") != chunk["endSentinel"]:
            raise RuntimeError(f"Codex did not preserve sentinels for {chunk['id']}")
        coverage = sequence_coverage(
            detex_text(source_chunk_path.read_text(encoding="utf-8")), xml_text(root)
        )
        if not chunk_is_complete(chunk, root, coverage):
            failed_path = response_path.with_suffix(".failed.xml")
            failed_path.write_text(pretty_xml(root), encoding="utf-8")
            raise RuntimeError(
                f"Codex transcription for {chunk['id']} failed the immediate completeness gate "
                f"(recall={coverage['recall']:.3%}, longest missing run={coverage['longestUnmatchedRun']}); "
                f"rejected output saved at {failed_path}"
            )
        response_path.write_text(pretty_xml(root), encoding="utf-8")
        write_json(metadata_path, metadata)
        chunk["completed"] = True
        chunk["responseSha256"] = sha256_file(response_path)
        chunk["responseMetadata"] = metadata
        chunk["chunkCoverage"] = coverage
        write_json(bundle / "conversion.json", provenance)
        roots.append(root)
    return roots


def assemble_bundle(
    paper: dict[str, Any],
    bundle: Path,
    provenance: dict[str, Any],
    chunks: list[ET.Element],
) -> None:
    pruned = (bundle / "source" / "pruned.tex").read_text(encoding="utf-8")
    source_text = (bundle / "source" / "source-baseline.txt").read_text(encoding="utf-8")
    source_path = source_for_manifest_path(provenance["source"]["path"])
    bib_entries = parse_bibtex(
        (source_path / provenance["source"]["bibliography"]).read_text(encoding="utf-8", errors="replace")
    )
    ordered_keys = citation_keys(pruned)

    root = ET.Element("PAPER", {"schemaVersion": "1", "sourceCommit": provenance["source"]["commit"]})
    ET.SubElement(root, "TITLE").text = paper["title"]
    authors = ET.SubElement(root, "AUTHORS")
    for author in paper["authors"]:
        ET.SubElement(authors, "AUTHOR").text = author
    ET.SubElement(root, "PUBLICATION_DATE").text = paper.get("publicationDate", "Draft")
    ET.SubElement(root, "PDF").text = "paper.pdf"

    summary_cache = PAPERS_ROOT / "work" / paper["slug"] / "lay-summary.txt"
    summary_metadata_path = summary_cache.with_suffix(".json")
    summary_metadata = {}
    if summary_metadata_path.exists():
        try:
            summary_metadata = json.loads(summary_metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary_metadata = {}
    if summary_cache.exists() and summary_metadata.get("provider") == PUBLISHER_PROVIDER:
        summary = summary_cache.read_text(encoding="utf-8").strip()
    else:
        summary_response, summary_meta = codex_generate(
            SUMMARY_PROMPT + "\n\nPAPER:\n" + source_text,
            SUMMARY_OUTPUT_SCHEMA,
            summary_cache.with_suffix(".codex.json"),
        )
        summary = summary_response["summary"].strip()
        summary_cache.parent.mkdir(parents=True, exist_ok=True)
        summary_cache.write_text(summary.strip() + "\n", encoding="utf-8")
        write_json(summary_metadata_path, summary_meta)
    ET.SubElement(root, "PLAINTEXT").text = summary.strip()

    for chunk in chunks:
        for child in list(chunk):
            root.append(child)
    root.append(bibliography_xml(bib_entries, ordered_keys))

    xml_path = bundle / "paper.xml"
    xml_path.write_text(pretty_xml(root), encoding="utf-8")
    write_json(bundle / "paper-context.json", context_from_xml(root))
    provenance["stage"] = "assembled"
    provenance["assembledAt"] = utc_now()
    provenance["paperXmlSha256"] = sha256_file(xml_path)
    provenance["contextSha256"] = sha256_file(bundle / "paper-context.json")
    write_json(bundle / "conversion.json", provenance)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="paper repository containing the root LaTeX file")
    parser.add_argument("--slug", required=True, help="manifest paper slug")
    parser.add_argument("--prepare-only", action="store_true", help="stop before calling Codex")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = load_manifest()
    paper = find_paper(manifest, args.slug)
    source = args.source.resolve() if args.source else source_for_manifest_path(paper["source"]["path"])
    bundle = bundle_path(paper)

    provenance = prepare_bundle(source, paper, bundle)
    if args.prepare_only:
        print("Preparation complete. No Codex conversion requests were made.")
        print(f"Prepared {len(provenance['chunks'])} chunks; rerun without --prepare-only to resume conversion.")
        return 0

    try:
        ensure_codex_login()
        chunks = convert_chunks(paper, bundle, provenance)
        assemble_bundle(paper, bundle, provenance, chunks)
    except CodexUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 75

    print("[6/6] Validating assembled paper...")
    from validate_paper import validate_slug

    report = validate_slug(args.slug, write=True)
    print(f"Validation {'PASSED' if report['passed'] else 'FAILED'}: {bundle / 'validation-report.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
