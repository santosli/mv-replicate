#!/usr/bin/env python3
"""Beat grid, bar phase, energy and loudness of your own song.

Beats come from librosa's tracker, or from --beats (a JSON list of times, or {"beats": [...]}) when
the tracker gets the song wrong; nothing is extrapolated on an even grid. If no beats are found the
run stops and asks for --beats.

The bar phase (which beat of --beats-per-bar, default 4, counts as beat 1) is an assumption, not a
detected downbeat: by default it is the phase where most lyric line starts fall on beat 1
("lyric_line_vote"), else the phase with the strongest onsets ("onset_strength"); --bar-phase sets it
by hand ("manual"). The output records which.

Optional --lyrics takes a JSON with lines [{start, line}] (e.g. examples/lyrics.example.json); each
line start is snapped to the nearest beat. Writes <out>/song-analysis.json.

  python analyze_song.py song.wav --out DIR [--lyrics lyrics.json] [--beats-per-bar 4] [--bar-phase 0] [--beats beats.json]
"""
import argparse, json, sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvr_common as M   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('song')
    ap.add_argument('--out', required=True)
    ap.add_argument('--lyrics', help='JSON file with "lines": [{start, line}]; use --lyrics-key for a nested key')
    ap.add_argument('--lyrics-key')
    ap.add_argument('--beats-per-bar', type=int, default=4)
    ap.add_argument('--bar-phase', type=int, help='beat index (0..beats-per-bar-1) of the first downbeat, set by hand')
    ap.add_argument('--beats', help='hand-made beat times (JSON list or {"beats": [...]}) instead of the tracker')
    a = ap.parse_args()
    try:
        run(a)
    except M.MvrError as e:
        M.die(e)
    except (ValueError, OSError, KeyError) as e:
        M.die(f'[analyze_song] {e}')


def run(a):
    import librosa
    bpb = a.beats_per_bar
    if bpb < 1:
        raise M.MvrError('analyze_song', '--beats-per-bar', 'must be >= 1')
    if a.bar_phase is not None and not 0 <= a.bar_phase < bpb:
        raise M.MvrError('analyze_song', '--bar-phase', f'must be in 0..{bpb - 1}')
    info = M.probe(a.song)
    if not info['audio']:
        raise M.MvrError('analyze_song', a.song, 'no audio stream')
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    y, sr = librosa.load(a.song, sr=22050, mono=True)
    dur = len(y) / sr
    if dur <= 0:
        raise M.MvrError('analyze_song', a.song, 'no audio samples')
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    if a.beats:
        d = M.load_json(a.beats)
        beats = np.array(d['beats'] if isinstance(d, dict) else d, dtype=float)
        if not len(beats) or not np.all(np.isfinite(beats)) or np.any(np.diff(beats) <= 0) or beats[0] < 0 or beats[-1] >= dur:
            raise M.MvrError('analyze_song', a.beats, f'beats must be finite, strictly increasing and inside 0..{dur:.3f}s')
        beat_source, bpm = 'manual', None
    else:
        bpm, bf = librosa.beat.beat_track(onset_envelope=onset, sr=sr)
        beats = librosa.frames_to_time(bf, sr=sr)
        bpm = float(np.atleast_1d(bpm)[0]) if len(beats) else None
        beat_source = 'librosa.beat_track'
    if len(beats) < 2:
        raise M.MvrError('analyze_song', a.song, f'found {len(beats)} beat(s); nothing to cut on. '
                                                 'Pass hand-made beat times with --beats')
    if bpm is None:
        bpm = 60 / float(np.median(np.diff(beats)))
    rms = librosa.feature.rms(y=y)[0]
    rt = librosa.frames_to_time(np.arange(len(rms)), sr=sr)
    beat_frames = librosa.time_to_frames(beats, sr=sr)

    lines = []
    if a.lyrics:
        d = M.load_json(a.lyrics)
        lines = (d[a.lyrics_key] if a.lyrics_key else d)['lines']
        for i, l in enumerate(lines):
            if not M.finite(l.get('start')) or (i and l['start'] < lines[i - 1]['start']):
                raise M.MvrError('analyze_song', f'lyrics line {i}', 'start must be finite and not before the previous line')

    def phase_score(p):
        if lines:
            idx = [int(np.argmin(abs(beats - l['start']))) for l in lines]
            return sum(1 for i in idx if (i - p) % bpb == 0)
        sel = beat_frames[p::bpb]
        sel = sel[sel < len(onset)]
        return float(onset[sel].mean()) if len(sel) else 0.0
    if a.bar_phase is not None:
        phase, method = a.bar_phase, 'manual'
    else:
        phase = max(range(min(bpb, len(beats))), key=phase_score)
        method = 'lyric_line_vote' if lines else 'onset_strength'

    beat_rows = []
    for i, b in enumerate(beats):
        nxt = beats[i + 1] if i + 1 < len(beats) else dur
        m = (rt >= b) & (rt < nxt)
        beat_rows.append({'i': i, 't': round(float(b), 3), 'bar_beat': (i - phase) % bpb + 1,
                          'energy': round(float(rms[m].mean()) if m.any() else 0.0, 4)})
    snapped = []
    for l in lines:
        i = int(np.argmin(abs(beats - l['start'])))
        snapped.append({**l, 'beat': i, 'beat_t': round(float(beats[i]), 3),
                        'off_s': round(float(l['start'] - beats[i]), 3), 'bar_beat': (i - phase) % bpb + 1})

    res = {'schema_version': M.SCHEMA_VERSION, 'file': a.song, 'duration': round(dur, 3),
           'tempo_bpm': round(bpm, 1), 'beat_source': beat_source,
           'beat_period_s': round(float(np.median(np.diff(beats))), 4),
           'beats_per_bar': bpb, 'bar_phase': phase, 'bar_phase_method': method,
           'bar_phase_note': 'an assumption used to number beats, not a detected downbeat',
           'loudness': M.loudness(a.song), 'beats': beat_rows, 'lines': snapped}
    M.atomic_write_json(out / 'song-analysis.json', res)
    print(json.dumps({k: res[k] for k in ('duration', 'tempo_bpm', 'beat_source', 'beat_period_s', 'beats_per_bar',
                                          'bar_phase', 'bar_phase_method')}, ensure_ascii=False))
    for s in snapped:
        print(f"{s['start']:6.2f} -> beat {s['beat']:3d} @ {s['beat_t']:6.2f} (bar beat {s['bar_beat']}, off {s['off_s']:+.2f})  {s['line']}")


if __name__ == '__main__':
    main()
