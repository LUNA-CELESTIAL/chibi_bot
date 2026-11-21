import os
import sys
import datetime
import traceback
import logging
import asyncio
import discord
from dotenv import load_dotenv

from video_generator import generate_vlog_video, load_json_file, PROMPTS_FILE
from weather import get_tokyo_weather

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MorningDutyRun")

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

def build_morning_speech(char_key: str, weather_info: dict) -> tuple[str, str]:
    """
    weather.py の返り値 (weather_key, display_label) と
    prompts.json の speeches (sunny, cloudy, rainy) を安全に紐付ける
    """
    prompts_data = load_json_file(PROMPTS_FILE)
    char_info = prompts_data.get("characters", {}).get(char_key, {})
    
    weather_key = weather_info.get("weather_key", "sunny")
    display_label = weather_info.get("display_label", "晴れ")
    
    speeches = char_info.get("speeches", {})
    # JSON内に該当天気のセリフがなければ安全にフォールバック
    speech = speeches.get(weather_key, f"おはよー！今日の東京は{display_label}だよ！")
    
    return weather_key, speech

async def main():
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info(f"送信用Botログイン成功: {client.user.name}")
        
        channel = client.get_channel(TARGET_CHANNEL_ID)
        if not channel:
            logger.error(f"指定されたチャンネル ID ({TARGET_CHANNEL_ID}) が見つかりません。")
            await client.close()
            return

        today_ordinal = datetime.date.today().toordinal()

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
            # 1. 天気情報の取得
            weather_info = get_tokyo_weather()
            weather_key = weather_info["weather_key"]
            display_label = weather_info["display_label"]

            logger.info(f"当番投稿処理開始: [{char_info['name']}] (天気: {display_label}, キー: {weather_key})")
            
            # 2. ComfyUIへ日本語セリフ埋め込みプロンプトを投げて動画生成
            # 修正ポイント: タプル (video_path, speech_text) で正しく受け取る！
            video_path, speech_text = await generate_vlog_video(char_info["key"], weather_key)

            if not os.path.exists(video_path):
                raise FileNotFoundError(f"動画ファイルが見つかりません: {video_path}")

            # 3. DiscordへEmbedメッセージ＋動画を送信
            file = discord.File(video_path, filename=os.path.basename(video_path))
            embed = discord.Embed(
                title=f"☀️ 本日のモーニング当番：{char_info['name']} ({char_info['key'].upper()})",
                description=f"### 💬 {char_info['name']} からのボイスメッセージ\n> 『{speech_text}』\n\n**📍 今日の東京の天気：** `{display_label}`",
                color=discord.Color.blue() if weather_key == "rainy" else discord.Color.gold()
            )
            embed.set_footer(text=f"うさぎのお星さまファクトリー | 投稿日時: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            await channel.send(embed=embed, file=file)
            logger.info(f"[{char_info['name']}] の動画＆メッセージ送信完了: {video_path}")
        except Exception as e:
            logger.error(f"本番投稿処理中にエラーが発生しました: {e}")
            logger.error(traceback.format_exc())
            try:
                await channel.send(f"⚠️ 今朝の当番動画の自動送信に失敗したよ！\n```python\n{e}\n```")
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
            logger.info(f"Discord Bot 起動試行 ({attempt}/{max_retries})...")
            asyncio.run(main())
            # 正常に終了した場合はループを抜ける
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