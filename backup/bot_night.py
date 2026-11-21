import os
import sys
import datetime
import traceback
import logging
import asyncio
import discord
from dotenv import load_dotenv

from video_generator import generate_vlog_video, load_json_file, PROMPTS_FILE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("NightDutyRun")

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID_STR = os.getenv("DISCORD_CHANNEL_ID")
FORCE_CHAR = os.getenv("FORCE_CHAR", "").strip().lower()

if not TOKEN or not CHANNEL_ID_STR:
    logger.error("エラー: .env に DISCORD_BOT_TOKEN または DISCORD_CHANNEL_ID が設定されていません。")
    sys.exit(1)

TARGET_CHANNEL_ID = int(CHANNEL_ID_STR)

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

def get_night_speech(char_key: str, today_ordinal: int) -> str:
    """
    24日周期でメッセージが被らないようにローテーション計算して取得する
    """
    prompts_data = load_json_file(PROMPTS_FILE)
    char_info = prompts_data.get("characters", {}).get(char_key, {})
    night_speeches = char_info.get("night_speeches", [])
    
    if not night_speeches:
        return "今日もお疲れ様！ゆっくり休んでね！"

    # 当番が1巡（8日）するごとに、パターン(0->1->2)をシフトさせる！
    speech_pattern_index = (today_ordinal // len(DUTY_CHARACTERS)) % len(night_speeches)
    return night_speeches[speech_pattern_index]

async def main():
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info(f"夜投稿用Botログイン成功: {client.user.name}")
        
        channel = client.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            logger.error(f"指定されたチャンネル ID ({TARGET_CHANNEL_ID}) が見つかりません。")
            await client.close()
            return

        today_ordinal = datetime.date.today().toordinal()

        # FORCE_CHAR が指定されていれば優先、なければ日替わりローテーション
        if FORCE_CHAR:
            char_info = next((c for c in DUTY_CHARACTERS if c["key"] == FORCE_CHAR), None)
            if not char_info:
                logger.warning(f"指定された FORCE_CHAR '{FORCE_CHAR}' が見つからないため自動計算にします。")
                duty_index = today_ordinal % len(DUTY_CHARACTERS)
                char_info = DUTY_CHARACTERS[duty_index]
        else:
            duty_index = today_ordinal % len(DUTY_CHARACTERS)
            char_info = DUTY_CHARACTERS[duty_index]

        try:
            # 1. 夜専用セリフの決定（ローテーション）
            speech_text = get_night_speech(char_info["key"], today_ordinal)

            logger.info(f"夜の当番投稿処理開始: [{char_info['name']}] (シチュエーション: night)")
            
            # 2. ComfyUIへ夜プロンプト('night')を投げて動画生成
            # ※video_generator.py は 'night' を渡せば 'situations.night' と 'night_speeches' を正しく扱うように連携
            video_path, _ = await generate_vlog_video(char_info["key"], "night")

            if not os.path.exists(video_path):
                raise FileNotFoundError(f"動画ファイルが見つかりません: {video_path}")

            # 3. DiscordへEmbedメッセージ＋動画を送信
            file = discord.File(video_path, filename=os.path.basename(video_path))
            embed = discord.Embed(
                title=f"🌙 本日の夜当番：{char_info['name']} ({char_info['key'].upper()})",
                description=f"### 💬 {char_info['name']} からのお疲れ様メッセージ\n> 『{speech_text}』",
                color=discord.Color.purple()
            )
            embed.set_footer(text=f"うさぎのお星さまファクトリー | 投稿日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            await channel.send(embed=embed, file=file)
            logger.info(f"[{char_info['name']}] の夜動画＆メッセージ送信完了: {video_path}")

        except Exception as e:
            logger.error(f"夜の投稿処理中にエラーが発生しました: {e}")
            logger.error(traceback.format_exc())
            try:
                await channel.send(f"⚠️ 今夜の当番動画の自動送信に失敗したよ！\n```python\n{e}\n```")
            except Exception as send_err:
                logger.error(f"エラー通知送信失敗: {send_err}")
        finally:
            await client.close()

    await client.start(TOKEN)

if __name__ == "__main__":
    max_retries = 3
    retry_delay = 5  # 秒

    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"夜用 Discord Bot 起動試行 ({attempt}/{max_retries})...")
            asyncio.run(main())
            break
        except Exception as e:
            logger.warning(f"接続または実行中にエラーが発生しました ({attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                logger.info(f"{retry_delay}秒後に再試行します...")
                import time
                time.sleep(retry_delay)
            else:
                logger.error("最大リトライ回数に達したため、処理を終了します。")
                sys.exit(1)