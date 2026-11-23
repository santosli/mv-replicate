"""Synthetic media for the regression suite: numbered frames, VFR clips, tones and impulses.

Every frame of a numbered clip is one flat colour that encodes its index in base 6
(r, g, b digits * 51), so a decoded frame can be read back as its source index.
Nothing here downloads anything; all media are generated in throw-away folders.
"""
import json, math, subprocess
from pathlib import Path

import numpy as np


def color_of(i):
    return ((i % 6) * 51, ((i // 6) % 6) * 51, ((i // 36) % 6) * 51)


def index_of(rgb):
    r, g, b = (int(round(float(c) / 51)) for c in rgb)
    return r + 6 * g + 36 * b


def _encode(frames_rgb, w, h, out, rate, extra_in=(), vf=None, codec=('-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p')):
    cmd = ['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(rate),
           '-i', '-', *extra_in]
    if vf:
        cmd += ['-vf', vf]
    cmd += [*codec, str(out)]
    p = subprocess.run(cmd, input=b''.join(frames_rgb), capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode()[-1500:])
    return Path(out)


def numbered_video(out, n, fps='60', w=64, h=36, start=0, codec=None):
    frames = [np.full((h, w, 3), color_of(start + i), np.uint8).tobytes() for i in range(n)]
    kw = {'codec': codec} if codec else {}
    return _encode(frames, w, h, out, fps, **kw)


def vfr_video(out, pts, w=64, h=36):
    """Frames 0..len(pts)-1 at the given presentation times (seconds), passthrough timing."""
    frames = [np.full((h, w, 3), color_of(i), np.uint8).tobytes() for i in range(len(pts))]
    expr = '0'
    for i in reversed(range(len(pts))):
        expr = f'if(eq(N\\,{i})\\,{pts[i]}\\,{expr})'
    return _encode(frames, w, h, out, 25, vf=f"setpts='{expr}/TB'",
                   codec=('-fps_mode', 'passthrough', '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p',
                          '-video_track_timescale', '1000'))


def gray_sequence_video(out, levels, fps=10, w=64, h=36, ten_bit=False):
    """Flat grey frames (8-bit levels 0-255) as 8-bit H.264 or 10-bit FFV1."""
    frames = [np.full((h, w, 3), lv, np.uint8).tobytes() for lv in levels]
    codec = ('-c:v', 'ffv1', '-pix_fmt', 'yuv420p10le') if ten_bit else ('-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p')
    return _encode(frames, w, h, out, fps, codec=codec)


def decode_indices(path, w=None, h=None):
    """Decode every frame and read the colour index at the centre."""
    info = json.loads(subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                      'stream=width,height', '-of', 'json', str(path)],
                                     capture_output=True, text=True).stdout)['streams'][0]
    w, h = w or info['width'], h or info['height']
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                         capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)
    return [index_of(f[h // 2, w // 2]) for f in a]


def write_wav(out, y, sr=48000):
    import soundfile as sf
    y = np.asarray(y, np.float64)
    if y.ndim == 1:
        y = np.stack([y, y], 1)
    sf.write(str(out), y, sr, subtype='PCM_24')
    return Path(out)


def music_like(seconds, sr=48000, bpm=120, seed=0):
    """Clicks on the beat over a soft chord: enough for beat tracking and loudness."""
    rnd = np.random.default_rng(seed)
    t = np.arange(int(seconds * sr)) / sr
    y = 0.08 * (np.sin(2 * np.pi * 220 * t) + np.sin(2 * np.pi * 277 * t) + np.sin(2 * np.pi * 330 * t))
    y += 0.01 * rnd.standard_normal(len(t))
    for b in np.arange(0.25, seconds, 60 / bpm):
        a, n = int(b * sr), int(0.03 * sr)
        if a + n < len(y):
            y[a:a + n] += 0.5 * np.sin(2 * np.pi * 1000 * np.arange(n) / sr) * np.linspace(1, 0, n)
    return y


def peaky(seconds, sr=48000):
    """Very high peak-to-average: quiet hiss with a few sharp high-frequency bursts."""
    y = 0.002 * np.random.default_rng(1).standard_normal(int(seconds * sr))
    for s in np.arange(0.5, seconds - 0.1, 1.3):
        a, n = int(s * sr), int(0.004 * sr)
        y[a:a + n] += 0.98 * np.sin(2 * np.pi * 7000 * np.arange(n) / sr)
    return y


def impulse(seconds, at, sr=48000):
    y = np.zeros(int(seconds * sr))
    y[int(round(at * sr))] = 0.9
    return y


def whisper_json(out, tokens):
    """tokens: [(text, t_dtw_seconds_or_None_or_value, offset_seconds)]"""
    toks = []
    for text, dtw, off in tokens:
        t = {'text': text, 'offsets': {'from': int(round(off * 1000)), 'to': int(round(off * 1000)) + 200}}
        if dtw is not None:
            t['t_dtw'] = dtw if isinstance(dtw, str) or (isinstance(dtw, float) and not math.isfinite(dtw)) else int(round(dtw * 100))
        toks.append(t)
    Path(out).write_text(json.dumps({'transcription': [{'tokens': toks}]}, ensure_ascii=False, allow_nan=True))
    return Path(out)
