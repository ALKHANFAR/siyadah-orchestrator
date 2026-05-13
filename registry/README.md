# Activepieces Tool Registry (Siyadah)

Generated, file-based mirror of the Activepieces piece schemas used by the
Siyadah Orchestrator. The registry is the **single in-repo source of truth**
for piece/action/trigger metadata: schema hashes, required fields, dropdown
fields, dynamic fields, and auth requirements.

> **Source of truth is Activepieces only.** Every file in this directory is
> produced by `scripts/sync_activepieces_registry.py` fetching a piece
> schema from the live Activepieces API and serialising it deterministically.
> Do **not** hand-edit; rerun the sync script instead.

## Layout

```
registry/
├── pieces/
│   ├── webhook.json
│   ├── google-sheets.json
│   ├── gmail.json
│   ├── slack.json
│   ├── hubspot.json
│   ├── clickup.json
│   ├── airtable.json
│   ├── google-calendar.json
│   ├── google-drive.json
│   └── stripe.json
└── indexes/
    ├── actions_index.json          piece → [action names]
    ├── triggers_index.json         piece → [trigger names]
    ├── required_fields_index.json  piece → action → [required field names]
    ├── dropdown_fields_index.json  piece → action → [DROPDOWN/MULTI_SELECT_DROPDOWN field names]
    ├── dynamic_fields_index.json   piece → action → [DYNAMIC field names]
    ├── auth_index.json             piece → { type, required }
    └── schema_hashes.json          piece → { hash, version, synced_at, source_url }
```

## Per-piece file shape

```jsonc
{
  "pieceName":   "@activepieces/piece-google-sheets",
  "displayName": "Google Sheets",
  "version":     "0.14.0",
  "auth": {
    "type":     "OAUTH2",      // null when no auth required
    "required": true
  },
  "actions": {
    "insert_row": {
      "displayName":     "Insert Row",
      "description":     "...",
      "required_fields": ["spreadsheetId", "sheetId"],
      "dropdown_fields": ["spreadsheetId", "sheetId"],
      "dynamic_fields":  ["values"],
      "property_types": {
        "spreadsheetId":      "DROPDOWN",
        "sheetId":            "DROPDOWN",
        "first_row_headers":  "STATIC_DROPDOWN",
        "values":             "DYNAMIC"
      },
      "props": { /* verbatim AP props payload */ }
    }
  },
  "triggers": { /* same shape as actions */ },
  "schema_hash":             "sha256:…",
  "synced_at":               "2026-05-13T07:09:00Z",
  "source_url":              "<AP_BASE_URL>/api/v1/pieces/@activepieces/piece-google-sheets",
  "registry_format_version": 1
}
```

`schema_hash` is `sha256` over a canonical (sorted, no whitespace) JSON dump
of the AP-returned schema. It is the integrity anchor for `--validate-only`.

## Sync script

```
python -m scripts.sync_activepieces_registry --help

# Dry run — fetch live schemas but write nothing
python -m scripts.sync_activepieces_registry --dry-run

# Sync the default 10 target pieces
python -m scripts.sync_activepieces_registry

# Sync a subset
python -m scripts.sync_activepieces_registry --pieces gmail,slack

# Check existing registry without touching the network
python -m scripts.sync_activepieces_registry --validate-only
```

### Required environment

| Variable           | Purpose                                                       |
|--------------------|---------------------------------------------------------------|
| `AP_BASE_URL`      | Base URL of the Activepieces instance (no trailing `/api/v1`) |
| `AP_TOKEN` *or*    | Pre-issued bearer token (skip sign-in)                        |
| `AP_EMAIL` + `AP_PASSWORD` | Operator credentials for `/api/v1/authentication/sign-in` |

If the target instance exposes `/api/v1/pieces/*` unauthenticated (e.g.
`https://cloud.activepieces.com`), no credentials are required.

### Invariants enforced by the script

- **No secrets in files or logs.** Bearer tokens are never echoed, never
  written to any registry file, and not part of the hashed payload.
- **No hand-invented fields.** Required/dropdown/dynamic field sets are
  derived only from the AP `type` / `required` metadata on each prop.
- **Deterministic output.** JSON is written with `sort_keys=True` so
  re-syncing an unchanged schema produces a byte-identical file (modulo
  `synced_at`).
- **Atomic writes.** Each file is written to a `*.tmp` then renamed.
- **Fail closed.** Network or schema errors return a non-zero exit code;
  no partial file is left behind.

## Target pieces (10)

| # | Piece name                                  | Short slug         |
|---|---------------------------------------------|--------------------|
| 1 | `@activepieces/piece-webhook`               | `webhook`          |
| 2 | `@activepieces/piece-google-sheets`         | `google-sheets`    |
| 3 | `@activepieces/piece-gmail`                 | `gmail`            |
| 4 | `@activepieces/piece-slack`                 | `slack`            |
| 5 | `@activepieces/piece-hubspot`               | `hubspot`          |
| 6 | `@activepieces/piece-clickup`               | `clickup`          |
| 7 | `@activepieces/piece-airtable`              | `airtable`         |
| 8 | `@activepieces/piece-google-calendar`       | `google-calendar`  |
| 9 | `@activepieces/piece-google-drive`          | `google-drive`     |
| 10| `@activepieces/piece-stripe`                | `stripe`           |
