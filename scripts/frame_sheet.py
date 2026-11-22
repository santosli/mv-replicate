"""Contact sheet for review: grab frames at given times and tile them, labelled, into one image
(no single-frame files left on disk). Review the sheet; only open a single frame when the sheet shows
something suspicious.

Usage:
  python frame_sheet.py <video> --out <sheet.jpg> [frame pickers...] [--crop x,y,w,h] [--cell 480] [--cols 6]
Frame pickers (combinable; sorted and de-duplicated):
  --at 1.5,01:02.3,90          given times (seconds or mm:ss.xx)
  --frames 0,120,11958         given frame numbers
  --every 2 [--range 30-90]    one frame every N seconds (whole video by default)
  --dense 398.6 --span 1 --step 3   every `step` frames within `span` s around a moment (motion, ghosting)
  --ends                       first, middle and last frame
--crop cuts a region in source pixels (text, faces, hands); --cell 0 keeps cells at source size.
More than --max cells (default 48) pages automatically: sheet.jpg, sheet-2.jpg, ...
Each cell is labelled with its time and frame number. Write sheets outside version control.
"""
import argparse, io, json, pathlib, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageDraw, ImageFont

LABEL_H = 28
FONT = next((p for p in ['/System/Library/Fonts/Menlo.ttc', '/System/Library/Fonts/Supplemental/Arial.ttf']
             if pathlib.Path(p).exists()), None)


def probe(path):
    j = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-count_packets', '-select_streams', 'v:0', '-show_entries',
                                   'stream=nb_read_packets,r_frame_rate:format=duration', '-of', 'json', str(path)],
                                  capture_output=True, text=True).stdout)
    s = j['streams'][0]; num, den = s['r_frame_rate'].split('/')
    return float(num) / float(den), float(j['format']['duration']), int(s['nb_read_packets'])


def seconds(v):
    m, _, s = v.rpartition(':')
    return (int(m) * 60 if m else 0) + float(s)


def grab(path, frame, fps, crop, last=False):
    vf = [f'crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}'] if crop else []
    # seeking to the last frame by time often lands past the stream end; read it backwards from the end instead
    seek = ['-sseof', '-1'] if last else ['-ss', f'{frame / fps:.4f}']
    vf = vf + ['reverse'] if last else vf
    cmd = ['ffmpeg', '-v', 'error', *seek, '-i', str(path), '-frames:v', '1']
    cmd += (['-vf', ','.join(vf)] if vf else []) + ['-f', 'image2pipe', '-vcodec', 'png', '-']
    data = subprocess.run(cmd, capture_output=True).stdout
    return Image.open(io.BytesIO(data)).convert('RGB') if data else None


def mmss(t): return f'{int(t // 60):02d}:{t % 60:05.2f}'


def build(images, frames, fps, cell, cols):
    w = cell or images[0].width
    h = round(images[0].height * w / images[0].width)
    rows = -(-len(images) // cols)
    sheet = Image.new('RGB', (cols * w, rows * (h + LABEL_H)), 'white')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(FONT, 18) if FONT else ImageFont.load_default()
    for i, (im, n) in enumerate(zip(images, frames)):
        x, y = i % cols * w, i // cols * (h + LABEL_H)
        sheet.paste(im if im.width == w else im.resize((w, h), Image.LANCZOS), (x, y))
        draw.text((x + 6, y + h + 4), f'{mmss(n / fps)}  #{n}', fill='black', font=font)
    return sheet


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('video'); ap.add_argument('--out', required=True)
    ap.add_argument('--at'); ap.add_argument('--frames'); ap.add_argument('--every', type=float)
    ap.add_argument('--range'); ap.add_argument('--dense', type=float); ap.add_argument('--span', type=float, default=1)
    ap.add_argument('--step', type=int, default=3); ap.add_argument('--ends', action='store_true')
    ap.add_argument('--crop'); ap.add_argument('--cell', type=int, default=480); ap.add_argument('--cols', type=int, default=6)
    ap.add_argument('--max', type=int, default=48)
    a = ap.parse_args()

    fps, dur, count = probe(a.video)
    last = count - 1
    want = set()
    if a.at: want |= {round(seconds(t) * fps) for t in a.at.split(',')}
    if a.frames: want |= {int(n) for n in a.frames.split(',')}
    if a.every:
        lo, hi = (seconds(x) for x in a.range.split('-')) if a.range else (0, dur)
        t = lo
        while t < hi: want.add(round(t * fps)); t += a.every
    if a.dense is not None:
        c, r = round(a.dense * fps), round(a.span * fps)
        want |= set(range(c - r, c + r + 1, a.step))
    if a.ends: want |= {0, last // 2, last}
    frames = sorted(n for n in want if 0 <= n <= last)
    if not frames: sys.exit(f'no frames to grab: give --at/--frames/--every/--dense/--ends within 0-{mmss(dur)}')

    crop = [int(v) for v in a.crop.split(',')] if a.crop else None
    with ThreadPoolExecutor(4) as ex:
        images = list(ex.map(lambda n: grab(a.video, n, fps, crop, n == last), frames))
    pairs = [(im, n) for im, n in zip(images, frames) if im]
    out = pathlib.Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    made = []
    for p in range(0, len(pairs), a.max):
        chunk = pairs[p:p + a.max]
        f = out if p == 0 else out.with_name(f'{out.stem}-{p // a.max + 1}{out.suffix}')
        build([c[0] for c in chunk], [c[1] for c in chunk], fps, a.cell, min(a.cols, len(chunk))).save(f, quality=85)
        made.append(str(f))
    print(json.dumps({'frames': len(pairs), 'missing': len(frames) - len(pairs), 'sheets': made}, ensure_ascii=False))


if __name__ == '__main__':
    main()
