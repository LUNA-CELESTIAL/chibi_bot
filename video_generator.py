import os
import sys
import json
import asyncio
import urllib.request
import urllib.parse
import logging
import datetime
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()
COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
OUTPUT_BASE_DIR = os.getenv("COMFYUI_OUTPUT_DIR", "/home/lulu/luna/luna_docker/draw_agents/outputs/video")

PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "prompts.json")
WORKFLOW_FILE = os.path.join(os.path.dirname(__file__), "MiniMax.json")

def load_json_file(file_path: str) -> dict:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"必要な設定ファイルが見つかりません: {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)

def build_dynamic_prompt(
    character_name: str, 
    situation_key: str = "sunny", 
    prompts_file: str = PROMPTS_FILE,
    custom_speech: str = None,
    duration_sec: float = 5.0
) -> tuple[str, str, str]:
    data = load_json_file(prompts_file)
    char_key = character_name.lower()

    if char_key not in data["characters"]:
        raise ValueError(f"キャラキー '{character_name}' が {os.path.basename(prompts_file)} に存在しません。")
    if situation_key not in data["situations"]:
        raise ValueError(f"シチュエーション '{situation_key}' が {os.path.basename(prompts_file)} に存在しません。")

    char_info = data["characters"][char_key]
    sit_info = data["situations"][situation_key]
    common_base = data["common_base"]

    today_ordinal = datetime.date.today().toordinal()

    # custom_speech が渡された場合はそれを優先（AIアレンジ用）
    if custom_speech:
        speech_text = custom_speech
    elif situation_key == "night":
        night_speeches = char_info.get("night_speeches", [])
        if night_speeches:
            speech_pattern_index = (today_ordinal // 8) % len(night_speeches)
            speech_text = night_speeches[speech_pattern_index]
        else:
            speech_text = "今日もお疲れ様！ゆっくり休んでね！"
    else:
        speeches = char_info.get("speeches", {})
        speech_text = speeches.get(situation_key, "やっほー！今日もいい日だね！")

    action_prompt = sit_info["action"].format(speech=speech_text)
    if duration_sec >= 9.0:
        action_prompt = action_prompt.replace("00:02-00:05", "00:02-00:10").replace("5 seconds", "10 seconds")

    full_prompt = (
        f"{common_base}. Character design: {char_info['visual']}. "
        f"Motion: {action_prompt} Atmosphere: {sit_info['mood']}."
    )
    return char_info["image"], speech_text, full_prompt

async def generate_vlog_video(
    character_name: str, 
    situation_key: str = "sunny",
    prompts_file: str = PROMPTS_FILE,
    custom_speech: str = None,
    duration_sec: float = 5.0
) -> tuple[str, str]:
    image_rel_path, speech_text, prompt_text = build_dynamic_prompt(
        character_name, situation_key, prompts_file, custom_speech, duration_sec=duration_sec
    )

    workflow = load_json_file(WORKFLOW_FILE)

    if "114" in workflow and "inputs" in workflow["114"]:
        workflow["114"]["inputs"]["image"] = image_rel_path
    else:
        raise KeyError("MiniMax.json 内に '114' (LoadImage) ノードが見つかりません。")

    if "105:104" in workflow and "inputs" in workflow["105:104"]:
        workflow["105:104"]["inputs"]["prompt"] = prompt_text
    else:
        raise KeyError("MiniMax.json 内に '105:104' (MiniMaxH3ImageToVideo) ノードが見つかりません。")

    if "105:111" in workflow and "inputs" in workflow["105:111"]:
        workflow["105:111"]["inputs"]["value"] = float(duration_sec)
    else:
        logger.warning("MiniMax.json 内に '105:111' (Float duration) ノードが見つからないためデフォルト尺を使用します。")

    payload = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(f"{COMFYUI_URL}/prompt", data=payload, headers={"Content-Type": "application/json"})

    logger.info(f"[{character_name}] ComfyUIへジョブ送信中... (尺: {duration_sec}秒, シチュエーション: {situation_key}, 発話: {speech_text})")

    try:
        with urllib.request.urlopen(req) as response:
            res_json = json.loads(response.read().decode("utf-8"))
            prompt_id = res_json.get("prompt_id")
            logger.info(f"ジョブ送信完了 (prompt_id: {prompt_id})")
    except urllib.error.HTTPError as e:
        error_detail = e.read().decode("utf-8", errors="replace")
        logger.error(f"ComfyUI API {e.code}エラー詳細: {error_detail}")
        raise ConnectionError(f"ComfyUI バリデーションエラー ({e.code}): {error_detail}")
    except Exception as e:
        logger.error(f"ComfyUI API通信エラー: {e}")
        raise ConnectionError(f"ComfyUI ({COMFYUI_URL}) への接続に失敗しました: {e}")

    video_path = await _wait_for_completion(prompt_id)
    return video_path, speech_text

async def _wait_for_completion(prompt_id: str, timeout: int = 600) -> str:
    start_time = asyncio.get_event_loop().time()
    logger.info(f"動画生成完了を待機中 (prompt_id: {prompt_id})...")

    while (asyncio.get_event_loop().time() - start_time) < timeout:
        await asyncio.sleep(3)
        try:
            req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
            with urllib.request.urlopen(req) as response:
                history = json.loads(response.read().decode("utf-8"))

                if prompt_id in history:
                    outputs = history[prompt_id].get("outputs", {})
                    if "92" in outputs:
                        node_output = outputs["92"]
                        video_list = node_output.get("gifs") or node_output.get("videos") or node_output.get("images")
                        if video_list and len(video_list) > 0:
                            filename = video_list[0]["filename"]
                            full_path = os.path.join(OUTPUT_BASE_DIR, filename)

                            if os.path.exists(full_path):
                                return full_path
                            else:
                                fallback_path = os.path.join(OUTPUT_BASE_DIR, os.path.basename(filename))
                                if os.path.exists(fallback_path):
                                    return fallback_path
                                logger.warning(f"ファイル未発見 - 期待パス: {full_path}")
        except Exception as e:
            logger.warning(f"ポーリング監視一時エラー: {e}")

    raise TimeoutError(f"動画生成がタイムアウトしました ({timeout}秒経過)。")