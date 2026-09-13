# Install

[English](../en/install.md) · [한국어](../ko/install.md) · [すべてのページ](../README.md)

snowpea には2つのランタイムが必要です。Python 3.11+（[uv](https://docs.astral.sh/uv/) が管理します）と Node 20+ です。どちらか欠けている場合はインストーラーが用意します。ターミナル UI は Python wheel の中にあらかじめバンドルされているため、お使いのマシンで `npm install` を実行する必要はありません。

## One-liner

**macOS, Linux, WSL2**

```bash
curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh | sh
```

**Windows (PowerShell)**

```powershell
iwr https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.ps1 | iex
```

そして確認します。

```bash
snowpea --version
```

このスクリプトは冪等です。もう一度実行するとその場でアップグレードされます。公式インストーラー経由で uv をインストールし、`node --version` が 20 以上を報告することを確認し、`snowpea` コマンドを uv tool としてインストールし、`~/.local/bin` がまだ `PATH` になければシェルのプロファイルに追記します。Windows ではデータディレクトリは `~/.snowpea` の代わりに `%LOCALAPPDATA%\snowpea` になります。

何もせずに何が行われるかだけを確認したい場合は、先にダウンロードしてから `--dry-run` を渡してください。

```bash
curl -fsSL https://raw.githubusercontent.com/Wafour-Developer/snowpea-agent/main/installer/install.sh -o install.sh
sh install.sh --dry-run
```

## Manual install

スクリプトをシェルにパイプで流し込みたくない場合、あるいは one-liner が途中の手順で失敗し自分の手でやり直したい場合は、次のようにします。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # uv, if missing
uv tool install snowpea-agent
snowpea --version
```

ターミナル UI のためには Node 20+ が `PATH` 上になければなりません。ヘッドレス実行（`snowpea -c`）と各種 `snowpea <subcommand>` は Node がなくても動作します。TUI だけが Node を必要とします。

## Install source overrides

インストーラーはインストール元の上書きを受け付けます。CI やオフラインでのインストールが使うのがこれです。

```bash
sh install.sh --from-checkout
SNOWPEA_WHEEL_URL=./snowpea_agent-0.1.0-py3-none-any.whl sh install.sh
SNOWPEA_INSTALL_SOURCE=git+https://github.com/Wafour-Developer/snowpea-agent sh install.sh
```

`SNOWPEA_BIN_DIR` は `snowpea` の実行ファイルが置かれる場所を変え、`--from-checkout` はスクリプト自身が置かれているチェックアウトをインストールします。

## Running from a checkout

```bash
git clone https://github.com/Wafour-Developer/snowpea-agent.git
cd snowpea-agent
uv sync
npm ci
npm run build
uv run snowpea --version
```

`npm run build` は `tui/dist/snowpea-tui.js` を生成し、`snowpea` はパッケージ済みバンドルの後にこれを探しに行きます。UI の作業中は、`SNOWPEA_TUI_ENTRY` に自分のエントリーファイルを指定すれば、バンドルを完全にスキップできます。

## Updating

snowpea は1日に1度、バックグラウンドで新しいリリースを確認し、その答えを `$SNOWPEA_HOME/update-check.json` にキャッシュします。あなたが言わない限り、何かがインストールされることはありません。

**ターミナル UI で。** アップデートが待っているとき、画面の上部にバナーが出ます。

```text
⬆ Update available v0.1.2 (current v0.1.1) — press U or type /update
```

`U` を押す（または `/update` と打つ）して `y` と答えてください。アップグレードはバックグラウンドで走り、進捗はバナーが伝え、終わると snowpea は新しいバージョンで自分自身を再起動します。走っているターンが中断されることはありません。

**コマンドラインから。**

```bash
snowpea update --check
snowpea update
```

`--check` は現在のバージョンと最新のバージョンを報告するだけで、何もインストールしません。付けない場合、`snowpea update` はアップグレードを実行し、デーモンを止め、もう一度 `snowpea` を起動するよう伝えます。`snowpea --version` も保留中のアップデートに触れますが、使うのはキャッシュされた答えだけなので、ネットワークを待つことはありません。

アップグレードは出力を `$SNOWPEA_HOME/logs/update.log` に書きます。実行するのは `uv tool install --force --reinstall` です。`uv` が `PATH` にない場合は、インストーラーが `$SNOWPEA_HOME/install.json` に記録した方法が代わりに使われ、何も分からない場合は、snowpea は推測せずに手で実行すべきコマンドを表示します。

これを制御する設定は `$SNOWPEA_HOME/settings.json` に2つあります。

```json
{ "updates": { "check": true, "channel": "auto" } }
```

`check: false` は毎日のバックグラウンド確認をオフにし、`/update` と `snowpea update` は必要なときに動くまま残します。`channel` は `auto`（パッケージが公開されていれば PyPI、そうでなければリポジトリの git タグ）、`pypi`、`git` のいずれかです。

代わりに手でアップグレードするには、

```bash
uv tool upgrade snowpea-agent
snowpea daemon stop
snowpea --version
```

手でアップグレードしたあとはデーモンを停止してください。デーモンが起動したままだと古いコードがメモリ上に残り続け、次に接続するクライアントはインストール済みのものと一致しなくなったプロトコルバージョンでネゴシエーションしてしまいます。

## Uninstalling

```bash
snowpea daemon stop
uv tool uninstall snowpea-agent
```

これでもデータは残ります。データも削除したい場合は `$SNOWPEA_HOME`（`~/.snowpea`、Windows では `%LOCALAPPDATA%\snowpea`）を削除してください。デーモンをサービスとして登録していた場合は、先に登録解除してください。

```bash
snowpea service uninstall
```

## When something goes wrong

**インストール直後に `snowpea: command not found` になる。** このシェルでは `~/.local/bin` が `PATH` に入っていません。新しいターミナルを開くか、シェルのプロファイルを source してください。インストーラーは行を追記しますが、いま立っているシェル自体を書き換えることはできません。

**Node がない、または古すぎる。** TUI が起動しません。パッケージマネージャーか [nodejs.org](https://nodejs.org) から Node 20+ をインストールし、もう一度 `snowpea` を実行してください。それ以外の機能はその間も動き続けます。

```bash
snowpea -c "hello" --json
```

**デーモンが起動しない。** `$SNOWPEA_HOME/logs/daemon.log` を確認し、記録されているポートを古いプロセスが握ったままになっていないか確認してください。

```bash
snowpea daemon status
snowpea daemon stop
snowpea daemon start
```

`daemon status` は `$SNOWPEA_HOME/daemon.json` を読み込み、pid が生きているかを検証します。pid が死んでいる古いファイルは削除して問題ありません。

**社内プロキシ、またはオフラインのマシン。** `uv tool install` には PyPI へのアクセスが必要です。インストーラーを実行する前に `HTTPS_PROXY` を設定するか、自分で持ち込んだ wheel から `uv tool install ./snowpea_agent-0.1.0-py3-none-any.whl` でインストールしてください。

## Next

[Setup](setup.md) — pick a vendor and get a key in place.
