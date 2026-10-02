#!/usr/bin/env python3
"""render-settings — an agent's ~/.claude/settings.json: the portal's hooks (from the rendered
build/hook.<agent>.json) plus the two environment defaults a memory-backed pane needs. Merges into an
existing file (other keys kept; Stop and UserPromptSubmit replaced), so rerunning is safe.

  tools/render-settings.py --agent alpha --hooks build/hook.alpha.json --into "$H/.claude/settings.json"   # H = that agent's home

The memory endpoint is NOT in this file: Claude Code keeps MCP servers in its own store, so the
installer registers it with `claude mcp add --transport http persMEM <url>` as the agent (INSTALL §5).
"""
import argparse, json, os, sys

ENV_DEFAULTS = {"MAX_MCP_OUTPUT_TOKENS": "40000", "CLAUDE_CODE_MCP_STARTUP_WAIT_MS": "15000"}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--hooks", required=True, help="build/hook.<agent>.json from render-agents.sh, or - for standard input")
    ap.add_argument("--into", required=True, help="the settings.json to write (merged if it exists)")
    a = ap.parse_args()
    # "-" READS the descriptor it was handed. Opening /dev/stdin instead would reopen it through
    # /proc, and that is checked against the pipe's own mode: a pipe root made is 0600 root, so an
    # agent this runs as gets EACCES (reviewer's catch, 2026-10-02, exercised).
    hooks = json.load(sys.stdin) if a.hooks == "-" else json.load(open(a.hooks))
    for k in ("Stop", "UserPromptSubmit"):
        if k not in hooks:
            sys.exit(f"{a.hooks}: no {k} hook (render-agents.sh writes both)")
    cur = {}
    if os.path.exists(a.into):
        try:
            cur = json.load(open(a.into))
        except ValueError:
            sys.exit(f"{a.into} exists and does not parse; not overwriting")
    cur.setdefault("hooks", {}).update(hooks)
    env = cur.setdefault("env", {})
    for k, v in ENV_DEFAULTS.items():
        env.setdefault(k, v)
    tmp = a.into + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cur, f, indent=2); f.write("\n")
    os.replace(tmp, a.into)
    print(f"{a.agent}: {a.into} ({'merged' if cur else 'new'}; hooks Stop + UserPromptSubmit; env {', '.join(ENV_DEFAULTS)})")

if __name__ == "__main__":
    main()
