#!/usr/bin/env python3
"""Build deployable live-paper assets, excluding unapproved drafts."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from paperlib import PAPERS_ROOT, bundle_path, load_manifest, sha256_file, write_json
from validate_paper import validate_slug


def validate_manifest(manifest: dict[str, Any]) -> None:
    ids: set[str] = set()
    slugs: set[str] = set()
    for paper in manifest["papers"]:
        for field in ("id", "slug", "title", "authors", "status", "bundle", "source", "approval"):
            if field not in paper:
                raise ValueError(f"manifest paper is missing {field}")
        if paper["id"] in ids or paper["slug"] in slugs:
            raise ValueError("manifest paper ids and slugs must be unique")
        ids.add(paper["id"])
        slugs.add(paper["slug"])
        if paper["status"] not in {"draft", "published"}:
            raise ValueError(f"invalid status for {paper['slug']}: {paper['status']}")
        if not isinstance(paper["authors"], list) or not paper["authors"]:
            raise ValueError(f"paper {paper['slug']} must have at least one author")


def public_entry(paper: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": paper["id"],
        "slug": paper["slug"],
        "title": paper["title"],
        "authors": paper["authors"],
        "publicationDate": paper.get("publicationDate", ""),
        "status": paper["status"],
        "approved": paper["approved"],
        "chatEnabled": bool(paper.get("chatEnabled")),
        "xml": f"{paper['slug']}/paper.xml",
        "pdf": f"{paper['slug']}/paper.pdf",
        "context": f"{paper['slug']}/paper-context.json",
    }


def copy_bundle(bundle: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("paper.xml", "paper.pdf", "paper-context.json"):
        shutil.copy2(bundle / name, destination / name)
    assets = bundle / "assets"
    if assets.exists():
        shutil.copytree(assets, destination / "assets", dirs_exist_ok=True)


def build(output: Path, *, include_drafts: bool = False, local_chat_endpoint: str = "") -> list[dict[str, Any]]:
    manifest = load_manifest()
    validate_manifest(manifest)
    output.mkdir(parents=True, exist_ok=True)
    reader_output = output / "_reader"
    reader_output.mkdir(parents=True, exist_ok=True)
    for asset in ("reader.css", "reader.js"):
        shutil.copy2(PAPERS_ROOT / "reader" / asset, reader_output / asset)
    template = (PAPERS_ROOT / "reader" / "index.html").read_text(encoding="utf-8")
    deployed: list[dict[str, Any]] = []

    for paper in manifest["papers"]:
        report = validate_slug(paper["slug"], write=False)
        is_published = paper["status"] == "published"
        if is_published:
            if not paper.get("approved"):
                raise RuntimeError(f"published paper {paper['slug']} is not approved")
            if not report["passed"]:
                raise RuntimeError(f"published paper {paper['slug']} fails validation")
            report_path = bundle_path(paper) / "validation-report.json"
            approval = paper.get("approval", {})
            if not report_path.exists():
                raise RuntimeError(f"published paper {paper['slug']} has no saved validation report")
            saved_report = json.loads(report_path.read_text(encoding="utf-8"))
            if saved_report.get("artifactHashes") != report.get("artifactHashes"):
                raise RuntimeError(f"published paper {paper['slug']} validation report is stale")
            if sha256_file(report_path) != approval.get("validationReportSha256"):
                raise RuntimeError(f"published paper {paper['slug']} approval report hash is stale")
            if approval.get("sourceCommit") != paper.get("source", {}).get("commit"):
                raise RuntimeError(f"published paper {paper['slug']} approval source commit is stale")
        elif not include_drafts:
            print(f"Draft excluded from Pages artifact: {paper['slug']}")
            continue

        bundle = bundle_path(paper)
        if not (bundle / "paper.xml").exists():
            if include_drafts:
                print(f"Draft cannot be previewed until assembled: {paper['slug']}")
                continue
            raise RuntimeError(f"missing paper.xml for {paper['slug']}")

        destination = output / paper["slug"]
        copy_bundle(bundle, destination)
        config = {
            "paperId": paper["id"],
            "title": paper["title"],
            "xmlUrl": "paper.xml",
            "chatEnabled": bool(paper.get("chatEnabled")),
            "chatEndpoint": local_chat_endpoint if include_drafts else manifest.get("chatEndpoint", ""),
            "isDraft": not is_published,
        }
        config_json = json.dumps(config, ensure_ascii=False).replace("<", "\\u003c")
        (destination / "index.html").write_text(
            template.replace("__PAPER_CONFIG__", config_json),
            encoding="utf-8",
        )
        deployed.append(public_entry(paper))

    write_json(
        output / "manifest.json",
        {"schemaVersion": 1, "papers": deployed},
    )
    shutil.copy2(PAPERS_ROOT / "upstream" / "LICENSE.ModernPapers", output / "LICENSE.ModernPapers")
    return deployed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-drafts", action="store_true")
    parser.add_argument("--local-chat-endpoint", default="http://localhost:8787/v1/ask")
    args = parser.parse_args()
    try:
        deployed = build(
            args.output.resolve(),
            include_drafts=args.include_drafts,
            local_chat_endpoint=args.local_chat_endpoint,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"paper build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Built {len(deployed)} paper(s) into {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
