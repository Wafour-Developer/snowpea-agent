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
| `/model` | pick a model or profile from a list; `/model <ref>` switches directly |
| `$<agent> <prompt>` | hand one prompt to a named agent |
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
| `/team create <name> <agent...>` | create a project team from existing agents and activate it |
| `/team use <name>` / `/team list` | switch the active project team or list teams |
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

`tools list` と `commands list` はそれぞれ1回の RPC メソッド呼び出しだけで終了します。セッションを作らず、モデルも呼び出さないため、インストール直後や CI での動作確認として最適です。`tools list` は、裏にプロバイダーを持つツールについてはそのプロバイダーも表示するので、`web_search` には実際に答えることになる検索プロバイダーが出ます。

### Context

```bash
snowpea session context --json
snowpea session compact s-abc123 "keep the API design decisions"
```

`session context` は、生きているセッションごとに1行を表示します。使用トークン数、モデルのコンテキストウィンドウ、そしてその比率です。デーモンが判定できないウィンドウは、推測ではなく `?` と表示されます。ホスト型のベンダーはすべて静的な表に載っており、ローカルの vLLM や Ollama のサーバーには一度だけ尋ねてキャッシュし、`settings.json` の `providers.<vendor>.context_window` はその両方を上書きします。

圧縮は、長いセッションをそのウィンドウの中に収め続けます。`/compact` はここまでのすべてを1つの「Session summary」システムメッセージにまとめ、直近のいくつかのメッセージはそのまま残して、そこから続けます。`session compact` はシェルからの同じものです。ターンがウィンドウの `context.autoCompactPercent`（デフォルト85）を超えそうになると、ターンとターンのあいだで自動的にも行われます。ツールのループの途中では決して起きません。`context.autoCompact` を `false` にすれば、`/compact` だけに任せられます。

### Sessions

```bash
snowpea session list
snowpea session list --include-closed --workdir ~/src/api --json
snowpea session delete s-abc123
snowpea session clear --all
```

`session list` は生きているセッションを表示し、`--include-closed` を付けると保存されたものも表示します — ID、モード、作成時刻、作業ディレクトリ、そしてそれぞれが最後に見たプロンプトです。`session delete` と `session clear` は、保存されたセッションを、その添付と音声のファイルごと削除します。生きているセッションが削除されることはないので、先に閉じてください。`session list` から ID を選び、`snowpea -c "…" --resume <id>` で続けられます。

### Model profiles

```bash
snowpea model profiles --json
snowpea model default fast
snowpea model assign executor deep
```

`model profiles` は `models.profiles` をデフォルトの印つきで表示し、あわせて `agents.models` のエージェントごとの割り当ても表示します。`model assign` はエージェント1つをプロファイルに振り向けます。存在しないプロファイル ID はデーモンが拒否します。

### Search

```bash
snowpea search test "snowpea agent github"
snowpea search test "snowpea agent github" --json
```

`search test` は設定済みのプロバイダーで実際のクエリを1回実行し、どのプロバイダーが答えたかと、飛ばされた各プロバイダーが脱落した理由 — キーがない、インスタンスの URL が設定されていない、HTTP エラー — を表示します。デーモンは不要で、`$SNOWPEA_HOME/settings.json` を直接読みます。終了コードは、どれかのプロバイダーが答えたときは 0、どれも答えられなかったときは 2 です。

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
snowpea job schedule --at "0 9 * * *" --task "summarize yesterday's commits" --channel telegram:123456
snowpea job schedule --in 10m --task "check the build" --mode plan
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
snowpea team list
snowpea team create delivery architect executor verifier
snowpea team use delivery
snowpea team delete delivery
snowpea service install
snowpea service status
snowpea service uninstall
```

`team status` は実行中のチームについて、各タスクの状態とリトライ回数を報告します。`team list`、`create`、`use`、`delete` のほうは、再利用できるエージェントの名簿を管理します。`/team create` が `<workdir>/.snowpea/settings.json` に書き込むのと同じプロジェクトのチームで、グローバルのものと一緒に、有効なチームに印を付けて一覧されます。`team delete` が消せるのはプロジェクトのチームだけです。`service` はデーモンをログイン時に起動するよう登録します — Linux では systemd user unit、macOS では launchd agent、Windows ではスケジュールされたタスクです。デフォルトでは無効になっており、誰もターミナルにログインしなくてもスケジュールやゲートウェイを再起動後も生き残らせたい場合にのみ必要です。

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
| `--resume SESSION_ID` | continue a saved session instead of opening a new one |
| `--approve-none` | deny every approval instead of prompting |

## Running a slash command headlessly

レジストリはコアの中にあるため、スラッシュコマンドはそのままヘッドレスなプロンプトとして有効です。

```bash
snowpea -c "/ralph add a failing test then make it pass" --mode auto
snowpea -c "/deepinit" --json
```

CLI 自身はこれを解析しません。テキストをそのままコアに渡し、コアが TUI と全く同じようにディスパッチします。

## Next

[Plugins](plugins.md) — adding commands of your own.
