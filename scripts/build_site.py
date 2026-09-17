#!/usr/bin/env python3
"""Static site generator for the connector library demo site.

Reads the repo's own README.md tables (already-curated status/version/
one-line descriptions) as the catalog source of truth, and each
connector's SKILL.md + connector.json for the detail pages. Pure
stdlib -- no pip dependencies. Output is committed to docs/, served by
GitHub Pages from main branch /docs.

Usage: python3 scripts/build_site.py
Re-run any time source content changes, then commit docs/.
"""
import base64
import html
import json
import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
LOGO_SRC_DIR = REPO_ROOT / "assets" / "logos"
RAW_BASE = "https://github.com/oatman-crogl/crogl-connector-skills-public/raw/main"

MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@4.3.0/marked.min.js"

# Hand-curated -- neither tags nor a logo are modeled anywhere in
# connector.json/SKILL.md, so this is the source of truth for both.
# Keys not listed here simply render with no tags / no logo.
TAGS = {
    "abuseipdb": ["Threat Intelligence"],
    "bigquery": ["Data Warehouse", "Querying"],
    "cisco-secure-access-ravpn": ["Network Security", "VPN"],
    "cisco-xdr-incidents": ["XDR", "Incident Response"],
    "cortex-xdr": ["XDR", "Endpoint Security"],
    "darktrace": ["Network Detection", "AI Security"],
    "defender": ["Endpoint Security", "EDR"],
    "intune": ["Endpoint Management", "MDM"],
    "ionic": ["Data Platform", "Querying"],
    "jira-datacenter": ["Ticketing", "ITSM"],
    "pagerduty": ["Incident Response", "On-Call"],
    "palo-alto-xsoar": ["SOAR", "Incident Response"],
    "proofpoint-ices": ["Email Security"],
    "purview-dlp": ["Data Loss Prevention", "Compliance"],
    "qradar": ["SIEM"],
    "recordedfuture": ["Threat Intelligence"],
    "runzero": ["Asset Discovery", "Attack Surface"],
    "servicenow-chatops": ["ITSM", "ChatOps"],
    "solarwinds-service-desk": ["Ticketing", "ITSM"],
    "sophos-central": ["Endpoint Security", "EDR"],
    "splunk-enterprise-security": ["SIEM"],
    "taegis": ["XDR"],
    "tanium": ["Endpoint Management", "Vulnerability Management"],
    "tenable-io": ["Vulnerability Management"],
    "tines": ["SOAR", "Automation"],
    "windows-update-reports": ["Patch Management"],
    "cb-edr-reference": ["Endpoint Security", "EDR"],
    "cortex-xdr-reference": ["XDR"],
    "github-reference": ["DevOps", "Source Control"],
    "wiz-reference": ["CNAPP", "Cloud Security"],
}

# Maps a key to a filename under assets/logos/ (source, committed -- this
# is the maintained bank, mirrored in from the private repo's
# connector-coverage/logos/; add a file there and a line here for a new
# connector). The generator copies only the ones actually referenced here
# into docs/assets/logos/ each run.
LOGOS = {
    "abuseipdb": "abuseipdb.svg",
    "bigquery": "bigquery.svg",
    "cisco-secure-access-ravpn": "cisco.svg",
    "cisco-xdr-incidents": "cisco.svg",
    "cortex-xdr": "paloalto.png",
    "cortex-xdr-reference": "paloalto.png",
    "defender": "defender.svg",
    "intune": "intune.svg",
    "jira-datacenter": "jira-datacenter.png",
    "palo-alto-xsoar": "paloalto.png",
    "proofpoint-ices": "proofpoint.png",
    "purview-dlp": "purview.svg",
    "qradar": "qradar.svg",
    "recordedfuture": "recordedfuture.png",
    "servicenow-chatops": "servicenow.png",
    "solarwinds-service-desk": "solarwinds.png",
    "sophos-central": "sophos.png",
    "splunk-enterprise-security": "splunk-es.png",
    "tanium": "tanium.svg",
    "tines": "tines.png",
    "windows-update-reports": "windows-update.svg",
    "cb-edr-reference": "carbonblack.png",
    "github-reference": "github.svg",
    "wiz-reference": "wiz.svg",
}


def h1_title(body: str, fallback_key: str) -> str:
    m = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
    if m:
        return m.group(1).strip()
    return fallback_key.replace("-", " ").title()


def parse_frontmatter(text: str):
    assert text.startswith("---\n"), "expected frontmatter"
    end = text.index("\n---\n", 4)
    fm_text = text[4:end]
    body = text[end + 5 :]
    fm = {}
    for line in fm_text.split("\n"):
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip().strip('"')
    return fm, body.strip()


def parse_readme_table(readme_text: str, header_prefix: str):
    lines = readme_text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith(header_prefix))
    header_cells = [c.strip() for c in lines[start].strip("|").split("|")]
    end = start + 2  # skip header + separator
    rows = []
    while end < len(lines) and lines[end].strip().startswith("|"):
        cells = [c.strip() for c in lines[end].strip("|").split("|")]
        rows.append(dict(zip(header_cells, cells)))
        end += 1
    return rows


STATUS_RE = re.compile(r"!\[([a-z-]+)\]\((https://img\.shields\.io/badge/status-[^)]+)\)")
DIST_LINK_RE = re.compile(r"\[[^\]]+\]\(\./(dist/[^)]+)\)")


def dist_path_from_cell(cell: str):
    """Extract the repo-relative dist/... path from a Distributable cell,
    or None when the cell is just an em dash (no distributable, e.g. a
    superseded reference skill with nothing to import)."""
    m = DIST_LINK_RE.search(cell)
    return m.group(1) if m else None


def status_from_cell(cell: str):
    m = STATUS_RE.search(cell)
    if not m:
        return "unknown", ""
    return m.group(1), m.group(2)


def key_from_cell(cell: str):
    m = re.search(r"`([a-z0-9-]+)`", cell)
    return m.group(1) if m else cell.strip()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def md_embed(markdown_body: str) -> str:
    """Base64-embed markdown so no HTML/JS escaping is needed at all."""
    b64 = base64.b64encode(markdown_body.encode("utf-8")).decode("ascii")
    return b64


PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<link rel="stylesheet" href="{css_path}">
</head>
<body>
"""

PAGE_FOOT = """
</body>
</html>
"""

NAV = """<header class="site-header">
  <a class="wordmark" href="{root}index.html">Crogl Connector Library</a>
  <span class="preview-pill">Preview</span>
</header>
"""


def badge_img(status: str, status_badge_url: str, version: str) -> str:
    version_badge = f"https://img.shields.io/badge/version-{version.replace('-', '--')}-blue"
    return (
        f'<img class="badge" src="{status_badge_url}" alt="status: {html.escape(status)}"> '
        f'<img class="badge" src="{version_badge}" alt="version {html.escape(version)}">'
    )


def tags_pill_html(key: str) -> str:
    tags = TAGS.get(key)
    if not tags:
        return ""
    return f'<div class="tag-pill">{html.escape(", ".join(tags).upper())}</div>\n'


def logo_img_html(key: str, css_class: str = "logo") -> str:
    filename = LOGOS.get(key)
    if not filename:
        return ""
    return f'<img class="{css_class}" src="{"../" if css_class == "logo-detail" else ""}assets/logos/{filename}" alt="">'


def render_card(entry) -> str:
    href = f"{entry['kind']}/{entry['key']}.html"
    return f"""<a class="card" href="{href}" data-name="{html.escape(entry['key'])}" data-status="{html.escape(entry['status'])}" data-kind="{entry['kind']}">
  {tags_pill_html(entry['key'])}
  <div class="card-top">
    {logo_img_html(entry['key'])}
    <h3>{html.escape(entry['name_display'])}</h3>
  </div>
  <p class="card-desc">{html.escape(entry['blurb'])}</p>
  <div class="card-meta">{badge_img(entry['status'], entry['status_badge_url'], entry['version'])}</div>
</a>
"""


def render_field_row(f) -> str:
    kind = html.escape(f.get("kind", ""))
    required = "Yes" if f.get("required") else "No"
    secret = "Yes" if f.get("secret") else "No"
    label = html.escape(f.get("label", f.get("key", "")))
    desc = html.escape(f.get("description", ""))
    return f"""<tr><td><strong>{label}</strong></td><td>{kind}</td><td>{required}</td><td>{secret}</td><td>{desc}</td></tr>
"""


def render_detail_page(entry, css_path: str) -> str:
    fields_html = ""
    if entry.get("fields"):
        rows = "".join(render_field_row(f) for f in entry["fields"])
        fields_html = f"""<h2>What you'll need</h2>
<table class="fields">
<thead><tr><th>Field</th><th>Type</th><th>Required</th><th>Secret</th><th>Description</th></tr></thead>
<tbody>{rows}</tbody>
</table>
"""
    provisioning_html = ""
    if entry.get("provisioning_pdf_name"):
        provisioning_html = (
            f'<p><a class="btn btn-secondary" href="../assets/provisioning/{entry["provisioning_pdf_name"]}" '
            f'download="{entry["provisioning_pdf_name"]}">Credential setup guide (PDF)</a></p>\n'
        )

    download_html = f'<p><a class="btn btn-primary" href="{entry["download_url"]}">Download {entry["kind_label"]}</a></p>\n'

    md_b64 = md_embed(entry["body"])
    tags_html = tags_pill_html(entry["key"])
    logo_html = logo_img_html(entry["key"], css_class="logo-detail")

    return PAGE_HEAD.format(title=f"{entry['name_display']} — Crogl Connector Library", css_path=css_path) + NAV.format(root="../") + f"""
<main class="detail">
  <p class="back"><a href="../index.html">&larr; All connectors</a></p>
  {tags_html}
  <div class="detail-top">
    {logo_html}
    <h1>{html.escape(entry['name_display'])}</h1>
  </div>
  <div class="badges">{badge_img(entry['status'], entry['status_badge_url'], entry['version'])}</div>
  <p class="blurb">{html.escape(entry['blurb'])}</p>
  {download_html}
  {provisioning_html}
  {fields_html}
  <h2>Full documentation</h2>
  <div id="md-content" class="markdown-body">Loading&hellip;</div>
  <script id="md-source" type="text/plain">{md_b64}</script>
  <script src="{MARKED_CDN}"></script>
  <script>
    (function() {{
      var b64 = document.getElementById('md-source').textContent;
      var bytes = Uint8Array.from(atob(b64), function(c) {{ return c.charCodeAt(0); }});
      var md = new TextDecoder('utf-8').decode(bytes);
      document.getElementById('md-content').innerHTML = marked.parse(md);
    }})();
  </script>
</main>
""" + PAGE_FOOT


INDEX_TEMPLATE = """
<main class="index">
  <section class="hero">
    <h1>Crogl Connector Library</h1>
    <p>Browse and download Crogl-compatible connectors for your SOC stack. Each connector is a self-contained, importable package &mdash; docs, credential requirements, and CLI all included.</p>
  </section>
  <div class="filters">
    <button class="filter-btn active" data-filter-kind="all">All</button>
    <button class="filter-btn" data-filter-kind="connector">Community connectors</button>
    <button class="filter-btn" data-filter-kind="reference">Reference skills</button>
    <input class="search" type="search" id="search" placeholder="Search connectors&hellip;">
  </div>
  <div class="grid" id="grid">
{cards}
  </div>
  <p class="empty" id="empty" hidden>No connectors match.</p>
</main>
<script>
(function() {{
  var buttons = document.querySelectorAll('.filter-btn');
  var search = document.getElementById('search');
  var cards = document.querySelectorAll('.card');
  var empty = document.getElementById('empty');
  var activeKind = 'all';

  function apply() {{
    var q = search.value.trim().toLowerCase();
    var visible = 0;
    cards.forEach(function(c) {{
      var kindOk = activeKind === 'all' || c.dataset.kind === activeKind;
      var textOk = !q || c.textContent.toLowerCase().indexOf(q) !== -1;
      var show = kindOk && textOk;
      c.hidden = !show;
      if (show) visible++;
    }});
    empty.hidden = visible !== 0;
  }}

  buttons.forEach(function(b) {{
    b.addEventListener('click', function() {{
      buttons.forEach(function(x) {{ x.classList.remove('active'); }});
      b.classList.add('active');
      activeKind = b.dataset.filterKind;
      apply();
    }});
  }});
  search.addEventListener('input', apply);
}})();
</script>
"""


def main():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    comm_rows = parse_readme_table(readme, "| Connector type |")
    ref_rows = parse_readme_table(readme, "| Reference skill |")

    entries = []

    for row in comm_rows:
        key = key_from_cell(row["Connector type"])
        status, status_url = status_from_cell(row["Status"])
        skill_path = REPO_ROOT / "community" / key / "SKILL.md"
        connector_json_path = REPO_ROOT / "community" / key / "connector.json"
        fm, body = parse_frontmatter(skill_path.read_text(encoding="utf-8"))
        fields = []
        if connector_json_path.exists():
            cj = json.loads(connector_json_path.read_text(encoding="utf-8"))
            fields = cj.get("credential_schema", {}).get("fields", [])
        prov_pdf = REPO_ROOT / "docs" / "provisioning" / key / "provisioning.pdf"
        dist_path = dist_path_from_cell(row["Distributable"])
        entries.append({
            "kind": "connector",
            "kind_label": "connector",
            "key": key,
            "name_display": h1_title(body, fm.get("name", key)),
            "blurb": row["Target product"],
            "status": status,
            "status_badge_url": status_url,
            "version": row["Version"],
            "fields": fields,
            "body": body,
            "download_url": f"{RAW_BASE}/{dist_path}" if dist_path else None,
            "provisioning_pdf_src": prov_pdf if prov_pdf.exists() else None,
            "provisioning_pdf_name": f"{key}-provisioning.pdf" if prov_pdf.exists() else None,
        })

    for row in ref_rows:
        key = key_from_cell(row["Reference skill"])
        status, status_url = status_from_cell(row["Status"])
        skill_path = REPO_ROOT / "references" / key / "SKILL.md"
        fm, body = parse_frontmatter(skill_path.read_text(encoding="utf-8"))
        dist_path = dist_path_from_cell(row["Distributable"])
        entries.append({
            "kind": "reference",
            "kind_label": "reference skill",
            "key": key,
            "name_display": h1_title(body, fm.get("name", key)),
            "blurb": row["Target product"],
            "status": status,
            "status_badge_url": status_url,
            "version": row["Version"],
            "fields": [],
            "body": body,
            "download_url": f"{RAW_BASE}/{dist_path}" if dist_path else None,
            "provisioning_pdf_src": None,
            "provisioning_pdf_name": None,
        })

    # Superseded artifacts (a reference skill replaced by a full community
    # connector) are excluded from the site entirely -- no card, no page.
    entries = [e for e in entries if e["status"] != "superseded"]
    entries.sort(key=lambda e: e["name_display"].lower())

    # Fully rebuild generated subtrees each run -- otherwise a removed/renamed
    # connector (e.g. one that becomes superseded) leaves a stale orphaned
    # page behind instead of disappearing from the site.
    for sub in ("connector", "reference", "assets/provisioning", "assets/logos"):
        d = DOCS / sub
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    for entry in entries:
        out = DOCS / entry["kind"] / f"{entry['key']}.html"
        out.write_text(render_detail_page(entry, css_path="../assets/styles.css"), encoding="utf-8")
        if entry["provisioning_pdf_src"]:
            dest = DOCS / "assets" / "provisioning" / entry["provisioning_pdf_name"]
            dest.write_bytes(entry["provisioning_pdf_src"].read_bytes())

    # Copy only the logos actually referenced by a current entry into the
    # generated docs/assets/logos/ -- keeps the served site lean even
    # though the source bank (assets/logos/) may hold more than are used.
    used_logos = {LOGOS[e["key"]] for e in entries if e["key"] in LOGOS}
    for filename in used_logos:
        src = LOGO_SRC_DIR / filename
        if src.exists():
            shutil.copy2(src, DOCS / "assets" / "logos" / filename)

    cards_html = "\n".join(render_card(e) for e in entries)
    index_html = (
        PAGE_HEAD.format(title="Crogl Connector Library", css_path="assets/styles.css")
        + NAV.format(root="")
        + INDEX_TEMPLATE.format(cards=cards_html)
        + PAGE_FOOT
    )
    (DOCS / "index.html").write_text(index_html, encoding="utf-8")

    print(f"Built {len(entries)} detail pages + index.html into {DOCS}")


if __name__ == "__main__":
    main()
