#!/usr/bin/env python3
"""Measure a reference MV: cuts, flashes, black dips, colour, loudness, tempo.

Writes <out>/ref-analysis.json and two contact sheets (1 frame per second). The numbers are
raw material for the style spec; cut counts on animation are unreliable (camera moves,
dissolves and strobes), so always confirm sections by reading the sheets.

Run with a Python that has librosa and Pillow:  python analyze_ref.py ref.mp4 --out DIR
"""
import argparse, json, subprocess, sys, tempfile
from pathlib import Path

import numpy as np

SHEET = Path(__file__).resolve().parent / 'frame_sheet.py'


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def probe(path):
    out = sh(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type,width,height,r_frame_rate',
              '-of', 'json', str(path)]).stdout
    d = json.loads(out)
    v = next(s for s in d['streams'] if s['codec_type'] == 'video')
    num, den = map(int, v['r_frame_rate'].split('/'))
    return {'duration': float(d['format']['duration']), 'width': v['width'], 'height': v['height'], 'fps': num / den}


def scene_cuts(path, thr, tmp):
    f = Path(tmp) / f'sc{thr}.txt'
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', f'scdet=threshold={thr},metadata=print:file={f}',
                    '-an', '-f', 'null', '-'], check=True)
    return [round(float(l.split('=')[1]), 3) for l in f.read_text().splitlines() if 'lavfi.scd.time' in l]


def frame_stats(path, tmp):
    f = Path(tmp) / 'stats.txt'
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf',
                    f'scale=320:-2,signalstats,metadata=print:file={f}', '-an', '-f', 'null', '-'], check=True)
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


def loudness(path):
    err = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(path), '-af', 'ebur128=peak=true', '-f', 'null', '-'],
                         capture_output=True, text=True).stderr
    tail = err[err.rfind('Summary:'):]
    def grab(key):
        line = next(l for l in tail.splitlines() if l.strip().startswith(key))
        return float(line.split(':')[1].split()[0])
    return {'integrated_lufs': grab('I:'), 'true_peak_dbfs': grab('Peak:')}


def tempo(path, tmp):
    import librosa
    wav = Path(tmp) / 'a.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', str(path), '-vn', '-ac', '1', '-ar', '22050', str(wav)], check=True)
    y, sr = librosa.load(wav, sr=22050)
    bpm, beats = librosa.beat.beat_track(y=y, sr=sr, units='time')
    return float(np.atleast_1d(bpm)[0]), [round(float(b), 3) for b in beats]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('ref')
    ap.add_argument('--out', required=True)
    ap.add_argument('--window', type=float, default=4.0, help='seconds per summary window')
    a = ap.parse_args()
    ref, out = Path(a.ref), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    info = probe(ref)
    with tempfile.TemporaryDirectory() as tmp:
        cuts = {str(t): scene_cuts(ref, t, tmp) for t in (10, 20, 30)}
        rows = frame_stats(ref, tmp)
        bpm, beats = tempo(ref, tmp)
    loud = loudness(ref)

    y = np.array([r['YAVG'] for r in rows])
    t = np.array([r['t'] for r in rows])
    flashes = [round(float(t[i]), 2) for i in range(1, len(y)) if y[i] > 200 and y[i] - y[i - 1] > 40]
    blacks = [round(float(t[i]), 2) for i in range(1, len(y)) if y[i] < 20 <= y[i - 1]]

    windows = []
    w = a.window
    for s in np.arange(0, info['duration'], w):
        m = (t >= s) & (t < s + w)
        if not m.any():
            continue
        c10 = sum(1 for c in cuts['10'] if s <= c < s + w)
        c20 = sum(1 for c in cuts['20'] if s <= c < s + w)
        windows.append({'start': round(float(s), 2), 'end': round(float(min(s + w, info['duration'])), 2),
                        'cuts_thr10': c10, 'cuts_thr20': c20,
                        'luma': round(float(y[m].mean()), 1),
                        'sat': round(float(np.mean([r['SATAVG'] for r, k in zip(rows, m) if k])), 1)})

    res = {'file': str(ref), 'info': info, 'loudness': loud, 'tempo_bpm': round(bpm, 1), 'beats': beats,
           'cuts': cuts, 'flashes': flashes, 'black_dips': blacks,
           'overall': {'luma': round(float(y.mean()), 1), 'sat': round(float(np.mean([r['SATAVG'] for r in rows])), 1)},
           'windows': windows,
           'note': 'cuts_thr10 over-counts strobes and fast camera moves, thr20/30 miss dissolves; confirm on the sheets.'}
    (out / 'ref-analysis.json').write_text(json.dumps(res, ensure_ascii=False, indent=1))
    subprocess.run([sys.executable, str(SHEET), str(ref), '--out', str(out / 'ref-sheet.jpg'), '--every', '1',
                    '--cols', '8', '--cell', '240'], check=True)
    print(json.dumps({k: res[k] for k in ('info', 'loudness', 'tempo_bpm', 'overall')}, ensure_ascii=False))
    print('cuts', {k: len(v) for k, v in cuts.items()}, 'flashes', flashes, 'black', blacks)


if __name__ == '__main__':
    main()
