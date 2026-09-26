#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 GameMaker 字体图集（PNG + CSV）转换为 TTF。

CSV 格式假设：
    第一行:  元数据（跳过），例如  "Gulim 16px";12;False;False;0;0;1;1
    数据行:  char;x;y;width;height;shift;offset
      - char   = Unicode 码点
      - x, y   = 图集里源矩形左上角
      - width, height = 源尺寸（像素）
      - shift  = 水平 advance
      - offset = 相对笔位置的左偏移 (lsb)

目录结构：
    main.py
    fallback.ttf
    input/  (支持任意层叠子目录)
        x.png / glyphs_x.csv  ->  output/x.ttf
        sub/
            y.png / glyphs_y.csv  ->  output/sub/y.ttf

依赖：
    pip install pillow fonttools
"""

import csv
import copy
import sys
import traceback
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("请先安装 Pillow:  pip install pillow")
    sys.exit(1)

try:
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont
except ImportError:
    print("请先安装 fontTools:  pip install fonttools")
    sys.exit(1)


# ---------------- 配置区 ----------------
BASE_DIR      = Path(__file__).resolve().parent
INPUT_DIR     = BASE_DIR / "input"
OUTPUT_DIR    = BASE_DIR / "output"
FALLBACK_PATH = BASE_DIR / "fallback.ttf"

# 图集里每个字形单元格的基线位置（从单元格顶部算起的像素数）。
# 对于 height=16 的像素字体，12 表示基线在顶部下方 12 像素处
# （即顶部 12px 供字形主体、下面 4px 供降部）。
# 如果生成的字整体偏高/偏低，调这个数字。
BASELINE_FROM_TOP = 12

# 透明度阈值
ALPHA_THRESHOLD = 127
# ----------------------------------------


# ============== CSV 解析 ==============

def _to_int(s):
    try:
        return int(s)
    except (ValueError, TypeError):
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return 0


def parse_csv(csv_path: Path) -> dict:
    """解析 GameMaker 字体 CSV。

    返回 {codepoint: {'x','y','w','h','shift','offset'}}
    """
    result = {}
    with open(csv_path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f, delimiter=';')
        for row in reader:
            if not row or not any(c.strip() for c in row):
                continue
            first = row[0].strip()
            # 跳过元数据行（引号开头 / 非数字）
            if first.startswith('"') or not first.lstrip('-').isdigit():
                continue
            try:
                cp = int(first)
            except ValueError:
                continue
            if len(row) < 5:
                continue

            x      = _to_int(row[1])
            y      = _to_int(row[2])
            w      = _to_int(row[3])
            h      = _to_int(row[4])
            shift  = _to_int(row[5]) if len(row) > 5 else w
            offset = _to_int(row[6]) if len(row) > 6 else 0

            result[cp] = {
                'x': x, 'y': y, 'w': w, 'h': h,
                'shift': shift, 'offset': offset,
            }
    return result


# ============== 位图 -> 轮廓 ==============

def bitmap_from_atlas(atlas_img: Image.Image, x, y, w, h):
    """从图集里裁出字形位图，返回 0/1 的二维列表。"""
    if atlas_img.mode != 'RGBA':
        atlas_img = atlas_img.convert('RGBA')
    crop = atlas_img.crop((x, y, x + w, y + h))
    pixels = list(crop.getdata())

    has_alpha = any(p[3] < 250 for p in pixels)

    bitmap = []
    idx = 0
    for _ in range(h):
        row = []
        for _ in range(w):
            p = pixels[idx]; idx += 1
            if has_alpha:
                row.append(1 if p[3] > ALPHA_THRESHOLD else 0)
            else:
                lum = (p[0] * 299 + p[1] * 587 + p[2] * 114) // 1000
                row.append(1 if lum < 128 else 0)
        bitmap.append(row)
    return bitmap


def glyph_from_bitmap(bitmap, x_origin, y_top, scale):
    """位图 -> TTGlyph（每个连续水平像素段变成一个填充矩形）。"""
    pen = TTGlyphPen(None)
    h = len(bitmap)
    w = len(bitmap[0]) if h else 0

    for row in range(h):
        col = 0
        while col < w:
            if bitmap[row][col]:
                start = col
                while col < w and bitmap[row][col]:
                    col += 1
                end = col
                x0 = x_origin + start * scale
                x1 = x_origin + end   * scale
                y1 = y_top - row       * scale
                y0 = y_top - (row + 1) * scale
                pen.moveTo((x0, y0))
                pen.lineTo((x1, y0))
                pen.lineTo((x1, y1))
                pen.lineTo((x0, y1))
                pen.closePath()
            else:
                col += 1
    return pen.glyph()


def make_notdef(upem):
    """给 .notdef 生成一个空心方框。"""
    pen = TTGlyphPen(None)
    w = upem // 2
    h = int(upem * 0.7)
    t = max(2, upem // 60)
    pen.moveTo((0, 0))
    pen.lineTo((w, 0))
    pen.lineTo((w, h))
    pen.lineTo((0, h))
    pen.closePath()
    pen.moveTo((t, t))
    pen.lineTo((t, h - t))
    pen.lineTo((w - t, h - t))
    pen.lineTo((w - t, t))
    pen.closePath()
    return pen.glyph()


# ============== 构建 TTF ==============

def build_ttf(glyph_data: dict, atlas_path: Path, fallback_path: Path,
              out_path: Path, family_name: str):
    atlas = Image.open(atlas_path)

    # --- 加载 fallback ---
    fb_font = None
    if fallback_path and Path(fallback_path).exists():
        try:
            fb_font = TTFont(str(fallback_path))
        except Exception as e:
            print(f"  警告：加载 fallback.ttf 失败：{e}")

    # --- 确定 upem ---
    upem = fb_font['head'].unitsPerEm if fb_font else 1000

    # --- 确定像素大小：用字形高度 ---
    heights = [g['h'] for g in glyph_data.values() if g['h'] > 0]
    pixel_h = max(heights) if heights else 16
    scale = upem / float(pixel_h)

    glyph_order = ['.notdef']
    glyphs = {'.notdef': make_notdef(upem)}
    hmtx = {'.notdef': (upem // 2, 0)}
    cmap = {}
    used_names = {'.notdef'}

    def unique(name):
        base = name
        i = 1
        while name in used_names:
            name = f"{base}_{i}"
            i += 1
        used_names.add(name)
        return name

    # --- 从图集生成字形 ---
    for cp, info in sorted(glyph_data.items()):
        if cp in cmap:
            continue
        name = unique(f"u{cp:04X}" if cp <= 0xFFFF else f"u{cp:X}")

        w, h = info['w'], info['h']
        advance = int(round(info['shift'] * scale))
        if advance < 1:
            advance = max(1, int(round(w * scale)))

        # 空白字符（宽/高为 0，如 space 之外）
        if w <= 0 or h <= 0:
            glyphs[name] = TTGlyphPen(None).glyph()
            hmtx[name] = (max(1, advance), 0)
            glyph_order.append(name)
            cmap[cp] = name
            continue

        bitmap = bitmap_from_atlas(atlas, info['x'], info['y'], w, h)

        # x_origin: 从画笔位置向右偏移 offset 像素
        x_origin = info['offset'] * scale
        # 单元格顶部相对基线的 y = BASELINE_FROM_TOP
        # 由于 TTF 是 y 轴向上，所以顶部在 +BASELINE_FROM_TOP 处
        y_top = BASELINE_FROM_TOP * scale

        g = glyph_from_bitmap(bitmap, x_origin, y_top, scale)
        glyphs[name] = g
        glyph_order.append(name)

        try:
            xs = [pt[0] for pt in g.coordinates]
            lsb = int(min(xs)) if xs else int(x_origin)
        except Exception:
            lsb = int(round(x_origin))

        hmtx[name] = (advance, lsb)
        cmap[cp] = name

    # --- 用 fallback 补全缺失字符 ---
    if fb_font is not None:
        fb_glyf = fb_font['glyf']
        fb_hmtx = fb_font['hmtx']
        fb_cmap = fb_font.getBestCmap() or {}
        fb_order_set = set(fb_font.getGlyphOrder())

        needed = {}

        def collect(name):
            if name in needed or name not in fb_order_set:
                return
            new_name = unique("fb_" + name)
            needed[name] = new_name
            g = fb_glyf[name]
            if g.isComposite():
                for comp in g.components:
                    collect(comp.glyphName)

        for cp, fb_name in fb_cmap.items():
            if cp in cmap:
                continue
            collect(fb_name)

        for fb_name, new_name in needed.items():
            g = copy.deepcopy(fb_glyf[fb_name])
            if g.isComposite():
                for comp in g.components:
                    comp.glyphName = needed.get(comp.glyphName, comp.glyphName)
            glyphs[new_name] = g
            glyph_order.append(new_name)
            try:
                adv, lsb = fb_hmtx[fb_name]
            except KeyError:
                adv, lsb = upem // 2, 0
            hmtx[new_name] = (adv, lsb)

        for cp, fb_name in fb_cmap.items():
            if cp in cmap:
                continue
            if fb_name in needed:
                cmap[cp] = needed[fb_name]

    # --- 度量 ---
    if fb_font is not None:
        asc = fb_font['hhea'].ascent
        desc = fb_font['hhea'].descent
        line_gap = fb_font['hhea'].lineGap
    else:
        asc = int(upem * 0.8)
        desc = -int(upem * 0.2)
        line_gap = 0

    # --- 组装 ---
    fb = FontBuilder(upem, isTTF=True)
    fb.setupGlyphOrder(glyph_order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics(hmtx)
    fb.setupHorizontalHeader(ascent=asc, descent=desc, lineGap=line_gap)
    fb.setupNameTable({
        'familyName': family_name,
        'styleName': 'Regular',
        'uniqueFontIdentifier': f"{family_name} Regular",
        'fullName': f"{family_name} Regular",
        'psName': family_name.replace(' ', '') + '-Regular',
        'version': 'Version 1.0',
    })
    fb.setupOS2(
        sTypoAscender=asc,
        sTypoDescender=desc,
        sTypoLineGap=line_gap,
        usWinAscent=max(0, asc),
        usWinDescent=max(0, -desc),
        achVendID='NONE',
        fsSelection=0x40,        # REGULAR
        usWeightClass=400,
        usWidthClass=5,
    )
    fb.setupPost()
    fb.save(str(out_path))


# ============== 文件匹配 / 主流程 ==============

def find_csv_for_png(png_path: Path):
    """找 PNG 对应的 CSV：先同名，再 glyphs_ 前缀。"""
    same = png_path.with_suffix('.csv')
    if same.exists():
        return same
    prefixed = png_path.with_name('glyphs_' + png_path.stem + '.csv')
    if prefixed.exists():
        return prefixed
    return None


def process_png(png_path: Path, output_root: Path):
    csv_path = find_csv_for_png(png_path)
    if csv_path is None:
        print(f"[跳过] {png_path} : 找不到匹配的 CSV")
        return

    # 相对 input 计算输出路径，保留层叠目录结构
    try:
        rel = png_path.relative_to(INPUT_DIR)
    except ValueError:
        rel = Path(png_path.name)

    out_path = output_root / rel.with_suffix('.ttf')
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[处理] {png_path} + {csv_path.name}  ->  {out_path}")

    try:
        glyph_data = parse_csv(csv_path)
        if not glyph_data:
            print("  警告：CSV 里没有解析到任何字形，跳过")
            return
        build_ttf(glyph_data, png_path, FALLBACK_PATH,
                  out_path, png_path.stem)
        print(f"  完成：{len(glyph_data)} 个字形")
    except Exception:
        print("  失败：")
        traceback.print_exc()


def main():
    if not INPUT_DIR.exists():
        print(f"输入目录不存在：{INPUT_DIR}")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 递归查找 input 下所有 PNG
    png_files = sorted(INPUT_DIR.rglob('*.png'))
    if not png_files:
        print(f"{INPUT_DIR} 下没有找到 PNG 文件")
        return

    if not FALLBACK_PATH.exists():
        print(f"提示：没有 fallback.ttf（{FALLBACK_PATH}）；缺失字符将没有字形。")

    for png_path in png_files:
        process_png(png_path, OUTPUT_DIR)

    print("全部完成。")


if __name__ == '__main__':
    main()