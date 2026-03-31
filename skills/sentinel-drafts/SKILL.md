---
name: sentinel-drafts
description: List pending draft Sentinel rules with provenance
user-invocable: true
---

# Sentinel Drafts

List all pending draft rules proposed by Sentinel Scribe.

## Steps

1. Read all `.draft.yaml` files from `.claude/sentinel/drafts/`

2. For each draft, extract from the YAML:
   - `id` — rule identifier
   - `trigger` — file_write, bash, mcp, or any
   - `severity` — block or warn
   - `scope` — glob patterns (show first 2, then "and N more" if longer)
   - `_draft.source` — user_prompt or documentation
   - `_draft.synthesized` — when it was created (show as relative time: "2h ago", "3d ago")
   - `_draft.evidence` — the human words that triggered this (show first item, truncated to 60 chars)

3. Sort by `_draft.synthesized` (newest first)

4. Display as a formatted list:

```
N pending draft rules:

  id (severity, trigger) — age
    Scope: glob1, glob2
    Evidence: "human words..."

  id2 (severity, trigger) — age
    Scope: glob1
    Evidence: "human words..."
```

5. If no drafts exist, say: "No pending draft rules. Scribe will propose rules as it observes conventions in your prompts, or run /sentinel-learn to scan documentation."

6. After listing, remind: "Use /sentinel-promote <id> to activate a draft, or /sentinel-dismiss <id> to discard it."
