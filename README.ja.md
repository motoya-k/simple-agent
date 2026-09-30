# simple-agent

**最小構成で、すべてを差し替えられる AI エージェント。モデルも、入力も、出力も、記憶も、自分で選べます。**

[English](README.md) · [日本語](README.ja.md)

> 英語版の [README.md](README.md) が最新です。内容が食い違う場合は英語版を正としてください。

多くのエージェントは、ループ、ツール、チャットとの連携、記憶、モデルがひとまとまりの製品として提供されます。そのため、どれか 1 つを変えたくなると、残りも含めてフォークすることになります。simple-agent は逆の方針を取ります。コアは標準ライブラリだけで書いた数千行の Python で、その周りの層はそれぞれ小さなインターフェースの後ろにあり、設定で差し替えられます。

## 他のエージェントとの比較

|                  | simple-agent | [Hermes Agent](https://github.com/NousResearch/hermes-agent) | [OpenClaw](https://github.com/openclaw/openclaw) |
| ---------------- | ------------ | ------------ | ------------ |
| 言語             | Python       | Python       | TypeScript   |
| 実行時の依存     | **0**        | 45           | 66           |
| コード行数¹      | **約 5.3k**  | 約 887k      | 約 4.4M      |
| ライセンス       | MIT          | MIT          | MIT          |

¹ テストを除いたソースの行数。2026-09-29 に各リポジトリのデフォルトブランチで計測しました。

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
| ハーネス | `loop`（組み込み）、`pi`（[pi-mono](https://github.com/badlogic/pi-mono) をサブプロセスで実行） | `SIMPLE_AGENT_ENGINE`、`SIMPLE_AGENT_PI_ARGS` |
| 入力と出力 | `Source` → `Router` → `Sink`。IMAP メールの Source を同梱（ターミナルの REPL は別のホスト） | コード：`simple_agent/seams.py` |
| 記憶 | `local`、`mem0`、`hindsight` | `SIMPLE_AGENT_MEMORY_BACKEND` |
| 会話履歴 | SQLite（既定）、Postgres | `SIMPLE_AGENT_DATABASE_URL` |

設定は環境変数か `~/.simple-agent/config.yaml`（同じキーを小文字で）に書きます。両方にある場合は環境変数が優先されます。設定項目の一覧は `.env.example` にあります。

プロバイダやエンジンを足すときは、ファイルを 1 つ追加し、登録表に 1 行書くだけです。ループには手を入れません。

### 自由に組み合わせる

ハーネス・LLM・記憶の 3 つは、互いに独立して選べます。ハーネスが受け持つのはループだけです。モデル、記憶、ツールの許可は simple-agent が一度だけ決めて、ハーネスに渡します。

```yaml
# ~/.simple-agent/config.yaml
engine: pi              # ループを回すハーネス
provider: bedrock       # LLM。pi には --provider amazon-bedrock として渡る
memory_backend: mem0    # 記憶。pi も simple-agent のツール経由で読み書きする
```

外部のハーネスは、`simple-agent --mcp` を通して記憶・スキル・過去の会話の検索を使います。これは標準入出力で動く MCP サーバーで、そのルートで許可されたツールだけを公開します。pi は MCP に対応していないので、同梱の pi 拡張機能が橋渡しをします。MCP に対応したハーネスなら、`{"command": "simple-agent", "args": ["--mcp", "--tools", "memory_search,memory_save"]}` のように直接つなげます。ハーネスが設定された LLM を扱えない組み合わせは、起動した時点でエラーになります。

## メール

```bash
SIMPLE_AGENT_IMAP_HOST=imap.gmail.com \
SIMPLE_AGENT_IMAP_USER=agent@example.com \
SIMPLE_AGENT_IMAP_PASSWORD=... \
SIMPLE_AGENT_EMAIL_ALLOW=@example.com \
simple-agent --email
```

メールボックスをポーリングし、差出人とスレッドの組ごとに 1 つの会話として処理します。返信はしません。エージェントが何かをするときは、ツールを通して行います。メールボックスは一切変更しません（読み取り専用で開き、`BODY.PEEK` で取得します）。処理済みのメールはエージェント自身のデータベースに記録します。起動した時点ですでに届いていたメールは処理しません。

**メールは信頼できない入力です。** 受信箱には誰でもメールを送れますし、差出人アドレスは簡単に偽装できます。そのため、差出人の許可リストはコストを抑える役にしか立ちません。本当の防御線はツールの制限です。メールから動く会話は、既定では読み取り専用のツールだけを使い（`SIMPLE_AGENT_EMAIL_TOOLS`）、会話後のバックグラウンドレビューも実行しません。こうすることで、メールの内容が記憶やスキルに書き込まれ、信頼しているほかのセッションに読み込まれることを防ぎます。ツールを広げる場合は、このリスクを理解したうえで行ってください。

## 開発

```bash
pip install -e ".[dev]"
pytest
```

## 謝辞

この設計は、Nous Research の [Hermes Agent](https://github.com/NousResearch/hermes-agent) を書き直すところから始まりました。`simple_agent/session.py` と `simple_agent/review.py` のレビュー用プロンプトは、MIT License のもとで Hermes Agent から取り入れたものです。詳しくは [LICENSE](LICENSE) を参照してください。

## ライセンス

[MIT](LICENSE)
