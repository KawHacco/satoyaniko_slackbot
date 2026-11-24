# Slack Message Sender

`/send` スラッシュコマンドとモーダル UI を通じて、Slack チャンネルへの**即時送信**、**単発予約投稿**、**繰り返し予約投稿**、および**予約の管理（編集・一時停止・再開・削除）**を行う Python アプリケーションです。

Socket Mode で Slack Bolt アプリを稼働させつつ、独立したバックグラウンドスレッドが SQLite（`data/schedules.db`）を参照して予約投稿を自動実行します。

---

## 主な機能

* **統合モーダル（`/send`）**
  * **送信モードの切り替え**: `即時送信` / `単発予約` / `繰り返し予約`
  * **メッセージ・添付ファイル**: リッチテキスト入力（絵文字補完など）と最大 10 個（各最大 100MB、合計最大 200MB）のファイル添付に対応
  * **カスタム表示名・アイコン絵文字**: テキスト投稿時の表示名（`username`）とアイコン絵文字（`icon_emoji`）を指定可能
  * **投稿先チャンネル指定**: モーダル内で選択するか、未選択の場合はコマンド実行チャンネルを使用（`/send #channel` のようなメンション指定にも対応）
* **多彩な繰り返し条件（`Asia/Tokyo` 基準）**
  * 毎日 / 平日のみ（月〜金）
  * 毎週 / 毎週（曜日指定）
  * 毎 N 日 / 毎 N 週 / 毎 N か月
  * 毎月の指定日（1〜31日、月末を超える月は自動で末日調整） / 毎月末日
  * 毎月第 N 曜日 / 毎月最終曜日
  * 終了日（`YYYY-MM-DD`）の設定に対応
* **予約投稿の管理画面**
  * 対象チャンネルに紐づく予約一覧を表示（`投稿済`、`先`、`次回`、`周期`、`本文` を表示）
  * 予約の**編集**、**一時停止**、**再開**、**削除**に対応
  * 予約時の添付ファイルはサーバー上の `data/attachments/<schedule_id>/` に永続保存され、完了または削除時に整理されます

---

## 必要要件・セットアップ

### 1. Python と依存パッケージのインストール

Python 3.14 をインストールしてください。

プロジェクトのフォルダで以下のコマンドを実行します。

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 環境変数（.env）の設定

プロジェクトルートに `.env` ファイルを作成し、必要なトークンを設定します。

```env
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
# 任意: 全ユーザーの予約を管理できる管理者ユーザー ID（カンマ区切り）
ADMIN_USER_IDS=U01234567,U89ABCDEF
```

### 3. Slack App 側の設定目安

- Socket Mode: 有効化
- Slash Commands: `/send` を作成
- Interactivity & Shortcuts: 有効化
- 必要な Bot Token Scopes を設定し、ワークスペースへインストール


### 2. 環境変数（`.env`）の設定

プロジェクトルートに `.env` ファイルを作成し、必要なトークンを設定します。

```env
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
# 任意: 全ユーザーの予約を管理できる管理者ユーザー ID（カンマ区切り）
ADMIN_USER_IDS=U01234567,U89ABCDEF
```

### 3. Slack App 側の設定目安

* **Socket Mode**: 有効化（`SLACK_APP_TOKEN` 用に `connections:write` スコープを持つ App-Level Token を発行）
* **Slash Commands**: `/send` を作成
* **Interactivity & Shortcuts**: 有効化（モーダル送信やボタン操作の受信に必要）
* **Bot Token Scopes**: メッセージ投稿（`chat:write`、カスタム表示名・絵文字を使う場合は必要に応じて権限を確認）、ファイル取得・アップロード（`files:read`、`files:write` など利用する機能に応じたスコープ）を設定し、ワークスペースへインストール

---

## 起動方法

```bash
# Windows
.venv\Scripts\python.exe app.py

# macOS / Linux
.venv/bin/python app.py
```

起動すると SQLite データベースと保存ディレクトリが自動初期化され、バックグラウンドのスケジューラーワーカーと Socket Mode ハンドラーが開始されます。

---

## ディレクトリ構成

```text
.
satoyaniko_slackbot/
├── app.py
├── requirements.txt
├── README.md
├── .env.example
├── .gitignore
├── run.bat
└── data/                 # 起動時に自動生成
    ├── schedules.db
    └── attachments/
```

---

## License

This project is licensed under the GNU General Public License v3.0.
See the [LICENSE](LICENSE) file for details.
