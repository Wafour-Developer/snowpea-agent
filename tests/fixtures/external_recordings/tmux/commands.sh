# Command sequences used in spike S5 (2026-11-15). Not a runnable test; a record.
# Placeholders: <PROJ> project dir, <UUID> session id, <S> tmux session name.
T="tmux -L snowpea-spike"          # dedicated server socket: /tmp/tmux-$UID/snowpea-spike

# start (tmux 3.2a); the pane process IS the CLI (pane_pid == CLI pid)
$T new-session -d -s cl -x 200 -y 50 -c <PROJ> "claude --model haiku --session-id <UUID>"
$T new-session -d -s cx -x 200 -y 50 -c <PROJ> "codex -s read-only -a on-request"
ID=$(cursor-agent create-chat)    # prints a chat uuid; no model call
$T new-session -d -s cu -x 200 -y 50 -c <PROJ> "cursor-agent --resume $ID"
$T new-session -d -s ag -x 200 -y 50 -c <PROJ> "agy"

# first start in a new folder: every CLI shows a trust dialog
$T send-keys -t cl Down; $T send-keys -t cl Enter      # claude: default is "No, exit"
$T send-keys -t cx Enter                               # codex: default "1. Trust and continue"
$T send-keys -t cu a                                   # cursor: "[a] Trust this workspace"
$T send-keys -t ag Enter                               # agy: default "Yes, I trust this folder"

# single-line prompt
$T send-keys -t <S> -l 'Reply with exactly: OK'; $T send-keys -t <S> Enter
# multi-line / Korean prompt (bracketed paste keeps the newline inside the prompt)
printf 'line one\n(한국어 줄)' | $T load-buffer -b sp -
$T paste-buffer -p -d -b sp -t <S>; $T send-keys -t <S> Enter
# clear the input box
$T send-keys -t <S> C-u

# permission mode cycling (Shift+Tab)
$T send-keys -t cl BTab    # claude: auto -> manual -> accept edits -> plan -> auto
$T send-keys -t ag BTab    # agy: auto -> plan -> (unlabelled) -> accept-edits -> auto

# approve the dialog
$T send-keys -t cl 1       # claude "1. Yes" (3 = No, Esc = cancel)
$T send-keys -t cx y       # codex "Yes, proceed (y)" (esc = No)

# readiness / turn state
jq -r .status ~/.claude/sessions/<pane_pid>.json          # idle | busy | waiting
tail -f ~/.claude/projects/<enc>/<UUID>.jsonl             # system/turn_duration = turn end
tail -f ~/.codex/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl # event_msg task_started / task_complete
tail -f ~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl
sqlite3 "file:~/.cursor/chats/<hash>/<ID>/store.db?mode=ro" 'select rowid,data from blobs'

# adoption: which pane holds a CLI pid
tr '\0' '\n' </proc/<pid>/environ | grep -E '^TMUX(_PANE)?='   # socket path,server pid,session idx + %pane
tmux -S <socket> list-panes -a -F '#{pane_pid} #{pane_tty} #{session_name} #{window_index}.#{pane_index} #{pane_current_command} #{pane_id}'

# sharing: a second client (from a pty) shrinks the window; restore after detach
$T resize-window -t cl -x 200 -y 50

# teardown
$T kill-server
