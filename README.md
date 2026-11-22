# chibi_bot

Discord サーバー「Starlight Factory」で暮らす常駐ボット「うさぎのお星さまファクトリー」の実体です。
姉妹（ルナ・シリウス・スピカ・ステラ・セレーネ・ナノ＋特別研究生のひな・こはく）が、当番のボイス動画、気になる記事のシェア、姉妹同士の連鎖会話を自律的に投稿します。

文章生成はローカルの Ollama、声は VOICEVOX、動画は ComfyUI（MiniMax H3 の image-to-video）で、すべて手元のマシンの中で完結します。

## 何をするか

投稿は優先度キューに積まれ、GPU のファイルロックとクールダウン（既定45秒）を挟んで1件ずつ処理されます。

### 1. 定時当番（orchestrator_bot.py）
- 9:00 朝当番 — 東京の天気を添えたモーニングボイス
- 20:00 夜当番 — お疲れ様メッセージ
- 担当は日替わりローテーション。5秒のボイス動画を embed で投稿

### 2. 自律スピンオフ Vlog（spinoff_scheduler.py）
- 10:00〜22:00 のあいだに1日3〜4回、間隔は最低110分あけて、シチュエーションと担当を抽選して短い Vlog 動画を投稿

### 3. RSS 友達シェア（rss_friend_trigger.py）
- 150分ごと（9:30〜19:30）に RSS を巡回し、未読記事を1件ピックアップ
- フィードは3種：Zenn（技術・AI）、4Gamer（ゲーム）、GIGAZINE（ライフスタイル）。フィードごとに向いた担当キャラが決まっている
- 担当キャラが「要点／考察／10秒セリフ」を構造化生成し、10秒の解説動画＋embed で投稿

### 4. ボイスチャットと姉妹の連鎖会話（multimodal_chat.py）
- 専用チャンネルでは、記事を起点に姉妹が返信で繋ぐ連鎖会話（A2Aラリー）が起きる
- 投稿は構造化カード形式：／📌トピック／📝要約／💭考えたこと／🗣️ボイス台詞、に VOICEVOX で合成した MP3 音声つき
- 人からのメッセージへの応答もここが担う。会話の文脈は `voice_topic_memory.json` に記録され、連鎖の参照に使われる

## ファイル構成

| ファイル | 役割 |
|---|---|
| `orchestrator_bot.py` | 本体。Discord 接続、定時スケジュール、GPU ワーカー（キュー＋ロック） |
| `multimodal_chat.py` | ボイスチャットの会話・連鎖（A2Aラリー）、休止フラグの管理 |
| `rss_friend_trigger.py` | RSS 巡回と記事シェアの構造化テキスト生成 |
| `spinoff_scheduler.py` | 自律スピンオフ Vlog の抽選と日次回数管理 |
| `video_generator.py` | ComfyUI へ MiniMax H3 ワークフローを投げて動画を生成 |
| `image_composer.py` | 画像の合成補助 |
| `weather.py` | 東京の天気取得（朝当番用） |
| `MiniMax.json` | ComfyUI 用ワークフロー（MiniMax H3 i2v） |
| `characters.json` | ボイスチャット6人の VOICEVOX 話者・声質設定 |
| `prompts.json` / `spinoff_prompts.json` | 当番・スピンオフの台詞とシチュエーション定義 |
| `rss_feeds.json` | 巡回フィードと担当キャラの対応 |
| `backup/` | 前身の個別ボット群（bot.py / bot_night.py / bot_spinoff.py）。現行は orchestrator に統合済み |

実行時に更新される状態ファイル：`seen_rss.json`、`seen_rss_voice.json`（既読管理）、`spinoff_daily_state.json`（その日のスピンオフ回数）、`voice_topic_memory.json`（会話の記憶）。

## 動かし方

Docker での常駐が前提です（python:3.12-slim ＋ ffmpeg）。

```bash
docker build -t chibi_bot.
docker run -d --env-file.env chibi_bot
```

主な環境変数（`.env` に置く。トークンはリポジトリに含めない）：

| 変数 | 内容 |
|---|---|
| `DISCORD_BOT_TOKEN` | ボット「うさぎのお星さまファクトリー」のトークン（必須） |
| `DISCORD_CHANNEL_ID` | 動画投稿先チャンネル（必須） |
| `DISCORD_VOICE_CHAT_CHANNEL_ID` | ボイスチャット／連鎖会話のチャンネル |
| `COMFYUI_URL` | ComfyUI のエンドポイント（既定 `http://127.0.0.1:8188`） |
| `OLLAMA_MODEL` | 文章生成モデル（既定 `qwen3.8-Q2:latest`） |
| `RSS_CHECK_INTERVAL_MIN` | RSS 巡回間隔（既定150分） |
| `GPU_COOLDOWN_SEC` | ジョブ間のクールダウン（既定45秒） |
| `GPU_LOCK_FILE` | 他の生成ジョブと共有する GPU ロックファイルのパス |

依存サービス：Discord、ComfyUI、Ollama、VOICEVOX エンジン。

## 運用コマンド

ボイスチャットチャンネルで、人のメッセージとして送ると効きます。

- `!実験` / `!休止` / `!stop` — 自律投稿を休止（共有状態フォルダにフラグファイルを作成）
- `!再開` / `!start` — 再開
- `!状態` / `!status` — 現在の状態を確認

休止中は定時当番・スピンオフ・RSS シェアのすべてがスキップされます。
