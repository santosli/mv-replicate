#!/usr/bin/env python3
"""Animated bottom lyrics as one transparent layer for the whole song.

Five styles: karaoke, pop, terminal, bubbles, lightbar. Character times come from
lyric_chars.py (whisper DTW). Style values use a 720 px-high reference canvas and scale
with the frame. For style trials, render 8 s of a few styles with --start / --duration.

  python lyric_layer.py lyric-chars.json --style bubbles \
      --set ink=#3B82F6 --set lift=22 --set pop_scale=0.3 --yield 标题字@34.12 --duration 68 --out lyrics.mov

  --yield TEXT@T   the line containing TEXT is cut before TEXT and ends at T (a title card
                   shows those words; the bottom line gives way instead of doubling them)
Then overlay it in the EDL as {"type": "layer", "src": "<lyrics.mov>", "at": 0, "dur": <song>}.

Font: --font, else $MV_REPLICATE_FONT, else HarmonyOS Sans SC Black if installed
(check the font's own licence before publishing).
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterator

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvr_common as M  # noqa: E402


def default_font():
    """Body font: $MV_REPLICATE_FONT, else HarmonyOS Sans SC Black in a usual font folder."""
    env = os.environ.get('MV_REPLICATE_FONT')
    if env:
        return Path(env).expanduser()
    for d in ('~/Library/Fonts', '/Library/Fonts', '~/.local/share/fonts', '~/.fonts', '/usr/share/fonts/truetype'):
        p = Path(d).expanduser() / 'HarmonyOS_Sans_SC_Black.ttf'
        if p.exists():
            return p
    sys.exit('no default font: set MV_REPLICATE_FONT=/path/to/a/CJK/font.ttf or pass --font (see README)')

# Pixel values in the style settings use a 720px-high reference canvas.
STYLES = {
    "karaoke": dict(size=51, tracking=5, stroke=3, ink="#112A50",
                    neutral="#EAF5FF", active="#43EDFF", sweep_head=0.23),
    "pop": dict(size=50, tracking=5, stroke=3, ink="#522232",
                neutral="#FFF5E0", active="#FFD06D", pop_time=0.23,
                settle_y=8, overshoot=0.36),
    "terminal": dict(size=46, tracking=5, stroke=1, ink="#102C38",
                     neutral="#C3E0EB", active="#85FFE8", panel=(8, 28, 40, 225),
                     glitch_time=0.075, split=5, accent="#62DDDA", magenta="#FF56A6"),
    "bubbles": dict(size=49, tracking=6, stroke=5, ink="#93DCFF", edge="#164B9B",
                    neutral="#FFFFFF", active="#B8FAFF", lift=9, tilt=7,
                    bubble_time=0.19, bubble=(116, 224, 255)),
    "lightbar": dict(size=49, tracking=5, stroke=2, ink="#252B4C",
                     neutral="#F8F5FF", active="#FFE092", ribbon=(30, 27, 53, 228),
                     slide=66, slide_time=0.24, shine_time=0.56),
}

# The first frame with an effect should land before its source cue even after
# rounding to the frame grid. Cap the lead at 30 ms for normal video rates.
def cue_lead(fps: float) -> float:
    return min(1.5 / fps, 0.030)


def clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def ease_out(value: float) -> float:
    return 1 - (1 - clamp(value)) ** 3


@dataclass(frozen=True)
class Token:
    text: str
    start: float
    end: float
    sung: bool = True


@dataclass(frozen=True)
class Lyric:
    start: float
    end: float
    tokens: tuple[Token, ...]


def parse_line(raw: dict) -> Lyric:
    """Keep punctuation from line text without assigning it a vocal cue."""
    tokens = []
    text = raw["line"]
    chars = raw["chars"]
    position = 0
    for index, char in enumerate(chars):
        word, start = char["c"], char["t"]
        at = text.index(word, position)
        if at > position:
            punct_at = tokens[-1].start if tokens else start
            tokens.append(Token(text[position:at], punct_at, punct_at, sung=False))
        end = chars[index + 1]["t"] if index + 1 < len(chars) else raw["end"]
        tokens.append(Token(word, start, end))
        position = at + len(word)
    if position < len(text):
        tokens.append(Token(text[position:], tokens[-1].start, tokens[-1].start, sung=False))
    return Lyric(min(raw["start"], chars[0]["t"]), raw["end"], tuple(tokens))


class Typography:
    def __init__(self, lyric: Lyric, size: tuple[int, int], style: dict, font_path: Path,
                 *, lower_left: bool = False):
        self.lyric = lyric
        self.width, self.height = size
        self.style = style
        self.lower_left = lower_left
        self.scale = self.height / 720
        self.baseline = round(self.height * 0.906)
        self.tracking = self.px(style["tracking"])
        self.stroke = max(1, self.px(style["stroke"]))
        extra = self.px(2) if "edge" in style else 0
        font_size = max(1, self.px(style["size"]))
        while True:
            font = ImageFont.truetype(str(font_path), font_size)
            advances = [round(font.getlength(token.text)) for token in lyric.tokens]
            line_width = sum(advances) + self.tracking * (len(advances) - 1)
            if line_width <= self.width * 0.80 or font_size == 1:
                break
            font_size = max(1, font_size - 2)
        self.font = font
        self.left = round(self.width * 0.045) if lower_left else round((self.width - line_width) / 2)
        self.right = self.left + line_width
        self.padding = self.stroke + extra + self.px(4)
        self.cells = []
        x = self.left
        for token, advance in zip(lyric.tokens, advances, strict=True):
            self.cells.append((token, x, advance))
            x += advance + self.tracking

    def px(self, value: float) -> int:
        return round(value * self.scale)

    @lru_cache(maxsize=256)
    def glyph(self, text: str, fill: str) -> Image.Image:
        ascent, descent = self.font.getmetrics()
        image = Image.new("RGBA", (round(self.font.getlength(text)) + self.padding * 2,
                                   ascent + descent + self.padding * 2))
        draw = ImageDraw.Draw(image)
        anchor = (self.padding, self.padding + ascent)
        if "edge" in self.style:
            draw.text(anchor, text, font=self.font, anchor="ls", fill=fill,
                      stroke_width=self.stroke + self.px(2), stroke_fill=self.style["edge"])
        draw.text(anchor, text, font=self.font, anchor="ls", fill=fill,
                  stroke_width=self.stroke, stroke_fill=self.style["ink"])
        return image

    def place(self, layer: Image.Image, glyph: Image.Image, x: int, dy: int = 0,
              scale: float = 1, angle: float = 0) -> None:
        y = self.baseline - self.font.getmetrics()[0] - self.padding + dy
        center_x = x - self.padding + glyph.width / 2
        center_y = y + glyph.height / 2
        if scale != 1:
            glyph = glyph.resize((max(1, round(glyph.width * scale)),
                                  max(1, round(glyph.height * scale))), Image.Resampling.LANCZOS)
        if angle:
            glyph = glyph.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
        layer.alpha_composite(glyph, (round(center_x - glyph.width / 2),
                                      round(center_y - glyph.height / 2)))

    def current_cell(self, time: float):
        return next((cell for cell in reversed(self.cells)
                     if cell[0].sung and cell[0].start <= time), None)


def karaoke(layer: Image.Image, layout: Typography, time: float) -> None:
    style = layout.style
    for token, x, advance in layout.cells:
        neutral = layout.glyph(token.text, style["neutral"])
        glyph = neutral
        if time >= token.start:
            active = layout.glyph(token.text, style["active"])
            progress = 1.0
            if token.sung:
                progress = clamp((time - token.start) / max(0.001, token.end - token.start))
                progress = max(style["sweep_head"], progress)
            if progress == 1:
                glyph = active
            else:
                # Replace the painted pixels in place, preserving the original alpha.
                edge = layout.padding + round(advance * progress)
                glyph = neutral.copy()
                glyph.paste(active.crop((0, 0, edge, active.height)), (0, 0))
        layout.place(layer, glyph, x)


def pop(layer: Image.Image, layout: Typography, time: float) -> None:
    style = layout.style
    draw = ImageDraw.Draw(layer)
    for token, x, advance in layout.cells:
        age = time - token.start
        if age < 0:
            continue
        progress = clamp(age / style["pop_time"])
        if token.sung and progress < 1:
            bounce = style["overshoot"] * math.sin(math.pi * progress) * (1 - progress)
            glyph_scale = 1 + bounce
            vertical = layout.px(style["settle_y"] * math.cos(math.pi * progress) * (1 - progress) ** 2)
            if progress < 0.65:
                center = x + advance / 2
                radius = layout.px(11 + 8 * progress)
                y = layout.baseline + layout.px(5)
                draw.line((center - radius, y, center + radius, y),
                          fill=(255, 169, 86, round(220 * (1 - progress))), width=max(1, layout.px(3)))
        else:
            glyph_scale, vertical = 1, 0
        fill = style["active"] if token.sung and progress < 0.85 else style["neutral"]
        layout.place(layer, layout.glyph(token.text, fill), x, vertical, glyph_scale)


def terminal(layer: Image.Image, layout: Typography, time: float) -> None:
    style = layout.style
    px = layout.px
    draw = ImageDraw.Draw(layer)
    left, right = layout.left - px(29), layout.right + px(29)
    top, bottom = layout.baseline - px(53), layout.baseline + px(10)
    cut = px(11)
    corners = [(left + cut, top), (right, top), (right, bottom - cut),
               (right - cut, bottom), (left, bottom), (left, top + cut)]
    draw.polygon(corners, fill=style["panel"])
    draw.line(corners + [corners[0]], fill=(76, 199, 203, 215), width=max(1, px(1)))
    for x, direction in [(left + px(12), 1), (right - px(12), -1)]:
        draw.line([(x + direction * px(6), top + px(20)), (x, top + px(20)),
                   (x, bottom - px(18)), (x + direction * px(6), bottom - px(18))],
                  fill=style["accent"], width=max(1, px(2)))
    current = layout.current_cell(time)
    for cell in layout.cells:
        token, x, advance = cell
        is_current = cell is current
        if is_current:
            age = time - token.start
            shift = px(style["split"] * (1 - ease_out(age / style["glitch_time"])))
            if shift:
                # Offset a thin horizontal slice, leaving the full glyph readable.
                for offset, color in [(-shift, style["accent"]), (shift, style["magenta"])]:
                    glyph = layout.glyph(token.text, color)
                    slice_y = layout.padding + px(22)
                    strip = Image.new("RGBA", glyph.size)
                    strip.paste(glyph.crop((0, slice_y, glyph.width, slice_y + px(15))), (0, slice_y))
                    layout.place(layer, strip, x + offset)
            draw.line((x, bottom - px(5), x + advance, bottom - px(5)),
                      fill=style["accent"], width=max(1, px(2)))
        color = style["active"] if is_current else style["neutral"]
        layout.place(layer, layout.glyph(token.text, color), x)


def bubbles(layer: Image.Image, layout: Typography, time: float) -> None:
    style = layout.style
    px = layout.px
    particles = Image.new("RGBA", layer.size)
    draw = ImageDraw.Draw(particles)
    current = layout.current_cell(time)
    for index, cell in enumerate(layout.cells):
        token, x, advance = cell
        is_current = cell is current
        tilt = (-1 if index % 2 else 1) * style["tilt"]
        vertical = px(-2 if index % 2 else 1)
        lift = 0.0
        if is_current:
            progress = clamp((time - token.start) / style["bubble_time"])
            lift = 1 - ease_out(progress)
            vertical -= px(style["lift"] * lift)
            tilt *= 1 + 0.45 * lift
            for dx, dy, radius in [(-19, 16, 4), (18, 9, 5), (28, 25, 3)]:
                bx = x + advance / 2 + px(dx * (0.7 + 0.3 * progress))
                by = layout.baseline - px(27 + dy + 11 * progress)
                r = px(radius + progress * 2)
                alpha = round(235 * (1 - progress))
                draw.ellipse((bx-r, by-r, bx+r, by+r),
                             outline=(*style["bubble"], alpha), width=max(1, px(2)))
        color = style["active"] if is_current else style["neutral"]
        scale = 1 + style.get("pop_scale", 0) * (lift if is_current else 0)
        layout.place(layer, layout.glyph(token.text, color), x, vertical, scale=scale, angle=tilt)
    ornaments = [(layout.left - px(27), -1)]
    if not layout.lower_left:
        ornaments.append((layout.right + px(27), 1))
    for x, direction in ornaments:
        y = layout.baseline - px(20)
        draw.ellipse((x-px(6), y-px(6), x+px(6), y+px(6)),
                     fill=(224, 250, 255, 244), outline="#2D70C0", width=max(1, px(2)))
        x += direction * px(13)
        y -= px(13)
        draw.ellipse((x-px(3), y-px(3), x+px(3), y+px(3)),
                     fill=(224, 250, 255, 240), outline="#2D70C0", width=max(1, px(1)))
    layer.alpha_composite(particles)


def lightbar(layer: Image.Image, layout: Typography, time: float) -> None:
    style = layout.style
    px = layout.px
    age = time - layout.lyric.start
    offset = 0 if layout.lower_left else px(style["slide"] * (1 - ease_out(age / style["slide_time"])))
    left, right = layout.left - px(38) + offset, layout.right + px(38) + offset
    top, bottom = layout.baseline - px(52), layout.baseline + px(13)
    draw = ImageDraw.Draw(layer)
    draw.polygon([(left + px(22), top), (right, top), (right - px(22), bottom),
                  (left, bottom)], fill=style["ribbon"])
    draw.line((left + px(15), bottom - px(2), right - px(36), bottom - px(2)),
              fill=(220, 177, 103, 150), width=max(1, px(1)))
    if age < style["shine_time"]:
        progress = clamp(age / style["shine_time"])
        x = left + px(30) + progress * (right - left - px(72))
        draw.line((x - px(12), bottom - px(2), x + px(12), bottom - px(2)),
                  fill=(255, 225, 164, round(230 * math.sin(math.pi * progress))),
                  width=max(1, px(2)))
    current = layout.current_cell(time)
    for cell in layout.cells:
        token, x, _ = cell
        color = style["active"] if cell is current else style["neutral"]
        layout.place(layer, layout.glyph(token.text, color), x + offset)


PAINTERS = {"karaoke": karaoke, "pop": pop, "terminal": terminal,
            "bubbles": bubbles, "lightbar": lightbar}


def overlay_frames(style_name: str, lyric_json: Path | dict, start: float, end: float,
                   width: int, height: int, fps: float, *,
                   font_path: Path | None = None,
                   lower_left_until: float | None = None) -> Iterator[Image.Image]:
    """Yield straight-alpha RGBA frames using start/end on the original song clock.

    An opening close-up may show only the sung prefix at lower left until a cut.
    That cut uses the video clock, without the lead applied to vocal cues.
    """
    if end <= start or fps <= 0 or min(width, height) <= 0:
        raise ValueError("Expected positive dimensions, fps, and clip duration")
    font_path = font_path or default_font()
    data = lyric_json if isinstance(lyric_json, dict) else json.loads(Path(lyric_json).read_text())
    lead = cue_lead(fps)
    lines = sorted(
        (parse_line(line) for line in data["lines"] if line["chars"] and line["end"] > start),
        key=lambda line: line.start,
    )
    layouts = [Typography(line, (width, height), STYLES[style_name], font_path)
               for line in lines if line.start < end + lead]
    starts = [layout.lyric.start for layout in layouts]
    paint = PAINTERS[style_name]

    @lru_cache(maxsize=16)
    def closeup_layout(line: Lyric, prefix: tuple[Token, ...]) -> Typography:
        return Typography(Lyric(line.start, line.end, prefix), (width, height),
                          STYLES[style_name], font_path, lower_left=True)

    for index in range(M.Q(end, fps) - M.Q(start, fps)):
        layer = Image.new("RGBA", (width, height))
        video_time = start + index / fps
        time = video_time + lead
        active = bisect_right(starts, time) - 1
        if active >= 0 and time < layouts[active].lyric.end:
            layout = layouts[active]
            if lower_left_until is not None and video_time < lower_left_until:
                line = layout.lyric
                prefix = tuple(token for token in line.tokens if token.start <= time)
                if not prefix:
                    yield layer
                    continue
                layout = closeup_layout(line, prefix)
            paint(layer, layout, time)
        yield layer



def char_spans(ln: dict) -> list[int]:
    """Text position of each sung unit in the line (same walk as parse_line)."""
    pos, out = 0, []
    for ch in ln["chars"]:
        at = ln["line"].index(ch["c"], pos)
        out.append(at)
        pos = at + len(ch["c"])
    return out


def yield_candidates(data: dict, text: str) -> list[dict]:
    """Every occurrence of `text` (in any line) with the time its first sung unit is sung."""
    cands = []
    for li, ln in enumerate(data["lines"]):
        if not ln.get("chars"):
            continue
        spans = char_spans(ln)
        s = ln["line"].find(text)
        while s >= 0:
            ks = [k for k, p in enumerate(spans) if s <= p < s + len(text)]
            if ks:
                cands.append({"index": len(cands), "line": li, "pos": s, "unit": ks[0],
                              "t": ln["chars"][ks[0]]["t"], "text": ln["line"]})
            s = ln["line"].find(text, s + 1)
    return cands


def yield_to_title(data: dict, text: str, at: float, index: int | None = None) -> dict:
    """The one occurrence of `text` sung nearest `at` (or candidate #index) gives way to a title card:
    its line is cut before `text` (text, chars and first_char_t together) and ends at `at`.
    Other occurrences are untouched. Raises ValueError when the choice is missing or ambiguous."""
    cands = yield_candidates(data, text)
    listing = "; ".join(f"#{c['index']} line {c['line']} at {c['t']:.2f}s" for c in cands)
    if not cands:
        raise ValueError(f"--yield {text}@{at}: no sung occurrence of {text!r}")
    if index is not None:
        if not 0 <= index < len(cands):
            raise ValueError(f"--yield {text}@{at}#{index}: candidates are {listing}")
        c = cands[index]
    else:
        cands_by = sorted(cands, key=lambda c: abs(c["t"] - at))
        c = cands_by[0]
        if len(cands_by) > 1 and abs(abs(cands_by[1]["t"] - at) - abs(c["t"] - at)) < 0.05:
            raise ValueError(f"--yield {text}@{at} is ambiguous ({listing}); add #index, e.g. {text}@{at}#{c['index']}")
        if abs(c["t"] - at) > 2.0:
            raise ValueError(f"--yield {text}@{at}: nearest occurrence is sung at {c['t']:.2f}s ({listing})")
    lines = [dict(ln) for ln in data["lines"]]
    ln = lines[c["line"]]
    ln["line"] = ln["line"][:c["pos"]]
    ln["chars"] = ln["chars"][:c["unit"]]
    ln["end"] = round(max(at, ln["start"]), 3)
    ln["first_char_t"] = ln["chars"][0]["t"] if ln["chars"] else None
    ln["yielded"] = {"text": text, "at": at, "candidate": c["index"]}
    return {**data, "lines": lines}


def parse_value(v: str):
    if v.startswith("#"):
        return v
    try:
        return json.loads(v)          # numbers, lists such as [170,230,255]
    except json.JSONDecodeError:
        return v


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("lyrics", type=Path, help="lyric-chars.json from lyric_chars.py")
    ap.add_argument("--style", choices=STYLES, default="bubbles")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a style value")
    ap.add_argument("--yield", dest="yields", action="append", default=[], metavar="TEXT@T[#k]",
                    help="the occurrence of TEXT sung nearest T gives way to a title (k picks a listed candidate)")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--fps", default="60", help="number or num/den")
    ap.add_argument("--font", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    style = dict(STYLES[a.style])
    for kv in a.set:
        k, v = kv.split("=", 1)
        style[k] = parse_value(v)
    STYLES["_custom"], PAINTERS["_custom"] = style, PAINTERS[a.style]
    try:
        data = M.load_json(a.lyrics)
        for y in a.yields:
            text, at = y.rsplit("@", 1)
            index = None
            if "#" in at:
                at, k = at.split("#", 1)
                index = int(k)
            data = yield_to_title(data, text, float(at), index)
    except (ValueError, KeyError) as e:
        M.die(f"[lyric_layer] {e}")
    w, h = map(int, a.size.split("x"))
    fps = M.frac(a.fps)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.out.with_name(f".{a.out.stem}.{os.getpid()}.tmp{a.out.suffix}")
    try:
        n = M.encode_rgba(overlay_frames("_custom", data, a.start, a.start + a.duration, w, h, fps,
                                         font_path=a.font), w, h, fps, tmp)
        os.replace(tmp, a.out)
    except M.MvrError as e:
        tmp.unlink(missing_ok=True)
        M.die(e)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    print("wrote", a.out, f"({n} frames)")


if __name__ == "__main__":
    main()
