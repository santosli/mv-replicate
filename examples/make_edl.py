#!/usr/bin/env python3
"""Skeleton generator: build an EDL from a beat-indexed PLAN, validate it, render one version.

Copy this file into your MV folder and edit CLIPS, AVOID, PLAN and the title card below - or keep
the file as is and put those in a project file (--project, see examples/project.example.json).
Change cuts here (or in the project file), never by hand in the generated EDL.

Inputs, relative to --work:
  song/song-analysis.json   from scripts/analyze_song.py
  song/lyric-chars.json     from scripts/lyric_chars.py (optional: no lyric text without it)

Every run writes one version folder and nothing else:
  versions/<version>/edl.json, lyrics.mov, mv-<H>p.mp4, mv-<H>p.qa.json, sheet.jpg, manifest.json
Temporary files go to a fresh folder under <work>/.work and are removed. A version that already has
a film is never touched again: make the next cut as a new --version (v2, v3 ...), so every earlier
version keeps its EDL, lyric layer, film and report exactly as they were reviewed.
versions/latest-passing.json is updated only when a version passes machine QA; user approval is
recorded separately (manifest "review"), machine QA is not approval.

  MV_REPLICATE_DIR=/path/to/mv-replicate python make_edl.py --work my-mv --version v1 [--no-render]
"""
import argparse, json, os, subprocess, sys
from pathlib import Path

SKILL = Path(os.environ.get('MV_REPLICATE_DIR') or Path(__file__).resolve().parents[1]).expanduser()
SCRIPTS = SKILL / 'scripts'

# ---------------------------------------------------------------- the plan (defaults; --project overrides)
SONG = 'song/song.wav'
CLIPS = {'A': 'clips/a.mp4', 'B': 'clips/b.mp4', 'C': 'clips/c.mp4'}

# Source ranges never to show, in source seconds [start, end): broken frames, clipping limbs, shots
# rejected in review. Written into the EDL, so the renderer refuses any frame on screen in them.
AVOID = {'B': [[2.0, 2.6]]}

GRADE = 'eq=saturation=1.2:contrast=1.04:brightness=0.01'
PASTEL = '#DCEBFF'

# (start beat, clip, source `from` seconds or {"end": s}, options). A segment runs until the next
# entry's beat; the last one runs to the end of the song. Sections follow the reference style rules
# (examples/style-spec.example.json), not the reference's time codes.
PLAN = [
    [None, 'A', 0.0, {'note': 'frame 0 is a real shot: no black open'}],
    [4, 'B', 0.0, {'note': 'intro: one shot per 2 beats'}],
    [6, 'C', 0.5, {}],
    [8, 'A', 3.0, {'layout': {'frame': 'vertical', 'pos': 'left', 'bg': PASTEL}, 'note': 'vertical frame on a pastel ground'}],
    [12, 'B', 3.0, {'flash_in': 0.15, 'shake': 0.5, 'note': 'new section: white flash + shake'}],
    [16, 'C', {'end': 4.0}, {'punch': 0.3, 'note': 'anchored on the clip end so it never reads past it'}],
]

# Big text that pops on sung units: which lyric line, which units, where each piece sits.
TITLE = {'line': 0, 'units': [0, 1, 2], 'pos': [[0.30, 0.30, -8], [0.50, 0.25, 6], [0.70, 0.31, -5]],
         'style': {'size': 0.20, 'fill': '#FFF7C2', 'fill2': '#FFC21A', 'stroke': '#C2410C', 'stroke_w': 0.009,
                   'stroke2': '#FFFFFF', 'stroke2_w': 0.004, 'glow': '#FF9F1C', 'shadow': '#3B1600', 'shear': 0.12}}
OUT_DUR, CUT_GAP, POP = 0.12, 0.02, 0.2     # fade-out length, margin before the cut, pop-in length


def fail(msg):
    print(f'error: [make_edl] {msg}', file=sys.stderr)
    sys.exit(2)


def next_cut_after(t, cuts):
    return next((c for c in cuts if c > t + 1e-9), None)


def title_overlay(lines, cuts, dur, title):
    """Pieces pop on their sung units and are gone before the real next cut (not a beat guess)."""
    if not title or not lines or title['line'] >= len(lines):
        return []
    chars = lines[title['line']]['chars']
    if not chars or max(title['units']) >= len(chars):
        return []
    cs = [chars[k] for k in title['units']]
    last = max(c['t'] for c in cs)
    cut = next_cut_after(last, cuts) or dur
    out = round(cut - OUT_DUR - CUT_GAP, 3)
    if out < last + POP:
        fail(f"title card: last piece {cs[-1]['c']!r} pops at {last:.3f}s but the next cut is at {cut:.3f}s; "
             f"it needs {POP + OUT_DUR + CUT_GAP:.2f}s to land and fade. Move that cut later or the card earlier.")
    at = round(min(c['t'] for c in cs) - 0.1, 3)
    return [{'type': 'text_pop', 'at': at, 'dur': round(out + OUT_DUR + 0.01 - at, 3), 'out': out, 'out_dur': OUT_DUR,
             'style': title['style'], 'wobble': [last],
             'pieces': [{'text': c['c'], 'at': c['t'], 'x': x, 'y': y, 'rot': r} for c, (x, y, r) in zip(cs, title['pos'])]}]


def build(work, a, cfg, preset):
    song = json.loads((work / 'song/song-analysis.json').read_text())
    beat = [b['t'] for b in song['beats']]
    dur = song['duration']
    lc = work / 'song/lyric-chars.json'
    lines = json.loads(lc.read_text())['lines'] if lc.exists() else []
    plan = cfg['plan']
    starts = []
    for i, (k, *_rest) in enumerate(plan):
        if k is None:
            starts.append(0.0)
        elif not isinstance(k, int) or not 0 <= k < len(beat):
            fail(f'plan[{i}]: beat {k!r} is not a beat index 0..{len(beat) - 1}')
        else:
            starts.append(beat[k])
    starts.append(dur)
    segs = []
    for i, (k, clip, frm, opt) in enumerate(plan):
        if clip not in cfg['clips']:
            fail(f'plan[{i}]: unknown clip {clip!r}')
        at, d = round(starts[i], 6), round(starts[i + 1] - starts[i], 6)
        s = {'at': at, 'dur': d, 'src': cfg['clips'][clip], 'note': opt.get('note', '')}
        s.update(frm if isinstance(frm, dict) else {'from': frm})
        s.update({key: v for key, v in opt.items() if key != 'note'})
        segs.append(s)
    vdir = f'versions/{a.version}'
    H = a.size[1]
    ov = title_overlay(lines, starts[1:-1], dur, cfg.get('title'))
    if lines:
        ov.append({'type': 'layer', 'src': f'{vdir}/lyrics.mov', 'at': 0.0, 'dur': dur})
    return {'song': cfg['song'], 'duration': dur, 'size': list(a.size), 'fps': a.fps, 'grade': cfg['grade'],
            'loudness': preset.get('loudness', {'I': -14.0, 'TP': -1.0}), 'end_fade': preset.get('end_fade', 1.5),
            'work_dir': '.work', 'out': f'{vdir}/mv-{H}p.mp4',
            'avoid': {cfg['clips'][c]: r for c, r in cfg['avoid'].items()},
            'segments': segs, 'overlays': ov}, lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--work', required=True, help='MV folder; every path in the plan is relative to it')
    ap.add_argument('--version', default='v1', help='version folder name under versions/ (v1, v2, review-2, ...)')
    ap.add_argument('--project', help='JSON with song / clips / avoid / plan / grade / title to use instead of the defaults above')
    ap.add_argument('--preset', help='house defaults (presets/*.json): size, fps, lyric style, loudness, end fade')
    ap.add_argument('--size', help='WxH; review cut first, 2K only after it is approved')
    ap.add_argument('--fps')
    ap.add_argument('--lyric-style')
    ap.add_argument('--no-render', action='store_true', help='write and validate the EDL only')
    a = ap.parse_args()
    if not (SCRIPTS / 'render_edl.py').exists():
        fail(f'no render_edl.py in {SCRIPTS}: set MV_REPLICATE_DIR to the mv-replicate skill folder')
    sys.path.insert(0, str(SCRIPTS))
    import mvr_common as M
    import validate_edl as V
    work = Path(a.work).resolve()
    preset = M.load_json(a.preset) if a.preset else {}
    a.size = tuple(map(int, (a.size or 'x'.join(map(str, preset.get('size', [1280, 720])))).split('x')))
    a.fps = a.fps or preset.get('fps', 60)
    a.lyric_style = a.lyric_style or preset.get('lyric_style', 'bubbles')
    cfg = {'song': SONG, 'clips': CLIPS, 'avoid': AVOID, 'plan': PLAN, 'grade': GRADE, 'title': TITLE}
    if a.project:
        cfg.update(M.load_json(a.project))
    if not all(ch.isalnum() or ch in '-_.' for ch in a.version) or a.version.startswith('.'):
        fail(f'--version {a.version!r}: use letters, digits, - _ . only')
    vdir = work / 'versions' / a.version
    film = vdir / f'mv-{a.size[1]}p.mp4'
    if film.exists() or (vdir / 'manifest.json').exists():
        fail(f'version {a.version} already has a film ({vdir}); make the next cut as a new --version')
    if not (work / 'song/song-analysis.json').exists():
        fail(f'{work}/song/song-analysis.json is missing: run scripts/analyze_song.py first')

    edl, lines = build(work, a, cfg, preset)
    res = V.validate(edl, work, None, check_media=True, pending=[vdir / 'lyrics.mov'] if lines else [])
    for w in res['warnings']:
        print('warning:', w, file=sys.stderr)
    if res['errors']:
        for e in res['errors']:
            print('error: [validate]', e, file=sys.stderr)
        fail('the plan does not make a valid EDL; nothing written')
    edl_path = vdir / 'edl.json'
    M.atomic_write_json(edl_path, edl)
    print(f"{len(edl['segments'])} segments, wrote {edl_path}")
    if a.no_render:
        return
    py = sys.executable
    if lines:
        args = [py, SCRIPTS / 'lyric_layer.py', work / 'song/lyric-chars.json', '--style', a.lyric_style,
                '--duration', str(edl['duration']), '--size', 'x'.join(map(str, a.size)), '--fps', str(a.fps),
                '--out', vdir / 'lyrics.mov']
        for k, v in preset.get('lyric_set', {}).items():
            args += ['--set', f'{k}={v}']
        subprocess.run([str(x) for x in args], check=True)
    r = subprocess.run([py, str(SCRIPTS / 'render_edl.py'), str(edl_path), '--root', str(work)])
    if r.returncode:
        sys.exit(r.returncode)
    subprocess.run([py, str(SCRIPTS / 'frame_sheet.py'), str(film), '--out', str(vdir / 'sheet.jpg'), '--every', '1',
                    '--cell', '240', '--cols', '8'], check=True)
    qa = M.load_json(vdir / f'{film.stem}.qa.json')
    inputs = [work / cfg['song'], work / 'song/song-analysis.json', lc_path(work)] + [work / p for p in cfg['clips'].values()]
    manifest = {'schema_version': M.SCHEMA_VERSION, 'version': a.version, 'edl': str(edl_path.relative_to(work)),
                'film': str(film.relative_to(work)), 'qa': {'status': qa['status'], 'report': f'{film.stem}.qa.json'},
                'config': {'size': list(a.size), 'fps': a.fps, 'lyric_style': a.lyric_style, 'preset': a.preset,
                           'project': a.project, 'generator': str(Path(__file__).resolve().name)},
                'input_fingerprints': {str(Path(k).relative_to(work)): v for k, v in M.fingerprint(inputs).items()},
                'edl_sha256': M.sha256(edl_path), 'tool_versions': M.tool_versions(),
                'review': {'user_approved': None, 'note': 'set by the person who watched it; machine QA is not approval'}}
    M.atomic_write_json(vdir / 'manifest.json', manifest)
    if qa['status'] == 'pass':
        M.atomic_write_json(work / 'versions/latest-passing.json', {'version': a.version, 'film': manifest['film']})
    print('version ready:', vdir)


def lc_path(work):
    return work / 'song/lyric-chars.json'


if __name__ == '__main__':
    main()
