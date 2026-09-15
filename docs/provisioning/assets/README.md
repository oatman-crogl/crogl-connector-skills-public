# Provisioning-guide assets

Shared across every connector's provisioning guide -- not per-connector. See [../../provisioning-guides.md](../../provisioning-guides.md) for the full rationale (why these guides exist, why AsciiDoc/PDF, how they relate to `connector.json`).

## What's here

| File | Purpose |
|---|---|
| `crogl-logo.png` | Title-page logo. |
| `pdf-theme.yml` | Custom title-page layout (extends asciidoctor-pdf's default theme). Without it, the logo and title end up scattered with large, badly-distributed gaps -- see the comments in the file and `../../provisioning-guides.md` for why. |
| `template.adoc` | Starting skeleton for a new connector's guide. |
| `adoc2pdf.sh` | Build script -- wraps the `asciidoctor-pdf` invocation so you don't need to remember the path convention or flags. |

## Starting a new guide

```bash
mkdir -p docs/provisioning/<connector-type>
cp docs/provisioning/assets/template.adoc docs/provisioning/<connector-type>/provisioning.adoc
```

Fill in every `<placeholder>` in the copy -- cross-reference `community/<connector-type>/connector.json`'s `credential_schema.fields` for what values are required rather than restating them, and name any vendor-UI trap explicitly (e.g. Entra's identically-named Delegated vs Application permissions) rather than paraphrasing it.

Then build:

```bash
docs/provisioning/assets/adoc2pdf.sh <connector-type>
```

Requires the `asciidoctor-pdf` gem (`gem install asciidoctor-pdf`) -- macOS system Ruby is too old for its dependencies, so install a current Ruby first (e.g. `brew install ruby`) if the gem install fails.

Commit the `.adoc` source and the built `.pdf` together.

## Write one for every connector type

Don't skip a guide just because a credential's *shape* looks simple ("it's just one API key field") -- that says nothing about whether *obtaining* the value takes a real console step (an admin-role requirement, a specific settings-menu path, a licensing wrinkle). `connector.json`'s `credential_schema.fields` descriptions say what values are needed; the guide says how a human gets each one. If a step genuinely can't be confirmed against a live console, mark it best-guess/TBD in the guide rather than leaving the guide unwritten. See [docs/provisioning-guides.md](../../provisioning-guides.md) for the full rationale.
