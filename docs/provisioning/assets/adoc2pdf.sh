#!/usr/bin/env bash
# adoc2pdf.sh -- build a provisioning guide's PDF from its AsciiDoc source.
#
# Usage:
#   docs/provisioning/assets/adoc2pdf.sh <connector-type>
#
# Builds docs/provisioning/<connector-type>/provisioning.adoc to
# docs/provisioning/<connector-type>/provisioning.pdf.
#
# Requires the asciidoctor-pdf gem on PATH (gem install asciidoctor-pdf).
# macOS system Ruby is too old for asciidoctor-pdf's dependencies -- install
# a current Ruby first (e.g. brew install ruby) if the gem install fails.

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <connector-type>" >&2
  exit 1
fi

connector_type="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
provisioning_dir="$(cd "$script_dir/.." && pwd)"
src="$provisioning_dir/$connector_type/provisioning.adoc"
out="$provisioning_dir/$connector_type/provisioning.pdf"

if [ ! -f "$src" ]; then
  echo "error: $src not found" >&2
  exit 1
fi

if ! command -v asciidoctor-pdf >/dev/null 2>&1; then
  echo "error: asciidoctor-pdf not found on PATH." >&2
  echo "install with: gem install asciidoctor-pdf (requires a current Ruby, not macOS system Ruby)" >&2
  exit 1
fi

asciidoctor-pdf "$src" -o "$out"
echo "Built $out"
