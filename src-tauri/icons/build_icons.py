"""把 AI 生成的候选图加工成可用的应用图标。

用法：
    python build_icons.py            # 只出对照表（多尺寸 × 明暗底），不改动 icons/
    python build_icons.py --apply A  # 把候选 A 写入 icons/（先备份原文件）

候选图的问题：ImageGen 返回的是 RGB（白底，没有 alpha），圆角外面是白的。
这里用「从四角泛洪」把外部白底抠掉，再裁到内容边界、等比缩放，保证四个候选口径一致。
"""

from __future__ import annotations

import glob
import os
import shutil
import sys

from PIL import Image, ImageChops, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
CAND_DIR = os.path.join(HERE, "candidates")
ART = os.path.abspath(os.path.join(HERE, "..", "..", "tests", ".artifacts", "icons"))

# 候选名 -> 源文件（按生成顺序，A 是靛蓝→青的扁平渐变）
LABELS = {
    "A": "A_modern_app_icon_on_a_transpa",      # 渐变活力 · 靛蓝→青
    "B": "A_vibrant_app_icon_on_a_transp",      # 渐变活力 · 粉→橙
    "C": "A_premium_3D_glassmorphism_app",      # 3D 玻璃
    "D": "A_warm_hand_drawn_app_icon_on_",      # 手绘温暖
}

SIZES = [512, 256, 128, 64, 48, 32, 24, 16]
ICO_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]


def key_out_background(im: Image.Image, thresh: int = 26) -> Image.Image:
    """从四个角泛洪，把与角落同色的外部区域抠成透明。"""
    rgb = im.convert("RGB")
    w, h = rgb.size
    probe = rgb.resize((320, 320), Image.LANCZOS)
    sentinel = (255, 0, 255)
    for corner in [(0, 0), (319, 0), (0, 319), (319, 319)]:
        try:
            ImageDraw.floodfill(probe, corner, sentinel, thresh=thresh)
        except Exception:
            pass
    mask = Image.new("L", probe.size, 0)
    px = probe.load()
    mp = mask.load()
    for y in range(320):
        for x in range(320):
            if px[x, y] == sentinel:
                mp[x, y] = 255
    mask = mask.resize((w, h), Image.LANCZOS)
    # 边缘羽化一点点，避免硬锯齿
    mask = mask.filter(__import__("PIL.ImageFilter", fromlist=["ImageFilter"]).GaussianBlur(1.2))
    out = im.convert("RGBA")
    alpha = out.split()[3]
    out.putalpha(Image.composite(Image.new("L", (w, h), 255), alpha, mask.point(lambda v: 255 - v)))
    return out


def normalize(im: Image.Image, pad: float = 0.0) -> Image.Image:
    """裁到不透明内容的外框，再等比放进正方形画布（留 pad 比例透明边）。"""
    box = im.split()[3].getbbox()
    if box:
        im = im.crop(box)
    side = 1024
    inner = int(side * (1 - 2 * pad))
    w, h = im.size
    scale = inner / max(w, h)
    im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
    return canvas


def mask_rounded(im: Image.Image, ratio: float = 0.22) -> Image.Image:
    """统一圆角。AI 出的图四个角经常不一致（这张只有右下是圆的），必须自己裁一次。"""
    side = im.size[0]
    radius = int(side * ratio)
    ss = 4  # 超采样，避免圆角锯齿
    m = Image.new("L", (side * ss, side * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, side * ss - 1, side * ss - 1], radius * ss, fill=255)
    m = m.resize((side, side), Image.LANCZOS)
    out = im.copy()
    out.putalpha(ImageChops.multiply(out.split()[3], m))
    return out


def down(img: Image.Image, size: int) -> Image.Image:
    """先降到 4 倍再逐步二分，比一次性大比例缩放干净。"""
    cur = img
    while cur.width // 2 >= size:
        cur = cur.resize((cur.width // 2, cur.height // 2), Image.LANCZOS)
    return cur.resize((size, size), Image.LANCZOS)


def source_path(key: str) -> str:
    hits = glob.glob(os.path.join(CAND_DIR, LABELS[key] + "*.png"))
    if not hits:
        raise SystemExit(f"找不到候选 {key} 的源图: {LABELS[key]}")
    return sorted(hits)[0]


def load(key: str) -> Image.Image:
    im = normalize(key_out_background(Image.open(source_path(key))))
    return mask_rounded(im)


def contact_sheet() -> str:
    os.makedirs(ART, exist_ok=True)
    keys = list(LABELS)
    pad, big = 18, 180
    smalls = (48, 32, 24, 16)
    row_h = big + pad
    cell_w = pad + big + pad + sum(s + 14 for s in smalls) + 90
    H = pad + len(keys) * 2 * row_h + pad
    sheet = Image.new("RGB", (cell_w, H), (243, 243, 245))
    d = ImageDraw.Draw(sheet)

    for i, key in enumerate(keys):
        icon = load(key)
        for j, bg in enumerate([(243, 243, 245), (32, 33, 36)]):
            y = pad + (i * 2 + j) * row_h
            sheet.paste(Image.new("RGB", (cell_w, row_h), bg), (0, y))
            fg = (110, 110, 115) if j == 0 else (170, 170, 175)
            d.text((pad, y + row_h // 2 - 4), f"{key} {'浅' if j == 0 else '深'}", fill=fg)
            x = pad + 46
            sheet.paste(down(icon, big), (x, y + (row_h - big) // 2), down(icon, big))
            x += big + pad
            for s in smalls:
                sheet.paste(down(icon, s), (x, y + (row_h - s) // 2), down(icon, s))
                x += s + 14

    path = os.path.join(ART, "candidates-sheet.png")
    sheet.save(path)
    return path


def apply(key: str) -> None:
    img = load(key)
    backup = os.path.join(HERE, "_prev")
    os.makedirs(backup, exist_ok=True)
    for f in glob.glob(os.path.join(HERE, "*.png")) + glob.glob(os.path.join(HERE, "*.ico")):
        dst = os.path.join(backup, os.path.basename(f))
        if not os.path.exists(dst):  # 只在首次备份，避免第二次 apply 把备份覆盖成新图
            shutil.copy2(f, dst)

    pairs = [("32x32.png", 32), ("64x64.png", 64), ("128x128.png", 128),
             ("128x128@2x.png", 256), ("icon.png", 512)]
    for name, size in pairs:
        down(img, size).save(os.path.join(HERE, name))
    # ICO 由 Pillow 从 1024 母版按 sizes 逐档缩放，比 append_images 可靠（后者会只留最后一档）
    img.save(os.path.join(HERE, "icon.ico"), format="ICO", sizes=ICO_SIZES)
    print(f"已写入 icons/（候选 {key}），旧图标备份在 icons/_prev/")


if __name__ == "__main__":
    if "--apply" in sys.argv:
        apply(sys.argv[sys.argv.index("--apply") + 1].upper())
    else:
        print("对照表:", contact_sheet())
