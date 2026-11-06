#!/usr/bin/env bash
# Feeds fake events into a throwaway log and opens the panel, so you can see it without Claude Code.
set -e
cd "$(dirname "$0")/.."
export AGENT_PANEL_HOME="$(mktemp -d)"
P="$PWD/examples/demo-project"
ev(){ echo "$1" | CLAUDE_PROJECT_DIR="$P" python3 agent_panel.py hook; }
ev '{"hook_event_name":"UserPromptSubmit","session_id":"demo"}'
ev '{"hook_event_name":"PreToolUse","session_id":"demo","tool_name":"Agent","tool_input":{"subagent_type":"planner","description":"plan dark mode"}}'
ev '{"hook_event_name":"SubagentStart","session_id":"demo","agent_id":"p1","agent_type":"planner"}'
ev '{"hook_event_name":"PreToolUse","session_id":"demo","agent_id":"p1","agent_type":"planner","tool_name":"Grep","tool_input":{"pattern":"theme"}}'
ev '{"hook_event_name":"SubagentStop","session_id":"demo","agent_id":"p1","agent_type":"planner"}'
ev '{"hook_event_name":"SubagentStart","session_id":"demo","agent_id":"c1","agent_type":"coder"}'
ev '{"hook_event_name":"PreToolUse","session_id":"demo","agent_id":"c1","agent_type":"coder","tool_name":"Edit","tool_input":{"file_path":"'"$P"'/src/theme.css"}}'
ev '{"hook_event_name":"SubagentStart","session_id":"demo","agent_id":"t1","agent_type":"tester"}'
ev '{"hook_event_name":"PreToolUse","session_id":"demo","agent_id":"t1","agent_type":"tester","tool_name":"Bash","tool_input":{"description":"Run the test suite"}}'
echo "Demo log in $AGENT_PANEL_HOME"
python3 agent_panel.py "$@"
