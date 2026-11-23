#!/usr/bin/env python3
"""Measure a reference MV: cuts, flashes, black dips, colour, loudness, tempo.

Writes <out>/ref-analysis.json and a 1-frame-per-second contact sheet. The numbers are raw material
for the style spec; cut counts on animation are unreliable (camera moves, dissolves and strobes), so
always confirm sections by reading the sheets.

Colour statistics are taken after converting every SDR input (8- or 10-bit, limited or full range)
to 8-bit limited-range YUV inside ffmpeg, so thresholds (flash: luma > 200 and +40; black: < 20) mean
the same for all inputs. HDR (PQ / HLG) is not converted: colour, flash and black analysis is
reported as unsupported instead of applying SDR thresholds. A reference without an audio track still
gets the visual analysis; loudness and tempo are reported as unavailable.

  python analyze_ref.py ref.mp4 --out DIR [--window 4]
"""
import argparse, json, subprocess, sys, tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvr_common as M   # noqa: E402

SHEET = Path(__file__).resolve().parent / 'frame_sheet.py'
HDR_TRANSFERS = ('smpte2084', 'arib-std-b67')


def scene_cuts(path, thr, tmp):
    f = Path(tmp) / f'sc{thr}.txt'
    r = M.run(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:v:0', '-vf', f'scdet=threshold={thr},metadata=print:file={f}',
               '-an', '-f', 'null', '-'])
    if r.returncode:
        raise M.MvrError('analyze_ref', 'scdet', r.stderr.strip()[-300:])
    return [round(float(l.split('=')[1]), 3) for l in f.read_text().splitlines() if 'lavfi.scd.time' in l]


def frame_stats(path, tmp):
    f = Path(tmp) / 'stats.txt'
    r = M.run(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:v:0', '-vf',
               f'scale=320:-2:out_range=tv,format=yuv420p,signalstats,metadata=print:file={f}', '-an', '-f', 'null', '-'])
    if r.returncode:
        raise M.MvrError('analyze_ref', 'signalstats', r.stderr.strip()[-300:])
    rows, cur = [], {}
    for l in f.read_text().splitlines():
        if l.startswith('frame:'):
            if cur:
                rows.append(cur)
            cur = {'t': float(l.split('pts_time:')[1])}
        elif 'signalstats.' in l:
            k, v = l.split('signalstats.')[1].split('=')
            if k in ('YAVG', 'SATAVG', 'HUEAVG'):
                cur[k] = float(v)
    if cur:
        rows.append(cur)
    return rows


def tempo(path, tmp):
    import librosa
    wav = Path(tmp) / 'a.wav'
    M.run(['ffmpeg', '-v', 'error', '-y', '-i', path, '-vn', '-ac', '1', '-ar', '22050', wav])
    y, sr = librosa.load(wav, sr=22050)
    bpm, beats = librosa.beat.beat_track(y=y, sr=sr, units='time')
    bpm = float(np.atleast_1d(bpm)[0]) if len(beats) else None
    return bpm, [round(float(b), 3) for b in beats]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ref')
    ap.add_argument('--out', required=True)
    ap.add_argument('--window', type=float, default=4.0, help='seconds per summary window')
    ap.add_argument('--no-sheet', action='store_true')
    a = ap.parse_args()
    ref, out = Path(a.ref), Path(a.out)
    try:
        info = M.probe(ref)
    except M.MvrError as e:
        M.die(e)
    if not info['video']:
        M.die(f'[analyze_ref] {ref}: no video stream')
    out.mkdir(parents=True, exist_ok=True)
    v = info['video']
    fmt = {k: v.get(k) for k in ('pix_fmt', 'bits_per_raw_sample', 'color_range', 'color_transfer', 'color_primaries')}
    hdr = v.get('color_transfer') in HDR_TRANSFERS
    res = {'schema_version': M.SCHEMA_VERSION, 'file': str(ref),
           'info': {'duration': info['duration'], 'width': v['width'], 'height': v['height'],
                    'fps': float(M.frac(v['avg_frame_rate'] or v['r_frame_rate'])), 'format': fmt}}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            cuts = {str(t): scene_cuts(ref, t, tmp) for t in (10, 20, 30)}
            rows = [] if hdr else frame_stats(ref, tmp)
            audio = None
            if info['audio']:
                bpm, beats = tempo(ref, tmp)
                audio = {'status': 'ok', 'loudness': M.loudness(ref), 'tempo_bpm': round(bpm, 1) if bpm else None,
                         'beats': beats}
                if not beats:
                    audio.update(status='partial', reason='no beats detected')
        except M.MvrError as e:
            M.die(e)
    res['cuts'] = cuts
    res['audio'] = audio or {'status': 'unavailable', 'reason': 'the reference has no audio track',
                             'loudness': None, 'tempo_bpm': None, 'beats': []}
    if hdr:
        res['colour'] = {'status': 'unsupported', 'reason': f"HDR transfer {v.get('color_transfer')}: SDR thresholds do "
                                                            'not apply and no tone mapping is implemented'}
        res['flashes'] = res['black_dips'] = None
        res['windows'] = []
    else:
        y = np.array([r['YAVG'] for r in rows])
        t = np.array([r['t'] for r in rows])
        sat = np.array([r.get('SATAVG', 0.0) for r in rows])
        res['colour'] = {'status': 'ok', 'scale': '8-bit limited-range luma (16-235) after conversion',
                         'luma': round(float(y.mean()), 1) if len(y) else None,
                         'sat': round(float(sat.mean()), 1) if len(sat) else None}
        res['flashes'] = [round(float(t[i]), 2) for i in range(1, len(y)) if y[i] > 200 and y[i] - y[i - 1] > 40]
        res['black_dips'] = [round(float(t[i]), 2) for i in range(1, len(y)) if y[i] < 20 <= y[i - 1]]
        windows, w = [], a.window
        for s in np.arange(0, info['duration'] or (t[-1] if len(t) else 0), w):
            m = (t >= s) & (t < s + w)
            if not m.any():
                continue
            windows.append({'start': round(float(s), 2), 'end': round(float(min(s + w, info['duration'])), 2),
                            'cuts_thr10': sum(1 for c in cuts['10'] if s <= c < s + w),
                            'cuts_thr20': sum(1 for c in cuts['20'] if s <= c < s + w),
                            'luma': round(float(y[m].mean()), 1), 'sat': round(float(sat[m].mean()), 1)})
        res['windows'] = windows
    res['note'] = 'cuts_thr10 over-counts strobes and fast camera moves, thr20/30 miss dissolves; confirm on the sheets.'
    M.atomic_write_json(out / 'ref-analysis.json', res)
    if not a.no_sheet:
        r = subprocess.run([sys.executable, str(SHEET), str(ref), '--out', str(out / 'ref-sheet.jpg'), '--every', '1',
                            '--cols', '8', '--cell', '240'], capture_output=True, text=True)
        if r.returncode:
            M.die(f'[analyze_ref] contact sheet failed: {r.stderr.strip()[-300:]}')
    print(json.dumps({'info': res['info'], 'audio': {k: res['audio'][k] for k in ('status', 'tempo_bpm')},
                      'colour': res['colour']}, ensure_ascii=False))
    print('cuts', {k: len(v) for k, v in cuts.items()}, 'flashes', res['flashes'], 'black', res['black_dips'])


if __name__ == '__main__':
    main()
