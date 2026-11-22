#!/usr/bin/env python3
"""Beat grid, bar phase, energy and loudness of your own song.

Optional --lyrics takes a JSON with lines [{start, line}] (e.g. a whisper-aligned lyric timing file);
each line start is snapped to the nearest beat, and the bar phase (which beat of 4 is the downbeat)
is chosen so that most line starts land on downbeats. Writes <out>/song-analysis.json.

Run with a Python that has librosa:  python analyze_song.py song.wav --out DIR [--lyrics lyrics.json]
"""
import argparse, json, subprocess
from pathlib import Path

import numpy as np


def loudness(path):
    err = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(path), '-af', 'ebur128=peak=true', '-f', 'null', '-'],
                         capture_output=True, text=True).stderr
    tail = err[err.rfind('Summary:'):]
    def grab(key):
        line = next(l for l in tail.splitlines() if l.strip().startswith(key))
        return float(line.split(':')[1].split()[0])
    return {'integrated_lufs': grab('I:'), 'true_peak_dbfs': grab('Peak:')}


def main():
    import librosa
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('song')
    ap.add_argument('--out', required=True)
    ap.add_argument('--lyrics', help='JSON file with "lines": [{start, line}]; use --lyrics-key for a nested key')
    ap.add_argument('--lyrics-key')
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    y, sr = librosa.load(a.song, sr=22050, mono=True)
    dur = len(y) / sr
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    bpm, bf = librosa.beat.beat_track(onset_envelope=onset, sr=sr)
    beats = librosa.frames_to_time(bf, sr=sr)
    bpm = float(np.atleast_1d(bpm)[0])
    rms = librosa.feature.rms(y=y)[0]
    rt = librosa.frames_to_time(np.arange(len(rms)), sr=sr)

    lines = []
    if a.lyrics:
        d = json.loads(Path(a.lyrics).read_text())
        if a.lyrics_key:
            d = d[a.lyrics_key]
        lines = d['lines']
    # bar phase: prefer the phase where lyric line starts fall on beat 1; fall back to onset strength
    def phase_score(p):
        if lines:
            idx = [int(np.argmin(abs(beats - l['start']))) for l in lines]
            return sum(1 for i in idx if (i - p) % 4 == 0)
        return float(onset[bf[p::4]].mean())
    phase = max(range(4), key=phase_score)

    beat_rows = []
    for i, b in enumerate(beats):
        nxt = beats[i + 1] if i + 1 < len(beats) else dur
        m = (rt >= b) & (rt < nxt)
        beat_rows.append({'i': i, 't': round(float(b), 3), 'bar_beat': (i - phase) % 4 + 1,
                          'energy': round(float(rms[m].mean()) if m.any() else 0.0, 4)})
    snapped = []
    for l in lines:
        i = int(np.argmin(abs(beats - l['start'])))
        snapped.append({**l, 'beat': i, 'beat_t': round(float(beats[i]), 3),
                        'off_s': round(float(l['start'] - beats[i]), 3), 'bar_beat': (i - phase) % 4 + 1})

    res = {'file': a.song, 'duration': round(dur, 3), 'tempo_bpm': round(bpm, 1),
           'beat_period_s': round(float(np.median(np.diff(beats))), 4), 'bar_phase': phase,
           'loudness': loudness(a.song), 'beats': beat_rows, 'lines': snapped}
    (out / 'song-analysis.json').write_text(json.dumps(res, ensure_ascii=False, indent=1))
    print(json.dumps({k: res[k] for k in ('duration', 'tempo_bpm', 'beat_period_s', 'bar_phase', 'loudness')}, ensure_ascii=False))
    for s in snapped:
        print(f"{s['start']:6.2f} -> beat {s['beat']:3d} @ {s['beat_t']:6.2f} (bar beat {s['bar_beat']}, off {s['off_s']:+.2f})  {s['line']}")


if __name__ == '__main__':
    main()
