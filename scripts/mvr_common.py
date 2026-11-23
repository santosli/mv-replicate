"""Shared conventions for mv-replicate: time quantisation, source-frame selection, AVOID ranges,
media identity, path safety, strict JSON and atomic files. See references/conventions.md.

Every script imports this module from its own folder, so it never needs installing.
"""
from __future__ import annotations

import contextlib, fcntl, hashlib, json, math, os, platform, shutil, subprocess, sys, tempfile
from bisect import bisect_right
from fractions import Fraction
from pathlib import Path

SCHEMA_VERSION = 1


class MvrError(Exception):
    """A failure with a stage and a location, printed as one line and exit code 2."""

    def __init__(self, stage, where, reason):
        super().__init__(f'[{stage}] {where}: {reason}')
        self.stage, self.where, self.reason = stage, where, reason


def die(err):
    print(f'error: {err}', file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------- numbers and time

def frac(x):
    """Exact rational from a JSON number or string ('30000/1001', '29.97', 0.1 -> 1/10)."""
    if isinstance(x, Fraction):
        return x
    if isinstance(x, int):
        return Fraction(x)
    if isinstance(x, float):
        if not math.isfinite(x):
            raise ValueError(f'non-finite number {x}')
        return Fraction(repr(x))
    return Fraction(str(x))


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def Q(t, fps):
    """Output frame boundary of time t: round half up of t * fps. Segment k has Q(end) - Q(start) frames."""
    return math.floor(frac(t) * frac(fps) + Fraction(1, 2))


# ---------------------------------------------------------------- probing

def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


_PROBE_CACHE = {}


def probe(path):
    """Container and stream facts plus, for video, every decoded frame's integer pts in display order."""
    path = Path(path)
    st = path.stat()
    key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    r = run(['ffprobe', '-v', 'error', '-show_entries',
             'format=duration:stream=index,codec_type,codec_name,width,height,pix_fmt,r_frame_rate,avg_frame_rate,'
             'time_base,start_time,duration,sample_rate,channels,bits_per_raw_sample,color_range,color_transfer,'
             'color_primaries,color_space:stream_disposition=attached_pic', '-of', 'json', path])
    if r.returncode:
        raise MvrError('probe', str(path), r.stderr.strip()[-300:] or 'ffprobe failed')
    d = json.loads(r.stdout)
    v = next((s for s in d.get('streams', []) if s.get('codec_type') == 'video'
              and not s.get('disposition', {}).get('attached_pic')), None)
    a = next((s for s in d.get('streams', []) if s.get('codec_type') == 'audio'), None)
    info = {'path': str(path), 'duration': float(d.get('format', {}).get('duration') or 0) or None,
            'video': None, 'audio': None}
    if a:
        info['audio'] = {'codec': a.get('codec_name'), 'sample_rate': int(a.get('sample_rate') or 0),
                         'channels': a.get('channels'), 'time_base': a.get('time_base')}
    if v:
        info['video'] = {k: v.get(k) for k in ('codec_name', 'width', 'height', 'pix_fmt', 'r_frame_rate', 'avg_frame_rate',
                                                'time_base', 'bits_per_raw_sample', 'color_range', 'color_transfer',
                                                'color_primaries', 'color_space')}
    _PROBE_CACHE[key] = info
    return info


def frame_table(path):
    """(time_base, [pts ints in display order], [end pts ints]) from actually decoding the video stream."""
    path = Path(path)
    info = probe(path)
    if 'frames' in info:
        return info['frames']
    if not info['video']:
        raise MvrError('probe', str(path), 'no video stream')
    cached = _disk_cache(path)
    if cached:
        info['frames'] = cached
        return cached
    r = run(['ffprobe', '-v', 'error', '-threads', '0', '-select_streams', 'v:0', '-show_entries',
             'frame=best_effort_timestamp,duration', '-of', 'csv=p=0', path])
    if r.returncode:
        raise MvrError('probe', str(path), 'could not decode frames: ' + r.stderr.strip()[-300:])
    tb = frac(info['video']['time_base'])
    pts, durs = [], []
    for line in r.stdout.split():
        parts = line.split(',')
        if not parts[0] or parts[0] == 'N/A':
            continue
        pts.append(int(parts[0]))
        durs.append(int(parts[1]) if len(parts) > 1 and parts[1] not in ('', 'N/A') else None)
    if not pts:
        raise MvrError('probe', str(path), 'decoded no video frames')
    order = sorted(range(len(pts)), key=lambda i: pts[i])
    pts = [pts[i] for i in order]
    durs = [durs[i] for i in order]
    ends = []
    for i, p in enumerate(pts):
        if i + 1 < len(pts):
            ends.append(pts[i + 1])
        else:
            dur = durs[i]
            if dur is None:
                rate = frac(info['video']['avg_frame_rate'] or info['video']['r_frame_rate'] or '25')
                dur = int(round(1 / (rate * tb))) if rate else 1
            ends.append(p + max(1, dur))
    info['frames'] = (tb, pts, ends)
    _disk_cache(path, info['frames'])
    return info['frames']


def _disk_cache(path, value=None):
    """Frame tables cost a full decode, so they are cached on disk, keyed by the file's real path, size,
    modification time and the ffprobe version. MV_REPLICATE_CACHE sets the folder; '0' turns it off."""
    root = os.environ.get('MV_REPLICATE_CACHE')
    if root == '0':
        return None
    root = Path(root).expanduser() if root else Path(os.environ.get('XDG_CACHE_HOME', '~/.cache')).expanduser() / 'mv-replicate'
    st = Path(path).stat()
    key = json.dumps([str(Path(path).resolve()), st.st_size, st.st_mtime_ns, tool_versions_ffprobe()])
    f = root / 'frames' / (hashlib.sha1(key.encode()).hexdigest() + '.json')
    try:
        if value is None:
            if f.exists():
                d = json.loads(f.read_text())
                if d.get('key') == key:
                    return (Fraction(d['tb']), d['pts'], d['ends'])
            return None
        tb, pts, ends = value
        atomic_write_text(f, json.dumps({'key': key, 'tb': str(tb), 'pts': pts, 'ends': ends}))
    except (OSError, ValueError, KeyError):
        return None


_FFPROBE_VERSION = []


def tool_versions_ffprobe():
    if not _FFPROBE_VERSION:
        try:
            _FFPROBE_VERSION.append(run(['ffprobe', '-version']).stdout.splitlines()[0])
        except Exception:
            _FFPROBE_VERSION.append('unknown')
    return _FFPROBE_VERSION[0]


def tick(t, tb):
    """Time in seconds -> nearest time-base tick (half up). Decimal times such as 1/60 s are not exact
    binary floats; rounding to the tick first makes "exactly on a frame's PTS" mean what it says."""
    return math.floor(frac(t) / tb + Fraction(1, 2))


def cover(pts, tb, t):
    """Index of the frame on screen at source time t (last frame with pts <= tick(t)), or None before the first."""
    k = bisect_right(pts, tick(t, tb)) - 1
    return k if k >= 0 else None


# ---------------------------------------------------------------- source-frame selection (mirrors render_edl)

def seg_frame_count(seg, fps):
    return Q(frac(seg['at']) + frac(seg['dur']), fps) - Q(seg['at'], fps)


def seg_source_start(seg):
    speed = frac(seg.get('speed', 1))
    if 'from' in seg:
        return frac(seg['from'])
    return frac(seg['end']) - frac(seg['dur']) * speed


def select_frames(tb, pts, ends, start, n, fps, speed=1):
    """Source indices shown on output frames 0..n-1, exactly as the render graph picks them:
    setpts=trunc((PTS - S) / speed) with S = round(start / tb), then fps=round=up with start_time 0.
    The result is the frame on screen at start + k * speed / fps (to within one time-base tick).
    Returns (indices, None) or (partial, reason) when the source runs out."""
    S = tick(start, tb)
    sp = float(frac(speed))
    slots = []
    for p in pts:
        np_ = int((p - S) / sp)                      # setpts: double, then truncation toward zero
        slots.append(math.ceil(Fraction(np_) * tb * frac(fps)))   # fps filter, round=up
    out = []
    for k in range(n):
        j = bisect_right(slots, k) - 1
        if j < 0:
            return out, f'no source frame at output frame {k} (source starts after the selection)'
        out.append(j)
    last_needed = frac(start) + frac(n - 1) * frac(speed) / frac(fps)
    end_of_source = Fraction(ends[-1]) * tb
    if last_needed >= end_of_source:
        return out, (f'needs source up to {float(last_needed):.4f}s but the clip ends at {float(end_of_source):.4f}s')
    return out, None


def segment_sources(seg, fps, root):
    """[(path, kind, [source frame indices])] for every video input the segment shows."""
    n = seg_frame_count(seg, fps)
    out = []
    if 'src' in seg:
        path = resolve_path(seg['src'], root)
        tb, pts, ends = frame_table(path)
        start = seg_source_start(seg)
        if seg.get('freeze'):
            idx, why = select_frames(tb, pts, ends, start, 1, fps)
            idx = idx * n if idx else idx
        else:
            idx, why = select_frames(tb, pts, ends, start, n, fps, seg.get('speed', 1))
        if why:
            raise MvrError('validate', f"segment {seg.get('_i')} src {seg['src']}", why)
        if seg.get('reverse'):
            idx = idx[::-1]
        out.append((path, 'src', idx))
    if seg.get('wipe_in'):
        wp = seg['wipe_in']
        path = resolve_path(wp['src'], root)
        tb, pts, ends = frame_table(path)
        wn = min(n, Q(wp.get('dur', 0.9), fps) + 1)
        # conservative: every frame of the second input that can appear before the wipe completes
        idx, why = select_frames(tb, pts, ends, wp['from'], wn, fps)
        if why:
            raise MvrError('validate', f"segment {seg.get('_i')} wipe_in {wp['src']}", why)
        out.append((path, 'wipe_in', idx))
    return out


def source_args(path, start):
    """Input options that keep original timestamps (-copyts) and seek coarsely to before the frame
    on screen at `start`, so the graph sees exact source pts."""
    tb, pts, _ = frame_table(path)
    j = cover(pts, tb, start)
    first = Fraction(pts[j if j is not None else 0]) * tb
    ss = max(Fraction(0), first - Fraction(1, 2))
    return ['-ss', f'{float(ss):.6f}', '-copyts', '-i', str(path)]


def select_chain(path, start, n, fps, speed=1, freeze=False, reverse=False):
    """Filter prefix that yields exactly the frames select_frames() predicts."""
    tb, _, _ = frame_table(path)
    S = tick(start, tb)
    sp = float(frac(speed))
    f = [f'setpts=trunc((PTS-{S})/{sp!r})', f'fps={frac(fps)}:start_time=0:round=up']
    if freeze:
        f += ['trim=end_frame=1', f'tpad=stop_mode=clone:stop={n - 1}']
    else:
        f += [f'trim=end_frame={n}']
    if reverse:
        f.append('reverse')
    return f


# ---------------------------------------------------------------- AVOID

def forbidden_frames(path, ranges):
    """Source frames on screen at any time inside an AVOID range [a, b): pts < b and end > a."""
    tb, pts, ends = frame_table(path)
    bad = {}
    for a, b in ranges:
        A, B = frac(a), frac(b)
        for j, (p, e) in enumerate(zip(pts, ends)):
            if Fraction(p) * tb < B and Fraction(e) * tb > A:
                bad.setdefault(j, (float(A), float(B)))
    return bad


def check_avoid(segments, fps, root, avoid):
    """avoid: {path or alias-resolved path: [[a, b], ...]}. Returns a list of error strings."""
    if not avoid:
        return []
    by_id = {}
    for p, rngs in avoid.items():
        path = resolve_path(p, root)
        if not path.exists():
            raise MvrError('validate', f'avoid[{p}]', 'file does not exist')
        for r in rngs:
            if len(r) != 2 or not all(finite(x) for x in r) or not r[0] < r[1]:
                raise MvrError('validate', f'avoid[{p}]', f'range {r} must be [start, end) with start < end')
        by_id.setdefault(media_id(path), (path, []))[1].extend(rngs)
    errors = []
    for seg in segments:
        if 'src' not in seg:
            continue
        for path, kind, idx in segment_sources(seg, fps, root):
            mid = media_id(path)
            if mid not in by_id:
                continue
            bad = forbidden_frames(path, by_id[mid][1])
            hits = sorted({j for j in idx if j in bad})
            if hits:
                tb, pts, _ = frame_table(path)
                j = hits[0]
                errors.append(f"segment {seg['_i']} ({seg.get('note', '')}) {kind} {path.name}: shows source frame {j} "
                              f"(t={float(pts[j] * tb):.4f}s) inside avoided range {list(bad[j])}"
                              + (f' and {len(hits) - 1} more' if len(hits) > 1 else ''))
    return errors


# ---------------------------------------------------------------- paths and identity

def resolve_path(p, root):
    p = Path(os.path.expanduser(str(p)))
    return (p if p.is_absolute() else Path(root) / p).resolve()


def media_id(path):
    st = Path(path).resolve().stat()
    return (st.st_dev, st.st_ino)


def is_within(child, parent):
    child, parent = Path(child).resolve(), Path(parent).resolve()
    return child == parent or parent in child.parents


def sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def fingerprint(paths):
    out = {}
    for p in paths:
        p = Path(p)
        if p.exists() and p.is_file():
            out[str(p)] = {'sha256': sha256(p), 'bytes': p.stat().st_size}
    return out


def tool_versions():
    def first(cmd):
        try:
            return run(cmd).stdout.splitlines()[0]
        except Exception:
            return None
    v = {'python': platform.python_version(), 'platform': platform.platform(),
         'ffmpeg': first(['ffmpeg', '-version']), 'ffprobe': first(['ffprobe', '-version'])}
    try:
        import PIL
        v['pillow'] = PIL.__version__
    except Exception:
        v['pillow'] = None
    return v


# ---------------------------------------------------------------- files

def dumps(obj):
    """Standard JSON only: NaN / Infinity raise instead of being written."""
    return json.dumps(obj, ensure_ascii=False, indent=1, allow_nan=False)


def load_json(path):
    def bad(c):
        raise ValueError(f'{path}: non-finite number {c} is not allowed')
    return json.loads(Path(path).read_text(), parse_constant=bad)


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def atomic_write_json(path, obj):
    atomic_write_text(path, dumps(obj) + '\n')


def publish(candidate, target, replace=False):
    """Move a finished candidate onto target: atomic; refuses an existing target unless replace."""
    candidate, target = Path(candidate), Path(target)
    if replace:
        os.replace(candidate, target)
        return
    try:
        os.link(candidate, target)       # fails if target exists: no window where it is half-written
    except FileExistsError:
        raise MvrError('publish', str(target), 'already exists; pass --replace to swap it for the new candidate')
    except OSError:
        # file systems without hard links (exFAT, some network shares): the caller holds the output
        # lock, so check-then-rename cannot race with another mv-replicate run
        if target.exists():
            raise MvrError('publish', str(target), 'already exists; pass --replace to swap it for the new candidate')
        os.replace(candidate, target)
        return
    os.unlink(candidate)


@contextlib.contextmanager
def output_lock(target):
    """Exclusive flock next to the final output. Released by the OS if the process dies."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.parent / f'.{target.name}.lock'
    f = open(lock, 'a+')
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MvrError('lock', str(target), 'another run is writing this output right now')
        yield
    finally:
        f.close()


@contextlib.contextmanager
def run_dir(parent, keep=False, prefix='run-'):
    """A fresh directory owned by this run inside `parent`; only it is ever removed."""
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    try:
        yield d
    finally:
        if not keep:
            shutil.rmtree(d, ignore_errors=True)
        else:
            print(f'kept work files in {d}', file=sys.stderr)


# ---------------------------------------------------------------- media helpers

def encode_rgba(frames, W, H, fps, out):
    """Stream RGBA PIL images straight into a qtrle .mov (no per-frame PNG files). Returns the frame count."""
    p = subprocess.Popen(['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgba', '-s', f'{W}x{H}',
                          '-r', str(frac(fps)), '-i', '-', '-c:v', 'qtrle', '-pix_fmt', 'argb', str(out)],
                         stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    count = 0
    try:
        for im in frames:
            if im.mode != 'RGBA' or im.size != (W, H):
                raise MvrError('encode', str(out), f'frame {count} is {im.mode} {im.size}, expected RGBA {W}x{H}')
            p.stdin.write(im.tobytes())
            count += 1
        p.stdin.close()
    except BrokenPipeError:
        pass
    except BaseException:
        p.kill()
        p.wait()
        raise
    errtxt = p.stderr.read().decode(errors='replace')
    if p.wait():
        raise MvrError('encode', str(out), errtxt.strip()[-500:] or 'ffmpeg failed')
    return count


def loudness(path, af=None):
    """EBU R128 integrated loudness and true peak of the (decoded) audio. Values are None with a
    reason when they cannot be measured (silence, under 0.4 s, no audio)."""
    cmd = ['ffmpeg', '-hide_banner', '-nostats', '-i', str(path), '-vn']
    cmd += ['-af', (af + ',' if af else '') + 'ebur128=peak=true:framelog=quiet', '-f', 'null', '-']
    r = run(cmd)
    tail = r.stderr[r.stderr.rfind('Summary:'):] if 'Summary:' in r.stderr else ''
    res = {'I': None, 'TP': None, 'status': 'ok', 'reason': None}
    if r.returncode or not tail:
        res.update(status='unavailable', reason=(r.stderr.strip()[-300:] or 'no ebur128 summary'))
        return res
    for line in tail.splitlines():
        line = line.strip()
        for key, field in (('I:', 'I'), ('Peak:', 'TP')):
            if line.startswith(key):
                v = line.split(':', 1)[1].split()[0]
                try:
                    x = float(v)
                    res[field] = x if math.isfinite(x) else None
                except ValueError:
                    res[field] = None
    if res['I'] is None or res['I'] <= -69.9:
        res.update(I=None, status='unmeasurable', reason='integrated loudness undefined (silence or shorter than 0.4 s)')
    return res


def decode_audio(path, sr=48000, mono=True):
    import numpy as np
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ac', '1' if mono else '2', '-ar', str(sr),
                        '-f', 'f32le', '-'], capture_output=True)
    if r.returncode:
        raise MvrError('decode', str(path), r.stderr.decode(errors='replace').strip()[-300:])
    return np.frombuffer(r.stdout, np.float32)


def lag_samples(ref, test, max_lag):
    """Lag (in samples) of `test` against `ref` by cross-correlation over the loudest window."""
    import numpy as np
    n = min(len(ref), len(test))
    if n < 4 * max_lag:
        return None
    win = min(n - 2 * max_lag, 48000 * 4)
    e = np.convolve(ref[:n] ** 2, np.ones(4800), 'valid')
    c = int(np.argmax(e[max_lag:n - win - max_lag])) + max_lag if n - win - 2 * max_lag > 0 else max_lag
    a = ref[c:c + win].astype(np.float64)
    if not np.any(a):
        return None
    best, arg = -1, 0
    for lag in range(-max_lag, max_lag + 1):
        b = test[c + lag:c + lag + win].astype(np.float64)
        if len(b) < win:
            continue
        v = float(np.dot(a, b))
        if v > best:
            best, arg = v, lag
    return arg
