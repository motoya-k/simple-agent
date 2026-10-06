# simple-agent

**最小構成で、すべてを差し替えられる AI エージェント。モデルも、入力も、出力も、記憶も、自分で選べます。**

[English](README.md) · [日本語](README.ja.md)

> 英語版の [README.md](README.md) が最新です。内容が食い違う場合は英語版を正としてください。

多くのエージェントは、ループ、ツール、チャットとの連携、記憶、モデルがひとまとまりの製品として提供されます。そのため、どれか 1 つを変えたくなると、残りも含めてフォークすることになります。simple-agent は逆の方針を取ります。コアは標準ライブラリだけで書いた数千行の Python で、その周りの層はそれぞれ小さなインターフェースの後ろにあり、設定で差し替えられます。

はじめての方は [INTRODUCTION.ja.md](INTRODUCTION.ja.md) をどうぞ。エージェントの実装を読むのが初めてという前提で、ループ、ツール、記憶の 3 層、本番に必要なものまでを一通り解説しています。

## 他のエージェントとの比較

|                  | simple-agent | [Hermes Agent](https://github.com/NousResearch/hermes-agent) | [OpenClaw](https://github.com/openclaw/openclaw) |
| ---------------- | ------------ | ------------ | ------------ |
| 言語             | Python       | Python       | TypeScript   |
| 実行時の依存     | **0**        | 45           | 66           |
| コード行数¹      | **約 6.4k**  | 約 887k      | 約 4.4M      |
| ライセンス       | MIT          | MIT          | MIT          |

¹ テストを除いたソースの行数。simple-agent は 2026-10-06、他の 2 つは 2026-09-29 に、それぞれのデフォルトブランチで計測しました。

Hermes と OpenClaw は完成された製品で、多数のチャットプラットフォーム、アプリ、サンドボックスなど、最初からできることは圧倒的に多いです。simple-agent が向いているのは、エージェント全体を半日で読み切り、部品を自分で入れ替えたい場合です。

## クイックスタート

```bash
git clone https://github.com/motoya-k/simple-agent.git && cd simple-agent
pip install -e .
cp .env.example .env        # ANTHROPIC_API_KEY（または他のプロバイダのキー）を設定

simple-agent                # 対話モード
simple-agent "このリポジトリを要約して"   # 1 回だけ実行
```

Python 3.10 以上が必要です。それ以外の依存はありません。

## 差し替えられるもの

| 層 | 選択肢 | 設定 |
| --- | --- | --- |
| モデル | `anthropic`、`bedrock`（Converse。Bedrock API キーか、`AWS_PROFILE` の IAM 認証）、`gemini`、`openai`（Responses） | `SIMPLE_AGENT_PROVIDER`、`SIMPLE_AGENT_MODEL` |
| エージェントの人格 | プロファイル（指示文・使えるツール・学習の有無・記憶の namespace） | `SIMPLE_AGENT_PROFILE`、`~/.simple-agent/profiles/<名前>.md` |
| 入力と出力 | `Source` → `Router` → `Sink`。メール（IMAP）、Slack（Socket Mode）、時刻（cron）を同梱。1 プロセスに組み合わせられる（ターミナルの REPL は別のホスト） | コード：`simple_agent/seams.py` |
| ツール | stdio で動く MCP サーバー（組み込みのツールに加わる） | `~/.simple-agent/mcp.json` |
| ツールのポリシー | mod（呼び出しの拒否・引数の書き換え・出力の伏せ字） | `~/.simple-agent/mods/<名前>.py` |
| 保存先 | `~/.simple-agent` の下のファイル（既定）か Postgres。会話履歴・記憶・スキルがまとめて動く | `SIMPLE_AGENT_DATABASE_URL` |

設定は環境変数か `~/.simple-agent/config.yaml`（同じキーを小文字で）に書きます。両方にある場合は環境変数が優先されます。設定項目の一覧は `.env.example` にあります。

プロバイダを足すときは、ファイルを 1 つ追加し、登録表に 1 行書くだけです。ループには手を入れません。

### 書き込むものは 1 か所にまとめる

ターンをまたいで残るものは 3 つあります。会話履歴、長期記憶（チームが知っていること）、スキル（エージェントが自分で書いた手順）です。この 3 つは 1 つの設定で一緒に動くので、「半分だけ移した」状態になりません。

- **未設定** — `~/.simple-agent` の下のファイルに置きます。会話履歴は SQLite、記憶は namespace ごとの JSONL 1 ファイル、スキルは 1 つにつき 1 ディレクトリです。どれもエディタで開いて直せます。
- **`SIMPLE_AGENT_DATABASE_URL=postgresql://...`** — 3 つとも Postgres に置きます。ディスクが残らないコンテナ向けです（`pip install ".[postgres]"`）。

`SIMPLE_AGENT_MEMORY_NAMESPACE` は、その記憶と会話が属するチームです。1 つのデプロイを 2 チームで使っても、記憶も会話も混ざりません。

mem0 や Hindsight のような外部の記憶サービスは、ここでは保存先の選択肢ではありません。MCP サーバーとしてつなぎます（後述）。1 か所に書けばどのハーネスからも届くので、サービスごとのアダプタをこのリポジトリが抱えずに済みます。

## プロファイル：そのルートでのエージェントの人格

同じコアが、ターミナルの前にいる人にも、受信箱にメールを送ってきた知らない相手にも応えます。ただし応え方を同じにしてはいけません。プロファイルは、その違いを 1 つのファイルにまとめたものです。システムプロンプトに入る指示文、使えるツール、長期記憶とスキルに書き込んでよいか、どのチームの記憶を読むか——この 4 つです。

組み込みは 3 つあります。`terminal` はすべてのツールを使い、学習もします。`email` と `slack` は読み取り専用のツールだけで、何も学習しません。誰でも書き込める入口が、信頼している会話に何かを教えられてはならないからです。ツール制限と学習の可否は同じファイルの隣り合う行なので、片方だけ緩むことがありません。

組み込みを上書きしたり、新しく足したりするには、ファイルを 1 つ書きます。形式はスキルと同じです。

```markdown
---
tools: skill_view, google__*_list
learning: false
namespace: support
imap_host: imap.gmail.com
imap_user: support@example.com
email_allow: "@example.com"
---

あなたはサポートのメールに対応します。答える前に必ず調べること。返金を約束しないこと。
```

```bash
simple-agent --profile support            # ターミナルで使う
simple-agent --email --profile support    # メールのホストとして動かす
```

`tools: '*'` と書くとすべてのツールを使えます。環境変数のほうが優先されるので、コンテナではファイルを置かずに設定できます。4 つの項目は `SIMPLE_AGENT_SUPPORT_TOOLS`、`_LEARNING`、`_NAMESPACE`、その他の設定は `SIMPLE_AGENT_IMAP_HOST`、`SIMPLE_AGENT_EMAIL_ALLOW` です。パスワードは環境変数からしか読みません。ファイルには書かないので、プロファイルはリポジトリに入れても安全です。

## mod：ツール呼び出しに口を出す

ここにあるほかの差し込み口は、どれも「自分で実装するもの」です。provider は翻訳し、Source は受け取り、ツールは実行します。mod はそのどれにもなれないものです。つまり、**モデルにもツールにも任せたくない判断**です。「このルートでは `rm -rf` を絶対に通さない」「シェルが何を出力してもトークンは消す」「このパスはプロジェクトの外に出られないように書き換える」。

mod は `~/.simple-agent/mods/<名前>.py` に置くファイルで、2 つのフックのどちらか、または両方を書きます。

```python
def before_tool(name, arguments):
    if name == "terminal" and "rm -rf" in arguments.get("command", ""):
        return Deny("rm -rf is not allowed here")
    return None                                  # 意見なし

def after_tool(name, arguments, output, is_error):
    return output.replace(TOKEN, "[redacted]")   # モデルが読む内容
```

`before_tool` が返せるのは `None`、`Deny`（最初から名前が通っています。import しても構いません）、差し替え後の `arguments` の 3 つです。拒否はふつうのエラー結果としてモデルに届くので、モデルは理由を読んでから次に進めます。mod は書いた順に呼ばれ、後の mod は前の mod の判断を見ます。

ルートごとの mod はプロファイルに書き、どのルートでも必ず動かすものは `SIMPLE_AGENT_MODS` に書きます。後者はプロファイル側から外せません。

```markdown
---
tools: '*'
mods: no-rm, redact-secrets
---
```

ツール呼び出しが実際の動作になる道は `ToolRegistry.call` だけです（ループ、バックグラウンドのレビュアー、REPL のスラッシュコマンド、`--mcp` がすべてここを通ります）。だから 1 か所書いたルールは、誰が呼んでも効きます。MCP でツールを借りている別のハーネスからの呼び出しも含みます。

**2 つのフックは、わざと逆方向に倒れます。** `before_tool` が例外を出したら、その呼び出しは**拒否**します。プロファイルが名前を書いた mod が見つからないときも、起動時に止まります。落ちたポリシーは何も承認していないからです。`after_tool` が例外を出したときは無視して元の出力を使います。こちらはモデルが読む内容を整えるだけなので、伏せ字に失敗したことでターンを失うほうが損だからです。

書く前に 2 つだけ。mod は別プロセスの MCP サーバーではなく、**同じプロセスで動く Python** です。エージェントと同じ権限を持つ信頼コードなので、自分で書いたか中身を読んだものだけを置いてください。それと、読み取り専用のツールはスレッドに分かれて並列に走るので、フックは複数スレッドから同時に呼ばれても安全に書く必要があります。

## ほかのハーネスから使う

ループはこのリポジトリのものです。Claude Code、Codex、Goose など MCP に対応したハーネスで作業したい場合は、そのハーネスに `simple-agent --mcp` を登録します。これは標準入出力で動く MCP サーバーで、記憶・スキル・過去の会話の検索を公開します。どの記憶の保存先を設定していても、そのハーネスはこのエージェントと同じ記憶を読み書きします。公開するツールは `--tools` で絞れます。

```json
{"command": "simple-agent", "args": ["--mcp", "--tools", "memory_search,memory_save"]}
```

`--profile` を渡すと、プロファイル 1 つぶんの権限（ツールと記憶の namespace）をそのハーネスに貸せます。`--tools` はそこからさらに絞るだけで、広げることはできません。

ループそのものを相手のハーネスに渡すこともできます。[`examples/claude-code-host`](examples/claude-code-host) は、`claude -p` が返答を書き、残りはこのリポジトリが持つ構成の実例です。入口は Slack とメール、ルートごとに Profile（ツールの許可リストと learning の両方を Claude Code 側に翻訳して適用）、会話は Postgres、長期記憶は MCP 経由の Hindsight。[`examples/`](examples) にはもう 1 つ、時刻を Source にした例もあります。cron で GitHub の PR を見回り、人の対応が必要なものだけを報告し、なければ何も言いません。

PM 向け、営業向け、マーケ向けのエージェントは、同じループに別のツール（MCP サーバー）とスキルを渡したものです。

## MCP サーバーをつなぐ

`~/.simple-agent/mcp.json` にサーバーを書きます。形式は Claude Desktop・Cursor・Claude Code と同じです。

```json
{"mcpServers": {"google": {"command": "uvx", "args": ["some-google-workspace-mcp"], "env": {"...": "..."}}}}
```

サーバーのツールは `<サーバー名>__<ツール名>`（例：`google__calendar_list`）としてエージェントに加わります。ほかのハーネスにも `simple-agent --mcp` を通して届くので、サーバーの設定は 1 か所で済みます。許可リストにはワイルドカードが使えるので、たとえばメール経由のルートには読み取り系のツールだけを渡せます。

```bash
SIMPLE_AGENT_EMAIL_TOOLS='skill_view,google__*_list,google__*_get' simple-agent --email
```

対応しているのは標準入出力（stdio）で動くサーバーだけです。起動できなかったサーバーはログに記録して飛ばします。

外部の記憶サービスもここにつなぎます。mem0 や Hindsight は MCP サーバーを公開しているので、ここに 1 行足せば、組み込みのツールと並んでエージェントから使えます。サービスごとのアダプタをこのリポジトリが持つ必要はありません。

## メール

```bash
SIMPLE_AGENT_IMAP_HOST=imap.gmail.com \
SIMPLE_AGENT_IMAP_USER=agent@example.com \
SIMPLE_AGENT_IMAP_PASSWORD=... \
SIMPLE_AGENT_EMAIL_ALLOW=@example.com \
simple-agent --email
```

メールボックスをポーリングし、差出人とスレッドの組ごとに 1 つの会話として処理します。返信はしません。エージェントが何かをするときは、ツールを通して行います。メールボックスは一切変更しません（読み取り専用で開き、`BODY.PEEK` で取得します）。処理済みのメールはエージェント自身のデータベースに記録します。起動した時点ですでに届いていたメールは処理しません。

**メールは信頼できない入力です。** 受信箱には誰でもメールを送れますし、差出人アドレスは簡単に偽装できます。そのため、差出人の許可リストはコストを抑える役にしか立ちません。本当の防御線は `email` プロファイルです。読み取り専用のツールだけを使い、`learning: false` なので、メールの内容が記憶やスキルに書き込まれ、信頼しているほかのセッションに読み込まれることがありません。広げる場合は、この 2 つが同じファイルの隣り合う行であることを踏まえて、両方を意識したうえで行ってください。

## Slack

```bash
SIMPLE_AGENT_SLACK_APP_TOKEN=xapp-...   # アプリレベルトークン（connections:write）
SIMPLE_AGENT_SLACK_BOT_TOKEN=xoxb-...   # ボットトークン
SIMPLE_AGENT_SLACK_ALLOW='#ops,dm' \
simple-agent --slack
```

Socket Mode なので接続は内側から外へ出ていくだけです。公開 URL もロードバランサーも署名検証も要らず、メールのホストを動かしているコンテナがそのまま使えます。Slack アプリ側では Socket Mode を有効にし、`message.channels` と `message.im` を購読して、`chat:write`・`reactions:write`・`channels:history` / `im:history` を与えます。

メッセージの扱い方は 2 つに分かれます。チャンネルではメンションされたときだけ答え、質問にスレッドを付けて返します。質問・回答・その後のやりとりが 1 つの会話になるからです。ダイレクトメッセージはメンションを必要としません。ターンが動いているあいだは、質問したメッセージに 👀 が付きます。全員が読んでいるチャンネルに実況を流すのではなく、進捗はメッセージの上に出すべきものです。Markdown は送信時に Slack の記法へ変換し、1 通に収まらない長さの答えはコードブロックを壊さずに分割します。

受け取ったイベントは、確認応答（ack）を返す前に書き留めます。Socket Mode の猶予は 3 秒で、答えを作るには足りません。先に記録しておけば、ターンの途中で落ちても質問は失われず再実行されますし、再接続後に同じイベントが再送されても二度答えることがありません。

本文が **`やめて`** だけ（ほかに `stop`・`cancel`・`中止`・`ストップ` など）のメッセージは、「いま書いている答えを投稿するな」という意味になります。スレッドは順番に処理されるので、これは順番を待てない唯一のメッセージです。ロックの後ではなく**届いた時点で**読み、処理中の答えを配信せずに捨てます。判定は本文全体の一致なので、「stop the deploy and tell me what broke」は質問として扱われます。止まるのは答えだけで、**作業そのものは最後まで走ります**。語を変えるならプロファイルの `stop_words`、無効にするなら `none` です。

メールと同じく、ワークスペースも**信頼できない入力**です。そこにいる人は誰でも書き込めます。`slack` プロファイルは読み取り専用で `learning: false`、`slack_allow` は防御線ではなくコストの線引きです。

## スケジュール

ジョブは `~/.simple-agent/schedules/` に置くファイル 1 つです。形式はスキルやプロファイルと同じで、フロントマターの下が本文（プロンプト）です。

```markdown
---
schedule: 0 9 * * 1-5        # @daily、@hourly、@every 10m も使える
tz: Asia/Tokyo
to: slack:C0123ABC           # 答えを届ける先。省略すると誰にも伝えない
catch_up: true               # デプロイで飛んだ回を取り戻す
---

昨日失敗したデプロイと、まだ赤いままのものをまとめて。
```

```bash
simple-agent --cron            # スケジュールだけ
simple-agent --slack --cron    # 1 プロセスで、質問もスケジュールも
```

時刻もほかと同じ Source です。つまりスケジュールの実行は「時刻の欄が埋まったメッセージ」で、本文を書いたのは運用している本人なので、受信箱より信頼できる入口になります。ジョブはプロファイルを指定しなければホスト自身のものを使い、`history: true` を書かない限り毎回新しい会話として走ります。

実行は走らせる前に記録し、再実行はしません。9 時 0 分 30 秒にホストが落ちたら、その日報は 4 分後ではなく翌日に届きます。メールと Slack が再実行するのは、答えを待っている人がいるからです。スケジュールには次の回があります。この記録は、1 つのデータベースを共有する 2 つのタスクが同じ処理を二重に走らせないための仕組みでもあります。

## デプロイ（AWS ECS Fargate）

イメージに入っているのはエージェント本体だけです。本番向けの既定値は組み込み済みです。具体的には、root 以外のユーザーで動き、ログは JSON 形式、`terminal` ツールは無効、ヘルスチェック（`simple-agent --health`）付きで、起動すると `simple-agent --email` が動きます（`--slack --cron` など、どの組み合わせに変えても 1 つのタスクのままです）。MCP サーバーとその設定のように利用者が用意するものは、このイメージを元にした別のイメージに入れます。

```dockerfile
FROM ghcr.io/you/simple-agent:latest          # この repo の Dockerfile からビルドしたもの
RUN pip install --user workspace-mcp==1.30.0  # 動かすものはバージョンを固定する
COPY --chown=agent:agent mcp.json /home/agent/.simple-agent/mcp.json
COPY --chown=agent:agent support.md /home/agent/.simple-agent/profiles/support.md
```

タスクは 1 つで、次のように設定します。

- **状態は Postgres（RDS / Aurora）に置き**、コンテナには何も残しません。`SIMPLE_AGENT_DATABASE_URL` を 1 つ設定すれば、会話履歴・記憶・スキルがまとめて移ります。
- **タスクロール**に、推論プロファイルと、その振り分け先のモデルに対する `bedrock:InvokeModel` を付けます。認証情報はロールから取得して自動で更新するので、タスクにキーを置く必要はありません。
- **秘密情報は Secrets Manager から環境変数として渡します**（`SIMPLE_AGENT_IMAP_PASSWORD`、データベースの URL、MCP サーバー用のキー）。MCP サーバーは環境変数を引き継ぎます。
- `stopTimeout: 120`（SIGTERM を受けてから、処理中の会話に 90 秒の猶予を与えるため）、`initProcessEnabled: true`（終了した MCP サーバーのプロセスを回収するため）、`desiredCount: 1` にします。

`deploy/terraform` を使うと、ここまでの構成を既存の VPC の中にまとめて作れます。作るのは、ECR、クラスタとサービス、タスクロールと実行ロール（Bedrock の権限は、設定した 2 つの推論プロファイルに限定）、Secrets Manager の項目、ログ、RDS Postgres です。

```bash
cd deploy/terraform && cp terraform.tfvars.example terraform.tfvars  # 値を埋める
terraform init -backend-config="bucket=..." -backend-config="key=simple-agent.tfstate" \
  -backend-config="region=ap-northeast-1" -backend-config="encrypt=true"
terraform apply
```

モデルの呼び出しは、429 や 5xx が返ると間隔をあけて再試行します。同時に処理する会話は最大 `SIMPLE_AGENT_MAX_CONCURRENT_TURNS`（既定は 4）件で、1 つの会話の中では順番どおりに処理します。

## 開発

```bash
pip install -e ".[dev]"
pytest
```

## 謝辞

この設計は、Nous Research の [Hermes Agent](https://github.com/NousResearch/hermes-agent) を書き直すところから始まりました。`simple_agent/session.py` と `simple_agent/review.py` のレビュー用プロンプトは、MIT License のもとで Hermes Agent から取り入れたものです。詳しくは [LICENSE](LICENSE) を参照してください。

## ライセンス

[MIT](LICENSE)
