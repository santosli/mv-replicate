#!/usr/bin/env python3
"""Environment check: only what the chosen task needs. Missing optional pieces are reported as
unavailable / skipped, never as ok.

  python doctor.py                         # render essentials (Python modules, ffmpeg, encoders, filters)
  python doctor.py --task analyze          # + librosa for analyze_ref / analyze_song
  python doctor.py --task asr --whisper-model ~/models/ggml-large-v3-turbo-q5_0.bin
  python doctor.py --edl edl.json --root .  # + the fonts and media this EDL uses
  python doctor.py --inputs song.wav clip.mp4 [--json]

Exit code 0 when every required check is ok, 1 otherwise.
"""
import argparse, importlib, json, os, shutil, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvr_common as M   # noqa: E402

ENCODERS = ('libx264', 'aac', 'qtrle', 'pcm_s24le', 'ffv1')
FILTERS = {'render': ('scale', 'crop', 'fps', 'setpts', 'trim', 'tpad', 'reverse', 'overlay', 'alphamerge', 'fade',
                      'gblur', 'rotate', 'lutrgb', 'afade', 'atrim', 'alimiter', 'aresample', 'volume', 'ebur128'),
           'analyze': ('scdet', 'signalstats', 'ebur128', 'scale')}


def row(name, status, detail='', required=True):
    return {'check': name, 'status': status, 'detail': detail, 'required': required}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--task', action='append', choices=('render', 'analyze', 'asr'), help='repeatable; default render')
    ap.add_argument('--edl')
    ap.add_argument('--root', default='.')
    ap.add_argument('--whisper-model', action='append', default=[])
    ap.add_argument('--inputs', nargs='*', default=[])
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    tasks = set(a.task or ['render'])
    if a.edl:
        tasks.add('render')
    rows = []

    mods = ['PIL', 'numpy'] + (['librosa', 'soundfile'] if 'analyze' in tasks else ['soundfile'])
    for m in mods:
        try:
            mod = importlib.import_module(m)
            rows.append(row(f'python:{m}', 'ok', getattr(mod, '__version__', '')))
        except Exception as e:
            rows.append(row(f'python:{m}', 'fail', str(e), required=m != 'soundfile'))
    rows.append(row('python', 'ok' if sys.version_info >= (3, 10) else 'fail', sys.version.split()[0]))

    have_ff = all(shutil.which(x) for x in ('ffmpeg', 'ffprobe'))
    rows.append(row('ffmpeg/ffprobe', 'ok' if have_ff else 'fail', M.tool_versions()['ffmpeg'] if have_ff else 'not on PATH'))
    if have_ff:
        enc = M.run(['ffmpeg', '-hide_banner', '-encoders']).stdout
        for e in ENCODERS:
            rows.append(row(f'encoder:{e}', 'ok' if f' {e} ' in enc else 'fail', required=e != 'ffv1'))
        flt = M.run(['ffmpeg', '-hide_banner', '-filters']).stdout
        names = {line.split()[1] for line in flt.splitlines() if len(line.split()) > 2 and line[:1] == ' '}
        for t in sorted(tasks & set(FILTERS)):
            for f in FILTERS[t]:
                rows.append(row(f'filter:{f}', 'ok' if f in names else 'fail', t))
        rows.append(row('filter:drawtext', 'ok' if 'drawtext' in names else 'unavailable',
                        'not needed: text is drawn with Pillow', required=False))

    if 'asr' in tasks:
        wc = shutil.which('whisper-cli')
        rows.append(row('whisper-cli', 'ok' if wc else 'fail', wc or 'not on PATH (brew install whisper-cpp)'))
        if wc:
            h = subprocess.run([wc, '--help'], capture_output=True, text=True)
            txt = h.stdout + h.stderr
            rows.append(row('whisper-cli:-dtw', 'ok' if '-dtw' in txt else 'fail', 'DTW token timestamps'))
            rows.append(row('whisper-cli:-nfa', 'ok' if '-nfa' in txt else 'fail', 'needed so DTW is not silently disabled'))
        if not a.whisper_model:
            rows.append(row('whisper model', 'fail', 'give --whisper-model PATH (ggml-*.bin)'))
        for mp in a.whisper_model:
            p = Path(mp).expanduser()
            rows.append(row(f'model:{p.name}', 'ok' if p.is_file() and os.access(p, os.R_OK) else 'fail', str(p)))
    else:
        rows.append(row('whisper-cli', 'skipped', 'not needed for this task (add --task asr)', required=False))

    if a.edl:
        import validate_edl as V
        try:
            edl = M.load_json(a.edl)
            res = V.validate(edl, Path(a.root).expanduser(), a.edl, check_media=have_ff)
            for e in res['errors']:
                rows.append(row('edl', 'fail', e))
            for w in res['warnings']:
                rows.append(row('edl', 'warning', w, required=False))
            fonts = [p for p, k in res['plan']['inputs'].items() if k == 'font']
            for f in fonts:
                try:
                    from PIL import ImageFont
                    ImageFont.truetype(f, 20)
                    rows.append(row('font', 'ok', f))
                except Exception as e:
                    rows.append(row('font', 'fail', f'{f}: {e}'))
            if not fonts:
                rows.append(row('font', 'skipped', 'this EDL draws no text', required=False))
            if not res['errors']:
                rows.append(row('edl', 'ok', f"{len(res['plan']['segments'])} segments, {res['plan']['frames']} frames"))
        except (ValueError, OSError) as e:
            rows.append(row('edl', 'fail', str(e)))

    for ip in a.inputs:
        p = Path(ip).expanduser()
        if not p.is_file():
            rows.append(row(f'input:{p.name}', 'fail', 'missing'))
            continue
        try:
            info = M.probe(p)
            v, au = info['video'], info['audio']
            desc = []
            if v:
                desc.append(f"video {v['width']}x{v['height']} {v['pix_fmt']} {v['avg_frame_rate']} fps "
                            f"range={v['color_range']} transfer={v['color_transfer']}")
            if au:
                desc.append(f"audio {au['codec']} {au['sample_rate']} Hz x{au['channels']}")
            desc.append(f"{info['duration']} s")
            rows.append(row(f'input:{p.name}', 'ok' if (v or au) else 'fail', '; '.join(desc)))
        except M.MvrError as e:
            rows.append(row(f'input:{p.name}', 'fail', e.reason))

    bad = [r for r in rows if r['required'] and r['status'] not in ('ok', 'warning')]
    if a.json:
        print(M.dumps({'tasks': sorted(tasks), 'checks': rows, 'status': 'fail' if bad else 'ok'}))
    else:
        for r in rows:
            print(f"{r['status']:11s} {r['check']:22s} {r['detail']}")
        print('status:', 'fail' if bad else 'ok')
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
