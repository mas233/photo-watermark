#!/usr/bin/env python3
"""
img_watermark.py

从 stdin 读取图片文件或目录路径（非递归），将 EXIF 拍摄日期写为水印并保存到原目录的 _watermark 子目录。

改动：
- 添加针对位图水印的抗锯齿处理（缩放后轻微平滑 + 反锐化）。
- 右下角位置：距底部 = 画布高度 * 5%，距右侧 = 画布宽度 * 8%。
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

# piexif 可选
try:
    import piexif
except Exception:
    piexif = None

# ---- 默认配置 ----
DEFAULT_COLOR_HEX = "#FFFFFF"
DEFAULT_POS = "bottomright"  # topleft, topright, center, bottomleft, bottomright
DEFAULT_PADDING = 12
DEFAULT_JPEG_QUALITY = 95
FALLBACK_TO_FILETIME = False
MIN_FONT_SIZE = 12
TARGET_WIDTH_RATIO = 0.10  # 文本目标占画布宽度的比例
OUTLINE_WIDTH = 2
OUTLINE_COLOR = (0, 0, 0, 200)
# 右下偏移比例
BOTTOM_OFFSET_RATIO = 0.05  # 底部空白 = 图片高度 * 5%
RIGHT_OFFSET_RATIO = 0.08   # 右侧空白 = 图片宽度 * 8%
# 抗锯齿器参数
ANTIALIAS_BLUR_RADIUS = 0.6
UNSHARP_RADIUS = 1
UNSHARP_PERCENT = 120
UNSHARP_THRESHOLD = 3
# -------------------


def parse_hex_color(s):
    s = s.strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 3:
        s = ''.join([ch * 2 for ch in s])
    if len(s) != 6:
        raise ValueError("颜色格式应为 RRGGBB 或 #RRGGBB")
    r = int(s[0:2], 16)
    g = int(s[2:4], 16)
    b = int(s[4:6], 16)
    return (r, g, b, 255)


def find_system_font():
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


def _parse_exif_datetime(s):
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


def read_exif_date(path):
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
                for tag in (36867, 306):  # DateTimeOriginal, DateTime
                    v = exif.get(tag)
                    if v:
                        return _parse_exif_datetime(v)
    except Exception:
        return None
    return None


def ensure_rgba(img):
    if img.mode != "RGBA":
        return img.convert("RGBA")
    return img


def draw_text_with_outline_on_draw(draw, xy, text, font, fill, outline_fill=OUTLINE_COLOR, outline_width=OUTLINE_WIDTH):
    x, y = xy
    # draw outline
    for ox in range(-outline_width, outline_width + 1):
        for oy in range(-outline_width, outline_width + 1):
            if ox == 0 and oy == 0:
                continue
            draw.text((x + ox, y + oy), text, font=font, fill=outline_fill)
    # main text
    draw.text((x, y), text, font=font, fill=fill)


def is_image_file(path):
    ext = str(path).lower()
    return ext.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"))


def render_text_to_image(text, font, outline_width=OUTLINE_WIDTH, fill=(255, 255, 255, 255), outline_fill=OUTLINE_COLOR):
    """
    在内存中创建一个紧包围文本的透明 PNG，包含描边，返回 PIL.Image RGBA。
    font 可以是任何 ImageFont（位图或 truetype）。
    """
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


def scale_image_to_width_antialiased(img, target_w):
    """
    缩放并做抗锯齿处理：
    - 使用高质量重采样 LANCZOS
    - 对结果做轻微高斯模糊以平滑锯齿
    - 再做 UnsharpMask 以保留边缘清晰度（综合平滑与清晰）
    """
    if img.width == 0:
        return img
    scale = target_w / img.width
    if scale <= 0:
        return img
    new_w = max(1, int(round(img.width * scale)))
    new_h = max(1, int(round(img.height * scale)))
    resized = img.resize((new_w, new_h), resample=Image.LANCZOS)
    # 轻微平滑
    if ANTIALIAS_BLUR_RADIUS > 0:
        resized = resized.filter(ImageFilter.GaussianBlur(radius=ANTIALIAS_BLUR_RADIUS))
    # 反锐化以恢复边缘（参数可调）
    resized = resized.filter(ImageFilter.UnsharpMask(radius=UNSHARP_RADIUS, percent=UNSHARP_PERCENT, threshold=UNSHARP_THRESHOLD))
    return resized


def process_image(path, outdir, color=(255, 255, 255, 255), pos=DEFAULT_POS, padding=DEFAULT_PADDING,
                  quality=DEFAULT_JPEG_QUALITY, fallback_to_filetime=FALLBACK_TO_FILETIME):
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
    target_w = max(1, int(round(img_w * TARGET_WIDTH_RATIO)))

    # 尝试加载可缩放的 TrueType 字体
    truetype_candidate = None
    try:
        ImageFont.truetype("DejaVuSans.ttf", 20)
        truetype_candidate = "DejaVuSans.ttf"
    except Exception:
        sysfont = find_system_font()
        if sysfont:
            try:
                ImageFont.truetype(sysfont, 20)
                truetype_candidate = sysfont
            except Exception:
                truetype_candidate = None

    rgba = ensure_rgba(img)
    draw = ImageDraw.Draw(rgba)

    if truetype_candidate:
        # 直接按目标宽度来计算合适的字体大小
        size_guess = max(MIN_FONT_SIZE, int(img_w * TARGET_WIDTH_RATIO))
        font = ImageFont.truetype(truetype_candidate, size_guess)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw, _ = draw.textsize(text, font=font)
        if tw == 0:
            final_font = font
        else:
            scale = target_w / tw
            final_size = max(MIN_FONT_SIZE, int(round(size_guess * scale)))
            final_font = ImageFont.truetype(truetype_candidate, final_size)
        # measure final
        try:
            bbox = draw.textbbox((0, 0), text, font=final_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        except Exception:
            tw, th = draw.textsize(text, font=final_font)

        # compute position; special-case bottomright using % 偏移
        if pos == "topleft":
            x = padding
            y = padding
        elif pos == "topright":
            x = img_w - tw - padding
            y = padding
        elif pos == "center":
            x = (img_w - tw) // 2
            y = (img_h - th) // 2
        elif pos == "bottomleft":
            x = padding
            y = img_h - th - padding
        else:  # bottomright: use % 偏移
            right_offset = int(round(img_w * RIGHT_OFFSET_RATIO))
            bottom_offset = int(round(img_h * BOTTOM_OFFSET_RATIO))
            x = img_w - tw - right_offset
            y = img_h - th - bottom_offset
            if x < 0:
                x = max(0, img_w - tw - padding)
            if y < 0:
                y = max(0, img_h - th - padding)

        # Draw with outline
        try:
            draw_text_with_outline_on_draw(draw, (x, y), text, font=final_font, fill=color, outline_fill=OUTLINE_COLOR, outline_width=OUTLINE_WIDTH)
        except Exception:
            draw.text((x, y), text, font=final_font, fill=color)

        # save
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

        print(f"[已保存] {outpath} （文本：{text}，TrueType 字体，文本宽度：{tw}px，画布宽度：{img_w}px）")
        return

    # 如果没有 TrueType 字体：渲染位图水印并缩放 + 抗锯齿处理后叠加
    print("[警告] 未找到可用的 TrueType 字体，将渲染位图水印并通过抗锯齿缩放获得目标大小。")
    default_font = ImageFont.load_default()
    wm = render_text_to_image(text, default_font, outline_width=OUTLINE_WIDTH, fill=color, outline_fill=OUTLINE_COLOR)
    # supersample-ish antialias via our function
    wm_scaled = scale_image_to_width_antialiased(wm, target_w)
    tw = wm_scaled.width
    th = wm_scaled.height

    # compute position; special-case bottomright using % 偏移
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
    else:  # bottomright with % 偏移
        right_offset = int(round(img_w * RIGHT_OFFSET_RATIO))
        bottom_offset = int(round(img_h * BOTTOM_OFFSET_RATIO))
        x = img_w - tw - right_offset
        y = img_h - th - bottom_offset
        if x < 0:
            x = max(0, img_w - tw - DEFAULT_PADDING)
        if y < 0:
            y = max(0, img_h - th - DEFAULT_PADDING)

    rgba.paste(wm_scaled, (x, y), wm_scaled)

    # save
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

    print(f"[已保存] {outpath} （文本：{text}，位图缩放后宽度：{tw}px，画布宽度：{img_w}px）")


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

    if p.is_dir():
        outdir = p / "_watermark"
    else:
        outdir = p.parent / "_watermark"

    # 解析默认颜色
    try:
        color = parse_hex_color(DEFAULT_COLOR_HEX)
    except Exception:
        color = (255, 255, 255, 255)

    for t in targets:
        process_image(str(t), str(outdir), color=color, pos=DEFAULT_POS, padding=DEFAULT_PADDING,
                      quality=DEFAULT_JPEG_QUALITY, fallback_to_filetime=FALLBACK_TO_FILETIME)


if __name__ == "__main__":
    main()
