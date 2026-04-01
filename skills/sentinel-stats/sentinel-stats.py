#!/usr/bin/env python3
"""
sentinel-stats — Analyze Sentinel JSONL telemetry logs.

Usage:
  python sentinel-stats.py [log_file] [--json]

If log_file is omitted, resolves it from .claude/sentinel/config.yaml
(walks up from cwd, same as sentinel.py).
"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def load_entries(path):
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _parse_ts(ts_str):
    """Parse ISO timestamp to epoch seconds (best-effort)."""
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return 0.0


# Max seconds between a block and a retry to the same target for it
# to count as a "contested" block (likely false positive).
CONTESTED_WINDOW_S = 120


def _detect_contested(evals):
    """Detect blocks that were likely overridden by the developer.

    A block is "contested" when the same rule+target pair appears as
    blocked, and then the same target is written to again within
    CONTESTED_WINDOW_S seconds. This means the developer saw the block
    and told the agent to proceed anyway — a likely false positive.

    Returns:
      contested_by_rule: {rule_id: [target, ...]}
      contested_targets: {(rule_id, target): count}
    """
    # Build a timeline of blocks: [(timestamp, rule_id, target), ...]
    blocks = []
    for e in evals:
        if e.get("blocked"):
            ts = _parse_ts(e.get("ts", ""))
            target = e.get("target", "")
            rule_id = e.get("rule_id", "")
            if ts and target:
                blocks.append((ts, rule_id, target))

    # Build a set of all write timestamps per target (any eval = the
    # agent attempted to write to this target)
    target_writes = defaultdict(list)
    for e in evals:
        ts = _parse_ts(e.get("ts", ""))
        target = e.get("target", "")
        if ts and target:
            target_writes[target].append(ts)

    # For each target, sort timestamps for binary search
    for t in target_writes:
        target_writes[t].sort()

    contested_by_rule = defaultdict(list)
    contested_targets = defaultdict(int)

    for block_ts, rule_id, target in blocks:
        writes = target_writes.get(target, [])
        # Look for a write to the same target within the window AFTER the block
        for w_ts in writes:
            if block_ts < w_ts <= block_ts + CONTESTED_WINDOW_S:
                contested_by_rule[rule_id].append(target)
                contested_targets[(rule_id, target)] += 1
                break  # count once per block event

    return contested_by_rule, contested_targets


def compute_pipeline_stats(entries):
    """Aggregate scribe and context pipeline entries by action."""
    pipeline_entries = [e for e in entries
                        if e.get("level") in ("scribe", "context")]
    if not pipeline_entries:
        return {}

    actions = defaultdict(lambda: {"count": 0, "errors": 0, "total_ms": 0,
                                   "max_ms": 0})
    for e in pipeline_entries:
        action = e.get("action", "unknown")
        a = actions[action]
        a["count"] += 1
        ms = e.get("elapsed_ms", 0)
        a["total_ms"] += ms
        a["max_ms"] = max(a["max_ms"], ms)
        if e.get("error"):
            a["errors"] += 1

    result = {}
    for action, a in actions.items():
        result[action] = {
            "count": a["count"],
            "errors": a["errors"],
            "avg_ms": int(a["total_ms"] / a["count"]) if a["count"] else 0,
            "max_ms": a["max_ms"],
            "success_rate": round((a["count"] - a["errors"]) / a["count"], 2)
                           if a["count"] else 0.0,
        }
    return result


def _find_scribe_dir():
    """Resolve .sentinel/scribe/ directory, walking up from cwd."""
    cwd = os.getcwd()
    while True:
        scribe_dir = os.path.join(cwd, ".sentinel", "scribe")
        if os.path.isdir(scribe_dir):
            return scribe_dir
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent
    return None


def compute_observation_stats(obs_path):
    """Compute stats from observations.jsonl."""
    if not os.path.exists(obs_path):
        return None

    observations = []
    with open(obs_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                observations.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not observations:
        return None

    by_source = defaultdict(int)
    by_session = defaultdict(int)
    drafted_count = 0
    for obs in observations:
        by_source[obs.get("source", "unknown")] += 1
        by_session[obs.get("session_id", "unknown")] += 1
        if obs.get("drafted"):
            drafted_count += 1

    return {
        "total": len(observations),
        "by_source": dict(by_source),
        "draft_rate": round(drafted_count / len(observations), 2),
        "by_session": dict(by_session),
    }


def compute_dismissal_stats(dis_path):
    """Compute stats from dismissed.jsonl."""
    if not os.path.exists(dis_path):
        return None

    dismissals = []
    with open(dis_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                dismissals.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    if not dismissals:
        return None

    by_hash = defaultdict(int)
    for d in dismissals:
        by_hash[d.get("statement_hash", "unknown")] += 1

    top = sorted(by_hash.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "total": len(dismissals),
        "top_patterns": [{"key": k, "count": c} for k, c in top],
    }


def attach_scribe_stats(stats, entries, scribe_dir):
    """Populate the scribe section of stats from pipeline entries and scribe files."""
    pipeline = compute_pipeline_stats(entries)

    obs_stats = None
    dis_stats = None
    if scribe_dir:
        obs_path = os.path.join(scribe_dir, "observations.jsonl")
        dis_path = os.path.join(scribe_dir, "dismissed.jsonl")
        obs_stats = compute_observation_stats(obs_path)
        dis_stats = compute_dismissal_stats(dis_path)

    if not pipeline and obs_stats is None and dis_stats is None:
        stats["scribe"] = None
        return

    stats["scribe"] = {
        "pipeline": pipeline,
        "observations": obs_stats,
        "dismissals": dis_stats,
    }


def _compute_percentiles(ms_list):
    """Compute min/max/median/p95/mean from a sorted list of ms values."""
    if not ms_list:
        return {"min_ms": 0, "max_ms": 0, "median_ms": 0,
                "p95_ms": 0, "mean_ms": 0}
    s = sorted(ms_list)
    return {
        "min_ms": s[0],
        "max_ms": s[-1],
        "median_ms": s[len(s) // 2],
        "p95_ms": s[int(len(s) * 0.95)],
        "mean_ms": int(sum(s) / len(s)),
    }


def compute_stats(entries):
    evals = [e for e in entries if e.get("level") == "eval"]
    skipped = [e for e in entries if e.get("level") == "skipped"]

    # Per-rule stats
    rules = defaultdict(lambda: {
        "evals": 0, "violations": 0, "blocks": 0, "warns": 0,
        "skipped": 0, "total_ms": 0, "max_ms": 0,
        "confidences": [], "severity": "",
        "contested": 0,
    })

    # Per-trigger stats
    triggers = defaultdict(lambda: {"evals": 0, "violations": 0, "blocks": 0})

    # Per-tool stats
    tools = defaultdict(lambda: {"evals": 0, "violations": 0, "blocks": 0})

    # Per-model latency
    model_ms = defaultdict(list)

    # Near-miss detection
    near_misses = defaultdict(lambda: {"count": 0, "example_targets": []})
    near_miss_total = 0

    all_ms = []

    for e in evals:
        rid = e.get("rule_id", "unknown")
        r = rules[rid]
        r["evals"] += 1
        r["severity"] = e.get("severity", "")
        ms = e.get("elapsed_ms", 0)
        r["total_ms"] += ms
        r["max_ms"] = max(r["max_ms"], ms)
        conf = e.get("confidence", 0)
        r["confidences"].append(conf)

        if ms:
            all_ms.append(ms)

        # Trigger aggregation
        trig = e.get("trigger")
        if trig:
            triggers[trig]["evals"] += 1
            if e.get("violation"):
                triggers[trig]["violations"] += 1
            if e.get("blocked"):
                triggers[trig]["blocks"] += 1

        # Tool aggregation
        tool = e.get("tool")
        if tool:
            tools[tool]["evals"] += 1
            if e.get("violation"):
                tools[tool]["violations"] += 1
            if e.get("blocked"):
                tools[tool]["blocks"] += 1

        # Model latency
        model = e.get("model")
        if model and ms:
            model_ms[model].append(ms)

        if e.get("violation"):
            r["violations"] += 1
            if e.get("blocked"):
                r["blocks"] += 1
            else:
                r["warns"] += 1
        else:
            # Near-miss: non-violation where confidence is close to threshold
            threshold = e.get("threshold", 0)
            if threshold and conf and threshold - conf <= 0.1 and conf < threshold:
                near_miss_total += 1
                nm = near_misses[rid]
                nm["count"] += 1
                target = e.get("target", "unknown")
                if len(nm["example_targets"]) < 3:
                    nm["example_targets"].append(target)

    for e in skipped:
        rid = e.get("rule_id", "unknown")
        rules[rid]["skipped"] += 1

    # Detect contested blocks (likely false positives)
    contested_by_rule, contested_targets_raw = _detect_contested(evals)
    for rid, tgts in contested_by_rule.items():
        rules[rid]["contested"] += len(tgts)

    # Convert contested_targets to string keys
    contested_targets = {}
    for (rid, target), count in contested_targets_raw.items():
        contested_targets[f"{rid}:{target}"] = count

    # Per-target stats (files/commands that triggered the most evaluations)
    targets = defaultdict(lambda: {"evals": 0, "blocks": 0, "violations": 0})
    for e in evals:
        t = e.get("target", "unknown")
        targets[t]["evals"] += 1
        if e.get("violation"):
            targets[t]["violations"] += 1
        if e.get("blocked"):
            targets[t]["blocks"] += 1

    # Timeouts (skipped entries that mention timeout)
    timeouts = [e for e in skipped if "timeout" in e.get("reason", "").lower()]

    # Per-model performance
    models = {}
    for model, ms_list in model_ms.items():
        p = _compute_percentiles(ms_list)
        models[model] = {
            "evals": len(ms_list),
            "min_ms": p["min_ms"],
            "median_ms": p["median_ms"],
            "p95_ms": p["p95_ms"],
            "max_ms": p["max_ms"],
            "mean_ms": p["mean_ms"],
        }

    return {
        "evaluation": {
            "total_evals": len(evals),
            "total_violations": sum(1 for e in evals if e.get("violation")),
            "total_blocks": sum(1 for e in evals if e.get("blocked")),
            "total_skipped": len(skipped),
            "total_timeouts": len(timeouts),
            "rules": rules,
            "triggers": dict(triggers),
            "tools": dict(tools),
            "targets": dict(targets),
        },
        "performance": {
            "latency": _compute_percentiles(all_ms),
            "models": models,
        },
        "scribe": None,
        "health": {
            "issues": [],
            "near_misses": {
                "total": near_miss_total,
                "by_rule": dict(near_misses),
            },
            "contested_targets": contested_targets,
        },
    }


def fmt_bar(count, total, width=20):
    if total == 0:
        return " " * width
    filled = int(count / total * width)
    return "█" * filled + "░" * (width - filled)


def print_report(stats):
    s = stats
    print("═══════════════════════════════════════════════════════")
    print("  SENTINEL STATS")
    print("═══════════════════════════════════════════════════════")
    print()

    # Overview
    print(f"  Evaluations:  {s['total_evals']}")
    print(f"  Violations:   {s['total_violations']}")
    print(f"  Blocked:      {s['total_blocks']}")
    print(f"  Skipped:      {s['total_skipped']}")
    print(f"  Timeouts:     {s['total_timeouts']}")
    print()

    # Latency
    lat = s["latency"]
    if lat["max_ms"]:
        print("  Latency (ms)")
        print(f"    min: {lat['min_ms']}  median: {lat['median_ms']}  "
              f"p95: {lat['p95_ms']}  max: {lat['max_ms']}  mean: {lat['mean_ms']}")
        print()

    # Per-rule breakdown
    print("───────────────────────────────────────────────────────")
    print("  RULES")
    print("───────────────────────────────────────────────────────")
    for rid, r in sorted(stats["rules"].items(),
                         key=lambda x: x[1]["violations"], reverse=True):
        viol_rate = r["violations"] / r["evals"] * 100 if r["evals"] else 0
        avg_ms = int(r["total_ms"] / r["evals"]) if r["evals"] else 0
        avg_conf = (sum(r["confidences"]) / len(r["confidences"])
                    if r["confidences"] else 0)
        print()
        print(f"  {rid}  [{r['severity']}]")
        contested = r.get("contested", 0)
        contested_str = f"  contested: {contested}" if contested else ""
        print(f"    evals: {r['evals']}  violations: {r['violations']} "
              f"({viol_rate:.0f}%)  blocked: {r['blocks']}  "
              f"warned: {r['warns']}  skipped: {r['skipped']}"
              f"{contested_str}")
        print(f"    latency: avg {avg_ms}ms  max {r['max_ms']}ms  "
              f"confidence: avg {avg_conf:.2f}")
        print(f"    {fmt_bar(r['violations'], r['evals'])} "
              f"{r['violations']}/{r['evals']} violations")

    # Hottest targets
    print()
    print("───────────────────────────────────────────────────────")
    print("  HOTTEST TARGETS")
    print("───────────────────────────────────────────────────────")
    sorted_targets = sorted(stats["targets"].items(),
                            key=lambda x: x[1]["evals"], reverse=True)[:10]
    for target, t in sorted_targets:
        print(f"  {t['evals']:>4} evals  {t['violations']:>3} violations  "
              f"{t['blocks']:>3} blocked  {target}")

    # Rule health — flag noisy rules and suggest fixes
    print()
    print("───────────────────────────────────────────────────────")
    print("  RULE HEALTH")
    print("───────────────────────────────────────────────────────")

    issues = []
    for rid, r in sorted(stats["rules"].items()):
        if r["blocks"] == 0:
            continue
        override_rate = r["contested"] / r["blocks"] if r["blocks"] else 0
        if override_rate >= 0.3:
            issues.append(("noisy", rid, r, override_rate))
        elif r["skipped"] > r["evals"] * 0.3 and r["evals"] > 0:
            issues.append(("flaky", rid, r, 0))
        elif r["max_ms"] > 3000:
            issues.append(("slow", rid, r, 0))

    if not issues:
        print()
        print("  All rules healthy. No action needed.")
    else:
        for kind, rid, r, rate in issues:
            print()
            if kind == "noisy":
                print(f"  !! {rid}  — likely false positives")
                print(f"     {r['contested']}/{r['blocks']} blocks contested "
                      f"(override rate: {rate:.0%})")
                # Find the most-contested targets for this rule
                rule_targets = {
                    t: c for (rule_id, t), c
                    in stats.get("contested_targets", {}).items()
                    if rule_id == rid
                }
                if rule_targets:
                    top = sorted(rule_targets.items(),
                                 key=lambda x: x[1], reverse=True)[:3]
                    print("     Repeatedly overridden on:")
                    for t, c in top:
                        print(f"       {c}x  {t}")
                    print("     Suggested fix: add these to the rule's "
                          "exclude list, or switch to severity: warn")
            elif kind == "flaky":
                print(f"  ?? {rid}  — unreliable "
                      f"({r['skipped']} skipped / {r['evals']} evals)")
                print("     Suggested fix: check Ollama stability or "
                      "increase timeout_ms for this rule")
            elif kind == "slow":
                print(f"  ~~ {rid}  — slow (max {r['max_ms']}ms)")
                print("     Suggested fix: use a smaller model or "
                      "narrow the scope to reduce evaluations")
    print()


def _find_log_file():
    """Resolve log_file from .claude/sentinel/config.yaml, walking up from cwd."""
    cwd = os.getcwd()
    while True:
        config_dir = os.path.join(cwd, ".claude", "sentinel")
        if os.path.isdir(config_dir):
            for ext in ("yaml", "yml", "json"):
                config_path = os.path.join(config_dir, f"config.{ext}")
                if os.path.exists(config_path):
                    with open(config_path) as f:
                        if ext in ("yaml", "yml"):
                            if yaml is None:
                                print("PyYAML required to read config. "
                                      "Install with: pip install pyyaml",
                                      file=sys.stderr)
                                sys.exit(1)
                            cfg = yaml.safe_load(f) or {}
                        else:
                            cfg = json.load(f)
                    log_file = cfg.get("log_file")
                    if log_file:
                        if not os.path.isabs(log_file):
                            # Relative to repo root (parent of .claude/)
                            repo_root = os.path.dirname(os.path.dirname(config_dir))
                            log_file = os.path.join(repo_root, log_file)
                        return log_file
            return None
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv

    if args:
        path = args[0]
    else:
        path = _find_log_file()
        if not path:
            print("No log file specified and no log_file configured in "
                  ".claude/sentinel/config.yaml.\n"
                  "Either pass a path or add log_file to your config.",
                  file=sys.stderr)
            sys.exit(1)

    if not Path(path).exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)

    entries = load_entries(path)
    if not entries:
        print("No log entries found.", file=sys.stderr)
        sys.exit(1)

    stats = compute_stats(entries)

    if as_json:
        # Make it JSON-serializable (drop raw lists, convert tuple keys)
        for r in stats["rules"].values():
            del r["confidences"]
        stats["contested_targets"] = {
            f"{rule_id}:{target}": count
            for (rule_id, target), count in stats.get("contested_targets", {}).items()
        }
        print(json.dumps(stats, indent=2))
    else:
        print_report(stats)


if __name__ == "__main__":
    main()
