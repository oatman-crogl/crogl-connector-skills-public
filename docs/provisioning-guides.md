# Provisioning Guides

A provisioning guide is a human-facing walkthrough for setting up the vendor-side credentials a connector needs: what kind of account or app registration to create, what scopes/permissions to grant, whether admin consent is required, and how to get the resulting values into Crogl. It is written for whoever configures access in the customer's tenant -- a customer admin, or an FDE walking them through it on a call -- not for the agent.

This is a different audience than everything else in this repo. `SKILL.md` files (reference skills and community connector types) are context an agent reads before making an API call; a provisioning guide is a document a human reads, prints, or has open during a live setup.

Write a provisioning guide for every connector type -- don't decide one is unnecessary just because the credential shape looks simple. Whether a credential is "one field" or "three fields plus OAuth" says nothing about how much real-world friction there is in *obtaining* it: a single API key can still require an admin role and a specific settings-menu navigation to generate (Recorded Future's Enterprise-Administrator-gated token, for example), while some multi-field credentials are handed to a customer as a package with no console work of their own. `connector.json`'s `credential_schema.fields` descriptions stay useful on their own, but they don't replace a guide -- they say *what* values are needed, not *how* a human obtains them.

If a step genuinely can't be pinned down -- the vendor's own docs sit behind a customer login you don't have, or a menu path is inferred from third-party sources rather than a first-hand console check -- mark that specific step best-guess/TBD in the guide (see the `[IMPORTANT]`/`[NOTE]` admonition pattern in existing guides) rather than skipping the guide entirely. A guide with one flagged TBD step is still far more useful to whoever runs setup than no guide at all.

## Layout

```text
docs/provisioning/
├── assets/
│   ├── README.md                # local guide to this folder + starting a new guide
│   ├── crogl-logo.png          # shared across all provisioning guides
│   ├── pdf-theme.yml           # custom title-page layout, see below
│   ├── template.adoc           # starting point for a new guide
│   └── adoc2pdf.sh             # build script, see Build below
└── <connector-type>/
    ├── provisioning.adoc       # source
    └── provisioning.pdf        # built output, committed alongside the source
```

One subdirectory per connector that needs a guide. `assets/` is shared, not per-connector -- put the Crogl logo and any other brand assets reused across guides there once, and reference with `:imagesdir: ../assets` + `:title-logo-image: image:crogl-logo.png[pdfwidth=6in,align=center]` + `:pdf-theme: {docdir}/../assets/pdf-theme.yml` in each `.adoc`'s document header (note the `../` -- `assets/` is a sibling of each connector's subfolder, not a child of it; `{docdir}` is required for `pdf-theme` specifically, since unlike `imagesdir` it resolves relative to the invoking shell's working directory, not the document's, unless told otherwise).

Note on logo size and title-page layout: the default asciidoctor-pdf theme anchors the logo and title at fixed page-percentage positions (10% and 55% from the top) regardless of the logo's actual size. With a large logo (this repo uses 6in, since the source image is square and widening it also grows its height) those defaults leave a large, badly-distributed gap -- empty space above the logo, another gap between logo and title, dense content, then nothing. `pdf-theme.yml` overrides `title_page.logo.top` (8%) and `title_page.title.top` (42%) to pull the two together into one block near the top of the page, leaving the leftover whitespace consolidated as a normal-looking bottom margin instead of scattered gaps. If a future guide's logo/title needs different sizing, adjust these two values together and re-check the render -- don't change one without the other.

## Starting a new guide

```bash
mkdir -p docs/provisioning/<connector-type>
cp docs/provisioning/assets/template.adoc docs/provisioning/<connector-type>/provisioning.adoc
```

Fill in every `<placeholder>` in the copy, then build with `docs/provisioning/assets/adoc2pdf.sh <connector-type>` (see Build below). See also [assets/README.md](./provisioning/assets/README.md) for the same steps documented locally, right next to the files it references.

## Why AsciiDoc, and why not part of the reference skill

The deep-docs slot under `references/<vendor>-reference/references/` (or, for self-contained bundles, `community/<connector-type>/SKILL.md` itself) is agent context -- plain Markdown, loaded straight into the agent's window. Anything that renders (PDF, print layout, admonition styling) is wasted on that audience and actively lossy: a PDF has to be extracted back to text before an agent can use it, and that round-trip mangles tables and code blocks.

A provisioning guide has the opposite audience, so the opposite tradeoff applies. Nobody reads this as agent context. A human opens, prints, or is handed the file during setup, so a clean rendered PDF is the right output format, and AsciiDoc (versioned, diffable source that builds to that PDF) is a reasonable way to author it. Don't move provisioning content into a `SKILL.md` or a deep-docs `.md` file -- it belongs in `docs/provisioning/`, not in anything the agent reads.

## Relationship to `connector.json`

`connector.json`'s `credential_schema.fields` stay the single source of truth for *what* values are required (field keys, labels, whether a field is secret). A provisioning guide explains *how a human obtains each value* -- which portal, which menu, which permission name, whether consent needs an admin. Cross-reference the field keys; don't re-list or duplicate them.

## Authoring checklist

- Write one for every connector type -- this is not conditional on the credential looking complex. Even a single-API-key connector gets a guide; if there's truly nothing beyond "copy the key from your account page," say that plainly in a short guide rather than omitting the file.
- Cover, at minimum: what type of account/app registration to create (or state there is none), exact scope/permission names to grant (or state there is no scope picker), whether admin consent is required, how to generate the secret/credential Crogl needs, and where that value maps to a `connector.json` credential field.
- Use placeholder tenant/org names and generic screenshots only -- this repo's customer-shareable rules (`CONTRIBUTING.md` § 1) apply here exactly as they do everywhere else in the repo.
- Keep steps vendor-console-accurate where you can verify them live. Where you can't -- the vendor's own docs are login-walled, or you're corroborating menu labels from third-party integration guides rather than a first-hand check -- say so explicitly in the guide (an `[IMPORTANT]` or `[NOTE]` admonition works well) instead of either guessing silently or skipping that section. Best-guess/TBD content that's labeled as such is acceptable; unlabeled guesswork is not.

## Build

Requires the `asciidoctor-pdf` gem (`gem install asciidoctor-pdf`) -- not part of this repo's existing toolchain, so install it before building. macOS system Ruby is too old for its dependencies; install a current Ruby first (e.g. `brew install ruby`) if the gem install fails.

```bash
docs/provisioning/assets/adoc2pdf.sh <connector-type>
```

Equivalent to running directly:

```bash
asciidoctor-pdf docs/provisioning/<connector-type>/provisioning.adoc -o docs/provisioning/<connector-type>/provisioning.pdf
```

Commit source and built PDF together, same as reference-skill and connector-type zips.
