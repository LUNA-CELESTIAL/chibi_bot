import os
import json
import time
import random
import asyncio
import logging
import datetime
import traceback
from pathlib import Path
from dotenv import load_dotenv

from video_generator import load_json_file
from multimodal_chat import is_autonomous_paused, is_gpu_busy_nonblocking

logger = logging.getLogger("SpinoffScheduler")

load_dotenv()

JST = datetime.timezone(datetime.timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
SPINOFF_PROMPTS_FILE = os.getenv("SPINOFF_PROMPTS_FILE", str(BASE_DIR / "spinoff_prompts.json"))
SPINOFF_STATE_FILE = Path(os.getenv("SPINOFF_STATE_FILE", str(BASE_DIR / "spinoff_daily_state.json")))
GPU_LOCK_FILE = os.getenv("GPU_LOCK_FILE", "/home/lulu/luna/luna_docker/shared_state/gpu.lock")

SPINOFF_DAILY_MIN = int(os.getenv("SPINOFF_DAILY_MIN", "3"))
SPINOFF_DAILY_MAX = int(os.getenv("SPINOFF_DAILY_MAX", "4"))
SPINOFF_START_HOUR = int(os.getenv("SPINOFF_START_HOUR", "10"))
SPINOFF_END_HOUR = int(os.getenv("SPINOFF_END_HOUR", "22"))
SPINOFF_MIN_GAP_MIN = int(os.getenv("SPINOFF_MIN_GAP_MIN", "110"))
SPINOFF_CHECK_INTERVAL_MIN = int(os.getenv("SPINOFF_CHECK_INTERVAL_MIN", "25"))


def _load_daily_state(today_str: str) -> dict:
    if SPINOFF_STATE_FILE.exists():
        try:
            data = json.loads(SPINOFF_STATE_FILE.read_text(encoding="utf-8"))
            if data.get("date") == today_str:
                return data
        except Exception as e:
            logger.warning(f"スピンオフ状態ファイル読み込みフォールバック: {e}")

    target_count = random.randint(SPINOFF_DAILY_MIN, max(SPINOFF_DAILY_MIN, SPINOFF_DAILY_MAX))
    state = {
        "date": today_str,
        "target_count": target_count,
        "posted_count": 0,
        "used_chars": [],
        "used_situations": [],
        "last_posted_ts": 0.0,
    }
    _save_daily_state(state)
    logger.info(f"📅 [Spinoff] 本日({today_str})の自律Vlog目標回数を {target_count}回 に決定しました。")
    return state


def _save_daily_state(state: dict):
    try:
        SPINOFF_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"スピンオフ状態保存エラー: {e}")


def _is_near_fixed_duty(now: datetime.datetime) -> bool:
    """9:00 / 12:00 / 20:00 の定時動画の前後15分は譲り合う"""
    for h in (9, 12, 20):
        target = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if abs((now - target).total_seconds()) <= 15 * 60:
            return True
    return False


def _choose_situations_for_hour(hour: int, available_sits: list[str]) -> list[str]:
    """時間帯に合った自然なシチュエーション優先度を返す"""
    if 10 <= hour < 15:
        preferred = ["cafe_tea", "reading_chill", "gaming_fun"]
    elif 15 <= hour < 19:
        preferred = ["evening_walk", "cafe_tea", "gaming_fun"]
    else:
        preferred = ["gaming_fun", "reading_chill", "cafe_tea"]

    valid = [s for s in preferred if s in available_sits]
    return valid if valid else available_sits


async def autonomous_spinoff_loop(bot, duty_characters: list[dict], main_channel_id: int, arrange_fn, video_task_cls):
    """
    A案：1日の活動時間(10:00〜22:00)の中で目標3〜4回、
    GPUが空いていて他の処理と競合しない隙間を見計らって自律投稿するループ。
    """
    await bot.wait_until_ready()
    logger.info(
        f"✨ 自律スピンオフ(ゆるVlog)スケジューラ起動 "
        f"(活動時間: {SPINOFF_START_HOUR}:00〜{SPINOFF_END_HOUR}:00 / 目標: {SPINOFF_DAILY_MIN}〜{SPINOFF_DAILY_MAX}回/日)"
    )

    # 起動直後の混雑を避けるため少し待機
    await asyncio.sleep(random.randint(90, 240))

    while not bot.is_closed():
        try:
            now = datetime.datetime.now(JST)
            today_str = now.strftime("%Y-%m-%d")
            state = _load_daily_state(today_str)

            if is_autonomous_paused():
                logger.info("⏸️ [Spinoff] 実験・休止モード中のため見送ります。")
            elif not (SPINOFF_START_HOUR <= now.hour < SPINOFF_END_HOUR):
                logger.debug(f"🌙 [Spinoff] 活動時間外 ({now.hour}時) のため待機します。")
            elif state["posted_count"] >= state["target_count"]:
                logger.debug(f"✅ [Spinoff] 本日の目標 ({state['posted_count']}/{state['target_count']}回) 達成済み。")
            elif _is_near_fixed_duty(now):
                logger.info("🚦 [Spinoff] 定時動画の優先時間帯のため空気読み待機します。")
            elif not bot.task_queue.empty():
                logger.info("🚦 [Spinoff] 他の動画タスクが待機中のためスキップします。")
            elif is_gpu_busy_nonblocking(GPU_LOCK_FILE):
                logger.info("🔒 [Spinoff] 共通GPUロック使用中のためスキップします。")
            else:
                elapsed_min = (time.time() - state.get("last_posted_ts", 0.0)) / 60.0
                remaining_quota = state["target_count"] - state["posted_count"]
                remaining_hours = max(1.0, SPINOFF_END_HOUR - (now.hour + now.minute / 60.0))

                # 最低間隔を満たしており、かつ残り時間に対して自然な確率で発火
                trigger_prob = min(0.85, max(0.35, remaining_quota / (remaining_hours * 1.5)))
                if elapsed_min >= SPINOFF_MIN_GAP_MIN and random.random() < trigger_prob:
                    prompts_data = load_json_file(SPINOFF_PROMPTS_FILE)
                    available_sits = list(prompts_data.get("situations", {}).keys()) or [
                        "cafe_tea", "reading_chill", "gaming_fun", "evening_walk"
                    ]

                    # 今日まだ出ていないキャラを優先選出
                    unused_chars = [c for c in duty_characters if c["key"] not in state["used_chars"]]
                    char_pool = unused_chars if unused_chars else duty_characters
                    char_info = random.choice(char_pool)

                    # 時間帯に合ったシチュエーション（なるべく今日未使用のもの）を選出
                    time_sits = _choose_situations_for_hour(now.hour, available_sits)
                    unused_sits = [s for s in time_sits if s not in state["used_situations"]]
                    sit_key = random.choice(unused_sits if unused_sits else time_sits)

                    sit_title = prompts_data["situations"][sit_key].get("title", sit_key)
                    base_speech = prompts_data["characters"][char_info["key"]]["speeches"][sit_key]
                    final_speech = arrange_fn(char_info["name"], base_speech, sit_title)

                    await bot.enqueue_task(video_task_cls(
                        priority=2,
                        task_type="SpinoffDuty",
                        channel_id=main_channel_id,
                        char_info=char_info,
                        situation_key=sit_key,
                        prompts_file=SPINOFF_PROMPTS_FILE,
                        custom_speech=final_speech,
                        embed_title=f"[✨ ゆるVlog] {char_info['name']}（{sit_title}）",
                        embed_header=f"💬 {char_info['name']} のひとりごと",
                        embed_color=0x1ABC9C
                    ))

                    state["posted_count"] += 1
                    state["used_chars"].append(char_info["key"])
                    state["used_situations"].append(sit_key)
                    state["last_posted_ts"] = time.time()
                    _save_daily_state(state)

                    logger.info(
                        f"🎬✨ [Spinoff] 自律Vlog投入完了 ({state['posted_count']}/{state['target_count']}回目): "
                        f"キャラ={char_info['name']} / テーマ={sit_title}"
                    )
        except Exception as e:
            logger.error(f"❌ [Spinoff] スケジューラエラー: {e}")
            logger.error(traceback.format_exc())

        # 毎回20〜35分の揺らぎを持たせてチェック
        wait_sec = random.randint(max(10, SPINOFF_CHECK_INTERVAL_MIN - 5) * 60, (SPINOFF_CHECK_INTERVAL_MIN + 10) * 60)
        await asyncio.sleep(wait_sec)
