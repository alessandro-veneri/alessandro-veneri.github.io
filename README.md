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
├── .github/workflows/build-cv.yml   ← Auto-compile workflow
└── assets/
    ├── style.css                    ← Shared stylesheet
    ├── photo.jpg                    ← Profile photo
    ├── EUI-Logo.svg                 ← EUI logo
    └── cv.pdf                       ← Auto-built from Academic_CV/academic.tex
```

## Deployment

The site lives at **https://alessandro-veneri.github.io**, served by GitHub
Pages from the `main` branch of the public repo `alessandro-veneri.github.io`.
Pushing to `main` updates the live site within ~1 minute.

## Updating your CV

Edit any `.tex` file inside `Academic_CV/` (or `ref.bib`) and push to `main`.
A GitHub Actions workflow (`Build CV PDF`) compiles `academic.tex` with
`latexmk` + `biber` and commits the resulting PDF back to `assets/cv.pdf`,
which the CV button on the site always links to. You don't need a local
LaTeX install — the build runs in CI.

To trigger a rebuild without a content change, run the workflow manually:
**Actions → Build CV PDF → Run workflow**.

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
