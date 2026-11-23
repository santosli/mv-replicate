#!/usr/bin/env python3
"""Machine QA of a finished MV against its EDL. render_edl.py runs it before publishing.

Checks (each pass / fail / skipped, with the measured value and the tolerance used):
  video_decode     every frame decodes without errors; frame count == Q(duration)
  video_format     width / height == size; frame rate == fps
  audio_decode     audio decodes; decoded length within max(1 frame, 1 AAC frame) of duration
  audio_packets    packet timestamps continuous: each gap within half a packet duration
  loudness         final (decoded AAC) integrated loudness within ±loudness_I_tol LU of the target and
                   true peak <= target + true_peak_tol dB (only when the EDL has a loudness target)
  sync             final audio vs the rendered PCM (--pcm): cross-correlation lag within sync_ms
The overall status is pass only if no check fails and no required check is skipped.
Machine QA passing is not the same as the user approving the cut.

  python qa_render.py film.mp4 --edl edl.json [--pcm audio.wav] [--out report.json]
  python qa_render.py joined.mp4 --duration 75.4 --fps 60 --size 1280x720 [--loudness -14,-1]
The second form checks a film that has no single EDL, e.g. the main part joined with a credits tail.
"""
import argparse, json, subprocess, sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvr_common as M   # noqa: E402

DEFAULT_TOL = {'loudness_I_tol': 0.5, 'true_peak_tol': 0.1, 'sync_ms': 1.0}


def tolerances(edl):
    t = dict(DEFAULT_TOL)
    for k, v in (edl.get('qa') or {}).items():
        if k in t and M.finite(v) and v >= 0:
            t[k] = v
    return t


def check(status, **kw):
    return {'status': status, **kw}


def run_qa(video, edl, pcm=None, allow_loudness_miss=False):
    video = Path(video)
    tol = tolerances(edl)
    fps = M.frac(edl['fps'])
    W, H = edl['size']
    dur = edl['duration']
    want_frames = M.Q(dur, fps)
    checks, meas, warns = {}, {}, []
    targets = {'frames': want_frames, 'size': [W, H], 'fps': str(fps), 'duration': dur, 'loudness': edl.get('loudness')}

    # ---- video
    r = M.run(['ffmpeg', '-v', 'error', '-i', video, '-map', '0:v:0', '-f', 'null', '-'])
    decode_err = r.stderr.strip()
    r2 = M.run(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0', '-show_entries',
                'stream=nb_read_frames,width,height,avg_frame_rate,r_frame_rate', '-of', 'json', video])
    st = (json.loads(r2.stdout or '{}').get('streams') or [{}])[0]
    got = int(st['nb_read_frames']) if str(st.get('nb_read_frames', '')).isdigit() else None
    meas['video'] = {'frames': got, 'width': st.get('width'), 'height': st.get('height'),
                     'avg_frame_rate': st.get('avg_frame_rate')}
    checks['video_decode'] = check('pass' if got == want_frames and not r.returncode and not decode_err else 'fail',
                                   frames=got, expected=want_frames, errors=decode_err[-300:] or None)
    try:
        rate_ok = M.frac(st.get('avg_frame_rate') or '0/1') == fps
    except (ValueError, ZeroDivisionError):
        rate_ok = False
    checks['video_format'] = check('pass' if st.get('width') == W and st.get('height') == H and rate_ok else 'fail',
                                   size=[st.get('width'), st.get('height')], fps=st.get('avg_frame_rate'))

    # ---- audio decode and packets
    info = M.probe(video)
    if not info['audio']:
        checks['audio_decode'] = check('fail', reason='no audio stream')
    else:
        sr = info['audio']['sample_rate'] or 48000
        try:
            y = M.decode_audio(video, sr)
            alen = len(y) / sr
            limit = max(1 / float(fps), 1024 / sr) + 1e-6
            checks['audio_decode'] = check('pass' if abs(alen - dur) <= limit else 'fail',
                                           seconds=round(alen, 6), expected=dur, tolerance_s=round(limit, 6))
        except M.MvrError as e:
            y = None
            checks['audio_decode'] = check('fail', reason=e.reason)
        pk = M.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'packet=pts,duration',
                    '-of', 'csv=p=0', video]).stdout.split()
        rows = [tuple(int(x) for x in line.split(',')[:2]) for line in pk if line.count(',') >= 1 and 'N/A' not in line]
        tb = M.frac(info['audio']['time_base'])
        gaps = []
        for (p0, d0), (p1, _) in zip(rows, rows[1:]):
            g = p1 - (p0 + d0)
            if abs(g) > d0 / 2:
                gaps.append({'at_s': float(Fraction(p0) * tb), 'gap_s': float(Fraction(g) * tb)})
        checks['audio_packets'] = check('pass' if rows and not gaps else 'fail', packets=len(rows), gaps=gaps[:10],
                                        rule='each gap within half the packet duration')

        # ---- loudness
        fin = M.loudness(video)
        meas['final_loudness'] = fin
        if pcm:
            meas['pcm_loudness'] = M.loudness(pcm)
        L = edl.get('loudness')
        if not L:
            checks['loudness'] = check('skipped', reason='no loudness target in the EDL', required=False)
        elif fin['I'] is None or fin['TP'] is None:
            if allow_loudness_miss:
                checks['loudness'] = check('pass', deviation_accepted_by_user=True, reason=fin['reason'], measured=fin)
                warns.append(f"loudness not measurable ({fin['reason']}); accepted with --allow-loudness-miss")
            else:
                checks['loudness'] = check('fail', reason=fin['reason'], measured=fin)
        else:
            ok_i = abs(fin['I'] - L['I']) <= tol['loudness_I_tol']
            ok_tp = fin['TP'] <= L['TP'] + tol['true_peak_tol']
            st_ = 'pass' if ok_i and ok_tp else 'fail'
            c = check(st_, integrated=fin['I'], true_peak=fin['TP'], target=L)
            if st_ == 'fail' and allow_loudness_miss:
                c.update(status='pass', deviation_accepted_by_user=True,
                         deviation={'I': round(fin['I'] - L['I'], 2), 'TP_over': round(fin['TP'] - L['TP'], 2)})
                warns.append(f"loudness target missed (I {fin['I']}, TP {fin['TP']}); accepted with --allow-loudness-miss")
            checks['loudness'] = c

        # ---- sync against the PCM that was muxed
        if pcm and y is not None:
            ref = M.decode_audio(pcm, sr)
            lag = M.lag_samples(ref, y, max_lag=int(sr * 0.02))
            if lag is None:
                checks['sync'] = check('skipped', reason='audio too short or silent for cross-correlation', required=False)
            else:
                ms = 1000 * lag / sr
                checks['sync'] = check('pass' if abs(ms) <= tol['sync_ms'] else 'fail', lag_samples=lag,
                                       lag_ms=round(ms, 3), tolerance_ms=tol['sync_ms'],
                                       note='AAC priming is removed by the decoder via the edit list; any lag here is real')
        else:
            checks['sync'] = check('skipped', reason='no --pcm reference given', required=False)

    failed = [k for k, c in checks.items() if c['status'] == 'fail']
    missing_required = [k for k, c in checks.items() if c['status'] == 'skipped' and c.get('required', True)]
    status = 'pass' if not failed and not missing_required else 'fail'
    return {'schema_version': M.SCHEMA_VERSION, 'video': str(video), 'targets': targets, 'measurements': meas,
            'tolerances': tol, 'checks': checks, 'warnings': warns, 'status': status}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('video')
    ap.add_argument('--edl', help='the EDL the video was rendered from (targets and tolerances)')
    ap.add_argument('--duration', type=float, help='without --edl: expected length in seconds')
    ap.add_argument('--fps', help='without --edl: expected frame rate (number or num/den)')
    ap.add_argument('--size', help='without --edl: expected WxH')
    ap.add_argument('--loudness', help='without --edl: target "I,TP", e.g. -14,-1 (omit to skip the loudness check)')
    ap.add_argument('--pcm', help='the PCM WAV that was muxed, for the sync check')
    ap.add_argument('--out', help='write the report here (atomically) as well as printing a summary')
    a = ap.parse_args()
    if a.edl:
        edl = M.load_json(a.edl)
    elif a.duration and a.fps and a.size:
        w, h = map(int, a.size.split('x'))
        edl = {'duration': a.duration, 'fps': a.fps, 'size': [w, h]}
        if a.loudness:
            i, tp = map(float, a.loudness.split(','))
            edl['loudness'] = {'I': i, 'TP': tp}
    else:
        M.die('[qa] give --edl, or --duration, --fps and --size')
    try:
        rep = run_qa(a.video, edl, pcm=a.pcm)
        rep['tool_versions'] = M.tool_versions()
    except M.MvrError as e:
        M.die(e)
    if a.out:
        M.atomic_write_json(a.out, rep)
    for k, c in rep['checks'].items():
        extra = {x: v for x, v in c.items() if x not in ('status',)}
        print(f"{c['status']:7s} {k:14s} {json.dumps(extra, ensure_ascii=False)[:160]}")
    print('status:', rep['status'])
    sys.exit(0 if rep['status'] == 'pass' else 1)


if __name__ == '__main__':
    main()
