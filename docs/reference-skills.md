# Reference Skills

Reference skills are customer-shareable markdown cookbooks for a configured connector that does not yet have a community connector type. They teach the Crogl agent how to find the connector, compose correct calls, avoid vendor-specific API traps, and render useful analyst output.

Reference skills do not create connector instances. They assume a connector already exists in Crogl.

## Layout

```text
references/<vendor>-reference/
├── SKILL.md
└── references/                 # optional deep docs
```

## Role in this repo

Do not create or import a standalone reference skill for a connector that already has a community connector type. Move connector-specific reference material into `community/<connector-type>/SKILL.md` instead. A standalone reference skill for the same connector conflicts with Crogl's connector instance skill and adds unnecessary corrective turns.

## Install paths

Use the Crogl Skills UI for one-off testing. Upload `dist/references/<skill>.zip`.

For baked-in install configuration, copy the source directory to the install host:

```text
$CROGL_HOME/var/content/skills/<skill-name>/
```

Then restart `crogld`. Crogld walks the external skills directory on boot and upserts skills into Postgres.

## Authoring checklist

- Frontmatter has `name`, `description`, `version`, and `status`.
- Use `paired_connector_type` only when the community connector package does not exist yet.
- `description` should make clear this is for a connector without a community bundle.
- Body has a clear instruction to prefer the community bundle if one exists.
- Recipes include exact field values or explicit `<value>` placeholders.
- Auth section says what Crogl injects and what the agent must never ask for.
- Write actions require explicit user approval.
- Examples are customer-shareable and use placeholders.

## Build

```bash
(cd references && zip -r ../dist/references/<skill-name>.zip <skill-name> -x '*.DS_Store' -x '*.pyc' -x '*__pycache__*')
```
