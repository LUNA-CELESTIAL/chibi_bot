import os
import re
import json
import time
import fcntl
import random
import base64
import asyncio
import logging
import datetime
import traceback
import subprocess
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
import discord
from personas import PERSONAS
from dotenv import load_dotenv

try:
    import ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False

logger = logging.getLogger("MultimodalChat")

load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.8-Q2:latest")
VOICEVOX_URL = os.getenv("VOICEVOX_URL", "http://voicevox:50021")
SHARED_STATE_DIR = Path(os.getenv("SHARED_STATE_DIR", "/home/lulu/luna/luna_docker/shared_state"))
GPU_LOCK_FILE = os.getenv("GPU_LOCK_FILE", str(SHARED_STATE_DIR / "gpu.lock"))
PAUSE_FLAG_FILE = SHARED_STATE_DIR / "pause_autonomous.flag"

BASE_DIR = Path(__file__).resolve().parent
CHARACTERS_JSON_PATH = Path(os.getenv("CHARACTERS_JSON_PATH", str(BASE_DIR / "characters.json")))
RSS_FEEDS_PATH = BASE_DIR / "rss_feeds.json"
SEEN_VOICE_RSS_PATH = BASE_DIR / "seen_rss_voice.json"
TOPIC_MEMORY_PATH = BASE_DIR / "voice_topic_memory.json"
TEMP_VOICE_DIR = Path(os.getenv("TEMP_VOICE_DIR", "/tmp/chibi_voice"))

VOICE_RSS_MIN_INTERVAL_MIN = int(os.getenv("VOICE_RSS_MIN_INTERVAL_MIN", "40"))
VOICE_RSS_MAX_INTERVAL_MIN = int(os.getenv("VOICE_RSS_MAX_INTERVAL_MIN", "110"))
A2A_MIN_REPLIES = int(os.getenv("A2A_MIN_REPLIES", "1"))
A2A_MAX_REPLIES = int(os.getenv("A2A_MAX_REPLIES", "3"))

JST = datetime.timezone(datetime.timedelta(hours=9))

_SISTER_KEYS = ("luna", "spica", "sirius", "stella", "selene", "nano")
SISTER_PROFILES = {k: PERSONAS[k] for k in _SISTER_KEYS}

CHAR_NAME_MAP = {v["name"]: k for k, v in SISTER_PROFILES.items()}

PAUSE_COMMANDS = ("!実験", "！実験", "!休止", "！休止", "!stop", "！stop")
RESUME_COMMANDS = ("!再開", "！再開", "!start", "！start")
STATUS_COMMANDS = ("!状態", "！状態", "!status", "！status")


def is_autonomous_paused() -> bool:
    return PAUSE_FLAG_FILE.exists()


def set_autonomous_pause(paused: bool):
    SHARED_STATE_DIR.mkdir(parents=True, exist_ok=True)
    if paused:
        PAUSE_FLAG_FILE.write_text(
            f"paused_at={datetime.datetime.now(JST).isoformat()}\n",
            encoding="utf-8"
        )
    else:
        PAUSE_FLAG_FILE.unlink(missing_ok=True)


def is_gpu_busy_nonblocking(lock_path: str) -> bool:
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        fd.close()


def _acquire_flock_sync(lock_path: str, non_blocking: bool = False):
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if non_blocking else 0)
        fcntl.flock(fd, flags)
        return fd
    except BlockingIOError:
        fd.close()
        return None


def _release_flock_sync(fd):
    if fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()
        except Exception:
            pass


def load_characters_config() -> dict:
    if not CHARACTERS_JSON_PATH.exists():
        raise FileNotFoundError(f"characters.json が見つかりません: {CHARACTERS_JSON_PATH}")
    with open(CHARACTERS_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def detect_target_character(text: str, exclude_keys: set[str] | None = None) -> tuple[str, str]:
    excluded = exclude_keys or set()
    for jp_name, key in CHAR_NAME_MAP.items():
        if jp_name in text and key not in excluded:
            return key, jp_name
    candidates = [k for k in SISTER_PROFILES.keys() if k not in excluded]
    if not candidates:
        candidates = list(SISTER_PROFILES.keys())
    chosen = "spica" if ("spica" in candidates and not excluded) else random.choice(candidates)
    return chosen, SISTER_PROFILES[chosen]["name"]


# --- 構造化トピックメモリ（JSON）管理 ---
def _load_topic_memory() -> dict:
    if not TOPIC_MEMORY_PATH.exists():
        return {}
    try:
        return json.loads(TOPIC_MEMORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_topic_record(message_id: int, record: dict):
    mem = _load_topic_memory()
    mem[str(message_id)] = record
    # 最新200件のみ保持して肥大化防止
    if len(mem) > 200:
        keys = list(mem.keys())[-200:]
        mem = {k: mem[k] for k in keys}
    TOPIC_MEMORY_PATH.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_topic_record(message_id: int) -> dict | None:
    return _load_topic_memory().get(str(message_id))


def _sanitize_speech_for_tts(text: str) -> str:
    """VOICEVOX読み上げ用に記号や余計なプレフィックスを除去"""
    cleaned = re.sub(r"^(?:SPEECH|speech|セリフ)\s*[:：]\s*", "", text).strip()
    cleaned = cleaned.strip("「」『』\"'")
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"#[^\s#]+", "", cleaned)
    return cleaned.strip()


def _parse_json_safely(raw_text: str) -> dict:
    """LLM出力からJSONを確実に抽出・パースする"""
    text = raw_text.strip()
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


# --- RSS取得 ---
def _load_seen_voice_urls() -> list[str]:
    if not SEEN_VOICE_RSS_PATH.exists():
        return []
    try:
        return json.loads(SEEN_VOICE_RSS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_seen_voice_url(url: str):
    seen = _load_seen_voice_urls()
    if url not in seen:
        seen.append(url)
        seen = seen[-400:]
        SEEN_VOICE_RSS_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_rss_item_sync() -> dict | None:
    if not RSS_FEEDS_PATH.exists():
        logger.warning(f"rss_feeds.json が見つかりません: {RSS_FEEDS_PATH}")
        return None

    feeds = json.loads(RSS_FEEDS_PATH.read_text(encoding="utf-8")).get("feeds", [])
    if not feeds:
        return None

    valid_chars = set(load_characters_config().keys()) & set(SISTER_PROFILES.keys())
    seen_urls = _load_seen_voice_urls()

    for feed in random.sample(feeds, len(feeds)):
        try:
            req = urllib.request.Request(feed["url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as res:
                xml_data = res.read()

            root = ET.fromstring(xml_data)
            candidates = []
            items = root.findall(".//item")
            if items:
                for item in items[:12]:
                    t_el = item.find("title")
                    l_el = item.find("link")
                    d_el = item.find("description")
                    title = t_el.text.strip() if t_el is not None and t_el.text else ""
                    link = l_el.text.strip() if l_el is not None and l_el.text else ""
                    desc = re.sub(r"<[^>]+>", "", d_el.text or "").strip()[:300] if d_el is not None else ""
                    if title and link and link not in seen_urls:
                        candidates.append((title, link, desc))
            else:
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                for entry in root.findall(".//atom:entry", ns)[:12]:
                    t_el = entry.find("atom:title", ns)
                    l_el = entry.find("atom:link", ns)
                    s_el = entry.find("atom:summary", ns)
                    title = t_el.text.strip() if t_el is not None and t_el.text else ""
                    link = l_el.attrib.get("href", "").strip() if l_el is not None else ""
                    desc = re.sub(r"<[^>]+>", "", s_el.text or "").strip()[:300] if s_el is not None else ""
                    if title and link and link not in seen_urls:
                        candidates.append((title, link, desc))

            if not candidates:
                continue

            title, link, desc = random.choice(candidates[:5])
            preferred = [c for c in feed.get("preferred_chars", []) if c in valid_chars]
            char_key = random.choice(preferred) if preferred else random.choice(list(valid_chars))

            _save_seen_voice_url(link)
            return {
                "char_key": char_key,
                "char_name": SISTER_PROFILES[char_key]["name"],
                "category": feed.get("category", "general"),
                "title": title,
                "url": link,
                "summary": desc,
            }
        except Exception as e:
            logger.warning(f"Voice RSS取得スキップ ({feed.get('url')}): {e}")
            continue
    return None


# --- 構造化JSON生成（初回RSSシェア用） ---
def _generate_structured_rss_share_sync(article: dict) -> dict:
    if not HAS_OLLAMA:
        raise RuntimeError("ollama ライブラリがインストールされていません。")

    char_key = article["char_key"]
    profile = SISTER_PROFILES[char_key]

    prompt = (
        f"あなたは『LUNA's Constellations』の6人姉妹の1人「{profile['name']}」です。\n"
        f"性格・口調: {profile['persona']}\n\n"
        f"あなたは今、ネットで見つけた以下の外部ニュース記事を、他の姉妹やプロデューサーのルルに共有します。\n"
        f"【記事タイトル】{article['title']}\n"
        f"【記事概要】{article['summary'] or '（タイトルから客観的な内容を整理すること）'}\n\n"
        f"以下の5つのキーを持つJSONオブジェクトのみを出力してください。\n"
        f"1. \"topic\": 何についての話題か（15〜30文字の簡潔なテーマ名。例: AI音楽企業による著作権誤爆削除問題）\n"
        f"2. \"summary\": 記事の客観的な事実の要約（60〜110文字。あなた自身の体験ではなく外部のニュース事実として簡潔にまとめる）\n"
        f"3. \"thought\": {profile['name']}自身の視点・考察・疑問点（70〜120文字。他の姉妹やルルが深掘り議論しやすい論点を入れる）\n"
        f"4. \"speech\": 音声読み上げ用の自然な話し言葉（100〜160文字。要約と自分の考えを自然に繋げて話す。絵文字・記号・URL・ハッシュタグ・カギ括弧は禁止）\n"
        f"5. \"tags\": 検索用ハッシュタグの配列（2〜3個。例: [\"#AI\", \"#著作権\"]）"
    )

    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[{"role": "user", "content": prompt}],
        format="json"
    )
    raw = response["message"]["content"]

    try:
        data = _parse_json_safely(raw)
    except Exception as e:
        logger.warning(f"RSS JSONパースフォールバック発動: {e} | raw={raw[:150]}")
        data = {
            "topic": article["title"][:28],
            "summary": article["summary"][:100] or article["title"],
            "thought": "この仕組み、後からどうやって検証や異議申し立てをするのかすごく気になるよね。",
            "speech": f"ねえ聞いて！{article['title']}っていうニュースを見つけたんだ。これって仕組みの裏側がすごく気になるよね。"
            ,
            "tags": [f"#{article['category']}"]
        }

    topic = str(data.get("topic") or article["title"][:28]).strip()
    summary = str(data.get("summary") or article["summary"][:100] or article["title"]).strip()
    thought = str(data.get("thought") or "").strip()
    speech = _sanitize_speech_for_tts(str(data.get("speech") or f"{thought}"))

    raw_tags = data.get("tags") if isinstance(data.get("tags"), list) else []
    clean_tags = [t if str(t).startswith("#") else f"#{t}" for t in raw_tags if str(t).strip()]
    cat_tag = f"#{article['category']}"
    all_tags = [cat_tag] + [t for t in clean_tags if t != cat_tag]

    return {
        "speaker_key": char_key,
        "speaker": profile["name"],
        "topic": topic,
        "summary": summary,
        "thought": thought,
        "speech": speech,
        "tags": all_tags[:4],
        "article_title": article["title"],
        "url": article["url"],
        "source_type": "external_rss_news"
    }


# --- 構造化JSON生成（A2A姉妹返信 ＆ ルルとの対話・深掘り用） ---
def _generate_structured_reply_sync(
    char_key: str,
    user_text: str,
    image_bytes_list: list[bytes],
    chain_records: list[dict],
    fallback_context: str = "",
    is_sister_crosstalk: bool = False
) -> dict:
    if not HAS_OLLAMA:
        raise RuntimeError("ollama ライブラリがインストールされていません。")

    profile = SISTER_PROFILES.get(char_key, SISTER_PROFILES["spica"])

    # 直近の構造化レコードがあれば、誰の話題・誰の体験かを厳密に固定する
    latest_rec = chain_records[-1] if chain_records else None
    has_thread = bool(chain_records or fallback_context)

    if is_sister_crosstalk and latest_rec:
        role_note = (
            f"今、姉妹の「{latest_rec.get('speaker')}」がチャンネルにシェアした外部ニュースに対して、"
            f"あなたが横から興味を持って返信（A2A議論の深掘り）をする場面です。\n"
            f"【重要】これは外部ニュースの話題であり、あなたや姉妹自身の体験談ではありません。"
            f"{latest_rec.get('speaker')}の『考えたこと』を受け止め、別の角度からの技術的・本質的な考察や疑問を投げかけてください。"
        )
    elif latest_rec:
        role_note = (
            f"プロデューサーのルルからのメッセージに返信する場面です。\n"
            f"【最重要・主語厳守】返信元の話題は、姉妹（{latest_rec.get('speaker')}）がシェアした『外部ニュース記事』または直前の議論です。"
            f"ルル自身が被害に遭ったり体験したりした話ではありません！ルルは記事の内容や仕組みについて質問・深掘りを求めています。"
            f"記事の事実（summary）とこれまでの考察（thought）を踏まえ、分かりやすく掘り下げて答えてください。"
        )
    else:
        role_note = (
            "プロデューサーのルルからの新しいメッセージや画像に返信する場面です。"
            "ルルの発言や画像の内容を正確に受け止め、あなたのキャラクターらしく返答してください。"
        )

    context_block = ""
    if chain_records:
        structured_history = []
        for idx, rec in enumerate(chain_records, 1):
            structured_history.append({
                "turn": idx,
                "speaker": rec.get("speaker"),
                "source_type": rec.get("source_type", "conversation"),
                "topic": rec.get("topic"),
                "fact_summary": rec.get("summary"),
                "thought": rec.get("thought"),
                "article_title": rec.get("article_title", ""),
            })
        context_block += f"【これまでの議論の構造化データ（JSON）】\n{json.dumps(structured_history, ensure_ascii=False, indent=2)}\n\n"
    if fallback_context:
        context_block += f"【補足テキスト履歴】\n{fallback_context}\n\n"

    user_input_clean = user_text.strip() if user_text.strip() else "この画像を見て感想や気づいたことを教えて！"

    prompt = (
        f"あなたは『LUNA's Constellations』の6人姉妹の1人「{profile['name']}」です。\n"
        f"性格・口調: {profile['persona']}\n\n"
        f"{role_note}\n\n"
        f"{context_block}"
        f"【今回の入力メッセージ】\n{user_input_clean}\n\n"
        f"以下の4つのキーを持つJSONオブジェクトのみを出力してください。\n"
        f"1. \"topic\": 今回話しているトピック（15〜30文字）\n"
        f"2. \"summary\": 今回の返答の核となる事実・要点の整理（40〜90文字。外部ニュースの解説なら客観的事実をまとめる）\n"
        f"3. \"thought\": {profile['name']}が今回さらに考えたこと・深い視点・次への問いかけ（50〜110文字）\n"
        f"4. \"speech\": 音声読み上げ用の自然な話し言葉（{'90〜160文字' if has_thread else '50〜110文字'}。絵文字・記号・URL・ハッシュタグ・カギ括弧は一切使わないこと）\n"
        f"5. \"continue_discussion\": この話題について他の姉妹の意見も聞きたい・さらに議論を深掘りできると思う場合は true、綺麗に結論がまとまった場合は false（boolean型）"
    )

    user_msg: dict = {"role": "user", "content": prompt}
    if image_bytes_list:
        user_msg["images"] = [base64.b64encode(b).decode("utf-8") for b in image_bytes_list]

    response = ollama.chat(
        model=OLLAMA_MODEL,
        messages=[user_msg],
        format="json"
    )
    raw = response["message"]["content"]

    try:
        data = _parse_json_safely(raw)
    except Exception as e:
        logger.warning(f"Reply JSONパースフォールバック発動: {e} | raw={raw[:150]}")
        fallback_speech = _sanitize_speech_for_tts(raw)
        data = {
            "topic": latest_rec.get("topic", "会話の続き") if latest_rec else "ルルとの会話",
            "summary": fallback_speech[:80],
            "thought": fallback_speech[:100],
            "speech": fallback_speech
        }

    topic = str(data.get("topic") or (latest_rec.get("topic") if latest_rec else "会話")).strip()
    summary = str(data.get("summary") or "").strip()
    thought = str(data.get("thought") or "").strip()
    speech = _sanitize_speech_for_tts(str(data.get("speech") or thought or summary))

    return {
        "speaker_key": char_key,
        "speaker": profile["name"],
        "topic": topic,
        "summary": summary,
        "thought": thought,
        "speech": speech,
        "continue_discussion": bool(data.get("continue_discussion", True)),
        "article_title": latest_rec.get("article_title", "") if latest_rec else "",
        "url": latest_rec.get("url", "") if latest_rec else "",
        "source_type": latest_rec.get("source_type", "conversation") if latest_rec else "conversation"
    }


def _format_structured_discord_message(rec: dict, include_article_footer: bool = False) -> str:
    """v2：本人の考えを先頭に置き、要約と出典は後ろに回す形式。

    トピック見出し行は廃止し、記事タイトルは出典リンクが兼ねる
    （「〜について」の二重化もこれで起きない）。
    """
    lines = [f"**【{rec['speaker']}】**"]
    if rec.get("thought"):
        lines.append(f"💭 {rec['thought']}")
    lines.append(f"🗣️ 『{rec['speech']}』")
    if rec.get("summary"):
        lines.append(f"📝 要約：{rec['summary']}")

    if include_article_footer and rec.get("url"):
        tags_str = " ".join(rec.get("tags", []))
        lines.append("")
        if tags_str:
            lines.append(tags_str)
        lines.append(f"🔗 **{rec.get('article_title', '元記事')}**")
        lines.append(rec["url"])

    return "\n".join(lines)


def _synthesize_voice_mp3_sync(char_key: str, text: str) -> Path:
    TEMP_VOICE_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_characters_config()[char_key]
    speaker_id = cfg["voice_id"]

    encoded_text = urllib.parse.quote(text)
    query_url = f"{VOICEVOX_URL}/audio_query?text={encoded_text}&speaker={speaker_id}"
    req_q = urllib.request.Request(query_url, method="POST")
    with urllib.request.urlopen(req_q, timeout=20) as res:
        query_data = json.loads(res.read().decode("utf-8"))

    query_data["speedScale"] = cfg.get("speed_scale", 1.0)
    query_data["intonationScale"] = cfg.get("intonation_scale", 1.0)
    query_data["volumeScale"] = cfg.get("volume_scale", 1.0)

    synth_url = f"{VOICEVOX_URL}/synthesis?speaker={speaker_id}"
    req_s = urllib.request.Request(
        synth_url,
        data=json.dumps(query_data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    ts = int(time.time() * 1000)
    wav_path = TEMP_VOICE_DIR / f"voice_{char_key}_{ts}.wav"
    mp3_path = TEMP_VOICE_DIR / f"voice_{char_key}_{ts}.mp3"

    with urllib.request.urlopen(req_s, timeout=45) as res:
        wav_path.write_bytes(res.read())

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path), "-codec:a", "libmp3lame", "-b:a", "128k", str(mp3_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )
        if mp3_path.exists() and mp3_path.stat().st_size > 0:
            wav_path.unlink(missing_ok=True)
            return mp3_path
    except Exception as e:
        logger.warning(f"MP3変換をスキップしてWAVで送信します: {e}")

    return wav_path


async def _extract_reply_chain_structured(message: discord.Message, max_depth: int = 4) -> tuple[list[dict], str]:
    """返信チェーンを遡り、保存済みJSON構造化レコードと未保存テキストの両方を復元する"""
    records: list[dict] = []
    fallback_lines: list[str] = []
    curr = message

    for _ in range(max_depth):
        if not curr.reference or not curr.reference.message_id:
            break
        try:
            ref_id = curr.reference.message_id
            saved_rec = _get_topic_record(ref_id)
            ref_msg = curr.reference.resolved
            if not isinstance(ref_msg, discord.Message):
                ref_msg = await curr.channel.fetch_message(ref_id)

            if saved_rec:
                records.append(saved_rec)
            else:
                author_label = "姉妹Bot" if ref_msg.author.bot else f"ルル({ref_msg.author.display_name})"
                fallback_lines.append(f"- {author_label}: {ref_msg.content}")

            curr = ref_msg
        except Exception as e:
            logger.debug(f"返信元メッセージ取得スキップ: {e}")
            break

    records.reverse()
    fallback_lines.reverse()
    return records, "\n".join(fallback_lines)


async def _trigger_sister_crosstalk(root_message: discord.Message, first_record: dict):
    """A2Aマルチターン議論：最低1回は必ず返信し、エージェントの自律判断と盛り上がりで最大2〜3回まで議論を積み上げる"""
    history_records: list[dict] = [first_record]
    participated_keys: set[str] = {first_record["speaker_key"]}
    current_target_msg = root_message

    # 今回のスレッドでの目標上限ターン（2回か3回をランダム上限とし、各ターンで継続するか自律判断）
    max_turns = random.randint(max(1, A2A_MIN_REPLIES), max(A2A_MIN_REPLIES, A2A_MAX_REPLIES))

    for turn in range(1, max_turns + 1):
        # 1ターン目は少し間を置いて反応、2〜3ターン目は会話のテンポに合わせて待機
        wait_sec = random.randint(20, 50) if turn == 1 else random.randint(25, 65)
        await asyncio.sleep(wait_sec)

        if is_autonomous_paused():
            logger.info(f"⏸️ [A2Aラリー {turn}/{max_turns}] 実験・休止モード中のため停止しました。")
            break

        lock_fd = await asyncio.to_thread(_acquire_flock_sync, GPU_LOCK_FILE, True)
        if not lock_fd:
            logger.info(f"🔒 [A2Aラリー {turn}/{max_turns}] GPU使用中のため空気読みスキップしました。")
            break

        prev_rec = history_records[-1]
        next_key, next_name = detect_target_character("", exclude_keys=participated_keys)
        participated_keys.add(next_key)

        if turn == 1:
            trigger_prompt = (
                f"{prev_rec['speaker']}がシェアした『{prev_rec['topic']}』について、"
                f"{next_name}独自の視点から最初のリアクションと深掘りポイントを投げかけて！"
            )
        else:
            trigger_prompt = (
                f"これまでの{', '.join(r['speaker'] for r in history_records)}の議論を受けて、"
                f"{next_name}の立場からさらに別の角度の考察や本質的な問いかけを積み上げて！"
            )

        audio_path: Path | None = None
        try:
            reply_rec = await asyncio.to_thread(
                _generate_structured_reply_sync, next_key, trigger_prompt, [], history_records, "", True
            )
            await asyncio.to_thread(_release_flock_sync, lock_fd)
            lock_fd = None

            audio_path = await asyncio.to_thread(_synthesize_voice_mp3_sync, next_key, reply_rec["speech"])
            discord_file = discord.File(str(audio_path), filename=audio_path.name)
            msg_body = _format_structured_discord_message(reply_rec, include_article_footer=False)
            sent_reply = await current_target_msg.reply(
                content=msg_body,
                file=discord_file,
                mention_author=False
            )
            _save_topic_record(sent_reply.id, reply_rec)
            history_records.append(reply_rec)
            current_target_msg = sent_reply

            logger.info(
                f"🗣️✨ [A2Aラリー {turn}/{max_turns}] {prev_rec['speaker']} -> {next_name} 送信完了 "
                f"(継続意欲={reply_rec.get('continue_discussion')})"
            )

            # 最低回数(A2A_MIN_REPLIES)を満たした後は、エージェント自身の「まだ議論したいか」の判定に従う
            if turn >= A2A_MIN_REPLIES and not reply_rec.get("continue_discussion", False):
                logger.info(f"🏁 [A2Aラリー] {next_name} の判断により議論が綺麗にまとまったため終了します。")
                break

        except Exception as e:
            logger.warning(f"姉妹クロストーク(ターン{turn})スキップ: {e}")
            break
        finally:
            if lock_fd:
                await asyncio.to_thread(_release_flock_sync, lock_fd)
            if audio_path and audio_path.exists():
                audio_path.unlink(missing_ok=True)


async def voice_rss_background_loop(client: discord.Client, voice_channel_id: int | None):
    if not voice_channel_id:
        logger.info("DISCORD_VOICE_CHAT_CHANNEL_ID 未設定のため、Voice RSSループは待機しません。")
        return

    await client.wait_until_ready()
    logger.info(f"🎙️ Voice専用RSS不定期配信ループ起動 (間隔: {VOICE_RSS_MIN_INTERVAL_MIN}〜{VOICE_RSS_MAX_INTERVAL_MIN}分)")

    await asyncio.sleep(random.randint(120, 300))

    while not client.is_closed():
        now = datetime.datetime.now(JST)
        if datetime.time(9, 0) <= now.time() <= datetime.time(23, 59):
            if is_autonomous_paused():
                logger.info("⏸️ [VoiceRSS] 実験・休止モード中（pause_autonomous.flag）のため今回の配信を見送ります。")
            else:
                channel = client.get_channel(voice_channel_id)
                if channel:
                    lock_fd = await asyncio.to_thread(_acquire_flock_sync, GPU_LOCK_FILE, True)
                    if not lock_fd:
                        logger.info("🔒 [VoiceRSS] 他ジョブがGPUを使用中のため、今回のRSS配信を見送ります。")
                    else:
                        audio_path: Path | None = None
                        try:
                            article = await asyncio.to_thread(_fetch_rss_item_sync)
                            if article:
                                share_rec = await asyncio.to_thread(_generate_structured_rss_share_sync, article)
                                await asyncio.to_thread(_release_flock_sync, lock_fd)
                                lock_fd = None

                                char_key = share_rec["speaker_key"]
                                char_name = share_rec["speaker"]
                                audio_path = await asyncio.to_thread(_synthesize_voice_mp3_sync, char_key, share_rec["speech"])

                                msg_body = _format_structured_discord_message(share_rec, include_article_footer=True)
                                discord_file = discord.File(str(audio_path), filename=audio_path.name)
                                sent_msg = await channel.send(content=msg_body, file=discord_file)
                                _save_topic_record(sent_msg.id, share_rec)

                                logger.info(f"🎙️ [VoiceRSS] {char_name} が構造化配信完了: {share_rec['topic']}")

                                asyncio.create_task(_trigger_sister_crosstalk(sent_msg, share_rec))
                            else:
                                logger.info("🎙️ [VoiceRSS] 新着未読記事なしのためスキップ")
                        except Exception as e:
                            logger.error(f"❌ [VoiceRSS] 配信エラー: {e}")
                            logger.error(traceback.format_exc())
                        finally:
                            if lock_fd:
                                await asyncio.to_thread(_release_flock_sync, lock_fd)
                            if audio_path and audio_path.exists():
                                audio_path.unlink(missing_ok=True)

        wait_sec = random.randint(VOICE_RSS_MIN_INTERVAL_MIN * 60, VOICE_RSS_MAX_INTERVAL_MIN * 60)
        logger.info(f"⏳ [VoiceRSS] 次回配信まで約 {wait_sec // 60} 分待機します。")
        await asyncio.sleep(wait_sec)


async def handle_voice_chat_message(message: discord.Message):
    """Discordの専用チャンネルに届いた制御コマンド・テキスト・画像・返信(Reply)を処理"""
    clean_text = message.content.strip()
    logger.info(f"📩 [VoiceChat受信入口] ch={message.channel.id} user={message.author} text={clean_text!r}")

    # 1. ルル専用の明示的制御コマンド
    cmd_lower = clean_text.lower()
    if not message.attachments and cmd_lower.startswith(("!", "！")):
        if any(cmd_lower.startswith(c.lower()) for c in PAUSE_COMMANDS):
            set_autonomous_pause(True)
            await message.reply(
                "⏸️ **【スピカ】** 了解よ、ルル！今の間は定時動画も不定期ボイスも姉妹の雑談も全部ストップして静かにしておくから、実験に集中しなさい。（戻ったら `!再開` って打ちなさいね）",
                mention_author=False
            )
            logger.info("⏸️ ルルの指示により自律配信を休止モードに切り替えました。")
            return

        if any(cmd_lower.startswith(c.lower()) for c in RESUME_COMMANDS):
            set_autonomous_pause(False)
            await message.reply(
                "▶️ **【スピカ】** お疲れさま！休止モードを解除して、またいつもの日常運転に戻したわよ。",
                mention_author=False
            )
            logger.info("▶️ ルルの指示により自律配信を再開しました。")
            return

        if any(cmd_lower.startswith(c.lower()) for c in STATUS_COMMANDS):
            paused = is_autonomous_paused()
            gpu_busy = await asyncio.to_thread(is_gpu_busy_nonblocking, GPU_LOCK_FILE)
            status_str = "⏸️ **休止中（実験モード）**" if paused else "▶️ **稼働中（日常モード）**"
            gpu_str = "🔴 **使用中（ロック中）**" if gpu_busy else "🟢 **空き（待機中）**"
            await message.reply(
                f"📊 **【スピカ】現在のシステム状態**\n"
                f"・自律配信（定時動画＋VoiceRSS）: {status_str}\n"
                f"・共通GPUロック (`gpu.lock`): {gpu_str}",
                mention_author=False
            )
            return

    # 2. 返信チェーンの構造化JSONレコードを復元
    chain_records, fallback_context = await _extract_reply_chain_structured(message)

    default_char_key = None
    if not any(name in message.content for name in CHAR_NAME_MAP):
        if chain_records:
            default_char_key = chain_records[-1].get("speaker_key")
        elif fallback_context:
            m = re.findall(r"【(ルナ|スピカ|シリウス|ステラ|セレーネ|ナノ)】", fallback_context)
            if m:
                default_char_key = CHAR_NAME_MAP.get(m[-1])

    if default_char_key and default_char_key in SISTER_PROFILES:
        char_key = default_char_key
        char_name = SISTER_PROFILES[char_key]["name"]
    else:
        char_key, char_name = detect_target_character(message.content)

    logger.info(
        f"💬 [VoiceChat] 受信: user={message.author} -> 担当={char_name} | "
        f"構造化履歴={len(chain_records)}件"
    )

    image_bytes_list: list[bytes] = []
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            image_bytes_list.append(await att.read())

    if not clean_text and not image_bytes_list:
        return

    async with message.channel.typing():
        lock_fd = None
        audio_file_path: Path | None = None
        try:
            lock_fd = await asyncio.to_thread(_acquire_flock_sync, GPU_LOCK_FILE, False)
            reply_rec = await asyncio.to_thread(
                _generate_structured_reply_sync,
                char_key,
                message.content,
                image_bytes_list,
                chain_records,
                fallback_context,
                False
            )
            await asyncio.to_thread(_release_flock_sync, lock_fd)
            lock_fd = None

            logger.info(f"🗣️ [VoiceChat] {char_name} 構造化応答生成: topic={reply_rec['topic']}")
            audio_file_path = await asyncio.to_thread(_synthesize_voice_mp3_sync, char_key, reply_rec["speech"])

            msg_body = _format_structured_discord_message(reply_rec, include_article_footer=False)
            discord_file = discord.File(str(audio_file_path), filename=audio_file_path.name)
            sent_msg = await message.reply(
                content=msg_body,
                file=discord_file,
                mention_author=False
            )
            _save_topic_record(sent_msg.id, reply_rec)
            logger.info(f"✅ [VoiceChat] 構造化ボイス返信完了: {audio_file_path.name}")

        except Exception as e:
            logger.error(f"❌ [VoiceChat] 処理エラー: {e}")
            logger.error(traceback.format_exc())
            await message.reply(f"⚠️ ごめん、ボイス返信の生成中にエラーが起きちゃった！\n```python\n{e}\n```", mention_author=False)
        finally:
            if lock_fd:
                await asyncio.to_thread(_release_flock_sync, lock_fd)
            if audio_file_path and audio_file_path.exists():
                audio_file_path.unlink(missing_ok=True)
