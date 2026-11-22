#!/usr/bin/env python3
"""Skeleton generator: build an EDL from a beat-indexed PLAN, then render it.

Copy this file for each MV (and for each new version: make_edl_v2.py, ...), edit CLIPS, AVOID,
PLAN and overlays(), and re-run. Change cuts here, never by hand in the generated JSON.

Expects in --work (all paths below are relative to it):
  song/song-analysis.json   from scripts/analyze_song.py
  song/lyric-chars.json     from scripts/lyric_chars.py (optional: no lyric text without it)

  MV_REPLICATE_DIR=/path/to/mv-replicate python make_edl_v1.py --work my-mv [--size 1280x720] [--no-render]

MV_REPLICATE_DIR is the skill folder (the one holding scripts/); it can be left out while this file
still sits in the skill's examples/ folder.
"""
import argparse, json, os, subprocess, sys
from pathlib import Path

SCRIPTS = Path(os.environ.get('MV_REPLICATE_DIR') or Path(__file__).resolve().parents[1]).expanduser() / 'scripts'

SONG = 'song/song.wav'
CLIPS = {'A': 'clips/a.mp4', 'B': 'clips/b.mp4', 'C': 'clips/c.mp4'}

# Source ranges never to use: broken frames, clipping limbs, shots already rejected in review.
AVOID = {'B': [(2.0, 2.6)]}

GRADE = 'eq=saturation=1.2:contrast=1.04:brightness=0.01'
PASTEL = '#DCEBFF'

# (start beat, clip, source `from` seconds or {'end': s}, options). A segment runs until the next
# entry's beat; the last one runs to the end of the song. Sections follow the reference style
# rules (see the reference style JSON), not the reference's time codes.
PLAN = [
    (None, 'A', 0.0, {'note': 'frame 0 is a real shot: no black open'}),
    (4, 'B', 0.0, {'note': 'intro: one shot per 2 beats'}),
    (6, 'C', 0.5, {}),
    (8, 'A', 3.0, {'layout': {'frame': 'vertical', 'pos': 'left', 'bg': PASTEL}, 'note': 'vertical frame on a pastel ground'}),
    (12, 'B', 3.0, {'flash_in': 0.15, 'shake': 0.5, 'note': 'new section: white flash + shake'}),
    (16, 'C', {'end': 4.0}, {'punch': 0.3, 'note': 'anchored on the clip end so it never reads past it'}),
]

TITLE = {'size': 0.20, 'fill': '#FFF7C2', 'fill2': '#FFC21A', 'stroke': '#C2410C', 'stroke_w': 0.009,
         'stroke2': '#FFFFFF', 'stroke2_w': 0.004, 'glow': '#FF9F1C', 'shadow': '#3B1600', 'shear': 0.12}


def overlays(lines, beat):
    """Big text that pops on sung characters. Here: the first three characters of lyric line 0,
    each tilted its own way, gone 0.14 s before the cut that follows them."""
    if not lines or len(lines[0]['chars']) < 3:
        return []
    cs = lines[0]['chars'][:3]
    nxt = next((b for b in beat if b > cs[-1]['t'] + 0.6), cs[-1]['t'] + 1.2)
    out = round(nxt - 0.14, 3)
    pos = [(0.30, 0.30, -8), (0.50, 0.25, 6), (0.70, 0.31, -5)]
    return [{'type': 'text_pop', 'at': round(cs[0]['t'] - 0.1, 3), 'dur': round(out + 0.2 - (cs[0]['t'] - 0.1), 3),
             'out': out, 'out_dur': 0.12, 'style': TITLE, 'wobble': [cs[-1]['t']],
             'pieces': [{'text': c['c'], 'at': c['t'], 'x': x, 'y': y, 'rot': r} for c, (x, y, r) in zip(cs, pos)]}]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--work', required=True, help='project folder; paths in this file are relative to it')
    ap.add_argument('--size', default='1280x720', help='review cut first; 2K only after it is approved')
    ap.add_argument('--fps', type=int, default=60)
    ap.add_argument('--lyric-style', default='bubbles')
    ap.add_argument('--no-render', action='store_true')
    a = ap.parse_args()
    if not a.no_render and not (SCRIPTS / 'render_edl.py').exists():
        sys.exit(f'no render_edl.py in {SCRIPTS}: set MV_REPLICATE_DIR to the mv-replicate skill folder')
    work = Path(a.work).resolve()
    W, H = map(int, a.size.split('x'))

    song = json.loads((work / 'song/song-analysis.json').read_text())
    beat = [b['t'] for b in song['beats']]
    dur = song['duration']
    lc = work / 'song/lyric-chars.json'
    lines = json.loads(lc.read_text())['lines'] if lc.exists() else []

    starts = [0.0 if k is None else beat[k] for k, *_ in PLAN] + [dur]
    segs = []
    for i, (k, clip, frm, opt) in enumerate(PLAN):
        at, d = round(starts[i], 4), round(starts[i + 1] - starts[i], 4)
        s = {'at': at, 'dur': d, 'src': CLIPS[clip], 'note': opt.get('note', '')}
        s.update(frm if isinstance(frm, dict) else {'from': frm})
        s.update({key: v for key, v in opt.items() if key != 'note'})
        sp = s.get('speed', 1.0)
        lo = s.get('from', s.get('end', 0) - d * sp)
        hi = lo + d * sp
        for a0, a1 in AVOID.get(clip, []):
            if lo < a1 - 0.02 and hi > a0 + 0.02:
                sys.exit(f'{clip} {lo:.2f}-{hi:.2f} overlaps avoided range {a0}-{a1} ({s["note"]})')
        segs.append(s)

    ov = overlays(lines, beat)
    if lines:
        ov.append({'type': 'layer', 'src': 'render/lyrics.mov', 'at': 0.0, 'dur': dur})
    edl = {'song': SONG, 'duration': dur, 'size': [W, H], 'fps': a.fps, 'grade': GRADE,
           'loudness': {'I': -14.0, 'TP': -1.0}, 'end_fade': 1.5,
           'work_dir': 'render/work', 'out': f'render/mv-{H}p.mp4', 'segments': segs, 'overlays': ov}
    path = work / 'edl.json'
    path.write_text(json.dumps(edl, ensure_ascii=False, indent=1))
    print(f'{len(segs)} segments, wrote {path}')
    if a.no_render:
        return
    if lines:
        subprocess.run([sys.executable, str(SCRIPTS / 'lyric_layer.py'), str(lc), '--style', a.lyric_style,
                        '--duration', str(dur), '--size', a.size, '--fps', str(a.fps),
                        '--out', str(work / 'render/lyrics.mov')], check=True)
    subprocess.run([sys.executable, str(SCRIPTS / 'render_edl.py'), str(path), '--root', str(work)], check=True)


if __name__ == '__main__':
    main()
