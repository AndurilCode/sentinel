---
name: sentinel-learn
description: Scan documentation files for conventions and generate draft Sentinel rules
user-invocable: true
---

# Sentinel Learn

Scan repository documentation files (CLAUDE.md, ADRs, READMEs) for conventions that should be enforced as Sentinel rules.

## Prerequisites

Check that `.claude/sentinel/config.yaml` exists. If not, tell the user to run `/sentinel-init` first and stop.

## Steps

1. Run the scribe learn pipeline:

```bash
python3 sentinel_scribe.py --learn
```

2. Parse the JSON output. It contains: `files_scanned`, `conventions_found`, `drafts_created`.

3. Report to the user:
   - "Scanned N files, extracted M conventions, drafted K rules"
   - If K > 0: "Run /sentinel-drafts to review the proposed rules"
   - If K == 0 and M > 0: "Conventions found but all are already covered by active rules or were previously dismissed"
   - If M == 0: "No conventions found in documentation files"

## Configuration

The scribe scans files matching `scribe.doc_globs` in config.yaml. Default globs:
- `CLAUDE.md`, `AGENTS.md`, `README.md`, `docs/**/*.md`, `ADR*.md`

Users can customize via:
```yaml
scribe:
  doc_globs:
    - "CLAUDE.md"
    - "docs/architecture/**/*.md"
```

## Next steps

After scanning, suggest the most relevant action:

- `/sentinel-drafts` — review the proposed draft rules (if any were created)
- `/sentinel-config` — customize `scribe.doc_globs` to scan additional documentation sources
- `/sentinel-rule` — create a rule manually for conventions that aren't captured in documentation
