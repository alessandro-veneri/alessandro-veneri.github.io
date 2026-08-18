#!/usr/bin/env python3
"""Build draft paper assets and serve them locally for review."""

from __future__ import annotations

import argparse
import functools
import http.server
import shutil
from pathlib import Path

from build_papers import build
from paperlib import PAPERS_ROOT


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", help="paper slug to print after building")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--chat-endpoint", default="http://localhost:8787/v1/ask")
    args = parser.parse_args()
    output = PAPERS_ROOT / ".preview"
    if output.exists():
        shutil.rmtree(output)
    deployed = build(output, include_drafts=True, local_chat_endpoint=args.chat_endpoint)
    slugs = {paper["slug"] for paper in deployed}
    if args.slug and args.slug not in slugs:
        print(f"{args.slug} has not been assembled and cannot be previewed yet")
        return 1
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(output))
    server = http.server.ThreadingHTTPServer(("localhost", args.port), handler)
    suffix = f"/{args.slug}/" if args.slug else "/"
    print(f"Preview ready at http://localhost:{args.port}{suffix}")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
