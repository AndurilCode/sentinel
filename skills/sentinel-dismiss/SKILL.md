---
name: sentinel-dismiss
description: Dismiss a draft Sentinel rule and prevent re-proposal
user-invocable: true
---

# Sentinel Dismiss

Dismiss a draft rule and add it to the blocklist so Scribe won't re-propose it.

## Arguments

The user provides the draft rule ID as an argument: `/sentinel-dismiss <id>`

## Steps

1. Look for `.claude/sentinel/drafts/<id>.draft.yaml`
   - If not found, list available drafts and ask the user to pick one

2. Read the draft YAML and show a brief summary:
   - id, trigger, scope, and the evidence that generated it

3. Confirm: "Dismiss this draft? Scribe won't propose rules with the same scope and trigger again."
   - If user confirms, proceed
   - If user cancels, stop

4. Add to blocklist by running:

```python
from sentinel_scribe import add_dismissal
# Extract scope (first pattern) and trigger from the draft
add_dismissal(scribe_dir, scope, trigger, statement)
```

Or equivalently, append to `.sentinel/scribe/dismissed.jsonl`:
```json
{"scope": "<first scope pattern>", "trigger": "<trigger>", "statement_hash": "<hash>", "dismissed_at": "<iso timestamp>"}
```

5. Delete `drafts/<id>.draft.yaml`

6. Confirm: "Draft `<id>` dismissed. Scribe won't re-propose rules for this scope and trigger."
