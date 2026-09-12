# Commands

[English](../en/commands.md) · [한국어](../ko/commands.md) · [すべてのページ](../README.md)

コマンドには2つの面があります。スラッシュコマンドはセッションの内部で実行され、コアが所有しているため、同じ `/ralph` は TUI でも、`snowpea -c` でも、スケジュールされたジョブでも、Telegram のメッセージの中でも同じように振る舞います。CLI サブコマンドは、セッションをまったく開かずにデーモンを調べたり設定したりします。

```bash
snowpea commands list
snowpea commands list --json
```

これはライブのレジストリを表示し、インストール済みのプラグインが提供するコマンドも含まれます。UI 内の `/help` も同じものを表示します。

## Slash commands

### Session and mode

| Command | What it does |
|---|---|
| `/help` | list every available command |
| `/tools` | list registered tools with category, permission and state |
| `/compact [instructions]` | summarise the conversation so far and continue with the summary |
| `/plan`, `/accept`, `/auto` | switch mode |
| `/mode [plan\|accept\|auto\|save\|show]` | show, switch, or save the project default |
| `/approvals` | list unattended approvals waiting for an answer |
| `/allow <regex> [--global]` | promote a repeated prompt to a silent allow |
| `/allowlist [remove <id>]` | show or prune the allowlist |
| `/backend [local\|docker\|ssh] [json]` | show or change where tools execute |

### Terminal UI

以下はコアではなくターミナル UI 自身が処理します。そのため `snowpea commands list` には出てこず、人が前に座っているセッションでしか動きません。画面で何が起きるかは [The terminal UI](tui.md) にあります。

| Command | What it does |
|---|---|
| `/resume` | reopen the session this directory was last in, and replay it |
| `/attach <path>` | attach a file to the next prompt |
| `/voice` | arm voice input; `Ctrl+Space` then records |
| `/rec` | start or stop recording, same as `Ctrl+Space` |
| `/tts on\|off` | speak each reply as it finishes |
| `/update` | take the offered upgrade, same as `U` |

### Work

| Command | What it does |
|---|---|
| `/ralph <task>` | PRD loop: stories with acceptance criteria, implement, verify, review until APPROVE |
| `/ultrawork <task>` | split into independent parts, run them on concurrent subagents, merge the reports |
| `/deepinit [path]` | walk the repository and write hierarchical `AGENTS.md` files |
| `/team <n> <task>` | n workers, one git worktree each, branches merged as tasks finish |
| `/deep-interview <idea>` | Socratic interview that scores ambiguity and refuses to hand off until the spec holds |
| `/deep-research <topic>` | multi-source web research fanned out over subagents, answered with citations |
| `/ralplan <task>` | consensus planning — planner, architect and critic argue before any code is written |

`/ralph` は自らの状態を `<project>/.snowpea/ralph/` に `prd.json` と `progress.md` として保持するため、自分が何をしていると考えているかを読むことができます。収束できない場合は `ralph.max_iterations`（10）で停止します。`/ultrawork` と `/deepinit` は、デフォルトで `agents.max_concurrent`（3）の範囲でファンアウトします。最後の3つは `core/snowpea_core/builtin_skills/` にある `SKILL.md` ファイルで、あなた自身のスキルが使うのと同じローダーで読み込まれます。読んで、コピーして、変更してください。

### Generators

| Command | What it does |
|---|---|
| `/agent create "<description>"` | write an agent definition into `<project>/.snowpea/agents/<name>.md` |
| `/agent list` | list agent definitions |
| `/skill learn [name]` | turn the session you just finished into `<project>/.snowpea/skills/<name>/SKILL.md` |

生成されたエージェントは、リロード不要でその場から `delegate_task` の対象になります。

### Scheduling

| Command | What it does |
|---|---|
| `/schedule "<spec>" "<task>" [--channel X] [--mode M]` | register a job |
| `/schedule` | list jobs, or cancel one |

## CLI subcommands

### Inspecting

```bash
snowpea --version
snowpea tools list --json
snowpea commands list --json
snowpea agents --json
snowpea daemon status --json
```

`tools list` と `commands list` はそれぞれ1回の RPC メソッド呼び出しだけで終了します。セッションを作らず、モデルも呼び出さないため、インストール直後や CI での動作確認として最適です。

### Daemon

```bash
snowpea daemon status
snowpea daemon start
snowpea daemon stop
```

`status` はポート、pid、稼働時間、4つのキープアライブカウンター — sessions、jobs、gateway bindings、named agents — と、デーモンが終了しようとしているかどうか、終了しない場合はその理由を表示します。

### Providers

```bash
snowpea provider list
snowpea provider login openai
snowpea setup --vendor deepseek --key sk-...
```

### Skills and plugins

```bash
snowpea skill list
snowpea skill search "pdf"
snowpea skill install oh-my-claudecode
snowpea skill install ./my-plugin
snowpea skill remove my-plugin
```

### Jobs

```bash
snowpea job schedule --at "0 9 * * *" --task "昨日のコミットを要約して" --channel telegram:123456
snowpea job schedule --in 10m --task "ビルドを確認して" --mode plan
snowpea job list --json
snowpea job run <job-id>
snowpea job cancel <job-id>
```

`--at`、`--in`、`--every`、`--cron`、`--spec` は同じオプションの5つの呼び名であり、書こうとしているスケジュールにとって最も読みやすいものを使ってください。

### Gateway

```bash
snowpea gateway bind telegram TELEGRAM_BOT_TOKEN agent:scribe --user 987654
snowpea gateway list --json
snowpea gateway unbind <binding-id>
```

### Team and service

```bash
snowpea team status
snowpea service install
snowpea service status
snowpea service uninstall
```

`team status` は実行中のチームについて、各タスクの状態とリトライ回数を報告します。`service` はデーモンをログイン時に起動するよう登録します — Linux では systemd user unit、macOS では launchd agent、Windows ではスケジュールされたタスクです。デフォルトでは無効になっており、誰もターミナルにログインしなくてもスケジュールやゲートウェイを再起動後も生き残らせたい場合にのみ必要です。

### Global options

| Option | Meaning |
|---|---|
| `--version` | print the version and exit |
| `--home DIR` | override `SNOWPEA_HOME` for this invocation |
| `--mode plan\|accept\|auto` | mode for the session being started |
| `-c`, `--prompt TEXT` | run one headless turn and exit |
| `--json` | emit JSON Lines instead of prose |
| `--cwd DIR` | working directory of the session |
| `--timeout SEC` | abort the turn after SEC seconds |
| `--provider VENDOR` | vendor for this session |
| `--approve-none` | deny every approval instead of prompting |

## Running a slash command headlessly

レジストリはコアの中にあるため、スラッシュコマンドはそのままヘッドレスなプロンプトとして有効です。

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit" --json
```

CLI 自身はこれを解析しません。テキストをそのままコアに渡し、コアが TUI と全く同じようにディスパッチします。

## Next

[Plugins](../en/plugins.md) — adding commands of your own.
