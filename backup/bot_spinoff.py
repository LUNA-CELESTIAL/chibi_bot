import os
import sys
import time
import datetime
import traceback
import logging
import asyncio
import discord
from dotenv import load_dotenv

from video_generator import generate_vlog_video, load_json_file

# Ollamaのインポート（自律アレンジ用：未起動でも定型文に安全フォールバックする）
try:
    import ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("SpinoffDutyRun")

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
SPINOFF_CHANNEL_ID_STR = os.getenv("DISCORD_SPINOFF_CHANNEL_ID")
FORCE_CHAR = os.getenv("FORCE_CHAR", "").strip().lower()
FORCE_SIT = os.getenv("FORCE_SIT", "").strip().lower()
USE_OLLAMA_ARRANGE = os.getenv("USE_OLLAMA_ARRANGE", "false").strip().lower() == "true"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.8-Q2:latest")

SPINOFF_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "spinoff_prompts.json")

if not TOKEN or not SPINOFF_CHANNEL_ID_STR:
    logger.error("エラー: .env に DISCORD_BOT_TOKEN または DISCORD_SPINOFF_CHANNEL_ID が設定されていません。")
    sys.exit(1)

TARGET_CHANNEL_ID = int(SPINOFF_CHANNEL_ID_STR)

DUTY_CHARACTERS = [
    {"key": "hina", "name": "ひな"},
    {"key": "kohaku", "name": "こはく"},
    {"key": "luna", "name": "ルナ"},
    {"key": "nano", "name": "ナノ"},
    {"key": "selene", "name": "セレーネ"},
    {"key": "sirius", "name": "シリウス"},
    {"key": "stella", "name": "ステラ"},
    {"key": "spica", "name": "スピカ"},
]

SPINOFF_SITUATIONS = [
    "cafe_tea",
    "reading_chill",
    "gaming_fun",
    "evening_walk"
]

def arrange_speech_with_ollama(char_name: str, base_speech: str, situation_title: str) -> str:
    """
    Ollamaを使って定型文をベースに少しだけ自然なゆらぎ（アレンジ）を加える。
    失敗時や無効時はそのまま base_speech を返す安全設計。
    """
    if not USE_OLLAMA_ARRANGE or not HAS_OLLAMA:
        return base_speech

    prompt = (
        f"あなたはアニメキャラクター「{char_name}」です。\n"
        f"シチュエーション: {situation_title}\n"
        f"元のセリフ: 「{base_speech}」\n\n"
        f"この元のセリフの口調や意味を保ったまま、5秒で話せる長さ（30〜45文字程度）で、"
        f"少しだけ語尾や表現を変えた自然な日本語のセリフを1つだけ出力してください。"
        f"カギ括弧や説明は一切不要で、セリフのテキストのみを出力すること。"
    )
    try:
        response = ollama.chat(model=OLLAMA_MODEL, messages=[{"role": "user", "content": prompt}])
        arranged = response["message"]["content"].strip().strip("「」『』")
        if 10 <= len(arranged) <= 65:
            logger.info(f"Ollama自律アレンジ適用: {base_speech} -> {arranged}")
            return arranged
    except Exception as e:
        logger.warning(f"Ollamaアレンジをスキップして定型文を使用します: {e}")

    return base_speech

async def main():
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info(f"派生投稿用Botログイン成功: {client.user.name}")
        
        channel = client.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            logger.error(f"指定された派生チャンネル ID ({TARGET_CHANNEL_ID}) が見つかりません。")
            await client.close()
            return

        today_ordinal = datetime.date.today().toordinal()

        # 1. キャラ決定（8日周期）
        if FORCE_CHAR:
            char_info = next((c for c in DUTY_CHARACTERS if c["key"] == FORCE_CHAR), DUTY_CHARACTERS[0])
        else:
            duty_index = today_ordinal % len(DUTY_CHARACTERS)
            char_info = DUTY_CHARACTERS[duty_index]

        # 2. シチュエーション決定（キャラが一巡するごとに次のシチュエーションへシフト -> 32日間被りなし！）
        if FORCE_SIT and FORCE_SIT in SPINOFF_SITUATIONS:
            sit_key = FORCE_SIT
        else:
            sit_index = (today_ordinal // len(DUTY_CHARACTERS)) % len(SPINOFF_SITUATIONS)
            sit_key = SPINOFF_SITUATIONS[sit_index]

        try:
            prompts_data = load_json_file(SPINOFF_PROMPTS_FILE)
            sit_title = prompts_data["situations"][sit_key].get("title", sit_key)
            base_speech = prompts_data["characters"][char_info["key"]]["speeches"][sit_key]

            # 必要ならOllamaでセリフにゆらぎを与える
            final_speech = arrange_speech_with_ollama(char_info["name"], base_speech, sit_title)

            logger.info(f"派生投稿開始: [{char_info['name']}] / テーマ: {sit_title} ({sit_key})")
            
            video_path, speech_text = await generate_vlog_video(
                character_name=char_info["key"],
                situation_key=sit_key,
                prompts_file=SPINOFF_PROMPTS_FILE,
                custom_speech=final_speech
            )

            if not os.path.exists(video_path):
                raise FileNotFoundError(f"動画ファイルが見つかりません: {video_path}")

            file = discord.File(video_path, filename=os.path.basename(video_path))
            embed = discord.Embed(
                title=f"✨ ゆるっと日常Vlog：{char_info['name']} ({sit_title})",
                description=f"### 💬 {char_info['name']} のひとりごと\n> 『{speech_text}』",
                color=discord.Color.teal()
            )
            embed.set_footer(text=f"うさぎのお星さまファクトリー | 投稿日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            await channel.send(embed=embed, file=file)
            logger.info(f"[{char_info['name']}] の派生動画送信完了: {video_path}")

        except Exception as e:
            logger.error(f"派生投稿処理中にエラーが発生しました: {e}")
            logger.error(traceback.format_exc())
            try:
                await channel.send(f"⚠️ 派生動画の投稿に失敗したよ！\n```python\n{e}\n```")
            except Exception as send_err:
                logger.error(f"エラー通知送信失敗: {send_err}")
        finally:
            await client.close()

    await client.start(TOKEN)

if __name__ == "__main__":
    max_retries = 3
    retry_delay = 5

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"派生 Discord Bot 起動試行 ({attempt}/{max_retries})...")
            asyncio.run(main())
            break
        except Exception as e:
            logger.warning(f"接続または実行中にエラーが発生しました ({attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                logger.info(f"{retry_delay}秒後に再試行します...")
                time.sleep(retry_delay)
            else:
                logger.error("最大リトライ回数に達したため、処理を終了します。")
                sys.exit(1)