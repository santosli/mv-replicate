#!/usr/bin/env python3
"""Check an EDL before anything is rendered or deleted.

Covers: top-level fields, contiguous positive segments, frame counts on the output grid,
source availability (with the exact frame-selection rule the renderer uses), AVOID ranges on
every video input (main src and wipe_in), overlays and their files, fonts, and path conflicts
between inputs, the work folder and the output. render_edl.py runs the same checks first.

  python validate_edl.py edl.json [--root DIR] [--no-media] [--json]

Exit code 0: valid (warnings allowed). 1: errors. Messages name the segment / overlay / field.
"""
import argparse, copy, math, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvr_common as M   # noqa: E402

LAYOUTS = ('full', 'vertical', 'photo')
OVERLAYS = ('text_pop', 'text_slam', 'starburst', 'flash', 'layer')
NUM_EFFECTS = ('flash_in', 'shake', 'punch', 'whip_in', 'whip_out')


def default_font():
    env = os.environ.get('MV_REPLICATE_FONT')
    if env:
        return Path(env).expanduser()
    for d in ('~/Library/Fonts', '/Library/Fonts', '~/.local/share/fonts', '~/.fonts', '/usr/share/fonts/truetype'):
        p = Path(d).expanduser() / 'HarmonyOS_Sans_SC_Black.ttf'
        if p.exists():
            return p
    return None


def is_hex(c):
    return isinstance(c, str) and len(c) == 7 and c[0] == '#' and all(ch in '0123456789abcdefABCDEF' for ch in c[1:])


def validate(edl, root, edl_path=None, check_media=True, pending=()):
    """Returns {'errors': [...], 'warnings': [...], 'plan': {...}}; never touches the disk except reads.
    `pending`: files a generator is about to create (e.g. the lyric layer); accepted unchecked here and
    checked again by render_edl when they exist."""
    pending = {Path(p).resolve() for p in pending}
    edl = copy.deepcopy(edl)       # annotate a copy; the caller's EDL is written out unchanged
    root = Path(root).resolve()
    E, W = [], []
    plan = {'segments': [], 'overlays': [], 'inputs': {}, 'frames': None}

    def err(where, why):
        E.append(f'{where}: {why}')

    def num(where, v, lo=None, hi=None, lo_open=False, required=True):
        if v is None:
            if required:
                err(where, 'missing')
            return False
        if not M.finite(v):
            err(where, f'must be a finite number, got {v!r}')
            return False
        if lo is not None and (v <= lo if lo_open else v < lo):
            err(where, f'must be {">" if lo_open else ">="} {lo}, got {v}')
            return False
        if hi is not None and v > hi:
            err(where, f'must be <= {hi}, got {v}')
            return False
        return True

    inputs = {}

    def need_file(where, p, kind, video=False, audio=False):
        if not isinstance(p, str) or not p:
            err(where, 'must be a path string')
            return None
        path = M.resolve_path(p, root)
        if path in pending and not path.exists():
            inputs.setdefault(str(path), kind)
            return path
        if not path.is_file():
            err(where, f'{p} does not exist or is not a file')
            return None
        if not os.access(path, os.R_OK):
            err(where, f'{p} is not readable')
            return None
        inputs.setdefault(str(path), kind)
        if check_media and (video or audio):
            try:
                info = M.probe(path)
            except M.MvrError as e:
                err(where, e.reason)
                return None
            if video and not info['video']:
                err(where, f'{p} has no video stream')
                return None
            if audio and not info['audio']:
                err(where, f'{p} has no audio stream')
                return None
        return path

    # ---- top level
    size, fps = edl.get('size'), edl.get('fps')
    if not (isinstance(size, list) and len(size) == 2 and all(isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in size)):
        err('size', f'must be [width, height] positive integers, got {size!r}')
        size = None
    elif size[0] % 2 or size[1] % 2:
        err('size', f'{size} must be even in both dimensions for yuv420p H.264')
    try:
        fps_f = M.frac(fps)
        if fps_f <= 0 or fps_f > 240:
            raise ValueError
    except (TypeError, ValueError, ZeroDivisionError):
        err('fps', f'must be a positive number or "num/den" up to 240, got {fps!r}')
        fps_f = None
    dur = edl.get('duration')
    num('duration', dur, 0, lo_open=True)
    if edl.get('end_fade') is not None:
        num('end_fade', edl.get('end_fade'), 0, dur if M.finite(dur) else None)
    if edl.get('loudness') is not None:
        L = edl['loudness']
        if not isinstance(L, dict):
            err('loudness', 'must be {"I": LUFS, "TP": dBTP}')
        else:
            num('loudness.I', L.get('I'), -70, -5)
            num('loudness.TP', L.get('TP'), -20, 0)
    if edl.get('grade') is not None and not isinstance(edl['grade'], str):
        err('grade', 'must be an ffmpeg filter string')
    for k in ('work_dir', 'out', 'song'):
        if not isinstance(edl.get(k), str) or not edl.get(k):
            err(k, 'required path string')
    song = need_file('song', edl.get('song'), 'song', audio=True) if isinstance(edl.get('song'), str) else None
    if song and check_media and M.finite(dur):
        sd = M.probe(song)['duration']
        if sd is not None and sd + 1e-3 < dur:
            err('song', f'is {sd:.3f}s long but the EDL duration is {dur}s')

    # ---- segments
    segs = edl.get('segments')
    if not isinstance(segs, list) or not segs:
        err('segments', 'must be a non-empty list')
        segs = []
    t = 0.0
    total = 0
    good_segs = []
    for i, s in enumerate(segs):
        w = f'segments[{i}]'
        if not isinstance(s, dict):
            err(w, 'must be an object')
            continue
        s['_i'] = i
        ok = num(f'{w}.at', s.get('at')) & num(f'{w}.dur', s.get('dur'), 0, lo_open=True)
        if not ok:
            continue
        if abs(s['at'] - t) > 1e-6:
            err(f'{w}.at', f'is {s["at"]}, expected {t:.6f}: segments must be contiguous and in order')
        t = s['at'] + s['dur']
        n = M.seg_frame_count(s, fps_f) if fps_f else None
        if n is not None and n < 1:
            err(w, f'covers {s["dur"]}s, less than one output frame at {fps} fps')
        if ('src' in s) == ('color' in s):
            err(w, 'needs exactly one of "src" or "color"')
            continue
        if 'color' in s:
            if not isinstance(s['color'], str):
                err(f'{w}.color', 'must be an ffmpeg colour string')
            plan['segments'].append({'i': i, 'frames': n, 'color': s.get('color')})
            total += n or 0
            continue
        sp = s.get('speed', 1)
        num(f'{w}.speed', sp, 0, 16, lo_open=True)
        if ('from' in s) == ('end' in s):
            err(w, 'needs exactly one of "from" or "end" with "src"')
            continue
        num(f'{w}.from' if 'from' in s else f'{w}.end', s.get('from', s.get('end')), 0)
        lay = s.get('layout', 'full')
        if not (lay == 'full' or (isinstance(lay, dict) and lay.get('frame') in ('vertical', 'photo'))):
            err(f'{w}.layout', f'must be "full" or {{"frame": "vertical"|"photo", ...}}, got {lay!r}')
        if isinstance(lay, dict) and lay.get('pos', 'center') not in ('left', 'center', 'right'):
            err(f'{w}.layout.pos', 'must be left, center or right')
        for k in NUM_EFFECTS:
            if k in s:
                num(f'{w}.{k}', s[k], 0, s['dur'])
        if 'rot' in s:
            num(f'{w}.rot', s['rot'], -45, 45)
        if 'zoom' in s:
            z = s['zoom']
            if not (isinstance(z, list) and len(z) == 2 and all(M.finite(x) and x >= 1 for x in z)):
                err(f'{w}.zoom', f'must be [z0, z1] with values >= 1, got {z!r}')
        if 'focus' in s:
            fo = s['focus']
            if not (isinstance(fo, list) and len(fo) == 2 and all(M.finite(x) and 0 <= x <= 1 for x in fo)):
                err(f'{w}.focus', f'must be [fx, fy] in 0..1, got {fo!r}')
        if 'duotone' in s and not (isinstance(s['duotone'], list) and len(s['duotone']) in (2, 3) and all(map(is_hex, s['duotone']))):
            err(f'{w}.duotone', 'must be 2 or 3 "#RRGGBB" colours')
        if s.get('freeze') and s.get('reverse'):
            W.append(f'{w}: reverse has no effect on a freeze')
        src = need_file(f'{w}.src', s['src'], 'video', video=True)
        if s.get('wipe_in') is not None:
            wp = s['wipe_in']
            if not isinstance(wp, dict):
                err(f'{w}.wipe_in', 'must be {src, from, dur}')
            else:
                need_file(f'{w}.wipe_in.src', wp.get('src'), 'video', video=True)
                num(f'{w}.wipe_in.from', wp.get('from'), 0)
                num(f'{w}.wipe_in.dur', wp.get('dur', 0.9), 0, s['dur'], lo_open=True)
        if src and check_media and fps_f and n and n > 0:
            try:
                srcs = M.segment_sources(s, fps_f, root)
                plan['segments'].append({'i': i, 'frames': n, 'sources': [
                    {'path': str(p), 'kind': k, 'first': idx[0], 'last': idx[-1], 'count': len(idx)} for p, k, idx in srcs]})
                good_segs.append(s)
            except M.MvrError as e:
                err(e.where, e.reason)
            except (KeyError, TypeError, ValueError) as e:
                err(w, f'bad field: {e}')
        total += n or 0
    if segs and M.finite(dur) and abs(t - dur) > 1e-6:
        err('segments', f'end at {t:.6f}s, but duration is {dur}s')
    if fps_f and M.finite(dur):
        plan['frames'] = M.Q(dur, fps_f)
        if total and total != plan['frames']:
            err('segments', f'frame counts add up to {total}, expected Q(duration) = {plan["frames"]}')

    # ---- AVOID
    avoid = edl.get('avoid')
    if avoid is None:
        W.append('no "avoid" ranges in the EDL: forbidden source ranges were NOT checked by the renderer')
    elif not isinstance(avoid, dict):
        err('avoid', 'must be {"path": [[start, end], ...]}')
    elif check_media and fps_f and not E:
        try:
            for e in M.check_avoid(good_segs, fps_f, root, avoid):
                err('avoid', e)
        except M.MvrError as e:
            err(e.where, e.reason)
    plan['avoid_checked'] = bool(avoid) and check_media

    # ---- overlays
    for j, o in enumerate(edl.get('overlays', []) or []):
        w = f'overlays[{j}]'
        if not isinstance(o, dict) or o.get('type') not in OVERLAYS:
            err(f'{w}.type', f'must be one of {OVERLAYS}')
            continue
        if not (num(f'{w}.at', o.get('at')) & num(f'{w}.dur', o.get('dur'), 0, lo_open=True)):
            continue
        if M.finite(dur) and o['at'] >= dur:
            W.append(f'{w}: starts at {o["at"]}s, after the end of the film; it is never shown')
        if o['at'] < 0:
            W.append(f'{w}: starts at {o["at"]}s; the part before 0 s is cut off (the animation keeps its own clock)')
        if fps_f and M.Q(o['at'] + o['dur'], fps_f) - max(0, M.Q(o['at'], fps_f)) < 1:
            err(w, 'is shorter than one output frame after clipping')
        if o['type'] == 'layer':
            need_file(f'{w}.src', o.get('src'), 'layer', video=True)
        if o['type'] in ('text_pop', 'text_slam'):
            fonts = []
            if o['type'] == 'text_slam':
                if not isinstance(o.get('text'), str) or not o['text']:
                    err(f'{w}.text', 'required')
                fonts.append(o.get('font'))
            else:
                if not isinstance(o.get('style'), dict) or not M.finite(o['style'].get('size')):
                    err(f'{w}.style', 'needs a numeric "size" (fraction of the frame height)')
                num(f'{w}.out', o.get('out'))
                pieces = o.get('pieces')
                if not isinstance(pieces, list) or not pieces:
                    err(f'{w}.pieces', 'must be a non-empty list')
                    pieces = []
                for k, pc in enumerate(pieces):
                    pw = f'{w}.pieces[{k}]'
                    if not isinstance(pc, dict) or not isinstance(pc.get('text'), str) or not pc['text']:
                        err(f'{pw}.text', 'required')
                        continue
                    num(f'{pw}.at', pc.get('at'))
                    num(f'{pw}.x', pc.get('x'))
                    num(f'{pw}.y', pc.get('y'))
                    if 'rot' in pc:
                        num(f'{pw}.rot', pc['rot'])
                    st = {**(o.get('style') or {}), **(pc.get('style') or {})}
                    fonts.append(st.get('font'))
                    if M.finite(pc.get('at')) and M.finite(o.get('out')) and pc['at'] > o['out']:
                        err(f'{pw}.at', f'{pc["at"]} is after the overlay leaves ({o["out"]})')
                for x in o.get('wobble', []):
                    if not M.finite(x):
                        err(f'{w}.wobble', f'non-finite time {x!r}')
            for fnt in fonts:
                if fnt:
                    need_file(f'{w}.font', fnt, 'font')
                elif default_font() is None:
                    err(f'{w}.font', 'no font given and no default font found (set MV_REPLICATE_FONT)')
                else:
                    inputs.setdefault(str(default_font().resolve()), 'font')

    # ---- paths
    if edl_path:
        inputs.setdefault(str(Path(edl_path).resolve()), 'edl')
    if isinstance(edl.get('out'), str) and edl['out']:
        out = M.resolve_path(edl['out'], root)
        if out.suffix.lower() not in ('.mp4', '.mov', '.m4v'):
            err('out', 'must end in .mp4, .mov or .m4v')
        if out.is_dir():
            err('out', f'{out} is a directory')
        for p in inputs:
            if Path(p) == out or (out.exists() and os.path.samefile(p, out)):
                err('out', f'would overwrite an input ({inputs[p]}): {p}')
        plan['out'] = str(out)
    if isinstance(edl.get('work_dir'), str) and edl['work_dir']:
        wd = M.resolve_path(edl['work_dir'], root)
        if wd.exists() and not wd.is_dir():
            err('work_dir', f'{wd} exists and is not a directory')
        for p in inputs:
            if Path(p) == wd:
                err('work_dir', f'is an input file: {p}')
        plan['work_parent'] = str(wd)
    plan['inputs'] = inputs
    return {'errors': E, 'warnings': W, 'plan': plan}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('edl')
    ap.add_argument('--root', default='.', help='folder that relative paths in the EDL resolve against')
    ap.add_argument('--no-media', action='store_true', help='skip probing media (structure and paths only)')
    ap.add_argument('--json', action='store_true', help='print the full result as JSON')
    a = ap.parse_args()
    try:
        edl = M.load_json(a.edl)
    except (ValueError, OSError) as e:
        print(f'error: [validate] {a.edl}: {e}', file=sys.stderr)
        sys.exit(1)
    if not isinstance(edl, dict):
        print(f'error: [validate] {a.edl}: top level must be an object', file=sys.stderr)
        sys.exit(1)
    res = validate(edl, Path(a.root).expanduser(), a.edl, check_media=not a.no_media)
    if a.json:
        print(M.dumps(res))
    else:
        for e in res['errors']:
            print('error:', e)
        for w in res['warnings']:
            print('warning:', w)
        if not res['errors']:
            p = res['plan']
            print(f"ok: {len(p['segments'])} segments, {p['frames']} frames, avoid checked: {p['avoid_checked']}")
    sys.exit(1 if res['errors'] else 0)


if __name__ == '__main__':
    main()
