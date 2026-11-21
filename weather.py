import urllib.request
import json
import logging

logger = logging.getLogger(__name__)

def get_tokyo_weather() -> dict:
    """気象庁APIから東京の天気を取得し、sunny / cloudy / rainy に分類する"""
    url = "https://www.jma.go.jp/bosai/forecast/data/forecast/130000.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read().decode("utf-8"))
            
            raw_weather = data[0]["timeSeries"][0]["areas"][0]["weathers"][0]
            clean = raw_weather.replace(" ", " ").strip()
            
            # 優先度判定（雨・雪・雷が含まれていれば rainy）
            if any(kw in clean for kw in ["雨", "雪", "雷"]):
                weather_key = "rainy"
                display_label = "雨"
            elif "晴" in clean:
                weather_key = "sunny"
                display_label = "晴れ"
            elif any(kw in clean for kw in ["くもり", "曇"]):
                weather_key = "cloudy"
                display_label = "曇り"
            else:
                weather_key = "sunny"
                display_label = "晴れ"
                
            return {
                "location": "東京",
                "raw_weather": raw_weather,
                "weather_key": weather_key,     # 'sunny', 'cloudy', 'rainy'
                "display_label": display_label  # '晴れ', '曇り', '雨'
            }
    except Exception as e:
        logger.error(f"天気情報の取得に失敗しました: {e}")
        return {
            "location": "東京",
            "raw_weather": "晴れ",
            "weather_key": "sunny",
            "display_label": "晴れ"
        }