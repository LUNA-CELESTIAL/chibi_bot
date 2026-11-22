import os
import re
import html
import json
import random
import logging
import traceback
import urllib.request
import xml.etree.ElementTree as ET
import ollama
from video_generator import load_json_file
from personas import PERSONAS

logger = logging.getLogger("RssFriendTrigger")

BASE_DIR = os.path.dirname(__file__)
RSS_FEEDS_FILE = os.path.join(BASE_DIR, "rss_feeds.json")
SEEN_RSS_FILE = os.path.join(BASE_DIR, "seen_rss.json")
SPINOFF_PROMPTS_FILE = os.path.join(BASE_DIR, "spinoff_prompts.json")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.8-Q2:latest")

# 人格定義は personas.py に一本化（正は sweet-bits の設定）
CHAR_PERSONA_GUIDE = {
    k: f"{v['name']}（{v['persona']}）" for k, v in PERSONAS.items()
}


def _clean_html(raw_text: str) -> str:
    if not raw_text:
        return ""
    text = html.unescape(raw_text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600]


def _load_seen_urls() -> list[str]:
    if not os.path.exists(SEEN_RSS_FILE):
        return []
    try:
        with open(SEEN_RSS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_seen_url(url: str):
    seen = _load_seen_urls()
    if url not in seen:
        seen.append(url)
        seen = seen[-300:]
        with open(SEEN_RSS_FILE, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False, indent=2)


def _fetch_latest_unseen_article(feed_url: str, seen_urls: list[str]) -> tuple[str, str, str] | None:
    req = urllib.request.Request(feed_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        xml_data = res.read()

    root = ET.fromstring(xml_data)
    items = root.findall(".//item")
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns)[:10]:
            title_el = entry.find("atom:title", ns)
            link_el = entry.find("atom:link", ns)
            summary_el = entry.find("atom:summary", ns)
            if summary_el is None:
                summary_el = entry.find("atom:content", ns)
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.attrib.get("href", "").strip() if link_el is not None else ""
            summary = _clean_html(summary_el.text if summary_el is not None and summary_el.text else "")
            if title and link and link not in seen_urls:
                return title, link, summary
        return None

    for item in items[:10]:
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        if desc_el is None:
            desc_el = item.find("{http://purl.org/rss/1.0/modules/content/}encoded")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        summary = _clean_html(desc_el.text if desc_el is not None and desc_el.text else "")
        if title and link and link not in seen_urls:
            return title, link, summary
    return None


def _generate_friend_reaction_structured(char_key: str, char_name: str, article_title: str, article_summary: str) -> dict:
    prompts_data = load_json_file(SPINOFF_PROMPTS_FILE)
    sample_speeches = prompts_data["characters"].get(char_key, {}).get("speeches", {})
    samples_text = "\n".join([f"- {s}" for s in sample_speeches.values()])
    persona_desc = CHAR_PERSONA_GUIDE.get(char_key, f"{char_name}（親しみやすい口調）")
    summary_context = article_summary if article_summary else "（記事本文の抜粋なし。タイトルから技術的・社会的な要点を推測して補完すること）"

    prompt = (
        f"あなたは「{persona_desc}」です。友達のルルに面白い記事を10秒の動画メッセージで紹介します。\n"
        f"【口調サンプル】\n{samples_text}\n\n"
        f"【記事タイトル】\n{article_title}\n"
        f"【記事の概要】\n{summary_context}\n\n"
        f"以下の3つのキーを持つJSONオブジェクトのみを出力してください。抽象的なポエム感想（「なんて素敵でしょう」等）は厳禁です。\n"
        f"1. \"fact_summary\": この記事が何を伝えているか、具体的な技術・仕組み・出来事の要点を日本語1〜2文（60〜100文字）で簡潔にまとめる。\n"
        f"2. \"thought\": {char_name}らしい視点での具体的な考察・なぜそれが面白いのか・何に役立ちそうかを日本語1〜2文（60〜100文字）で書く。\n"
        f"3. \"speech\": 10秒動画で実際に喋るセリフ。上記の要点と考察の両方を自然に盛り込み、ルルに語りかける口調で【85〜115文字】の日本語にすること。カギ括弧は不要。"
    )

    try:
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            format="json",
        )
        raw = response["message"]["content"].strip()
        parsed = json.loads(raw)
        fact_summary = str(parsed.get("fact_summary", "")).strip()
        thought = str(parsed.get("thought", "")).strip()
        speech = str(parsed.get("speech", "")).strip().strip("「」『』")

        if not speech:
            raise ValueError("speechフィールドが空です")
        if len(speech) > 130:
            speech = speech[:127] + "…"

        return {
            "fact_summary": fact_summary or article_title,
            "thought": thought,
            "speech": speech,
        }
    except Exception as e:
        logger.error(f"構造化JSON生成フォールバック発動 ({char_name}): {e}\n{traceback.format_exc()}")
        fallback_speech = f"ねえルル、{article_title[:35]}って記事を見つけたよ！仕組みがすごく面白くて、私たちの開発にも応用できそうだよ！"
        return {
            "fact_summary": article_summary[:100] if article_summary else article_title,
            "thought": "具体的な仕組みや今後の応用がとても気になるトピックだね。",
            "speech": fallback_speech[:115],
        }


def pick_rss_friend_share(duty_characters: list[dict]) -> dict | None:
    """
    未読RSS記事を1件見つけ、担当キャラ・構造化トーク（要点/考察/10秒セリフ）・記事情報を返す。
    """
    feeds_data = load_json_file(RSS_FEEDS_FILE)
    feeds = feeds_data.get("feeds", [])
    if not feeds:
        return None

    seen_urls = _load_seen_urls()
    shuffled_feeds = random.sample(feeds, len(feeds))

    for feed in shuffled_feeds:
        try:
            article = _fetch_latest_unseen_article(feed["url"], seen_urls)
            if not article:
                continue

            title, link, summary = article
            char_key = random.choice(feed["preferred_chars"])
            char_info = next((c for c in duty_characters if c["key"] == char_key), duty_characters[0])

            structured = _generate_friend_reaction_structured(char_key, char_info["name"], title, summary)
            _save_seen_url(link)

            return {
                "char_info": char_info,
                "situation_key": feed.get("situation", "cafe_tea"),
                "speech": structured["speech"],
                "fact_summary": structured["fact_summary"],
                "thought": structured["thought"],
                "article_title": title,
                "article_url": link,
                "prompts_file": SPINOFF_PROMPTS_FILE,
                "duration_sec": 10.0,
            }
        except Exception as e:
            logger.warning(f"RSS取得または生成スキップ ({feed.get('url')}): {e}")
            continue

    return None
