# Draft status

This bundle is intentionally not deployed.

Preparation currently succeeds against the fresh 70-page PDF and produces 20
file-aware conversion chunks, dual source/PDF baselines, 403 display-math
blocks, 33 citation keys, and eight figures. Every top-level included TeX file
is kept separate; only large individual files are split into numbered parts.
Three raster figures have been copied; five TikZ/PGF figures still require
reviewed PDF crops through `figure-review.json`.

A first flattened-chunk Codex attempt was rejected by the extraction gate at
61.5% prose recall and is retained only in ignored work caches for diagnosis.
It was never deployable. The canonical file-aware conversion has not yet been
run, so `paper.xml` and `paper-context.json` do not exist and validation remains
red. Log the local Codex CLI into ChatGPT and rerun the publisher to resume.
Gemini is reserved for public Q&A after approval and publication.
