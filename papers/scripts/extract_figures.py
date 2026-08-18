#!/usr/bin/env python3
"""Render reviewed vector/TikZ figure crops from a paper PDF."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from paperlib import PAPERS_ROOT, bundle_path, find_paper, load_manifest, read_json, write_json


def render_review_pages(pdf: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["pdftocairo", "-png", "-r", "96", str(pdf), str(output / "page")],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "pdftocairo failed")


def render_crop(pdf: Path, destination: Path, crop: dict) -> None:
    required = ("page", "x", "y", "width", "height", "dpi")
    if any(crop.get(field) is None for field in required):
        raise ValueError(f"figure {crop.get('id')} has an incomplete crop")
    page = int(crop["page"])
    dpi = int(crop["dpi"])
    prefix = destination.with_suffix("")
    command = [
        "pdftocairo", "-png", "-singlefile", "-f", str(page), "-l", str(page),
        "-r", str(dpi), "-x", str(int(crop["x"])), "-y", str(int(crop["y"])),
        "-W", str(int(crop["width"])), "-H", str(int(crop["height"])),
        str(pdf), str(prefix),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"failed to render {crop.get('id')}")
    if not destination.exists() or destination.stat().st_size < 100:
        raise RuntimeError(f"crop for {crop.get('id')} produced no usable PNG")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--render-pages", action="store_true", help="render low-resolution review pages first")
    args = parser.parse_args()
    paper = find_paper(load_manifest(), args.slug)
    bundle = bundle_path(paper)
    pdf = bundle / "paper.pdf"
    review = read_json(bundle / "figure-review.json")
    inventory_path = bundle / "source-inventory.json"
    inventory = read_json(inventory_path)

    if args.render_pages:
        pages = PAPERS_ROOT / "work" / args.slug / "review-pages"
        if pages.exists():
            shutil.rmtree(pages)
        render_review_pages(pdf, pages)
        print(f"Rendered review pages to {pages}")

    by_id = {figure["id"]: figure for figure in inventory.get("figures", [])}
    rendered = 0
    for crop in review.get("figures", []):
        if any(crop.get(field) is None for field in ("page", "x", "y", "width", "height")):
            continue
        figure = by_id.get(crop.get("id"))
        if not figure:
            raise ValueError(f"unknown figure id in figure-review.json: {crop.get('id')}")
        destination = bundle / "assets" / f"{figure['id']}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        render_crop(pdf, destination, crop)
        figure["asset"] = f"assets/{destination.name}"
        figure["manualRequired"] = False
        figure["reviewedCrop"] = {key: crop[key] for key in ("page", "x", "y", "width", "height", "dpi")}
        rendered += 1
    write_json(inventory_path, inventory)
    print(f"Rendered {rendered} reviewed figure crop(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
