import os
import logging
from PIL import Image
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

INPUT_BASE_DIR = os.getenv("COMFYUI_INPUT_DIR", "/home/lulu/comfy/ComfyUI/input")

def fit_without_crop(img_path: str, slot_w: int, slot_h: int) -> Image.Image:
    img = Image.open(img_path).convert("RGBA")
    w, h = img.size
    scale = min(slot_w / w, slot_h / h)
    new_w, new_h = int(w * scale), int(h * scale)
    return img.resize((new_w, new_h), Image.Resampling.LANCZOS)

def compose_pair_image(
    image1_name: str,
    image2_name: str,
    output_name: str,
    canvas_width: int = 832,
    canvas_height: int = 736
) -> str:
    path1 = os.path.join(INPUT_BASE_DIR, image1_name)
    path2 = os.path.join(INPUT_BASE_DIR, image2_name)
    output_path = os.path.join(INPUT_BASE_DIR, output_name)

    if not os.path.exists(path1) or not os.path.exists(path2):
        raise FileNotFoundError(f"合成元画像が見つかりません: {path1} または {path2}")

    canvas = Image.new("RGB", (canvas_width, canvas_height), (255, 255, 255))
    slot_w = canvas_width // 2
    slot_h = canvas_height

    img1 = fit_without_crop(path1, slot_w, slot_h)
    img2 = fit_without_crop(path2, slot_w, slot_h)

    x1 = (slot_w - img1.width) // 2
    y1 = (slot_h - img1.height) // 2
    x2 = slot_w + (slot_w - img2.width) // 2
    y2 = (slot_h - img2.height) // 2

    canvas.paste(img1, (x1, y1), img1)
    canvas.paste(img2, (x2, y2), img2)

    canvas.save(output_path, "PNG")
    logger.info(f"ペア画像を生成完了 ({canvas_width}x{canvas_height}): {output_path}")
    return output_name

def generate_all_preset_pairs():
    """定義されたおすすめペア画像を一括作成する"""
    pair_targets = [
        ("spica_rabbit.png", "luna_rabbit.png", "spica_luna_pair.png"),
        ("sirius_rabbit.png", "stella_rabbit.png", "sirius_stella_pair.png"),
        ("selene_rabbit.png", "nano_rabbit.png", "selene_nano_pair.png"),
        ("hina_rabbit.png", "kohaku_rabbit.png", "hina_kohaku_pair.png"),
        ("spica_rabbit.png", "sirius_rabbit.png", "spica_sirius_pair.png"),
    ]
    
    for img1, img2, out_name in pair_targets:
        try:
            compose_pair_image(img1, img2, out_name)
            print(f"✅ 生成成功: {out_name}")
        except Exception as e:
            print(f"❌ 生成失敗 ({out_name}): {e}")

if __name__ == "__main__":
    generate_all_preset_pairs()