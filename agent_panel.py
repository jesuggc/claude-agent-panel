#!/usr/bin/env python3
"""Claude Agent Panel: live web dashboard for Claude Code subagents.

One file, standard library only. It plays three roles:

    python3 agent_panel.py install [--project DIR]   register the hooks (global by default)
    python3 agent_panel.py uninstall [--project DIR] remove them again
    python3 agent_panel.py [serve] [--port 8765]     open the panel at http://127.0.0.1:8765
    python3 agent_panel.py hook                      (called by Claude Code, reads the event on stdin)

The hook appends one JSON line per event to ~/.claude/agent-panel/logs/<project>.jsonl.
The panel rebuilds from those lines which agents are working, what they are doing,
and whether Claude is waiting for you. The team is read from .claude/agents/*.md.
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
import sys
import webbrowser
import zlib
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("AGENT_PANEL_HOME", Path.home() / ".claude" / "agent-panel"))
LOGS = HOME / "logs"
MAX_BYTES = 800_000          # trim a log when it grows past this...
KEEP_LINES = 3000            # ...down to its last N lines
IDLE_MINUTES = 20            # no events for this long -> the agent is considered stopped
HOOK_EVENTS = ["UserPromptSubmit", "PreToolUse", "SubagentStart", "SubagentStop",
               "Stop", "Notification", "SessionEnd"]
HOOK_MARK = "agent_panel.py"

# Colour names accepted by the `color:` field of Claude Code agents
COLOR_NAMES = {"red": "#F2685A", "orange": "#F29A4E", "yellow": "#F2C14E", "green": "#3ECF8E",
               "cyan": "#2EC4C4", "blue": "#4E9BF2", "purple": "#A06CF0", "pink": "#F06CA8",
               "gray": "#9AA3B5", "grey": "#9AA3B5"}
PALETTE = list(COLOR_NAMES.values())[:8] + ["#D97757", "#7FD1AE"]
ICONS = ["🤖", "🧠", "🔧", "📐", "🧪", "📝", "🔍", "🚀", "🎨", "📊", "🛰️", "🧩"]


def project_slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", path).strip("-") or "root"


# --------------------------------------------------------------------------- hook

def tool_summary(name: str, data: dict, root: str) -> str:
    if name == "Bash":
        return data.get("description") or data.get("command", "")[:120]
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return (data.get("file_path") or data.get("notebook_path") or "").replace(root + "/", "")
    if name == "WebSearch":
        return data.get("query", "")
    if name == "WebFetch":
        return data.get("url", "")
    if name in ("Glob", "Grep"):
        return data.get("pattern", "")
    if name in ("Agent", "Task"):
        return f"{data.get('subagent_type', '')}: {data.get('description', '')}"
    if name == "Skill":
        return data.get("skill", "")
    return ""


def run_hook() -> None:
    data = json.load(sys.stdin)
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    event = data.get("hook_event_name", "")
    line = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, "project": root,
            "session": (data.get("session_id") or "")[:12],
            "agent_id": data.get("agent_id"), "agent": data.get("agent_type")}
    if event == "PreToolUse":
        tool = data.get("tool_name", "")
        line["tool"] = tool
        line["detail"] = tool_summary(tool, data.get("tool_input") or {}, root)[:160]
    elif event == "Notification":
        line["detail"] = (data.get("message") or "")[:160]
        line["kind"] = data.get("notification_type")

    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / f"{project_slug(root)}.jsonl"
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    if log.stat().st_size > MAX_BYTES:
        lines = log.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- install

def settings_path(project: str | None) -> Path:
    if project:
        return Path(project).resolve() / ".claude" / "settings.json"
    return Path.home() / ".claude" / "settings.json"


def strip_our_hooks(hooks: dict) -> dict:
    """Return the hooks config without any entry that calls this script."""
    clean = {}
    for event, groups in hooks.items():
        kept = []
        for group in groups:
            inner = [h for h in group.get("hooks", []) if HOOK_MARK not in h.get("command", "")]
            if inner:
                kept.append({**group, "hooks": inner})
        if kept:
            clean[event] = kept
    return clean


def install(project: str | None, remove: bool = False) -> None:
    path = settings_path(project)
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    hooks = strip_our_hooks(settings.get("hooks", {}))
    if not remove:
        command = f'python3 "{Path(__file__).resolve()}" hook'
        for event in HOOK_EVENTS:
            group = {"hooks": [{"type": "command", "command": command, "timeout": 5, "async": True}]}
            if event == "PreToolUse":
                group = {"matcher": "*", **group}
            hooks.setdefault(event, []).append(group)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    action = "removed from" if remove else "installed in"
    print(f"Agent Panel hooks {action} {path}")
    if not remove:
        print("Restart your Claude Code sessions (or run /hooks) so they pick up the change,")
        print(f"then open the panel with:  python3 {Path(__file__).resolve()}")


# --------------------------------------------------------------------------- team

def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta, key = {}, None
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if m:
            key, value = m.group(1), m.group(2).strip()
            meta[key] = "" if value in (">", "|", ">-", "|-") else value.strip("\"'")
        elif key and line[:1] in (" ", "\t"):
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta


def short_role(description: str) -> str:
    first = re.split(r"(?<=[.!?])\s", description.strip(), maxsplit=1)[0]
    return first if len(first) <= 110 else first[:107] + "…"


def pick(seq: list, name: str):
    return seq[zlib.crc32(name.encode()) % len(seq)]


def load_team(project: Path) -> list[dict]:
    """Agents defined for the project (then user-level ones), plus optional overrides from
    .claude/agent-panel.json: {"agents": {"name": {"icon", "color", "role"}}, "title": "..."}"""
    team: dict[str, dict] = {}
    for scope, folder in (("project", project / ".claude" / "agents"),
                          ("user", Path.home() / ".claude" / "agents")):
        if not folder.is_dir():
            continue
        for md in sorted(folder.rglob("*.md")):
            meta = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            name = meta.get("name") or md.stem
            if name in team:
                continue
            color = meta.get("color", "").lower()
            team[name] = {"name": name, "scope": scope, "role": short_role(meta.get("description", "")),
                          "color": COLOR_NAMES.get(color) or (color if color.startswith("#") else None),
                          "icon": meta.get("icon")}
    overrides = load_config(project).get("agents", {})
    for name, extra in overrides.items():
        team.setdefault(name, {"name": name, "scope": "config", "role": "", "color": None, "icon": None})
        team[name].update({k: v for k, v in extra.items() if k in ("icon", "color", "role")})
    # Agents listed in the config keep that order and go first
    ordered = [team[n] for n in overrides] + [a for n, a in team.items() if n not in overrides]
    # Fill in missing colours and icons in order, so that teammates do not look alike
    for i, agent in enumerate(ordered):
        agent["color"] = agent["color"] or PALETTE[i % len(PALETTE)]
        agent["icon"] = agent["icon"] or ICONS[i % len(ICONS)]
    return ordered


def load_config(project: Path) -> dict:
    try:
        return json.loads((project / ".claude" / "agent-panel.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- state

def last_line(path: Path) -> dict:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        f.seek(max(0, pos - 4096))
        tail = f.read().decode("utf-8", errors="replace").strip().splitlines()
    for raw in reversed(tail):
        try:
            return json.loads(raw)
        except ValueError:
            continue
    return {}


def list_projects() -> list[dict]:
    projects = []
    for log in LOGS.glob("*.jsonl"):
        last = last_line(log)
        if last.get("project"):
            home = str(Path.home())
            shown = "~" + last["project"][len(home):] if last["project"].startswith(home + "/") else last["project"]
            projects.append({"id": log.stem, "path": last["project"], "shown": shown,
                             "name": Path(last["project"]).name, "last": last.get("ts")})
    return sorted(projects, key=lambda p: p["last"] or "", reverse=True)


def read_events(log: Path) -> list[dict]:
    events = []
    if log.exists():
        for raw in log.read_text(encoding="utf-8").splitlines()[-KEEP_LINES:]:
            try:
                events.append(json.loads(raw))
            except ValueError:
                continue
    return events


def build_runs(events: list[dict]) -> dict[str, dict]:
    """One run per subagent invocation (agent_id)."""
    runs: dict[str, dict] = {}
    for ev in events:
        aid = ev.get("agent_id")
        if not aid:
            continue
        run = runs.setdefault(aid, {"id": aid, "agent": ev.get("agent"), "session": ev.get("session"),
                                    "start": ev["ts"], "last": ev["ts"], "tools": 0, "active": True,
                                    "action": None, "end": None})
        run["agent"] = run["agent"] or ev.get("agent")
        run["last"] = ev["ts"]
        if ev["event"] == "PreToolUse":
            run["tools"] += 1
            run["action"] = f"{ev.get('tool', '')}: {ev.get('detail', '')}"
        elif ev["event"] == "SubagentStop":
            run["active"], run["end"] = False, ev["ts"]
    now = datetime.now()
    for run in runs.values():
        if run["active"] and (now - datetime.fromisoformat(run["last"])).total_seconds() > IDLE_MINUTES * 60:
            run["active"], run["end"] = False, run["last"]
    return runs


def team_state(team: list[dict], runs: dict[str, dict]) -> list[dict]:
    known = {a["name"] for a in team}
    # Built-in or plugin agents (Explore, general-purpose...) show up once they have been used
    for run in sorted(runs.values(), key=lambda r: r["start"]):
        if run["agent"] and run["agent"] not in known:
            known.add(run["agent"])
            team.append({"name": run["agent"], "scope": "builtin", "role": "",
                         "color": pick(PALETTE, run["agent"]), "icon": pick(ICONS, run["agent"])})
    today = datetime.now().date().isoformat()
    result = []
    for agent in team:
        mine = sorted((r for r in runs.values() if r["agent"] == agent["name"]), key=lambda r: r["start"])
        active = [r for r in mine if r["active"]]
        current = active[-1] if active else None
        latest = mine[-1] if mine else None
        result.append({**agent, "working": bool(current), "parallel": len(active),
                       "since": current["start"] if current else None,
                       "action": (current or latest or {}).get("action"),
                       "tools": current["tools"] if current else 0,
                       "last_seen": latest["last"] if latest else None,
                       "today": sum(r["start"].startswith(today) for r in mine)})
    return result


def waiting_for_user(events: list[dict], runs: dict[str, dict]) -> dict:
    """Per session: the last relevant event decides. A Stop of the main loop (with no subagent
    still working in that session) or a Notification means it is your turn."""
    busy = {r["session"] for r in runs.values() if r["active"]}
    sessions: dict[str, dict] = {}
    for ev in events:
        sid, kind = ev.get("session") or "?", ev.get("event")
        if kind == "Notification":
            sessions[sid] = {"yes": True, "since": ev["ts"], "reason": ev.get("kind") or "notification",
                             "detail": ev.get("detail")}
        elif kind == "Stop" and not ev.get("agent_id"):
            sessions[sid] = {"yes": True, "since": ev["ts"], "reason": "stop"}
        elif kind in ("UserPromptSubmit", "PreToolUse", "SubagentStart", "SubagentStop", "SessionEnd"):
            sessions[sid] = {"yes": False}
    waiting = [dict(s, session=sid) for sid, s in sessions.items()
               if s["yes"] and (s["reason"] != "stop" or sid not in busy)]
    if not waiting:
        return {"yes": False}
    newest = max(waiting, key=lambda s: s["since"])
    return {**newest, "count": len(waiting)}


def api_state(project_id: str | None) -> dict:
    projects = list_projects()
    current = next((p for p in projects if p["id"] == project_id), projects[0] if projects else None)
    if not current:
        return {"now": datetime.now().isoformat(timespec="seconds"), "projects": [], "project": None}
    root = Path(current["path"])
    events = read_events(LOGS / f"{current['id']}.jsonl")
    runs = build_runs(events)
    names = {r["id"]: r["agent"] for r in runs.values()}
    recent = [{**ev, "agent": ev.get("agent") or names.get(ev.get("agent_id"))} for ev in reversed(events[-60:])]
    history = sorted(runs.values(), key=lambda r: r["start"], reverse=True)[:12]
    return {
        "now": datetime.now().isoformat(timespec="seconds"),
        "projects": projects,
        "project": current,
        "title": load_config(root).get("title"),
        "waiting": waiting_for_user(events, runs),
        "team": team_state(load_team(root), runs),
        "runs": history,
        "events": recent[:50],
    }


# --------------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send(200, "text/html; charset=utf-8", (HERE / "panel.html").read_bytes())
        elif url.path == "/api/state":
            pid = parse_qs(url.query).get("project", [None])[0]
            self.send(200, "application/json", json.dumps(api_state(pid), ensure_ascii=False).encode())
        elif url.path == "/favicon.svg":
            self.send(200, mimetypes.types_map[".svg"], FAVICON.encode())
        else:
            self.send(404, "text/plain", b"not found")


FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 12" shape-rendering="crispEdges">'
           '<rect x="2" y="0" width="12" height="7" fill="#D97757"/><rect x="0" y="3" width="2" height="2" fill="#D97757"/>'
           '<rect x="14" y="3" width="2" height="2" fill="#D97757"/><rect x="3" y="7" width="1" height="3" fill="#D97757"/>'
           '<rect x="5" y="7" width="1" height="3" fill="#D97757"/><rect x="10" y="7" width="1" height="3" fill="#D97757"/>'
           '<rect x="12" y="7" width="1" height="3" fill="#D97757"/><rect x="4" y="2" width="1" height="2" fill="#141414"/>'
           '<rect x="11" y="2" width="1" height="2" fill="#141414"/></svg>')


def serve(port: int, open_browser: bool) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Agent Panel at {url}  (Ctrl+C to stop)")
    if not LOGS.exists():
        print("No activity yet. Did you run `install`? Events appear once Claude Code uses a tool.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="open the panel (default)")
    for parser in (p, s):
        parser.add_argument("--port", type=int, default=8765)
        parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    for name, help_text in (("install", "register the hooks"), ("uninstall", "remove the hooks")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--project", metavar="DIR",
                        help="only for this project (.claude/settings.json) instead of ~/.claude/settings.json")
    sub.add_parser("hook", help="internal: called by Claude Code")
    a = p.parse_args()

    if a.cmd == "hook":
        try:
            run_hook()
        except Exception:
            pass  # a hook must never get in Claude's way
        sys.exit(0)
    elif a.cmd in ("install", "uninstall"):
        install(a.project, remove=a.cmd == "uninstall")
    else:
        serve(a.port, not a.no_browser)


if __name__ == "__main__":
    main()
