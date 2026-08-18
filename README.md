# Alessandro Veneri — Academic Website

A minimal static academic website for GitHub Pages.

## Folder structure

```
/
├── index.html                       ← Home page
├── research.html                    ← Research page (expandable abstracts)
├── Academic_CV/                     ← LaTeX sources for the CV
│   ├── academic.tex                 ← Root file (compiled by CI)
│   └── *.tex, ref.bib               ← Section subfiles + bibliography
├── papers/                          ← Live-paper publisher, reader, manifest
├── services/paper-chat/             ← Cloudflare Worker (anonymous Q&A proxy)
├── .github/workflows/
│   ├── build-cv.yml                 ← Build and Deploy Site (publishes Pages)
│   ├── papers-ci.yml                ← Papers Pipeline CI (does not gate Pages)
│   └── deploy-paper-chat.yml        ← Deploy Worker (manual only)
└── assets/
    ├── photo.jpg                    ← Profile photo
    └── EUI-Logo.svg                 ← EUI logo
```

`assets/cv.pdf` is **not** in the repo — it is gitignored, compiled in CI, and
written straight into the published site. The CV button links to `assets/cv.pdf`,
which exists on the deployed site but not in a fresh clone.

## Live-paper infrastructure

Reusable ModernPapers-derived infrastructure lives in `papers/`, with the
anonymous Gemini proxy in `services/paper-chat/`. The advertising-auctions
paper is registered as a draft and is deliberately excluded from the GitHub
Pages artifact. Its research-page link is present only as an HTML comment.

The author-side publisher compiles and snapshots a paper, produces independent
LaTeX and PDF baselines, converts file-aware source units with the locally
authenticated Codex CLI, and blocks publication when prose coverage or
structural inventories do not reconcile. Distinct TeX files are never merged
for conversion; flattening is reserved for independent QA. Gemini is reserved
for public Q&A in the Worker after publication. See `papers/README.md` for
preparation, validation, preview, approval, and publishing commands.

## Deployment

The site lives at **https://alessandro-veneri.github.io**, in the public repo
`alessandro-veneri.github.io`. Pages is configured to **deploy from Actions**,
not to serve a branch — so the live site is whatever the **Build and Deploy
Site** workflow uploads, and a push only publishes if that workflow goes green.

Pushing to `main` compiles the CV, assembles `_site/` (pages, assets,
`assets/cv.pdf`, and any *approved* papers), and deploys. Allow ~2 minutes.

### Which workflow gates what

| Workflow | Runs on | Blocks the live site? |
| --- | --- | --- |
| **Build and Deploy Site** (`build-cv.yml`) | push to `main`, PRs, manual | **Yes** |
| **Papers Pipeline CI** (`papers-ci.yml`) | changes under `papers/` or `services/paper-chat/` | No |
| **Deploy Paper Chat Worker** (`deploy-paper-chat.yml`) | manual only | No |

Publisher and Worker unit tests run in **Papers Pipeline CI**, deliberately
*outside* the deploy workflow, so work in progress under `papers/` or
`services/` can never stop the CV from going live. This does not weaken the
publication gate, which is enforced at deploy time by `build_papers.py`: it
refuses to publish any paper that is unapproved, fails validation, or carries
stale approval hashes, and drafts are excluded from the artifact entirely.

Note that `papers/tests` shells out to `latexpand` (Debian/Ubuntu:
`texlive-extra-utils`), which `papers-ci.yml` installs. It is not available on a
bare runner, and it is not needed to build or deploy the site.

## Updating your CV

Edit any `.tex` file inside `Academic_CV/` (or `ref.bib`) and push to `main`.
The **Build and Deploy Site** workflow compiles `academic.tex` with `latexmk` +
`biber` and publishes the result to `assets/cv.pdf` on the live site. You don't
need a local LaTeX install — the build runs in CI, and no PDF is committed back
to the repo.

To trigger a rebuild without a content change, run the workflow manually:
**Actions → Build and Deploy Site → Run workflow**.

## If a push or publish fails

**The push itself hangs up.** Commits carrying images or PDFs can grow past the
point where git switches to chunked HTTP uploads, which can fail against GitHub:

```
send-pack: unexpected disconnect while reading sideband packet
fatal: the remote end hung up unexpectedly
```

Let git buffer the whole pack into a single request instead:

```bash
git config --global http.postBuffer 524288000
```

This is already configured on the primary machine; re-apply it on any new one.

**The push lands but the site doesn't change.** Check **Actions**. A red
**Build and Deploy Site** run means the deploy never happened — read the failing
step. A red **Papers Pipeline CI** run does *not* affect the live site.

## Adding coauthor links

In `research.html`, each coauthor name is wrapped in an `<a class="coauthor-link" href="#">` tag. Replace the `#` with the coauthor's website URL:

```html
<!-- Before -->
<a href="#" class="coauthor-link">Lewis Hammond</a>

<!-- After -->
<a href="https://lewishammond.com" class="coauthor-link">Lewis Hammond</a>
```

## Adding presentation venues

Each paper has a `<div class="paper-presentations">Presentations: </div>` line below the byline. It is **automatically hidden** when empty — no need to delete it. Once you have venues to list, add them after the colon:

```html
<div class="paper-presentations">Presentations: EUI Economics Workshop 2024; EARIE 2025</div>
```

## Updating research

Open `research.html` and edit the `.paper-item` blocks.
Each paper has a `<button class="paper-toggle">` (title + byline)
and a `<div class="paper-abstract">` (the collapsible abstract).
Copy an existing block to add a new paper.
