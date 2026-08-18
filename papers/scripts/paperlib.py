#!/usr/bin/env python3
"""Shared implementation for the live-paper publisher and validation gates."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable


PAPERS_ROOT = Path(__file__).resolve().parents[1]
SITE_ROOT = PAPERS_ROOT.parent
MANIFEST_PATH = PAPERS_ROOT / "manifest.json"
PUBLISHER_PROVIDER = "codex-cli"
PROMPT_VERSION = "codex-modernpapers-exact-v2"
MAX_CHUNK_CHARS = 30_000  # well below 20k tokens; favors faithful full transcription


@dataclass(frozen=True)
class SourceSegment:
    identifier: str
    level: str
    title: str
    tex: str
    start: int
    end: int


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def load_manifest() -> dict[str, Any]:
    manifest = read_json(MANIFEST_PATH)
    if manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("papers"), list):
        raise ValueError("papers/manifest.json has an unsupported schema")
    return manifest


def find_paper(manifest: dict[str, Any], slug: str) -> dict[str, Any]:
    matches = [paper for paper in manifest["papers"] if paper.get("slug") == slug]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one manifest entry for slug {slug!r}")
    return matches[0]


def bundle_path(paper: dict[str, Any]) -> Path:
    path = (PAPERS_ROOT / str(paper["bundle"])).resolve()
    if PAPERS_ROOT.resolve() not in path.parents:
        raise ValueError("paper bundle escapes papers directory")
    return path


def run_checked(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"required command not found: {args[0]}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{' '.join(args)} failed:\n{detail[-4000:]}")
    return result


def git_commit(source: Path) -> str:
    return run_checked(["git", "rev-parse", "HEAD"], cwd=source).stdout.strip()


def git_source_state(source: Path) -> dict[str, Any]:
    status = run_checked(["git", "status", "--porcelain"], cwd=source).stdout.splitlines()
    tracked_dirty = False
    for line in status:
        code = line[:2]
        if code != "??":
            tracked_dirty = True
            break
    return {"trackedDirty": tracked_dirty, "status": status}


def strip_tex_comments(text: str) -> str:
    output: list[str] = []
    for line in text.splitlines():
        cut = len(line)
        for index, char in enumerate(line):
            if char != "%":
                continue
            preceding = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                preceding += 1
                cursor -= 1
            if preceding % 2 == 0:
                cut = index
                break
        output.append(line[:cut].rstrip())
    return "\n".join(output)


def strip_if_false(text: str) -> str:
    r"""Evaluate literal \iffalse blocks while preserving an optional \else branch."""
    # Only literal conditionals are evaluated here. In particular, `\iff` is
    # a mathematical relation command, not the beginning of a TeX conditional.
    token_re = re.compile(r"\\iffalse\b|\\iftrue\b|\\else\b|\\fi\b")
    output: list[str] = []
    cursor = 0
    while True:
        start = re.search(r"\\iffalse\b", text[cursor:])
        if not start:
            output.append(text[cursor:])
            break
        absolute = cursor + start.start()
        output.append(text[cursor:absolute])
        depth = 0
        else_start: int | None = None
        end_pos: int | None = None
        for token in token_re.finditer(text, absolute):
            value = token.group(0)
            if value.startswith("\\if"):
                depth += 1
            elif value == "\\else" and depth == 1:
                else_start = token.end()
            elif value == "\\fi":
                depth -= 1
                if depth == 0:
                    if else_start is not None:
                        output.append(text[else_start:token.start()])
                    end_pos = token.end()
                    break
        if end_pos is None:
            raise ValueError("unterminated \\iffalse block")
        cursor = end_pos
    return "".join(output)


def strip_comment_environments(text: str) -> str:
    r"""Remove LaTeX comment environments, whose contents are not compiled."""
    pattern = re.compile(r"\\begin\s*\{comment\}[\s\S]*?\\end\s*\{comment\}", re.I)
    previous = None
    while previous != text:
        previous = text
        text = pattern.sub("", text)
    return text


def prune_fragment(text: str) -> str:
    text = strip_if_false(strip_tex_comments(strip_comment_environments(text)))
    text = re.sub(r"\\printbibliography(?:\[[^]]*\])?", "\n%BIBLIOGRAPHY_MARKER\n", text)
    text = re.sub(r"\\(?:newpage|clearpage|pagebreak)\b", "\n\n", text)
    text = re.sub(r"\\(?:vspace|hspace)\*?\s*\{[^{}]*\}", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_document_body(flat_tex: str) -> str:
    start = flat_tex.find("\\begin{document}")
    end = flat_tex.rfind("\\end{document}")
    if start < 0 or end < start:
        raise ValueError("flattened source has no complete document environment")
    return prune_fragment(flat_tex[start + len("\\begin{document}") : end]) + "\n"


def flatten_and_prune(source: Path, root_file: str) -> tuple[str, str]:
    flat = run_checked(["latexpand", root_file], cwd=source).stdout
    # Remove comments first: commented-out \iffalse tokens are not TeX
    # conditionals and may intentionally have no matching \fi.
    pruned = extract_document_body(strip_if_false(strip_tex_comments(flat)))
    return flat, pruned


def detex_text(tex: str) -> str:
    try:
        result = run_checked(["detex"], input_text=tex)
        return result.stdout
    except RuntimeError:
        fallback = re.sub(r"\\[A-Za-z@]+\*?(?:\[[^]]*\])?", " ", tex)
        fallback = re.sub(r"[{}$]", " ", fallback)
        return fallback


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(text))
    text = text.replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalized_tokens(text: str) -> list[str]:
    return re.findall(r"[\w]+(?:['-][\w]+)*", normalize_text(text).casefold(), re.UNICODE)


def paragraph_list(text: str) -> list[str]:
    return [normalize_text(p) for p in re.split(r"\n\s*\n", text) if len(normalize_text(p)) >= 20]


def balanced_content(text: str, open_brace: int) -> tuple[str, int]:
    if open_brace >= len(text) or text[open_brace] != "{":
        raise ValueError("expected opening brace")
    depth = 0
    for index in range(open_brace, len(text)):
        if text[index] == "{" and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif text[index] == "}" and (index == 0 or text[index - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return text[open_brace + 1 : index], index + 1
    raise ValueError("unbalanced braces")


def clean_title(tex_title: str) -> str:
    plain = normalize_text(detex_text(tex_title))
    return plain or normalize_text(re.sub(r"\\[A-Za-z@]+", "", tex_title).replace("{", "").replace("}", ""))


def source_segments(pruned: str) -> list[SourceSegment]:
    header_re = re.compile(r"\\(section|subsection|subsubsection)\*?(?:\[[^]]*\])?\s*\{")
    headers: list[tuple[int, int, str, str]] = []
    for match in header_re.finditer(pruned):
        title, end = balanced_content(pruned, match.end() - 1)
        headers.append((match.start(), end, match.group(1), clean_title(title)))

    segments: list[SourceSegment] = []
    if not headers or headers[0][0] > 0:
        end = headers[0][0] if headers else len(pruned)
        front = pruned[:end].strip()
        if front:
            segments.append(SourceSegment("front-matter", "front", "Front matter", front, 0, end))

    counters = {"section": 0, "subsection": 0, "subsubsection": 0}
    for index, (start, _title_end, level, title) in enumerate(headers):
        counters[level] += 1
        if level == "section":
            counters["subsection"] = 0
            counters["subsubsection"] = 0
        elif level == "subsection":
            counters["subsubsection"] = 0
        identifier = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:60]
        identifier = identifier or f"{level}-{counters[level]}"
        end = headers[index + 1][0] if index + 1 < len(headers) else len(pruned)
        segments.append(SourceSegment(identifier, level, title, pruned[start:end].strip(), start, end))
    return segments


def split_oversized(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = re.split(r"(\n\s*\n)", text)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) > max_chars:
            chunks.append(current.strip())
            current = ""
        current += paragraph
    if current.strip():
        chunks.append(current.strip())
    return chunks


def build_chunks(pruned: str) -> list[dict[str, Any]]:
    atomic: list[dict[str, Any]] = []
    for segment in source_segments(pruned):
        pieces = split_oversized(segment.tex)
        for piece_index, piece in enumerate(pieces, start=1):
            suffix = f" (part {piece_index}/{len(pieces)})" if len(pieces) > 1 else ""
            atomic.append(
                {
                    "segmentId": segment.identifier,
                    "level": segment.level,
                    "title": segment.title + suffix,
                    "sourceStart": segment.start,
                    "sourceEnd": segment.end,
                    "tex": piece,
                }
            )

    # Preserve logical boundaries, but group adjacent small sections so a long
    # paper does not consume the free tier with dozens of header-only calls.
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for item in atomic:
        item_chars = len(item["tex"])
        if current and current_chars + item_chars > MAX_CHUNK_CHARS:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        groups.append(current)

    chunks: list[dict[str, Any]] = []
    for group in groups:
        number = len(chunks) + 1
        identifier = f"chunk-{number:03d}"
        piece = "\n\n".join(item["tex"] for item in group)
        digest = sha256_text(piece)
        plain_tokens = normalized_tokens(detex_text(piece))
        title = group[0]["title"] if len(group) == 1 else f"{group[0]['title']} — {group[-1]['title']}"
        chunks.append(
            {
                "id": identifier,
                "segmentId": group[0]["segmentId"],
                "segmentIds": [item["segmentId"] for item in group],
                "level": group[0]["level"],
                "title": title,
                "sourceStart": group[0]["sourceStart"],
                "sourceEnd": group[-1]["sourceEnd"],
                "tokenEstimate": round(len(piece) / 4),
                "sha256": digest,
                "startSentinel": f"MP-START-{identifier}-{digest[:12]}",
                "endSentinel": f"MP-END-{identifier}-{digest[-12:]}",
                "openingProbe": plain_tokens[:20],
                "closingProbe": plain_tokens[-20:] if plain_tokens else [],
                "tex": piece,
            }
        )
    return chunks


def resolve_tex_input(source: Path, value: str) -> Path:
    candidate = (source / value.strip()).resolve()
    if candidate.suffix == "":
        candidate = candidate.with_suffix(".tex")
    if source.resolve() not in candidate.parents or not candidate.exists():
        raise FileNotFoundError(f"included TeX source is unavailable: {value}")
    return candidate


def build_file_chunks(source: Path, root_file: str) -> list[dict[str, Any]]:
    """Create ordered conversion chunks without merging distinct source files."""
    root_path = (source / root_file).resolve()
    raw_root = root_path.read_text(encoding="utf-8", errors="replace")
    document_start = raw_root.find("\\begin{document}")
    document_end = raw_root.rfind("\\end{document}")
    if document_start < 0 or document_end < document_start:
        raise ValueError(f"{root_file} has no complete document environment")
    body_start = document_start + len("\\begin{document}")
    body = raw_root[body_start:document_end]
    input_re = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")
    units: list[dict[str, Any]] = []
    cursor = 0
    root_part = 0

    def add_unit(path_value: str, text: str, line_start: int, unit_name: str) -> None:
        cleaned = prune_fragment(text)
        if not normalized_tokens(detex_text(cleaned)):
            return
        units.append(
            {
                "sourcePath": path_value,
                "sourceLineStart": line_start,
                "unitName": unit_name,
                "tex": cleaned,
            }
        )

    for match in input_re.finditer(body):
        fragment = body[cursor:match.start()]
        if fragment.strip():
            root_part += 1
            line_start = raw_root.count("\n", 0, body_start + cursor) + 1
            add_unit(root_file, fragment, line_start, f"{Path(root_file).stem}-part-{root_part:02d}")

        included = resolve_tex_input(source, match.group(1))
        relative = included.relative_to(source.resolve()).as_posix()
        expanded = run_checked(["latexpand", "--keep-comments", relative], cwd=source).stdout
        add_unit(relative, expanded, 1, re.sub(r"[^a-z0-9]+", "-", relative.casefold()).strip("-"))
        cursor = match.end()

    trailing = body[cursor:]
    if trailing.strip():
        root_part += 1
        line_start = raw_root.count("\n", 0, body_start + cursor) + 1
        add_unit(root_file, trailing, line_start, f"{Path(root_file).stem}-part-{root_part:02d}")

    chunks: list[dict[str, Any]] = []
    for unit in units:
        pieces = split_oversized(unit["tex"])
        piece_cursor = 0
        for part_index, piece in enumerate(pieces, start=1):
            relative_start = unit["tex"].find(piece, piece_cursor)
            if relative_start < 0:
                relative_start = piece_cursor
            piece_cursor = relative_start + len(piece)
            source_line_start = unit["sourceLineStart"] + unit["tex"].count("\n", 0, relative_start)
            digest = sha256_text(piece)
            number = len(chunks) + 1
            identifier = f"chunk-{number:03d}-{unit['unitName'][:44]}-p{part_index:02d}"
            plain_tokens = normalized_tokens(detex_text(piece))
            chunks.append(
                {
                    "id": identifier,
                    "segmentId": unit["unitName"],
                    "segmentIds": [unit["unitName"]],
                    "level": "source-file",
                    "title": f"{unit['sourcePath']} (part {part_index}/{len(pieces)})",
                    "sourcePath": unit["sourcePath"],
                    "sourceLineStart": source_line_start,
                    "sourcePart": part_index,
                    "sourceParts": len(pieces),
                    "tokenEstimate": round(len(piece) / 4),
                    "sha256": digest,
                    "startSentinel": f"MP-START-{identifier}-{digest[:12]}",
                    "endSentinel": f"MP-END-{identifier}-{digest[-12:]}",
                    "openingProbe": plain_tokens[:20],
                    "closingProbe": plain_tokens[-20:] if plain_tokens else [],
                    "tex": piece,
                }
            )
    return chunks


def parse_bibtex(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    cursor = 0
    while True:
        match = re.search(r"@[A-Za-z]+\s*\{\s*([^,\s]+)\s*,", text[cursor:])
        if not match:
            break
        start = cursor + match.start()
        open_brace = text.find("{", start)
        depth = 0
        end = None
        for index in range(open_brace, len(text)):
            if text[index] == "{" and (index == 0 or text[index - 1] != "\\"):
                depth += 1
            elif text[index] == "}" and (index == 0 or text[index - 1] != "\\"):
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            raise ValueError(f"unbalanced BibTeX entry near {match.group(1)}")
        entries[match.group(1)] = text[start:end]
        cursor = end
    return entries


CITE_RE = re.compile(
    r"\\(?:[A-Za-z]*cite[A-Za-z*]*|citeyear|Citeauthor)\s*"
    r"(?:\[[^]]*\]\s*){0,2}\{([^}]*)\}"
)


def citation_keys(tex: str) -> list[str]:
    keys: list[str] = []
    for match in CITE_RE.finditer(tex):
        for key in match.group(1).split(","):
            cleaned = key.strip()
            if cleaned and cleaned not in keys:
                keys.append(cleaned)
    return keys


def extract_labels(tex: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\\label\s*\{([^}]+)\}", tex)))


def extract_references(tex: str) -> list[str]:
    pattern = r"\\(?:ref|eqref|autoref|cref|Cref)\s*\{([^}]+)\}"
    return list(dict.fromkeys(re.findall(pattern, tex)))


def extract_figures(tex: str, source: Path, assets_dir: Path) -> list[dict[str, Any]]:
    assets_dir.mkdir(parents=True, exist_ok=True)
    figures: list[dict[str, Any]] = []
    used_names: set[str] = set()
    occurrences: list[tuple[int, str, re.Match[str]]] = []
    occurrences.extend(
        (match.start(), "includegraphics", match)
        for match in re.finditer(r"\\includegraphics(?:\[[^]]*\])?\s*\{([^}]+)\}", tex)
    )
    occurrences.extend(
        (match.start(), "tikz", match)
        for match in re.finditer(r"\\begin\s*\{tikzpicture\}", tex)
    )
    occurrences.extend(
        (match.start(), "tex-figure", match)
        for match in re.finditer(r"\\input\s*\{(figures/[^}]+)\}", tex)
    )

    for index, (position, kind, match) in enumerate(sorted(occurrences, key=lambda item: item[0]), start=1):
        if kind == "includegraphics":
            original = match.group(1).strip()
            candidate = source / original
            if not candidate.suffix:
                for suffix in (".png", ".jpg", ".jpeg", ".pdf"):
                    if candidate.with_suffix(suffix).exists():
                        candidate = candidate.with_suffix(suffix)
                        break
            asset: str | None = None
            missing = not candidate.exists()
            if not missing and candidate.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".svg"}:
                base = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate.name)
                name = base
                serial = 2
                while name.casefold() in used_names:
                    name = f"{candidate.stem}-{serial}{candidate.suffix}"
                    serial += 1
                used_names.add(name.casefold())
                destination = assets_dir / name
                shutil.copy2(candidate, destination)
                asset = f"assets/{name}"
            figures.append(
                {
                    "id": f"figure-{index}",
                    "kind": "raster" if asset else "pdf-or-missing",
                    "source": original,
                    "sourceStart": position,
                    "asset": asset,
                    "manualRequired": asset is None,
                    "missingSource": missing,
                }
            )
        else:
            figures.append(
                {
                    "id": f"figure-{index}",
                    "kind": kind,
                    "source": match.group(1) if kind == "tex-figure" else "inline tikzpicture",
                    "sourceStart": position,
                    "asset": None,
                    "manualRequired": True,
                    "missingSource": False,
                }
            )
    return figures


ENVIRONMENT_TAGS = {
    "thm": "THEOREM",
    "theorem": "THEOREM",
    "prop": "PROPOSITION",
    "proposition": "PROPOSITION",
    "lem": "LEMMA",
    "lemma": "LEMMA",
    "cor": "COROLLARY",
    "corollary": "COROLLARY",
    "defn": "DEFINITION",
    "definition": "DEFINITION",
    "ass": "ASSUMPTION",
    "assumption": "ASSUMPTION",
    "proof": "PROOF",
    "remark": "REMARK",
    "rem": "REMARK",
}


def source_inventory(tex: str, figures: list[dict[str, Any]]) -> dict[str, Any]:
    segments = source_segments(tex)
    environments: dict[str, int] = {tag: 0 for tag in sorted(set(ENVIRONMENT_TAGS.values()))}
    for environment, tag in ENVIRONMENT_TAGS.items():
        environments[tag] += len(re.findall(rf"\\begin\s*\{{{re.escape(environment)}\}}", tex))
    display_math = sum(
        len(re.findall(pattern, tex))
        for pattern in (
            r"\\begin\s*\{equation\*?\}",
            r"\\begin\s*\{align\*?\}",
            r"\\begin\s*\{gather\*?\}",
            r"\\\[",
        )
    )
    return {
        "sections": [
            {"id": segment.identifier, "level": segment.level, "title": segment.title}
            for segment in segments
            if segment.level != "front"
        ],
        "environments": environments,
        "displayMath": display_math,
        "figures": figures,
        "figureCount": len(figures),
        "tables": len(re.findall(r"\\begin\s*\{table\*?\}", tex)),
        "footnotes": len(re.findall(r"\\footnote\s*\{", tex)),
        "citationKeys": citation_keys(tex),
        "labels": extract_labels(tex),
        "references": extract_references(tex),
    }


def compile_fresh_pdf(source: Path, root_file: str, destination: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="modernpapers-build-") as temp_name:
        temp = Path(temp_name) / "source"
        shutil.copytree(
            source,
            temp,
            ignore=shutil.ignore_patterns(".git", ".DS_Store", "*.synctex.gz"),
        )
        # A working paper repository often contains stale local build products.
        # They are not source inputs and a corrupt .bcf can prevent latexmk from
        # starting, so remove only the root document's known build artifacts.
        root_stem = Path(root_file).with_suffix("")
        for suffix in (
            ".aux", ".bbl", ".bcf", ".blg", ".fdb_latexmk", ".fls",
            ".log", ".out", ".run.xml", ".synctex.gz",
        ):
            candidate = temp / f"{root_stem}{suffix}"
            if candidate.exists():
                candidate.unlink()
        result = run_checked(
            ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", root_file],
            cwd=temp,
        )
        pdf = temp / Path(root_file).with_suffix(".pdf")
        log = temp / Path(root_file).with_suffix(".log")
        if not pdf.exists():
            raise RuntimeError("LaTeX build completed without producing a PDF")
        log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        unresolved = bool(
            re.search(r"LaTeX Warning: (?:Citation|Reference).+undefined|There were undefined references", log_text)
        )
        if unresolved:
            raise RuntimeError("fresh PDF has unresolved citations or references")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf, destination)
        return {
            "sha256": sha256_file(destination),
            "bytes": destination.stat().st_size,
            "buildOutputTail": (result.stdout + result.stderr)[-2000:],
        }


def pdf_text(pdf_path: Path) -> str:
    return run_checked(["pdftotext", "-layout", str(pdf_path), "-"]).stdout


def make_baselines(pruned: str, pdf_path: Path) -> dict[str, Any]:
    source_plain = detex_text(pruned)
    pdf_plain = pdf_text(pdf_path)
    return {
        "sourcePlain": source_plain,
        "pdfPlain": pdf_plain,
        "sourceNormalized": normalize_text(source_plain),
        "pdfNormalized": normalize_text(pdf_plain),
    }


def sequence_coverage(source_text: str, output_text: str) -> dict[str, Any]:
    source = normalized_tokens(source_text)
    output = normalized_tokens(output_text)
    matcher = SequenceMatcher(a=source, b=output, autojunk=False)
    blocks = matcher.get_matching_blocks()
    matched = sum(block.size for block in blocks)
    longest = 0
    missing: list[dict[str, Any]] = []
    cursor = 0
    for block in blocks:
        gap = block.a - cursor
        if gap > 0:
            longest = max(longest, gap)
            missing.append(
                {
                    "startToken": cursor,
                    "tokenCount": gap,
                    "text": " ".join(source[cursor : min(block.a, cursor + 80)]),
                }
            )
        cursor = block.a + block.size
    return {
        "sourceTokens": len(source),
        "outputTokens": len(output),
        "matchedTokens": matched,
        "recall": round(matched / len(source), 6) if source else 1.0,
        "longestUnmatchedRun": longest,
        "missingRuns": sorted(missing, key=lambda item: item["tokenCount"], reverse=True)[:50],
    }


def xml_text(root: ET.Element, *, exclude: Iterable[str] = ("PLAINTEXT", "REFERENCES")) -> str:
    excluded = {tag.upper() for tag in exclude}
    pieces: list[str] = []

    def visit(element: ET.Element) -> None:
        if element.tag.upper() in excluded:
            return
        if element.text:
            pieces.append(element.text)
        for child in element:
            visit(child)
            if child.tail:
                pieces.append(child.tail)

    visit(root)
    return "\n\n".join(piece.strip() for piece in pieces if piece.strip())


def xml_inventory(root: ET.Element) -> dict[str, Any]:
    sections: list[dict[str, str]] = []
    for element in root.iter():
        tag = element.tag.upper()
        if tag in {"SECTION", "SUBSECTION", "SUBSUBSECTION"}:
            sections.append(
                {
                    "id": element.attrib.get("ref", ""),
                    "level": tag.casefold(),
                    "title": element.attrib.get("title", "") or (element.text or "").strip(),
                }
            )
    environment_counts = {
        tag: sum(1 for element in root.iter() if element.tag.upper() == tag)
        for tag in sorted(set(ENVIRONMENT_TAGS.values()))
    }
    return {
        "sections": sections,
        "environments": environment_counts,
        "displayMath": sum(1 for element in root.iter() if element.tag.upper() == "EQUATION"),
        "figureCount": sum(1 for element in root.iter() if element.tag.upper() == "FIGURE"),
        "tables": sum(1 for element in root.iter() if element.tag.upper() == "TABLE"),
        "footnotes": sum(1 for element in root.iter() if element.tag.upper() == "FOOTNOTE"),
        "citationKeys": sorted(
            {element.attrib.get("key", "") for element in root.iter() if element.tag.upper() == "CITATION"}
            - {""}
        ),
        "labels": sorted(
            {element.attrib.get("ref", "") for element in root.iter() if element.attrib.get("ref")}
        ),
        "references": sorted(
            {element.attrib.get("target", "") for element in root.iter() if element.tag.upper() == "XREF"}
            - {""}
        ),
    }


def context_from_xml(root: ET.Element) -> dict[str, Any]:
    blocks: list[dict[str, str]] = []
    current_anchor = "paper"
    for element in root.iter():
        tag = element.tag.upper()
        if tag in {"SECTION", "SUBSECTION", "SUBSUBSECTION"}:
            current_anchor = element.attrib.get("ref", current_anchor)
        if tag in {"P", "ABSTRACT", "THEOREM", "PROPOSITION", "LEMMA", "COROLLARY", "DEFINITION", "ASSUMPTION", "REMARK"}:
            text = normalize_text(" ".join(element.itertext()))
            if text:
                blocks.append({"anchor": element.attrib.get("ref", current_anchor), "text": text})
    return {"schemaVersion": 1, "blocks": blocks}


def safe_xml_fragment(text: str, expected_tag: str) -> ET.Element:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:xml)?\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = re.search(rf"<{expected_tag}\b", cleaned, flags=re.I)
    end = re.search(rf"</{expected_tag}>\s*$", cleaned, flags=re.I)
    if not start or not end:
        raise ValueError(f"model response is not a complete <{expected_tag}> fragment")
    fragment = cleaned[start.start() : end.end()]
    root = ET.fromstring(fragment)
    if root.tag.upper() != expected_tag.upper():
        raise ValueError(f"expected <{expected_tag}> but received <{root.tag}>")
    return root


def bibliography_xml(entries: dict[str, str], ordered_keys: list[str]) -> ET.Element:
    root = ET.Element("REFERENCES")
    for key in ordered_keys:
        entry = ET.SubElement(root, "REFERENCE", {"key": key})
        entry.text = format_bibtex_entry(entries[key]) if key in entries else f"Missing BibTeX entry: {key}"
    return root


def bibtex_fields(entry: str) -> dict[str, str]:
    comma = entry.find(",")
    if comma < 0:
        return {}
    body = entry[comma + 1 :].strip()
    if body.endswith("}"):
        body = body[:-1]
    fields: dict[str, str] = {}
    cursor = 0
    while cursor < len(body):
        while cursor < len(body) and (body[cursor].isspace() or body[cursor] == ","):
            cursor += 1
        match = re.match(r"([A-Za-z][A-Za-z0-9_-]*)\s*=\s*", body[cursor:])
        if not match:
            break
        name = match.group(1).casefold()
        cursor += match.end()
        if cursor >= len(body):
            break
        if body[cursor] == "{":
            value, cursor = balanced_content(body, cursor)
        elif body[cursor] == '"':
            cursor += 1
            start = cursor
            escaped = False
            while cursor < len(body):
                char = body[cursor]
                if char == '"' and not escaped:
                    break
                escaped = char == "\\" and not escaped
                if char != "\\":
                    escaped = False
                cursor += 1
            value = body[start:cursor]
            cursor += 1
        else:
            end = body.find(",", cursor)
            if end < 0:
                end = len(body)
            value = body[cursor:end].strip()
            cursor = end
        fields[name] = normalize_text(detex_text(value))
    return fields


def format_bibtex_entry(entry: str) -> str:
    fields = bibtex_fields(entry)
    if not fields:
        return normalize_text(detex_text(entry))
    authors = fields.get("author") or fields.get("editor") or "Unknown author"
    authors = authors.replace(" and ", "; ")
    year = fields.get("year") or fields.get("date") or "n.d."
    title = fields.get("title", "Untitled")
    venue = (
        fields.get("journaltitle")
        or fields.get("journal")
        or fields.get("booktitle")
        or fields.get("institution")
        or fields.get("publisher")
        or ""
    )
    parts = [f"{authors} ({year}).", f"{title}."]
    if venue:
        parts.append(f"{venue}.")
    if fields.get("url"):
        parts.append(fields["url"])
    return normalize_text(" ".join(parts))


def pretty_xml(element: ET.Element) -> str:
    ET.indent(element, space="  ")
    return ET.tostring(element, encoding="unicode", xml_declaration=False) + "\n"
