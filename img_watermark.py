#!/usr/bin/env python3
"""
img_watermark_stdin.py

交互脚本（基于你之前的版本，仅修改：颜色为 hex、字体接受百分比/像素/auto、位置五选一）。
其它功能不变（TrueType 优先、位图回退渲染并缩放、抗锯齿、右下偏移按比率等）。
"""

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

# piexif 可选（更稳健地读取 EXIF）
try:
    import piexif
except Exception:
    piexif = None

# ---- 默认配置（未改动功能性参数，仅默认值） ----
DEFAULT_COLOR_HEX = "#FFFFFF"
DEFAULT_POS = "bottomright"
DEFAULT_JPEG_QUALITY = 95
FALLBACK_TO_FILETIME = False
MIN_FONT_SIZE = 8
DEFAULT_TARGET_RATIO = 0.10  # 默认 10%
DEFAULT_PADDING = 12
OUTLINE_WIDTH = 2
OUTLINE_COLOR = (0, 0, 0, 200)
# 右下偏移比例（未改）
BOTTOM_OFFSET_RATIO = 0.05
RIGHT_OFFSET_RATIO = 0.08
# 抗锯齿参数（未改）
ANTIALIAS_BLUR_RADIUS = 0.6
UNSHARP_RADIUS = 1
UNSHARP_PERCENT = 120
UNSHARP_THRESHOLD = 3
# -------------------


def parse_hex_color_to_rgba(s: str) -> Tuple[int, int, int, int]:
    """解析 hex 字符串（#RRGGBB 或 RRGGBB 或 3位简写）为 RGBA 元组；出错抛异常。"""
    if not s:
        s = DEFAULT_COLOR_HEX
    s = s.strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 3:
        s = ''.join([c * 2 for c in s])
    if len(s) != 6:
        raise ValueError("颜色格式应为 6 位十六进制，例如 #RRGGBB")
    try:
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
    except Exception:
        raise ValueError("颜色包含非法十六进制字符")
    return (r, g, b, 255)


def find_system_font() -> Optional[str]:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _parse_exif_datetime(s: str) -> Optional[datetime]:
    if not s:
        return None
    if isinstance(s, bytes):
        try:
            s = s.decode()
        except Exception:
            s = s.decode("latin-1", errors="ignore")
    s = s.strip()
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    try:
        parts = s.split()
        if parts:
            p = parts[0].replace(":", "-")
            return datetime.strptime(p, "%Y-%m-%d")
    except Exception:
        return None
    return None


def read_exif_date(path: str) -> Optional[datetime]:
    try:
        if piexif:
            exif_dict = piexif.load(path)
            exif_ifd = exif_dict.get("Exif", {})
            if exif_ifd:
                dto = exif_ifd.get(piexif.ExifIFD.DateTimeOriginal) or exif_ifd.get(
                    piexif.ExifIFD.DateTimeDigitized
                )
                if dto:
                    if isinstance(dto, bytes):
                        dto = dto.decode(errors="ignore")
                    return _parse_exif_datetime(dto)
            zeroth = exif_dict.get("0th", {})
            dt = zeroth.get(piexif.ImageIFD.DateTime)
            if dt:
                if isinstance(dt, bytes):
                    dt = dt.decode(errors="ignore")
                return _parse_exif_datetime(dt)
        else:
            img = Image.open(path)
            exif = img._getexif()
            if exif:
                for tag in (36867, 306):
                    v = exif.get(tag)
                    if v:
                        return _parse_exif_datetime(v)
    except Exception:
        return None
    return None


def ensure_rgba(img: Image.Image) -> Image.Image:
    if img.mode != "RGBA":
        return img.convert("RGBA")
    return img


def draw_text_with_outline_on_draw(draw: ImageDraw.ImageDraw, xy: Tuple[int, int], text: str, font: ImageFont.ImageFont,
                                  fill: Tuple[int, int, int, int], outline_fill=OUTLINE_COLOR, outline_width=OUTLINE_WIDTH):
    x, y = xy
    for ox in range(-outline_width, outline_width + 1):
        for oy in range(-outline_width, outline_width + 1):
            if ox == 0 and oy == 0:
                continue
            draw.text((x + ox, y + oy), text, font=font, fill=outline_fill)
    draw.text((x, y), text, font=font, fill=fill)


def render_text_to_image(text: str, font: ImageFont.ImageFont,
                         outline_width=OUTLINE_WIDTH, fill=(255, 255, 255, 255), outline_fill=OUTLINE_COLOR) -> Image.Image:
    tmp = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    draw_tmp = ImageDraw.Draw(tmp)
    try:
        bbox = draw_tmp.textbbox((0, 0), text, font=font)
    except Exception:
        w, h = draw_tmp.textsize(text, font=font)
        bbox = (0, 0, w, h)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad = outline_width + 2
    canvas_w = tw + pad * 2
    canvas_h = th + pad * 2
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw_text_with_outline_on_draw(draw, (pad, pad), text, font=font, fill=fill, outline_fill=outline_fill, outline_width=outline_width)
    return img


def scale_image_to_width_antialiased(img: Image.Image, target_w: int) -> Image.Image:
    if img.width == 0:
        return img
    scale = target_w / img.width
    if scale <= 0:
        return img
    new_w = max(1, int(round(img.width * scale)))
    new_h = max(1, int(round(img.height * scale)))
    resized = img.resize((new_w, new_h), resample=Image.LANCZOS)
    if ANTIALIAS_BLUR_RADIUS > 0:
        resized = resized.filter(ImageFilter.GaussianBlur(radius=ANTIALIAS_BLUR_RADIUS))
    resized = resized.filter(ImageFilter.UnsharpMask(radius=UNSHARP_RADIUS, percent=UNSHARP_PERCENT, threshold=UNSHARP_THRESHOLD))
    return resized


def scale_image_to_height_antialiased(img: Image.Image, target_h: int) -> Image.Image:
    if img.height == 0:
        return img
    scale = target_h / img.height
    if scale <= 0:
        return img
    new_w = max(1, int(round(img.width * scale)))
    new_h = max(1, int(round(img.height * scale)))
    resized = img.resize((new_w, new_h), resample=Image.LANCZOS)
    if ANTIALIAS_BLUR_RADIUS > 0:
        resized = resized.filter(ImageFilter.GaussianBlur(radius=ANTIALIAS_BLUR_RADIUS))
    resized = resized.filter(ImageFilter.UnsharpMask(radius=UNSHARP_RADIUS, percent=UNSHARP_PERCENT, threshold=UNSHARP_THRESHOLD))
    return resized


def load_truetype_candidate() -> Optional[str]:
    try:
        ImageFont.truetype("DejaVuSans.ttf", 20)
        return "DejaVuSans.ttf"
    except Exception:
        sysfont = find_system_font()
        if sysfont:
            try:
                ImageFont.truetype(sysfont, 20)
                return sysfont
            except Exception:
                return None
    return None


def is_image_file(path: str) -> bool:
    ext = str(path).lower()
    return ext.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"))


# ---------- 新增：解析颜色/字体/位置输入（只这部分是改动） ----------
def parse_color_input(s: str) -> Tuple[int, int, int, int]:
    s = (s or "").strip()
    if not s:
        return parse_hex_color_to_rgba(DEFAULT_COLOR_HEX)
    try:
        return parse_hex_color_to_rgba(s)
    except Exception as e:
        print(f"[警告] 颜色解析失败 ({e})，使用默认 {DEFAULT_COLOR_HEX}")
        return parse_hex_color_to_rgba(DEFAULT_COLOR_HEX)


def parse_font_input(s: str) -> Tuple[str, float, Optional[int]]:
    """
    解析字体输入，返回 (mode, ratio, pixels)
    mode: 'auto' 或 'ratio' 或 'pixels'
    ratio: 用于 ratio 模式（0.08 表示 8%）
    pixels: 用于 pixels 模式（整数像素高度）
    支持输入示例： "" / "auto" -> ('auto', DEFAULT_TARGET_RATIO, None)
                     "8%" or "8" -> ('ratio', 0.08, None)
                     "24px" or "24" -> ('pixels', None, 24)
    """
    raw = (s or "").strip().lower()
    if raw == "" or raw == "auto":
        return "auto", DEFAULT_TARGET_RATIO, None
    # 如果包含 '%' 视为百分比
    if raw.endswith("%"):
        try:
            num = float(raw[:-1])
            return "ratio", max(0.001, min(1.0, num / 100.0)), None
        except Exception:
            return "auto", DEFAULT_TARGET_RATIO, None
    # 如果包含 'px' 或仅为整数 -> pixels
    if raw.endswith("px"):
        try:
            px = int(raw[:-2])
            return "pixels", None, max(1, px)
        except Exception:
            return "auto", DEFAULT_TARGET_RATIO, None
    # 若是纯数字，按两种解释：
    # - 若数字 <= 100 则视为百分比（兼容用户输入 "8" 意为 8%）
    # - 若数字 > 100 视为像素
    try:
        num = float(raw)
        if num <= 100:
            return "ratio", max(0.001, min(1.0, num / 100.0)), None
        else:
            return "pixels", None, max(1, int(num))
    except Exception:
        return "auto", DEFAULT_TARGET_RATIO, None


def parse_position_input(s: str) -> str:
    raw = (s or "").strip().lower()
    mapping = {
        "top-left": "topleft", "top_left": "topleft", "topleft": "topleft",
        "top-right": "topright", "top_right": "topright", "topright": "topright",
        "center": "center",
        "bottom-left": "bottomleft", "bottom_left": "bottomleft", "bottomleft": "bottomleft",
        "bottom-right": "bottomright", "bottom_right": "bottomright", "bottomright": "bottomright",
    }
    return mapping.get(raw, DEFAULT_POS)
# -------------------------------------------------------------------


def process_image(path: str, outdir: str, color: Tuple[int, int, int, int],
                  pos: str, font_input_mode: str, font_ratio: Optional[float], font_pixels: Optional[int],
                  quality: int = DEFAULT_JPEG_QUALITY, fallback_to_filetime: bool = FALLBACK_TO_FILETIME):
    path = Path(path)
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img)
    except Exception as e:
        print(f"[错误] 无法打开图片：{path} -> {e}")
        return

    dt = read_exif_date(str(path))
    if not dt and fallback_to_filetime:
        try:
            mtime = path.stat().st_mtime
            dt = datetime.fromtimestamp(mtime)
        except Exception:
            dt = None

    if not dt:
        print(f"[跳过] 无时间信息：{path}")
        return

    text = dt.strftime("%Y-%m-%d")
    img_w, img_h = img.size

    # 计算 target 宽/高（由 font_input_mode 决定）
    target_w = None
    target_h = None
    if font_input_mode in ("auto", "ratio"):
        ratio = font_ratio if font_ratio is not None else DEFAULT_TARGET_RATIO
        target_w = max(1, int(round(img_w * ratio)))
    elif font_input_mode == "pixels":
        target_h = max(1, int(font_pixels)) if font_pixels is not None else None

    # 尝试 TrueType 字体
    truetype_candidate = load_truetype_candidate()
    rgba = ensure_rgba(img)
    draw = ImageDraw.Draw(rgba)

    if truetype_candidate:
        # 使用 TrueType：对 ratio 模式我们按目标宽度调整字体大小；对 pixels 模式直接用像素大小
        if font_input_mode in ("auto", "ratio"):
            guess_size = max(MIN_FONT_SIZE, int(img_w * (font_ratio if font_ratio is not None else DEFAULT_TARGET_RATIO)))
            font = ImageFont.truetype(truetype_candidate, guess_size)
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                tw = bbox[2] - bbox[0]
            except Exception:
                tw, _ = draw.textsize(text, font=font)
            if tw <= 0:
                final_font = font
            else:
                scale = (target_w / tw) if tw > 0 else 1.0
                final_size = max(MIN_FONT_SIZE, int(round(guess_size * scale)))
                final_font = ImageFont.truetype(truetype_candidate, final_size)
        else:  # pixels 模式
            final_size = max(MIN_FONT_SIZE, font_pixels or MIN_FONT_SIZE)
            final_font = ImageFont.truetype(truetype_candidate, final_size)

        # 测量最终尺寸
        try:
            bbox = draw.textbbox((0, 0), text, font=final_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        except Exception:
            tw, th = draw.textsize(text, font=final_font)

        # 位置计算（bottomright 使用比例偏移）
        if pos == "topleft":
            x = DEFAULT_PADDING
            y = DEFAULT_PADDING
        elif pos == "topright":
            x = img_w - tw - DEFAULT_PADDING
            y = DEFAULT_PADDING
        elif pos == "center":
            x = (img_w - tw) // 2
            y = (img_h - th) // 2
        elif pos == "bottomleft":
            x = DEFAULT_PADDING
            y = img_h - th - DEFAULT_PADDING
        else:  # bottomright
            right_offset = int(round(img_w * RIGHT_OFFSET_RATIO))
            bottom_offset = int(round(img_h * BOTTOM_OFFSET_RATIO))
            x = img_w - tw - right_offset
            y = img_h - th - bottom_offset
            if x < 0:
                x = max(0, img_w - tw - DEFAULT_PADDING)
            if y < 0:
                y = max(0, img_h - th - DEFAULT_PADDING)

        # 绘制（带描边）
        try:
            draw_text_with_outline_on_draw(draw, (x, y), text, font=final_font, fill=color, outline_fill=OUTLINE_COLOR, outline_width=OUTLINE_WIDTH)
        except Exception:
            draw.text((x, y), text, font=final_font, fill=color)

        # 保存
        os.makedirs(outdir, exist_ok=True)
        outpath = Path(outdir) / path.name
        ext = path.suffix.lower()
        try:
            if ext in (".jpg", ".jpeg"):
                rgb = rgba.convert("RGB")
                rgb.save(outpath, format="JPEG", quality=quality)
            elif ext == ".png":
                rgba.save(outpath, format="PNG")
            else:
                try:
                    rgba.save(outpath)
                except Exception:
                    rgba.convert("RGB").save(outpath, format="PNG")
        except Exception as e:
            print(f"[错误] 保存失败：{outpath} -> {e}")
            return

        print(f"[已保存] {outpath} （TrueType, 宽={tw}px, 高={th}px, 画布宽={img_w}px）")
        return

    # 无 TrueType：位图渲染并按目标尺寸缩放抗锯齿（保持原行为）
    print("[警告] 未找到 TrueType 字体，使用位图渲染并缩放（抗锯齿）。")
    default_font = ImageFont.load_default()
    wm = render_text_to_image(text, default_font, outline_width=OUTLINE_WIDTH, fill=color, outline_fill=OUTLINE_COLOR)

    # 选择缩放目标：优先 target_w（ratio/auto），否则 target_h（pixels）
    if target_w is not None:
        wm_scaled = scale_image_to_width_antialiased(wm, target_w)
    elif target_h is not None:
        wm_scaled = scale_image_to_height_antialiased(wm, target_h)
    else:
        wm_scaled = wm  # fallback

    tw = wm_scaled.width
    th = wm_scaled.height

    # 位置（bottomright 使用比例偏移）
    if pos == "topleft":
        x = DEFAULT_PADDING
        y = DEFAULT_PADDING
    elif pos == "topright":
        x = img_w - tw - DEFAULT_PADDING
        y = DEFAULT_PADDING
    elif pos == "center":
        x = (img_w - tw) // 2
        y = (img_h - th) // 2
    elif pos == "bottomleft":
        x = DEFAULT_PADDING
        y = img_h - th - DEFAULT_PADDING
    else:  # bottomright
        right_offset = int(round(img_w * RIGHT_OFFSET_RATIO))
        bottom_offset = int(round(img_h * BOTTOM_OFFSET_RATIO))
        x = img_w - tw - right_offset
        y = img_h - th - bottom_offset
        if x < 0:
            x = max(0, img_w - tw - DEFAULT_PADDING)
        if y < 0:
            y = max(0, img_h - th - DEFAULT_PADDING)

    rgba.paste(wm_scaled, (x, y), wm_scaled)

    # 保存
    os.makedirs(outdir, exist_ok=True)
    outpath = Path(outdir) / path.name
    ext = path.suffix.lower()
    try:
        if ext in (".jpg", ".jpeg"):
            rgb = rgba.convert("RGB")
            rgb.save(outpath, format="JPEG", quality=quality)
        elif ext == ".png":
            rgba.save(outpath, format="PNG")
        else:
            try:
                rgba.save(outpath)
            except Exception:
                rgba.convert("RGB").save(outpath, format="PNG")
    except Exception as e:
        print(f"[错误] 保存失败：{outpath} -> {e}")
        return

    print(f"[已保存] {outpath} （位图缩放后 宽={tw}px, 高={th}px, 画布宽={img_w}px）")


def main():
    try:
        input_path = input("请输入图片文件或目录路径（按回车确认）： ").strip()
    except EOFError:
        print("未读取到输入，退出。")
        sys.exit(1)

    if not input_path:
        print("未输入路径，退出。")
        sys.exit(1)

    p = Path(input_path)
    if not p.exists():
        print("指定路径不存在，退出。")
        sys.exit(1)

    targets = []
    if p.is_dir():
        for child in p.iterdir():
            if child.is_file() and is_image_file(child.name):
                targets.append(child)
    else:
        if is_image_file(p.name):
            targets.append(p)
        else:
            print("指定的不是支持的图片文件（支持 jpg/png/webp/bmp/tif 等），退出。")
            sys.exit(1)

    if not targets:
        print("未找到要处理的图片，退出。")
        sys.exit(0)

    print(f"找到 {len(targets)} 张图片。")

    # 三次独立提示（按你的要求）
    # 1) 颜色（hex）
    try:
        color_input = input(f"请输入文字颜色（hex，例如 #FFFFFF，回车=默认 {DEFAULT_COLOR_HEX}）: ").strip()
    except EOFError:
        color_input = ""
    color = parse_color_input(color_input)

    # 2) 字体大小（auto / <percent>% / <pixels>）
    try:
        font_input = input("请输入字体大小（auto / <percent>% / <pixels>，回车=auto）: ").strip()
    except EOFError:
        font_input = ""
    mode, ratio, px = parse_font_input(font_input)

    # 3) 位置（五选一）
    try:
        pos_input = input(f"请输入水印位置（topleft/topright/center/bottomleft/bottomright，回车={DEFAULT_POS}）: ").strip()
    except EOFError:
        pos_input = ""
    pos = parse_position_input(pos_input)

    # 输出目录
    if p.is_dir():
        outdir = p / "_watermark"
    else:
        outdir = p.parent / "_watermark"

    print("开始处理图片...")

    for t in targets:
        process_image(str(t), str(outdir), color=color, pos=pos,
                      font_input_mode=mode, font_ratio=ratio, font_pixels=px,
                      quality=DEFAULT_JPEG_QUALITY, fallback_to_filetime=FALLBACK_TO_FILETIME)

    print("全部处理完成。输出目录：", outdir)


if __name__ == "__main__":
    main()
