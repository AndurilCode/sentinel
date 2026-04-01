---
name: sentinel-promote
description: Promote a draft Sentinel rule to active
user-invocable: true
---

# Sentinel Promote

Promote a draft rule to an active Sentinel rule.

## Arguments

The user provides the draft rule ID as an argument: `/sentinel-promote <id>`

## Steps

1. Look for `.claude/sentinel/drafts/<id>.draft.yaml`
   - If not found, list available drafts and ask the user to pick one

2. Read the draft YAML and display it to the user:
   - Show the full rule (id, trigger, severity, scope, exclude, prompt)
   - Show the provenance from `_draft` (source, evidence, confidence)

3. Ask: "Want to edit this rule before activating, or promote as-is?"
   - If edit: let the user modify fields, then proceed
   - If as-is: proceed directly

4. Move the file:
   - Copy `drafts/<id>.draft.yaml` to `rules/<id>.yaml`
   - The `_draft` block is inert (Sentinel ignores unknown fields), but optionally strip it for cleanliness
   - Delete the draft file from `drafts/`

5. Confirm: "Rule `<id>` is now active. It will be evaluated on the next matching tool use."

## Next steps

After promoting the rule, suggest these to the user:

- `/sentinel-stats` — monitor the new rule's performance once it starts firing
- `/sentinel-drafts` — review remaining pending drafts
