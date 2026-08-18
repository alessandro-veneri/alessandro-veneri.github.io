#!/usr/bin/env python3
"""Record human approval for a paper whose automated validation passes."""

from __future__ import annotations

import argparse

from paperlib import MANIFEST_PATH, bundle_path, find_paper, load_manifest, sha256_file, utc_now, write_json
from validate_paper import validate_slug


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument(
        "--confirm-human-review",
        action="store_true",
        help="confirm that the report's theorem, figure, table, summary, and sample checks were completed",
    )
    args = parser.parse_args()
    if not args.confirm_human_review:
        parser.error("--confirm-human-review is required")

    report = validate_slug(args.slug, write=True)
    if not report["passed"]:
        print("Cannot approve: automated validation has unapproved failures.")
        return 1
    manifest = load_manifest()
    paper = find_paper(manifest, args.slug)
    report_path = bundle_path(paper) / "validation-report.json"
    paper["approved"] = True
    paper["approval"] = {
        "approvedAt": utc_now(),
        "approvedBy": args.reviewer,
        "sourceCommit": report["artifactHashes"]["sourceCommit"],
        "validationReportSha256": sha256_file(report_path),
    }
    write_json(MANIFEST_PATH, manifest)
    print(f"Approved {args.slug}. Status remains {paper['status']!r}; publishing is a separate edit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
