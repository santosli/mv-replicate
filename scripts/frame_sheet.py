#!/usr/bin/env python3
"""Contact sheet for review: pick frames by real decoded index / real PTS, tile them with labels.

Frame picking follows references/conventions.md:
  --frames 0,120,11958         decoded frame indices (display order, from 0)
  --at 1.5,01:02.3,90          the frame on screen at each time (last frame with PTS <= t)
  --every 2 [--range 30-90]    the frame on screen every N seconds (whole video by default)
  --dense 398.6 --span 1 --step 3   every 3rd source frame whose PTS lies in [t - span, t + span]
  --ends                       first, middle and last decoded frame
Pickers combine; repeated hits on the same frame are shown once and counted.

Frames are cropped (--crop x,y,w,h in source pixels) and scaled (--cell width; 0 = source size)
inside ffmpeg, one page at a time with at most --workers pages in flight, so memory stays bounded.
Each cell is labelled with its real frame index and PTS. More than --max cells pages automatically:
sheet.jpg, sheet-2.jpg ... Pages are written under temporary names and published together.
Any failed request exits non-zero (strict); --lenient publishes what was found and reports the rest.
--json writes the selection (requests, hits, duplicates, failures, cell positions) for QA evidence.

  python frame_sheet.py video.mp4 --out sheet.jpg --every 1 [--cell 480 --cols 6] [--json pick.json]
"""
import argparse, json, math, os, subprocess, sys, threading
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvr_common as M   # noqa: E402

LABEL_H = 28
FONT = next((p for p in ['/System/Library/Fonts/Menlo.ttc', '/System/Library/Fonts/Supplemental/Arial.ttf',
                         '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'] if Path(p).exists()), None)


def seconds(v):
    m, _, s = v.rpartition(':')
    t = (int(m) * 60 if m else 0) + float(s)
    if not math.isfinite(t):
        raise ValueError(v)
    return t


def mmss(t):
    return f'{int(t // 60):02d}:{t % 60:06.3f}'


class Picker:
    def __init__(self, tb, pts, ends):
        self.tb, self.pts, self.n = tb, pts, len(pts)
        self.end = Fraction(ends[-1]) * tb
        self.requests, self.hits, self.failed = [], [], []

    def add(self, req, idx, reason=None):
        self.requests.append(req)
        if idx is None or not (0 <= idx < self.n):
            self.failed.append({**req, 'reason': reason or 'outside the decoded frames'})
        else:
            self.hits.append(idx)

    def at(self, t, how):
        if Fraction(repr(t)) >= self.end:
            self.add({'by': how, 't': t}, None, f'after the last frame ends ({float(self.end):.4f}s)')
        else:
            j = M.cover(self.pts, self.tb, t)
            self.add({'by': how, 't': t}, j, None if j is not None else 'before the first frame')


def extract_page(video, tb, idxs, pts, crop, size, state):
    """One ffmpeg pass: select exactly these pts, crop + scale inside ffmpeg, return rgb24 frames."""
    from PIL import Image
    cw, ch = size
    ss = max(Fraction(0), Fraction(pts[idxs[0]]) * tb - 1)
    sel = '+'.join(f'eq(pts\\,{pts[i]})' for i in idxs)
    vf = ([f'crop={crop[2]}:{crop[3]}:{crop[0]}:{crop[1]}'] if crop else []) + \
         [f'select={sel}', f'scale={cw}:{ch}:flags=area', 'format=rgb24']
    p = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{float(ss):.6f}', '-copyts', '-i', str(video), '-map', '0:v:0',
                        '-vf', ','.join(vf), '-fps_mode', 'passthrough', '-f', 'rawvideo', '-'], capture_output=True)
    fb = cw * ch * 3
    got = len(p.stdout) // fb
    images = [Image.frombytes('RGB', (cw, ch), p.stdout[k * fb:(k + 1) * fb]) for k in range(got)]
    with state['lock']:
        state['buffered'] += len(images)
        state['peak'] = max(state['peak'], state['buffered'])
    return images, p.returncode, p.stderr.decode(errors='replace').strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('video')
    ap.add_argument('--out', required=True)
    ap.add_argument('--at')
    ap.add_argument('--frames')
    ap.add_argument('--every', type=float)
    ap.add_argument('--range')
    ap.add_argument('--dense', type=float)
    ap.add_argument('--span', type=float, default=1)
    ap.add_argument('--step', type=int, default=3, help='source-frame step for --dense')
    ap.add_argument('--ends', action='store_true')
    ap.add_argument('--crop')
    ap.add_argument('--cell', type=int, default=480, help='cell width in pixels; 0 keeps the (cropped) source size')
    ap.add_argument('--cols', type=int, default=6)
    ap.add_argument('--max', type=int, default=48, help='cells per page')
    ap.add_argument('--workers', type=int, default=2, help='pages extracted in parallel (bounds memory)')
    ap.add_argument('--lenient', action='store_true', help='publish even if some requests fail')
    ap.add_argument('--json', help='write the selection report here')
    a = ap.parse_args()
    if a.every is not None and not (math.isfinite(a.every) and a.every > 0):
        M.die('[frame_sheet] --every: must be a positive number')
    if a.step < 1 or a.max < 1 or a.cols < 1 or a.workers < 1 or a.cell < 0:
        M.die('[frame_sheet] --step, --max, --cols and --workers must be >= 1; --cell >= 0')
    try:
        info = M.probe(a.video)
        tb, pts, ends = M.frame_table(a.video)
    except (M.MvrError, OSError) as e:
        M.die(e)
    W, H = info['video']['width'], info['video']['height']
    crop = None
    if a.crop:
        try:
            crop = [int(v) for v in a.crop.split(',')]
            assert len(crop) == 4
        except (ValueError, AssertionError):
            M.die(f'[frame_sheet] --crop {a.crop}: expected x,y,w,h integers')
        x, y, w, h = crop
        if x < 0 or y < 0 or w < 2 or h < 2 or x + w > W or y + h > H:
            M.die(f'[frame_sheet] --crop {a.crop}: outside the {W}x{H} frame')
    src_w, src_h = (crop[2], crop[3]) if crop else (W, H)
    cw = a.cell or src_w
    ch = max(2, round(src_h * cw / src_w))

    pk = Picker(tb, pts, ends)
    try:
        for t in (a.at.split(',') if a.at else []):
            pk.at(seconds(t), f'--at {t}')
        for n in (a.frames.split(',') if a.frames else []):
            pk.add({'by': f'--frames {n}'}, int(n))
        if a.every:
            lo, hi = (seconds(x) for x in a.range.split('-')) if a.range else (float(Fraction(pts[0]) * tb), float(pk.end))
            k = 0
            while lo + k * a.every < hi - 1e-9:
                t = round(lo + k * a.every, 9)
                pk.at(t, f'--every {a.every} @ {t:.3f}')
                k += 1
        if a.dense is not None:
            win = [j for j, p in enumerate(pts) if a.dense - a.span <= float(Fraction(p) * tb) <= a.dense + a.span]
            if not win:
                pk.add({'by': f'--dense {a.dense}'}, None, 'no frame in the window')
            for j in win[::a.step]:
                pk.add({'by': f'--dense {a.dense}'}, j)
        if a.ends:
            for j in (0, (len(pts) - 1) // 2, len(pts) - 1):
                pk.add({'by': '--ends'}, j)
    except ValueError as e:
        M.die(f'[frame_sheet] bad time or frame number: {e}')
    if not pk.requests:
        M.die('[frame_sheet] nothing requested: give --at/--frames/--every/--dense/--ends')
    frames = sorted(set(pk.hits))
    if pk.failed and (not a.lenient or not frames):
        for f in pk.failed:
            print('failed:', json.dumps(f, ensure_ascii=False), file=sys.stderr)
        M.die(f'[frame_sheet] {len(pk.failed)} of {len(pk.requests)} requests failed; nothing written'
              + ('' if a.lenient else ' (strict; use --lenient to publish the rest)'))

    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(FONT, 18) if FONT else ImageFont.load_default()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    pages = [frames[p:p + a.max] for p in range(0, len(frames), a.max)]
    state = {'lock': threading.Lock(), 'buffered': 0, 'peak': 0}
    cells, errors = [], []

    def build(pi):
        idxs = pages[pi]
        images, rc, err = extract_page(a.video, tb, idxs, pts, crop, (cw, ch), state)
        try:
            if rc or len(images) != len(idxs):
                errors.append(f'page {pi + 1}: got {len(images)} of {len(idxs)} frames (frames {idxs[0]}..{idxs[-1]})'
                              + (f': {err[-300:]}' if err else ''))
                return None
            cols = min(a.cols, len(idxs))
            rows = -(-len(idxs) // cols)
            sheet = Image.new('RGB', (cols * cw, rows * (ch + LABEL_H)), 'white')
            draw = ImageDraw.Draw(sheet)
            for k, (im, j) in enumerate(zip(images, idxs)):
                x, y = k % cols * cw, k // cols * (ch + LABEL_H)
                sheet.paste(im, (x, y))
                t = float(Fraction(pts[j]) * tb)
                draw.text((x + 6, y + ch + 4), f'{mmss(t)}  #{j}', fill='black', font=font)
                cells.append({'frame': j, 'pts': pts[j], 't': round(t, 6), 'page': pi + 1, 'box': [x, y, cw, ch]})
            name = out if pi == 0 else out.with_name(f'{out.stem}-{pi + 1}{out.suffix}')
            tmp = name.with_name(f'.{name.stem}.{os.getpid()}.tmp{name.suffix}')
            sheet.save(tmp, quality=85)
            return tmp, name
        finally:
            with state['lock']:
                state['buffered'] -= len(images)
            images.clear()

    with ThreadPoolExecutor(a.workers) as ex:
        results = list(ex.map(build, range(len(pages))))
    if errors:
        for r in results:
            if r:
                r[0].unlink(missing_ok=True)
        M.die('[frame_sheet] extraction failed; nothing written: ' + '; '.join(errors))
    made = []
    for tmp, name in results:
        os.replace(tmp, name)
        made.append(str(name))
    rep = {'video': str(a.video), 'requests': len(pk.requests), 'hits': len(pk.hits), 'unique_frames': len(frames),
           'duplicates': len(pk.hits) - len(frames), 'failed': pk.failed, 'sheets': made, 'cell': [cw, ch],
           'crop': crop, 'workers': a.workers, 'cells_per_page': a.max, 'peak_frames_in_memory': state['peak'],
           'cells': sorted(cells, key=lambda c: c['frame'])}
    if a.json:
        M.atomic_write_json(a.json, rep)
    print(json.dumps({k: rep[k] for k in ('requests', 'hits', 'unique_frames', 'duplicates', 'sheets')}
                     | {'failed': len(pk.failed)}, ensure_ascii=False))
    sys.exit(0)        # strict mode already exited on any failure; --lenient reports them above


if __name__ == '__main__':
    main()
