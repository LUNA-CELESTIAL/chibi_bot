import os
import sys
import time
import fcntl
import random
import datetime
import traceback
import logging
import asyncio
import urllib.request
from pathlib import Path
from dataclasses import dataclass, field
import discord
from discord.ext import tasks
from dotenv import load_dotenv

from video_generator import generate_vlog_video, load_json_file, PROMPTS_FILE
from weather import get_tokyo_weather
from rss_friend_trigger import pick_rss_friend_share
from multimodal_chat import handle_voice_chat_message, voice_rss_background_loop, is_autonomous_paused
from spinoff_scheduler import autonomous_spinoff_loop

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
logger = logging.getLogger("ChibiOrchestrator")

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
MAIN_CHANNEL_ID_STR = os.getenv("DISCORD_CHANNEL_ID")
VOICE_CHAT_CHANNEL_ID_STR = os.getenv("DISCORD_VOICE_CHAT_CHANNEL_ID", "").strip()
COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
GPU_COOLDOWN_SEC = int(os.getenv("GPU_COOLDOWN_SEC", "45"))
COMFYUI_HEALTH_RETRY_SEC = int(os.getenv("COMFYUI_HEALTH_RETRY_SEC", "15"))
USE_OLLAMA_ARRANGE = os.getenv("USE_OLLAMA_ARRANGE", "false").strip().lower() == "true"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.8-Q2:latest")
RSS_CHECK_INTERVAL_MIN = int(os.getenv("RSS_CHECK_INTERVAL_MIN", "150"))
# RSS友達シェア（動画版）は 2026-11-22 の編成見直しで一旦停止。ニュースは #lunachannel の声とテキスト側で扱う。再開する場合は ENABLE_RSS_FRIEND_VIDEO=true を設定する。
RSS_FRIEND_VIDEO_ENABLED = os.getenv("ENABLE_RSS_FRIEND_VIDEO", "false").strip().lower() == "true"
GPU_LOCK_FILE = os.getenv("GPU_LOCK_FILE", "/home/lulu/luna/luna_docker/shared_state/gpu.lock")

SPINOFF_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "spinoff_prompts.json")

if not TOKEN or not MAIN_CHANNEL_ID_STR:
    logger.error("必須環境変数 (DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID) が未設定です。")
    sys.exit(1)

MAIN_CHANNEL_ID = int(MAIN_CHANNEL_ID_STR)
VOICE_CHAT_CHANNEL_ID = int(VOICE_CHAT_CHANNEL_ID_STR) if VOICE_CHAT_CHANNEL_ID_STR else None

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
SPINOFF_SITUATIONS = ["cafe_tea", "reading_chill", "gaming_fun", "evening_walk"]

JST = datetime.timezone(datetime.timedelta(hours=9))


def _acquire_flock_sync(lock_path: str):
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_flock_sync(fd):
    if fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        except Exception as e:
            logger.warning(f"ロック解放時の警告: {e}")


@dataclass(order=True)
class VideoTask:
    priority: int
    created_at: float = field(default_factory=time.time)
    task_type: str = field(compare=False, default="")
    channel_id: int = field(compare=False, default=0)
    char_info: dict = field(compare=False, default_factory=dict)
    situation_key: str = field(compare=False, default="sunny")
    prompts_file: str = field(compare=False, default=PROMPTS_FILE)
    custom_speech: str | None = field(compare=False, default=None)
    embed_title: str = field(compare=False, default="")
    embed_header: str = field(compare=False, default="")
    embed_footer_extra: str = field(compare=False, default="")
    embed_color: int = field(compare=False, default=0xFFD700)
    duration_sec: float = field(compare=False, default=5.0)


def check_comfyui_alive() -> bool:
    try:
        req = urllib.request.Request(f"{COMFYUI_URL}/system_stats")
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status == 200
    except Exception:
        return False


def arrange_speech_with_ollama(char_name: str, base_speech: str, situation_title: str) -> str:
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
        logger.warning(f"Ollamaアレンジスキップ（定型文を使用）: {e}")
    return base_speech


class ChibiOrchestratorBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.task_queue: asyncio.PriorityQueue[VideoTask] = asyncio.PriorityQueue()
        self.worker_task: asyncio.Task | None = None

    async def setup_hook(self):
        self.worker_task = asyncio.create_task(self.gpu_worker())
        self.schedule_loop.start()
        if RSS_FRIEND_VIDEO_ENABLED:
            self.rss_friend_loop.start()
        else:
            logger.info("📴 RSS友達シェア（動画版）は編成から外れているため起動しません。")
        asyncio.create_task(voice_rss_background_loop(self, VOICE_CHAT_CHANNEL_ID))
        asyncio.create_task(autonomous_spinoff_loop(self, DUTY_CHARACTERS, MAIN_CHANNEL_ID, arrange_speech_with_ollama, VideoTask))
        logger.info("GPU交通整理ワーカー・定時スケジューラ・RSS友達トリガーを起動しました。")

    async def enqueue_task(self, task: VideoTask):
        await self.task_queue.put(task)
        logger.info(f"キュー登録完了: [{task.task_type}] キャラ={task.char_info.get('name')} 優先度={task.priority} (待機数: {self.task_queue.qsize()})")

    async def gpu_worker(self):
        await self.wait_until_ready()
        logger.info("GPUワーカー待機開始（共通ファイルロック＋クールダウン制御）")
        while not self.is_closed():
            task: VideoTask = await self.task_queue.get()
            lock_fd = None
            try:
                logger.info(f"🔒 [ChibiBot] 共通GPUロック待機中 ({GPU_LOCK_FILE})...")
                lock_fd = await asyncio.to_thread(_acquire_flock_sync, GPU_LOCK_FILE)
                logger.info("🟢 [ChibiBot] 共通GPUロック取得完了！")

                while not check_comfyui_alive():
                    logger.warning(f"ComfyUI ({COMFYUI_URL}) が応答しません。{COMFYUI_HEALTH_RETRY_SEC}秒待機して再確認します...")
                    await asyncio.sleep(COMFYUI_HEALTH_RETRY_SEC)

                channel = self.get_channel(task.channel_id)
                if not channel:
                    logger.error(f"送信先チャンネルが見つかりません: {task.channel_id}")
                    continue

                logger.info(f"=== ジョブ実行開始: [{task.task_type}] {task.char_info['name']} ===")
                video_path, speech_text = await generate_vlog_video(
                    character_name=task.char_info["key"],
                    situation_key=task.situation_key,
                    prompts_file=task.prompts_file,
                    custom_speech=task.custom_speech,
                    duration_sec=task.duration_sec
                )

                if not os.path.exists(video_path):
                    raise FileNotFoundError(f"生成された動画ファイルが見つかりません: {video_path}")

                desc = f"### {task.embed_header}\n> 『{speech_text}』"
                if task.embed_footer_extra:
                    desc += f"\n\n{task.embed_footer_extra}"

                file = discord.File(video_path, filename=os.path.basename(video_path))
                embed = discord.Embed(title=task.embed_title, description=desc, color=task.embed_color)
                embed.set_footer(text=f"うさぎのお星さまファクトリー | 投稿日時: {datetime.datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")

                await channel.send(embed=embed, file=file)
                logger.info(f"=== ジョブ送信完了: [{task.task_type}] {task.char_info['name']} ===")

            except Exception as e:
                logger.error(f"ジョブ実行エラー [{task.task_type}]: {e}")
                logger.error(traceback.format_exc())
                channel = self.get_channel(task.channel_id)
                if channel:
                    try:
                        await channel.send(f"⚠️ [{task.task_type}] の動画生成・送信に失敗しました:\n```python\n{e}\n```")
                    except Exception:
                        pass
            finally:
                self.task_queue.task_done()
                logger.info(f"❄️ GPUクールダウン待機中 ({GPU_COOLDOWN_SEC}秒)...")
                await asyncio.sleep(GPU_COOLDOWN_SEC)
                if lock_fd:
                    await asyncio.to_thread(_release_flock_sync, lock_fd)
                    logger.info("🔓 [ChibiBot] 共通GPUロックを解放しました。")

    @tasks.loop(time=[
        datetime.time(hour=9, minute=0, tzinfo=JST),
        datetime.time(hour=12, minute=0, tzinfo=JST),
        datetime.time(hour=20, minute=0, tzinfo=JST),
    ])
    async def schedule_loop(self):
        if is_autonomous_paused():
            logger.info("⏸️ [ScheduleLoop] 実験・休止モード中のため定時動画タスクをスキップします。")
            return
        now = datetime.datetime.now(JST)
        today_ordinal = now.date().toordinal()
        char_info = DUTY_CHARACTERS[today_ordinal % len(DUTY_CHARACTERS)]

        if now.hour == 9:
            weather_info = get_tokyo_weather()
            w_key = weather_info.get("weather_key", "sunny")
            d_label = weather_info.get("display_label", "晴れ")
            await self.enqueue_task(VideoTask(
                priority=1,
                task_type="MorningDuty",
                channel_id=MAIN_CHANNEL_ID,
                char_info=char_info,
                situation_key=w_key,
                prompts_file=PROMPTS_FILE,
                embed_title=f"[☀️ 朝当番] {char_info['name']} ({char_info['key'].upper()})",
                embed_header=f"💬 {char_info['name']} からのモーニングボイス",
                embed_footer_extra=f"**📍 今日の東京の天気：** `{d_label}`",
                embed_color=discord.Color.blue().value if w_key == "rainy" else discord.Color.gold().value
            ))

        elif now.hour == 12:
            logger.info("ℹ️ [ScheduleLoop] 12:00のSpinoffDutyは自律スピンオフスケジューラ(1日3〜4回)へ一本化済みのためスキップします。")

        elif now.hour == 20:
            await self.enqueue_task(VideoTask(
                priority=1,
                task_type="NightDuty",
                channel_id=MAIN_CHANNEL_ID,
                char_info=char_info,
                situation_key="night",
                prompts_file=PROMPTS_FILE,
                embed_title=f"[🌙 夜当番] {char_info['name']} ({char_info['key'].upper()})",
                embed_header=f"💬 {char_info['name']} からのお疲れ様メッセージ",
                embed_color=discord.Color.purple().value
            ))

    @tasks.loop(minutes=RSS_CHECK_INTERVAL_MIN)
    async def rss_friend_loop(self):
        if self.rss_friend_loop.current_loop == 0:
            return
        if is_autonomous_paused():
            logger.info("⏸️ [RssFriendLoop] 実験・休止モード中のためRSS友達シェアをスキップします。")
            return

        now = datetime.datetime.now(JST)
        if not (datetime.time(9, 30) <= now.time() <= datetime.time(19, 30)):
            return

        await asyncio.sleep(random.randint(300, 1500))

        if not self.task_queue.empty():
            logger.info("他のタスクが待機中のため、今回のRSS友達シェアはスキップします。")
            return

        share_data = await asyncio.to_thread(pick_rss_friend_share, DUTY_CHARACTERS)
        if not share_data:
            logger.info("RSS友達トリガー：新着記事なしのため待機します。")
            return

        c_info = share_data["char_info"]
        footer_lines = []
        if share_data.get("fact_summary"):
            footer_lines.append(f"📌 **要点:** {share_data['fact_summary']}")
        if share_data.get("thought"):
            footer_lines.append(f"💭 **考察:** {share_data['thought']}")
        footer_lines.append(f"🔗 **[{share_data['article_title']}]({share_data['article_url']})**")

        await self.enqueue_task(VideoTask(
            priority=3,
            task_type="RssFriendShare",
            channel_id=MAIN_CHANNEL_ID,
            char_info=c_info,
            situation_key=share_data["situation_key"],
            prompts_file=share_data["prompts_file"],
            custom_speech=share_data["speech"],
            embed_title=f"[📰 友達シェア・10秒解説] {c_info['name']} からの気になる記事",
            embed_header=f"💬 {c_info['name']} が記事を見つけたよ！",
            embed_footer_extra="\n".join(footer_lines),
            embed_color=discord.Color.orange().value,
            duration_sec=share_data.get("duration_sec", 10.0)
        ))

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if VOICE_CHAT_CHANNEL_ID and message.channel.id == VOICE_CHAT_CHANNEL_ID:
            await handle_voice_chat_message(message)

    async def on_ready(self):
        logger.info(f"常駐オーケストレーター起動完了: {self.user.name} (ID: {self.user.id}) | VoiceChat={VOICE_CHAT_CHANNEL_ID}")


if __name__ == "__main__":
    bot = ChibiOrchestratorBot()
    bot.run(TOKEN)
