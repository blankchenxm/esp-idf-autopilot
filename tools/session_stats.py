#!/usr/bin/env python3
"""
session_stats.py — legacy Claude Code per-stage time + token report.

Token counts live only in the Claude Code session log (~/.claude/projects/<proj>/*.jsonl),
so this is not a Codex cost reporter.

Usage:
    python tools/session_stats.py                       # latest session for this repo
    python tools/session_stats.py --session <path.jsonl>
    python tools/session_stats.py --out projects/crumb/DEVLOG-stats.md

Segmentation: the agent prints a line `STAGE: <label>` at the START of each stage/subsystem
(the SKILL Response format emits this). Everything from one marker until the next is that
stage's bucket. Activity before the first marker → "setup".

Columns:
    wall   — first→last timestamp span of the stage (includes idle)
    active — sum of gaps between entries, capped at --idle-cap s (excludes long idle)
    out    — output tokens the model generated  (the main lever: verbosity)
    cache+ — cache_creation tokens (new context pulled in: big file reads, tool results)
    cache~ — cache_read tokens (context replayed each turn; grows with conversation length)
    in     — uncached input tokens
    turns / tools — assistant turns and tool calls in the stage
"""

import sys, os, json, argparse, re, glob
from datetime import datetime

STAGE_RE = re.compile(r'^\s*STAGE:\s*(.+?)\s*$', re.M)


def default_session() -> str | None:
    proj = re.sub(r'[:\\/]', '-', os.getcwd())
    d = os.path.expanduser(f"~/.claude/projects/{proj}")
    files = glob.glob(os.path.join(d, "*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def load_entries(path):
    """Yield (ts, usage_dict|None, is_tool_use, stage_label|None) in file order."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("type") != "assistant":
                continue
            ts = parse_ts(e.get("timestamp", ""))
            msg = e.get("message") or {}
            usage = msg.get("usage")
            stage = None
            tool = False
            for b in (msg.get("content") or []):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    tool = True
                if b.get("type") == "text":
                    m = STAGE_RE.search(b.get("text") or "")
                    if m:
                        stage = m.group(1)
            yield ts, usage, tool, stage


def fmt_dur(sec):
    sec = int(sec)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--idle-cap", type=float, default=300.0,
                    help="max gap (s) counted as active time")
    args = ap.parse_args()

    sess = args.session or default_session()
    if not sess or not os.path.exists(sess):
        print("No session .jsonl found. Pass --session <path>.")
        sys.exit(1)

    entries = [e for e in load_entries(sess) if e[0]]  # need timestamps
    if not entries:
        print("No timestamped assistant entries.")
        sys.exit(1)

    # segment
    buckets = []  # (label, list_of_entries)
    cur_label, cur = "setup", []
    for ts, usage, tool, stage in entries:
        if stage:
            if cur:
                buckets.append((cur_label, cur))
            cur_label, cur = stage, []
        cur.append((ts, usage, tool))
    if cur:
        buckets.append((cur_label, cur))

    rows, tot = [], dict(wall=0, active=0, out=0, cc=0, cr=0, inp=0, turns=0, tools=0)
    for label, es in buckets:
        ts_list = [t for t, _, _ in es]
        wall = (max(ts_list) - min(ts_list)).total_seconds()
        active = 0.0
        for a, b in zip(ts_list, ts_list[1:]):
            d = (b - a).total_seconds()
            if d < args.idle_cap:
                active += d
        out = cc = cr = inp = turns = tools = 0
        for _, u, tool in es:
            turns += 1
            tools += 1 if tool else 0
            if u:
                out += u.get("output_tokens", 0)
                cc += u.get("cache_creation_input_tokens", 0)
                cr += u.get("cache_read_input_tokens", 0)
                inp += u.get("input_tokens", 0)
        rows.append((label, wall, active, out, cc, cr, inp, turns, tools))
        tot["wall"] += wall; tot["active"] += active; tot["out"] += out
        tot["cc"] += cc; tot["cr"] += cr; tot["inp"] += inp
        tot["turns"] += turns; tot["tools"] += tools

    def k(n): return f"{n/1000:.1f}k" if n >= 1000 else str(n)
    lines = [f"# Session stats - {os.path.basename(sess)}", "",
             f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} | idle-cap {int(args.idle_cap)}s", "",
             "| stage | wall | active | out | cache+ | cache~ | in | turns | tools |",
             "|---|---|---|---|---|---|---|---|---|"]
    for label, wall, active, out, cc, cr, inp, turns, tools in rows:
        lines.append(f"| {label} | {fmt_dur(wall)} | {fmt_dur(active)} | {k(out)} "
                     f"| {k(cc)} | {k(cr)} | {k(inp)} | {turns} | {tools} |")
    lines.append(f"| **TOTAL** | {fmt_dur(tot['wall'])} | {fmt_dur(tot['active'])} "
                 f"| **{k(tot['out'])}** | {k(tot['cc'])} | {k(tot['cr'])} | {k(tot['inp'])} "
                 f"| {tot['turns']} | {tot['tools']} |")
    lines += ["", "- **out** = generated tokens (verbosity lever). **cache+** = new context "
              "pulled in (big file reads / tool results - the other big lever). **cache~** = "
              "context replayed each turn (grows with conversation length).",
              "- Stages split on `STAGE:` marker lines; if few markers, buckets are coarse."]
    report = "\n".join(lines)

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"[ok] wrote {args.out}")
    print(report)


if __name__ == "__main__":
    main()
