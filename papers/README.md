# Live papers

This directory contains the reusable live-paper reader and the author-side
publisher. Drafts are validated in CI but are never copied into the GitHub
Pages artifact. A paper is deployable only when its manifest entry has both
`"status": "published"` and `"approved": true`, and its validation report
passes every publication gate.

## First-time preparation

From the `site` repository:

```sh
python3 papers/scripts/publish_paper.py \
  --source ../EUI-projects/adv-baseline \
  --slug digital-advertising-auctions \
  --prepare-only
```

This compiles a fresh PDF in an isolated temporary copy, builds dual text
baselines, inventories the document, and writes provenance. Conversion follows
the top-level `\\input` order from the root document: every included TeX file
is a separate source unit, and large files may split into numbered parts but
are never merged with another file. The flattened representation is used only
as an independent extraction baseline. Preparation also copies raster figures
and does not call Codex.

To convert prepared chunks, log the local Codex CLI into your ChatGPT account
and rerun without `--prepare-only`:

```sh
codex login status
python3 papers/scripts/publish_paper.py \
  --source ../EUI-projects/adv-baseline \
  --slug digital-advertising-auctions
```

Successful chunk responses are cached by content hash in `papers/work/`, so a
Codex interruption can be resumed without repeating completed calls. Each call
is non-interactive, ephemeral, read-only, and constrained by an output schema.
The publisher uses the existing local Codex login and never reads a Gemini key.
Set `CODEX_PUBLISHER_MODEL` only if you deliberately need to pin a particular
Codex model; otherwise the installed Codex CLI selects its configured default.

Gemini is reserved exclusively for reader questions after publication. Its API
key belongs only in the Cloudflare Worker secret described in
`services/paper-chat/README.md`.

## Validation and preview

```sh
python3 papers/scripts/validate_paper.py --slug digital-advertising-auctions
python3 papers/scripts/preview_papers.py --slug digital-advertising-auctions
```

The validator writes a JSON report containing prose coverage, missing runs,
section-level results, structural counts, missing assets, and human-review
samples. A draft may have a failing report; a published paper may not.

For TikZ/PGF/vector-only figures, render review pages, fill the crop coordinates
in the generated `figure-review.json`, and extract the approved crops:

```sh
python3 papers/scripts/extract_figures.py \
  --slug digital-advertising-auctions --render-pages
```

## Publishing

After reviewing every flagged mismatch, theorem/proposition statement,
figure, table, lay summary, and the reproducible samples in the report:

1. Record approval with `approve_paper.py`.
2. Change the manifest status to `published`.
3. Enable the already-commented link in `research.html` in a separate edit.

The Pages build independently checks the report hash and all publication gates.

## Tests

```sh
python3 -m unittest discover papers/tests
npm test --prefix services/paper-chat
```
