# Superseded reference-skill distributables

A reference skill lands here when a community connector bundle replaces it. The
zip is kept rather than deleted so an install that already imported the old
reference has a retrievable copy of exactly what it got, and so the migration is
auditable.

**Do not import anything in this directory.** Per
[CONTRIBUTING.md](../../../CONTRIBUTING.md), a reference skill and a community
bundle for the same connector must not both be imported: the reference's call
shapes are stale by definition once the bundle exists, and two skills claiming
the same connector is exactly the ambiguity the tier split is meant to remove.

| Zip | Superseded by | Retired | Source |
|---|---|---|---|
| `tines-reference.zip` | `tines` community bundle ([community/tines/](../../../community/tines/)) | 2026-08-12 | removed 2026-08-12; this zip is the only copy |

## When the source is removed

A superseded reference keeps its `references/<name>-reference/` source directory
only while it still carries material the bundle doesn't. Once that material has
been harvested into the bundle -- or established as out of scope -- the source is
deleted and this zip becomes the sole copy, which is the end state
CONTRIBUTING describes: migrate the content, stop shipping the reference.

Two things to expect when reading an archived zip:

1. **It carries the factual errors its own banner listed.** These cookbooks were
   authored against live tenants before the vendor docs were checked, so a
   retired reference typically has several wrong claims that the bundle fixed.
   The bundle is authoritative wherever they conflict.
2. **It may document surfaces the bundle deliberately omits.** `tines-reference`,
   for example, covers story execution (Send-to-Story and webhook triggers),
   which the read-only `tines` bundle does not implement. That material is here
   for whoever picks up execution, not because the bundle is incomplete.
