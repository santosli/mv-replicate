#!/usr/bin/env python3
"""Regression suite for mv-replicate. Media are generated on the fly in throw-away folders.

  python tests/run_tests.py                 # everything
  python tests/run_tests.py T05 T13         # selected IDs (test names start with the ID)
  python tests/run_tests.py -v              # verbose

Each test name starts with its ID from the improvement plan (T01..T27), so a failure names the case.
Destructive-path tests only ever touch folders they create, nested two levels inside their own temp dir.
"""
import hashlib, json, math, os, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SCRIPTS = REPO / 'scripts'
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))
import fixtures as F          # noqa: E402
import mvr_common as M        # noqa: E402

PY = sys.executable
HAVE_FFMPEG = shutil.which('ffmpeg') is not None and shutil.which('ffprobe') is not None
FONT = os.environ.get('MV_REPLICATE_FONT') or next(
    (str(p) for p in [Path('~/Library/Fonts/HarmonyOS_Sans_SC_Black.ttf').expanduser(),
                      Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf'),
                      Path('/System/Library/Fonts/Hiragino Sans GB.ttc'),
                      Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')] if p.exists()), None)
media = unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg/ffprobe not available: media test skipped')


def sh(args, cwd=None, env=None, timeout=600):
    e = dict(os.environ)
    if FONT:
        e.setdefault('MV_REPLICATE_FONT', FONT)
    e.update(env or {})
    return subprocess.run([str(a) for a in args], cwd=cwd, env=e, capture_output=True, text=True, timeout=timeout)


def tree_hashes(root, skip=()):
    out = {}
    for p in sorted(Path(root).rglob('*')):
        if p.is_file() and not any(s in p.parts for s in skip):
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Project:
    """A tiny MV project nested two levels inside its own temp dir: base/x/y/proj."""

    def __init__(self, fps=10, size=(128, 72), song_s=3.0):
        self.base = Path(tempfile.mkdtemp(prefix='mvr-test-'))
        self.dir = self.base / 'x' / 'y' / 'proj'
        (self.dir / 'clips').mkdir(parents=True)
        (self.dir / 'song').mkdir()
        self.fps, self.size = fps, list(size)
        F.numbered_video(self.dir / 'clips/a.mp4', 60, fps=str(fps))
        F.numbered_video(self.dir / 'clips/b.mp4', 60, fps=str(fps), start=60)
        F.write_wav(self.dir / 'song/song.wav', F.music_like(song_s))
        (self.dir / 'SENTINEL').write_text('keep me')

    def edl(self, segments=None, **kw):
        d = {'song': 'song/song.wav', 'duration': 2.0, 'size': self.size, 'fps': self.fps,
             'work_dir': 'tmp/work', 'out': 'out/film.mp4',
             'segments': segments or [{'at': 0, 'dur': 1.0, 'src': 'clips/a.mp4', 'from': 0.0},
                                      {'at': 1.0, 'dur': 1.0, 'src': 'clips/b.mp4', 'from': 1.0}]}
        d.update(kw)
        return d

    def write(self, edl, name='edl.json', raw=None):
        p = self.dir / name
        p.write_text(raw if raw is not None else json.dumps(edl, ensure_ascii=False))
        return p

    def render(self, edl_name='edl.json', *extra):
        return sh([PY, SCRIPTS / 'render_edl.py', edl_name, '--root', '.', *extra], cwd=self.dir)

    def cleanup(self):
        shutil.rmtree(self.base, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.p = Project()

    def tearDown(self):
        self.p.cleanup()

    def ok(self, r):
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-1500:])
        return r


# ============================================================== phase 1: paths, validation, outputs

@media
class Phase1(Base):
    def test_T01_workdir_dotdot_is_isolated(self):
        self.p.write(self.p.edl(work_dir='tmp/..'))
        before = tree_hashes(self.p.dir)
        self.ok(self.p.render())
        after = tree_hashes(self.p.dir, skip=('out',))
        self.assertEqual(before, {k: v for k, v in after.items()})
        self.assertFalse([x for x in self.p.dir.iterdir() if x.name.startswith('run-')], 'run folder left behind')
        self.assertTrue((self.p.dir / 'out/film.mp4').exists())

    def test_T02_symlinked_workdir_output_onto_input_and_same_target(self):
        other = self.p.base / 'x' / 'elsewhere'
        other.mkdir()
        (other / 'SENTINEL2').write_text('x')
        os.symlink(other, self.p.dir / 'link')
        self.p.write(self.p.edl(work_dir='link'))
        self.ok(self.p.render())
        self.assertEqual(sorted(x.name for x in other.iterdir()), ['SENTINEL2'])
        # output onto an input, directly and through a symlinked folder
        os.symlink(self.p.dir / 'clips', self.p.dir / 'alias')
        for out in ('clips/a.mp4', 'alias/b.mp4', 'edl.json'):
            self.p.write(self.p.edl(out=out), 'bad.json')
            before = tree_hashes(self.p.dir / 'clips')
            r = self.p.render('bad.json')
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('out', r.stderr)
            self.assertEqual(before, tree_hashes(self.p.dir / 'clips'))
        # same final target: the second run is refused while the first holds the lock
        self.p.write(self.p.edl(out='out/same.mp4'))
        a = subprocess.Popen([PY, SCRIPTS / 'render_edl.py', 'edl.json', '--root', '.'], cwd=self.p.dir,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env={**os.environ, 'MV_REPLICATE_FONT': FONT or ''})
        lock = self.p.dir / 'out/.same.mp4.lock'
        for _ in range(200):
            if lock.exists():
                break
            time.sleep(0.05)
        time.sleep(0.3)
        b = self.p.render()
        sa = a.communicate(timeout=300)
        self.assertEqual(a.returncode, 0, sa[1][-800:])
        self.assertNotEqual(b.returncode, 0)
        self.assertTrue('another run' in b.stderr or 'already exists' in b.stderr, b.stderr)
        good = hashlib.sha256((self.p.dir / 'out/same.mp4').read_bytes()).hexdigest()
        # a failing replacement leaves the published film untouched
        self.p.write(self.p.edl(out='out/same.mp4', grade='no_such_filter'))
        r = self.p.render('edl.json', '--replace')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(good, hashlib.sha256((self.p.dir / 'out/same.mp4').read_bytes()).hexdigest())
        self.assertFalse(list((self.p.dir / 'out').glob('.*candidate*')))

    def test_T03_invalid_edls_fail_before_work(self):
        segs_gap = [{'at': 0, 'dur': 1.0, 'src': 'clips/a.mp4', 'from': 0}, {'at': 1.2, 'dur': 0.8, 'src': 'clips/a.mp4', 'from': 0}]
        segs_rev = [{'at': 1.0, 'dur': 1.0, 'src': 'clips/a.mp4', 'from': 0}, {'at': 0, 'dur': 1.0, 'src': 'clips/a.mp4', 'from': 0}]
        segs_neg = [{'at': 0, 'dur': 2.5, 'src': 'clips/a.mp4', 'from': 0}, {'at': 2.5, 'dur': -0.5, 'src': 'clips/a.mp4', 'from': 0}]
        cases = {'gap': (self.p.edl(segs_gap), None, 'segments[1].at'),
                 'reversed': (self.p.edl(segs_rev), None, 'segments[0].at'),
                 'negative': (self.p.edl(segs_neg), None, 'segments[1].dur'),
                 'nan': (None, json.dumps(self.p.edl()).replace('"duration": 2.0', '"duration": NaN'), 'non-finite'),
                 'short_source': (self.p.edl([{'at': 0, 'dur': 2.0, 'src': 'clips/a.mp4', 'from': 5.0}]), None, 'clip ends')}
        for name, (edl, raw, needle) in cases.items():
            with self.subTest(name):
                self.p.write(edl, 'bad.json', raw)
                v = sh([PY, SCRIPTS / 'validate_edl.py', 'bad.json', '--root', '.'], cwd=self.p.dir)
                self.assertEqual(v.returncode, 1, v.stdout)
                self.assertIn(needle, v.stdout + v.stderr)
                r = self.p.render('bad.json')
                self.assertEqual(r.returncode, 2)
                self.assertIn(needle, r.stderr)
                self.assertFalse((self.p.dir / 'tmp').exists(), 'work folder created before validation')

    def test_T04_parallel_targets_do_not_interfere(self):
        self.p.write(self.p.edl(out='out/good.mp4'), 'good.json')
        self.p.write(self.p.edl(out='out/bad.mp4', grade='no_such_filter'), 'bad.json')
        env = {**os.environ, 'MV_REPLICATE_FONT': FONT or ''}
        procs = [subprocess.Popen([PY, SCRIPTS / 'render_edl.py', n, '--root', '.'], cwd=self.p.dir, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in ('good.json', 'bad.json')]
        outs = [p.communicate(timeout=300) for p in procs]
        self.assertEqual(procs[0].returncode, 0, outs[0][1][-800:])
        self.assertNotEqual(procs[1].returncode, 0)
        self.assertTrue((self.p.dir / 'out/good.mp4').exists())
        self.assertFalse((self.p.dir / 'out/bad.mp4').exists())
        self.assertEqual(json.loads((self.p.dir / 'out/good.qa.json').read_text())['status'], 'pass')
        self.assertEqual(list((self.p.dir / 'tmp/work').iterdir()), [])

    def test_T25_fractional_segments_total_frames_and_sync(self):
        self.p.fps = '30000/1001'
        durs = [0.37, 0.41, 0.333, 0.5, 0.389]
        segs, t = [], 0.0
        for i, d in enumerate(durs):
            segs.append({'at': round(t, 6), 'dur': d, 'src': 'clips/a.mp4', 'from': 0.1 * i})
            t = round(t + d, 6)
        self.p.write(self.p.edl(segs, duration=t, loudness={'I': -14, 'TP': -1}))
        self.ok(self.p.render())
        rep = json.loads((self.p.dir / 'out/film.qa.json').read_text())
        self.assertEqual(rep['status'], 'pass', json.dumps(rep['checks'], indent=1))
        self.assertEqual(rep['checks']['video_decode']['frames'], M.Q(t, M.frac('30000/1001')))
        self.assertEqual(sum(M.seg_frame_count(s, M.frac('30000/1001')) for s in segs), M.Q(t, M.frac('30000/1001')))
        self.assertEqual(rep['checks']['sync']['lag_samples'], 0)


# ============================================================== phase 2: frames, AVOID, lyric timing

def sheet_indices(rep):
    """Read back the source index painted in each cell of the sheets (not the label)."""
    from PIL import Image
    pages = {}
    out = []
    for c in rep['cells']:
        name = rep['sheets'][c['page'] - 1]
        im = pages.setdefault(name, Image.open(name).convert('RGB'))
        x, y, w, h = c['box']
        out.append((c['frame'], F.index_of(im.getpixel((x + w // 2, y + h // 2)))))
    return out


@media
class Phase2Frames(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix='mvr-fs-'))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def sheet(self, video, *args, ok=True):
        r = sh([PY, SCRIPTS / 'frame_sheet.py', video, '--out', self.d / 's.jpg', '--json', self.d / 's.json',
                '--cell', '48', '--cols', '4', *args])
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads((self.d / 's.json').read_text())
        return r

    def test_T05_exact_frames_60_and_ntsc(self):
        for rate, n in (('60', 120), ('30000/1001', 90)):
            with self.subTest(rate):
                v = F.numbered_video(self.d / 'v.mp4', n, fps=rate, codec=('-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p', '-g', '5', '-bf', '2'))
                rep = self.sheet(v, '--frames', '1,4,7')
                self.assertEqual(sheet_indices(rep), [(1, 1), (4, 4), (7, 7)])
                tb, pts, _ = M.frame_table(v)
                self.assertEqual([c['pts'] for c in rep['cells']], [pts[1], pts[4], pts[7]])
                times = ','.join(repr(float(M.Fraction(pts[i]) * tb)) for i in (1, 4, 7))
                rep = self.sheet(v, '--at', times)
                self.assertEqual(sheet_indices(rep), [(1, 1), (4, 4), (7, 7)])

    def test_T06_vfr_by_real_pts(self):
        v = F.vfr_video(self.d / 'vfr.mp4', [0, 0.5, 1.0, 1.4, 2.0])
        rep = self.sheet(v, '--at', '1.5')
        self.assertEqual(sheet_indices(rep), [(3, 3)])
        self.assertAlmostEqual(rep['cells'][0]['t'], 1.4, places=3)
        rep = self.sheet(v, '--every', '0.5')
        self.assertEqual([f for f, _ in sheet_indices(rep)], [0, 1, 2, 3, 4])
        self.assertEqual([f for f, g in sheet_indices(rep)], [g for f, g in sheet_indices(rep)])

    def test_T07_dense_ends_duplicates_last(self):
        v = F.numbered_video(self.d / 'v.mp4', 60, fps='10')
        rep = self.sheet(v, '--dense', '1.0', '--span', '0.25', '--step', '2')
        self.assertEqual(sheet_indices(rep), [(8, 8), (10, 10), (12, 12)])
        rep = self.sheet(v, '--ends')
        self.assertEqual(sheet_indices(rep), [(0, 0), (29, 29), (59, 59)])
        rep = self.sheet(v, '--at', '0.0,0.05,5.95')
        self.assertEqual((rep['requests'], rep['unique_frames'], rep['duplicates'], len(rep['failed'])), (3, 2, 1, 0))
        self.assertEqual(sheet_indices(rep), [(0, 0), (59, 59)])

    def test_T08_bad_crop_bad_file_out_of_range(self):
        v = F.numbered_video(self.d / 'v.mp4', 20, fps='10')
        r = self.sheet(v, '--ends', '--crop', '50,0,40,40', ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('--crop', r.stderr)
        bad = self.d / 'broken.mp4'
        bad.write_bytes(os.urandom(4096))
        r = self.sheet(bad, '--ends', ok=False)
        self.assertNotEqual(r.returncode, 0)
        r = self.sheet(v, '--at', '1.0,9.5', ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('9.5', r.stderr)
        self.assertFalse((self.d / 's.jpg').exists())

    def test_T09_scaled_in_ffmpeg_bounded_pages(self):
        v = F.numbered_video(self.d / 'v.mp4', 60, fps='10', w=320, h=180)
        r = sh([PY, SCRIPTS / 'frame_sheet.py', v, '--out', self.d / 'p.jpg', '--json', self.d / 'p.json',
                '--every', '0.1', '--cell', '32', '--max', '8', '--workers', '2'])
        self.assertEqual(r.returncode, 0, r.stderr)
        rep = json.loads((self.d / 'p.json').read_text())
        self.assertEqual(rep['unique_frames'], 60)
        self.assertEqual(len(rep['sheets']), 8)
        self.assertEqual(rep['cell'], [32, 18])
        self.assertLessEqual(rep['peak_frames_in_memory'], 16)
        self.assertEqual([f for f, g in sheet_indices(rep)], [g for f, g in sheet_indices(rep)])


@media
class Phase2Avoid(Base):
    def validate(self, edl):
        self.p.write(edl, 'v.json')
        return sh([PY, SCRIPTS / 'validate_edl.py', 'v.json', '--root', '.'], cwd=self.p.dir)

    def test_T10_aliases_and_wipe_input(self):
        os.symlink(self.p.dir / 'clips/b.mp4', self.p.dir / 'bl.mp4')
        avoid = {'clips/b.mp4': [[1.0, 2.0]]}
        cases = {
            'dotdot': ([{'at': 0, 'dur': 2.0, 'src': 'clips/../clips/b.mp4', 'from': 1.5}], 1),
            'symlink': ([{'at': 0, 'dur': 2.0, 'src': 'bl.mp4', 'from': 1.5}], 1),
            'wipe_second_input': ([{'at': 0, 'dur': 2.0, 'src': 'clips/a.mp4', 'from': 0,
                                    'wipe_in': {'src': 'bl.mp4', 'from': 1.2, 'dur': 0.5}}], 1),
            'touching_after': ([{'at': 0, 'dur': 2.0, 'src': 'clips/b.mp4', 'from': 2.0}], 0),
            'touching_before': ([{'at': 0, 'dur': 1.0, 'src': 'clips/b.mp4', 'from': 0.0},
                                 {'at': 1.0, 'dur': 1.0, 'src': 'clips/a.mp4', 'from': 0}], 0),
            'unaligned_boundary': ([{'at': 0, 'dur': 1.0, 'src': 'clips/b.mp4', 'from': 0.0},
                                    {'at': 1.0, 'dur': 1.0, 'src': 'clips/a.mp4', 'from': 0}], 1, {'clips/b.mp4': [[0.95, 2.0]]}),
        }
        for name, case in cases.items():
            with self.subTest(name):
                segs, want = case[0], case[1]
                r = self.validate(self.p.edl(segs, avoid=case[2] if len(case) > 2 else avoid))
                self.assertEqual(r.returncode, want, r.stdout)
                if want:
                    self.assertIn('avoided range', r.stdout)
                    if name == 'wipe_second_input':
                        self.assertIn('wipe_in', r.stdout)
        r = self.validate(self.p.edl())
        self.assertIn('NOT checked', r.stdout)

    def test_T11_one_forbidden_frame_is_caught(self):
        F.numbered_video(self.p.dir / 'clips/c60.mp4', 120, fps='60')
        self.p.fps = 60
        segs = [{'at': 0, 'dur': 1.0, 'src': 'clips/c60.mp4', 'from': 0.0},
                {'at': 1.0, 'dur': 1.0, 'src': 'clips/a.mp4', 'from': 0.0}]
        r = self.validate(self.p.edl(segs, avoid={'clips/c60.mp4': [[0.98, 1.5]]}))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('source frame 58', r.stdout)
        r = self.validate(self.p.edl(segs, avoid={'clips/c60.mp4': [[1.0, 1.5]]}))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_T12_freeze_checks_the_held_frame(self):
        segs = [{'at': 0, 'dur': 2.0, 'src': 'clips/a.mp4', 'from': 0.0, 'freeze': True}]
        self.assertEqual(self.validate(self.p.edl(segs, avoid={'clips/a.mp4': [[1.0, 1.5]]})).returncode, 0)
        r = self.validate(self.p.edl(segs, avoid={'clips/a.mp4': [[0.0, 0.05]]}))
        self.assertEqual(r.returncode, 1)
        self.assertIn('source frame 0', r.stdout)

    def test_T06_render_selection_matches_model_on_vfr_and_ntsc(self):
        clips = {'vfr': F.vfr_video(self.p.dir / 'vfr.mp4', [0, 0.5, 1.0, 1.4, 2.0, 2.04, 2.1, 2.5]),
                 'ntsc': F.numbered_video(self.p.dir / 'ntsc.mp4', 60, fps='30000/1001',
                                          codec=('-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv444p', '-g', '5', '-bf', '2'))}
        for name, path in clips.items():
            tb, pts, ends = M.frame_table(path)
            for start in (0, 0.3333, 1 / 60):
                for fps, speed in (('60', 1), ('24', 0.9), ('30000/1001', 1.8)):
                    for rev, frz in ((False, False), (True, False), (False, True)):
                        with self.subTest(name=name, start=start, fps=fps, speed=speed, rev=rev, frz=frz):
                            n = 10
                            exp, why = M.select_frames(tb, pts, ends, start, 1 if frz else n, fps, speed)
                            self.assertIsNone(why)
                            exp = (exp * n if frz else exp)[::-1] if rev else (exp * n if frz else exp)
                            vf = ','.join(M.select_chain(path, start, n, fps, speed, frz, rev))
                            out = self.p.dir / 'o.mkv'
                            r = sh(['ffmpeg', '-v', 'error', '-y', *M.source_args(path, start), '-vf', vf,
                                    '-frames:v', n, '-c:v', 'ffv1', out])
                            self.assertEqual(r.returncode, 0, r.stderr)
                            self.assertEqual(F.decode_indices(out), exp)

    def test_T13_reverse_is_exact(self):
        segs = [{'at': 0, 'dur': 1.0, 'src': 'clips/a.mp4', 'from': 1.0, 'reverse': True},
                {'at': 1.0, 'dur': 1.0, 'src': 'clips/b.mp4', 'from': 0.0}]
        self.p.write(self.p.edl(segs))
        self.ok(self.p.render())
        got = F.decode_indices(self.p.dir / 'out/film.mp4')
        self.assertEqual(got[:10], list(range(19, 9, -1)))
        self.assertEqual(got[10:], list(range(60, 70)))

    def test_T14_freeze_frames_and_layout(self):
        segs = [{'at': 0, 'dur': 1.0, 'src': 'clips/a.mp4', 'from': 2.0, 'freeze': True},
                {'at': 1.0, 'dur': 1.0, 'src': 'clips/b.mp4', 'from': 0.55, 'freeze': True,
                 'layout': {'frame': 'vertical', 'pos': 'center', 'slide': False}}]
        self.p.write(self.p.edl(segs))
        self.ok(self.p.render())
        got = F.decode_indices(self.p.dir / 'out/film.mp4')
        self.assertEqual(got, [20] * 10 + [65] * 10)


class Phase2Lyrics(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix='mvr-ly-'))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_lc(self, lines, tokens, *extra, duration=20, raw_whisper=None):
        (self.d / 'lyrics.json').write_text(json.dumps({'lines': lines}, ensure_ascii=False))
        if raw_whisper is not None:
            (self.d / 'w.json').write_text(raw_whisper)
        else:
            F.whisper_json(self.d / 'w.json', tokens)
        return sh([PY, SCRIPTS / 'lyric_chars.py', self.d / 'w.json', '--lyrics', self.d / 'lyrics.json',
                   '--duration', duration, '--out', self.d / 'out.json', *extra])

    def result(self):
        return M.load_json(self.d / 'out.json')

    def test_T15_dtw_must_be_valid(self):
        lines = [{'start': 0, 'line': '你好'}]
        r = self.run_lc(lines, [('你', None, 0.1), ('好', 0.5, 0.5)])
        self.assertEqual(r.returncode, 2)
        self.assertIn("token 0 '你'", r.stderr)
        self.assertIn('no t_dtw', r.stderr)
        r = self.run_lc(lines, [('你', -0.01, 0.1), ('好', 0.5, 0.5)])
        self.assertEqual(r.returncode, 2)
        self.assertIn('-nfa', r.stderr)
        r = self.run_lc(lines, [], raw_whisper='{"transcription":[{"tokens":[{"text":"你","t_dtw":NaN,"offsets":{"from":100}},'
                                              '{"text":"好","t_dtw":50,"offsets":{"from":500}}]}]}')
        self.assertEqual(r.returncode, 2)
        self.assertIn('not a finite', r.stderr)
        r = self.run_lc(lines, [('你', None, 0.1), ('好', None, 0.5)], '--time', 'offsets')
        self.assertEqual(r.returncode, 0, r.stderr)
        res = self.result()
        self.assertEqual(res['timing_source'], 'offsets')
        self.assertEqual([c['t'] for c in res['lines'][0]['chars']], [0.1, 0.5])

    def test_T16_repair_keeps_anchors_in_order(self):
        r = self.run_lc([{'start': 10, 'line': '甲乙丙丁'}, {'start': 14, 'line': '戊'}],
                        [('甲', 8, 8), ('乙', 9, 9), ('丙', 10.5, 10.5), ('丁', 11, 11), ('戊', 14.2, 14.2)])
        self.assertEqual(r.returncode, 0, r.stderr)
        ch = self.result()['lines'][0]['chars']
        ts = [c['t'] for c in ch]
        self.assertEqual(ts[2:], [10.5, 11.0])
        self.assertEqual(ts, sorted(ts))
        self.assertGreaterEqual(ts[0], 10.0)
        self.assertEqual([c['source'] for c in ch], ['interpolated', 'interpolated', 'asr', 'asr'])

    def test_T17_latin_word_takes_its_first_letter(self):
        r = self.run_lc([{'start': 0, 'line': 'Hello world'}], [('hel', 0.0, 0.0), ('lo', 0.3, 0.3), ('world', 1.0, 1.0)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(c['c'], c['t']) for c in self.result()['lines'][0]['chars']], [('Hello', 0.0), ('world', 1.0)])
        r = self.run_lc([{'start': 0, 'line': 'Hello world'}], [('Hello', 0.0, 0.0), ('world', 1.0, 1.0)])
        self.assertEqual([(c['c'], c['t']) for c in self.result()['lines'][0]['chars']], [('Hello', 0.0), ('world', 1.0)])

    def test_T18_edge_lines_conflicts_overrides(self):
        lines = [{'start': 0, 'line': '你好'}, {'start': 2, 'line': ''}, {'start': 3, 'line': '！！'},
                 {'start': 4, 'line': '啊啊啊'}, {'start': 6, 'line': '再见'}]
        toks = [('你', 0.1, 0.1), ('好', 0.6, 0.6), ('再', 6.2, 6.2), ('见', 6.7, 6.7)]
        r = self.run_lc(lines, toks, duration=7.00049)
        self.assertEqual(r.returncode, 0, r.stderr)
        res = self.result()
        self.assertEqual(res['lines'][1]['chars'], [])
        self.assertEqual(res['lines'][2]['skipped'], 'no displayable characters')
        mid = res['lines'][3]['chars']
        self.assertEqual({c['source'] for c in mid}, {'interpolated'})
        allt = [c['t'] for l in res['lines'] for c in l['chars']]
        self.assertEqual(allt, sorted(allt))
        self.assertEqual(res['lines'][-1]['end'], 7.0)
        r = self.run_lc(lines, toks, '--empty-lines', 'error', duration=7.0)
        self.assertEqual(r.returncode, 2)
        # conflicting anchors: stop by default, drop the fewest on request
        bad = [('你', 0.9, 0.9), ('好', 0.2, 0.2), ('再', 6.2, 6.2), ('见', 6.7, 6.7)]
        r = self.run_lc(lines[:1] + lines[4:], bad, duration=7)
        self.assertEqual(r.returncode, 2)
        self.assertIn('out of order', r.stderr)
        r = self.run_lc(lines[:1] + lines[4:], bad, '--drop-conflicting-anchors', duration=7)
        self.assertEqual(r.returncode, 0, r.stderr)
        t0 = [c['t'] for c in self.result()['lines'][0]['chars']]
        self.assertEqual(t0, sorted(t0))
        # overrides: applied, kept on rerun, stale when the lyrics change
        r = self.run_lc(lines, toks, '--override-template', self.d / 'ov.json', duration=7)
        ov = json.loads((self.d / 'ov.json').read_text())
        ov['items'] = [{'line': 3, 'unit': 1, 'c': '啊', 't': 4.9}]
        (self.d / 'ov.json').write_text(json.dumps(ov, ensure_ascii=False))
        for _ in range(2):
            r = self.run_lc(lines, toks, '--overrides', self.d / 'ov.json', duration=7)
            self.assertEqual(r.returncode, 0, r.stderr)
            c = self.result()['lines'][3]['chars']
            self.assertEqual((c[1]['t'], c[1]['source']), (4.9, 'override'))
            self.assertEqual([x['t'] for x in c], sorted(x['t'] for x in c))
        lines[3]['line'] = '啊啊哦'
        r = self.run_lc(lines, toks, '--overrides', self.d / 'ov.json', duration=7)
        self.assertEqual(r.returncode, 2)
        self.assertIn('stale', r.stderr)


# ============================================================== phase 3: effects, analysis, audio

class Phase3Yield(unittest.TestCase):
    def data(self):
        return {'lines': [
            {'line': '爱你', 'start': 0.0, 'end': 5.0, 'first_char_t': 0.0,
             'chars': [{'c': '爱', 't': 0.0}, {'c': '你', 't': 1.0}]},
            {'line': '爱你', 'start': 10.0, 'end': 12.0, 'first_char_t': 10.0,
             'chars': [{'c': '爱', 't': 10.0}, {'c': '你', 't': 11.0}]}]}

    def test_T19_yield_only_the_sung_occurrence(self):
        import lyric_layer as LL
        d = LL.yield_to_title(self.data(), '你', 11.0)
        self.assertEqual(d['lines'][0], self.data()['lines'][0])
        l1 = d['lines'][1]
        self.assertEqual((l1['line'], [c['c'] for c in l1['chars']], l1['end'], l1['first_char_t']), ('爱', ['爱'], 11.0, 10.0))
        with self.assertRaises(ValueError) as cm:
            LL.yield_to_title(self.data(), '爱你', 5.0)
        self.assertIn('ambiguous', str(cm.exception))
        d = LL.yield_to_title(self.data(), '爱你', 5.0, index=1)
        self.assertEqual(d['lines'][1]['chars'], [])
        self.assertIsNone(d['lines'][1]['first_char_t'])
        if HAVE_FFMPEG and FONT:
            tmp = Path(tempfile.mkdtemp(prefix='mvr-yl-'))
            try:
                (tmp / 'lc.json').write_text(json.dumps(self.data(), ensure_ascii=False))
                for spec in ('你@11', '爱你@10'):
                    r = sh([PY, SCRIPTS / 'lyric_layer.py', tmp / 'lc.json', '--yield', spec, '--duration', 12,
                            '--size', '128x72', '--fps', 10, '--out', tmp / 'l.mov'])
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(M.Q(12, 10), int(sh(['ffprobe', '-v', 'error', '-count_frames', '-select_streams', 'v:0',
                                                         '-show_entries', 'stream=nb_read_frames', '-of', 'csv=p=0',
                                                         tmp / 'l.mov']).stdout))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


@media
class Phase3Analysis(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix='mvr-an-'))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def analyze(self, video, name):
        r = sh([PY, SCRIPTS / 'analyze_ref.py', video, '--out', self.d / name, '--no-sheet'])
        self.assertEqual(r.returncode, 0, r.stderr)
        return M.load_json(self.d / name / 'ref-analysis.json')

    def test_T21_8bit_and_10bit_agree(self):
        levels = ([0] * 5 + [128] * 5 + [255] * 3 + [128] * 5 + [0] * 5 + [128] * 7) * 2
        a = self.analyze(F.gray_sequence_video(self.d / 'g8.mp4', levels), 'a8')
        b = self.analyze(F.gray_sequence_video(self.d / 'g10.mkv', levels, ten_bit=True), 'a10')
        self.assertEqual(b['info']['format']['pix_fmt'], 'yuv420p10le')
        self.assertTrue(a['flashes'] and a['black_dips'])
        self.assertEqual(a['flashes'], b['flashes'])
        self.assertEqual(a['black_dips'], b['black_dips'])
        for wa, wb in zip(a['windows'], b['windows']):
            self.assertLess(abs(wa['luma'] - wb['luma']), 2.0)

    def test_T22_no_audio_no_beats_manual_phase(self):
        res = self.analyze(F.numbered_video(self.d / 'silent.mp4', 30, fps='10'), 'r')
        self.assertEqual(res['audio']['status'], 'unavailable')
        F.write_wav(self.d / 'quiet.wav', [0.0] * 48000 * 3)
        r = sh([PY, SCRIPTS / 'analyze_song.py', self.d / 'quiet.wav', '--out', self.d / 's'])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('--beats', r.stderr)
        (self.d / 'beats.json').write_text(json.dumps([0.5, 1.0, 1.5, 2.0, 2.5]))
        r = sh([PY, SCRIPTS / 'analyze_song.py', self.d / 'quiet.wav', '--out', self.d / 's', '--beats', self.d / 'beats.json',
                '--beats-per-bar', 3, '--bar-phase', 1])
        self.assertEqual(r.returncode, 0, r.stderr)
        s = M.load_json(self.d / 's/song-analysis.json')
        self.assertEqual((s['beats_per_bar'], s['bar_phase'], s['bar_phase_method'], s['beat_source']), (3, 1, 'manual', 'manual'))
        self.assertEqual([b['bar_beat'] for b in s['beats']], [3, 1, 2, 3, 1])
        self.assertEqual(s['loudness']['status'], 'unmeasurable')
        F.write_wav(self.d / 'music.wav', F.music_like(6))
        r = sh([PY, SCRIPTS / 'analyze_song.py', self.d / 'music.wav', '--out', self.d / 'm'])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(M.load_json(self.d / 'm/song-analysis.json')['bar_phase_method'], 'onset_strength')


@media
class Phase3Audio(Base):
    def test_T23_pcm_keeps_its_timing(self):
        import render_edl as R
        R.ROOT = self.p.dir
        edl = self.p.edl(duration=3.0, loudness={'I': -14, 'TP': -1})
        work = self.p.base / 'w'
        work.mkdir()
        src = M.decode_audio(self.p.dir / 'song/song.wav', 48000)
        wav, _ = R.render_audio(edl, work, 6.0, -1.3, 0)
        lag = M.lag_samples(src, M.decode_audio(wav, 48000), 960)
        self.assertLessEqual(abs(lag), 1, f'PCM lag {lag} samples')
        # the old chain (alimiter without latency compensation) is measurably late
        old = work / 'old.wav'
        sh(['ffmpeg', '-v', 'error', '-y', '-i', self.p.dir / 'song/song.wav', '-af',
            'atrim=0:3,volume=6dB,alimiter=limit=0.84:attack=5:release=50:level=false', old])
        self.assertGreater(M.lag_samples(src, M.decode_audio(old, 48000), 960), 100)

    def test_T24_loudness_on_final_aac_or_clear_failure(self):
        F.write_wav(self.p.dir / 'song/peaky.wav', F.peaky(3.0))
        F.write_wav(self.p.dir / 'song/short.wav', F.music_like(0.3))
        F.write_wav(self.p.dir / 'song/silent.wav', [0.0] * 48000 * 2)
        L = {'I': -14, 'TP': -1}
        # normal music: passes on the decoded AAC
        self.p.write(self.p.edl(loudness=L, out='out/normal.mp4'))
        self.ok(self.p.render())
        rep = M.load_json(self.p.dir / 'out/normal.qa.json')
        self.assertEqual(rep['checks']['loudness']['status'], 'pass')
        self.assertLessEqual(abs(rep['measurements']['final_loudness']['I'] + 14), 0.5)
        self.assertLessEqual(rep['measurements']['final_loudness']['TP'], -0.9)
        # high peak-to-average: either meets the target or fails with measurements
        self.p.write(self.p.edl(song='song/peaky.wav', loudness=L, out='out/peaky.mp4'))
        r = self.p.render()
        if r.returncode == 0:
            self.assertEqual(M.load_json(self.p.dir / 'out/peaky.qa.json')['status'], 'pass')
        else:
            fails = list((self.p.dir / 'out').glob('peaky.failed-*.qa.json'))
            self.assertTrue(fails)
            self.assertIsNotNone(M.load_json(fails[0])['measurements']['final_loudness']['TP'])
            self.assertFalse((self.p.dir / 'out/peaky.mp4').exists())
        # silence and too-short audio: refused with a reason, never a NaN gain
        for song, dur in (('song/silent.wav', 2.0), ('song/short.wav', 0.3)):
            segs = [{'at': 0, 'dur': dur, 'src': 'clips/a.mp4', 'from': 0}]
            self.p.write(self.p.edl(segs, song=song, duration=dur, loudness=L, out=f'out/{Path(song).stem}.mp4'))
            r = self.p.render()
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn('loudness', r.stderr)
            self.assertNotIn('nan', r.stderr.lower())
            r = self.p.render('edl.json', '--allow-loudness-miss')
            self.assertEqual(r.returncode, 0, r.stderr)
            rep = M.load_json(self.p.dir / f'out/{Path(song).stem}.qa.json')
            self.assertTrue(rep['checks']['loudness'].get('deviation_accepted_by_user') or rep['warnings'])


# ============================================================== phase 4: generator, versions, smoke

class GenProject(Project):
    """Project with a clip C, a hand-made beat grid every 0.5 s and lyric unit times."""

    def __init__(self, song_s=6.0, chars=(0.2, 0.4, 0.6)):
        super().__init__(song_s=song_s)
        F.numbered_video(self.dir / 'clips/c.mp4', 60, fps='10', start=120)
        F.write_wav(self.dir / 'song/song.wav', F.music_like(song_s))
        beats = [round(0.5 * i, 3) for i in range(int(song_s / 0.5))]
        (self.dir / 'beats.json').write_text(json.dumps(beats))
        r = sh([PY, SCRIPTS / 'analyze_song.py', self.dir / 'song/song.wav', '--out', self.dir / 'song',
                '--beats', self.dir / 'beats.json'])
        assert r.returncode == 0, r.stderr
        units = ['甲', '乙', '丙']
        (self.dir / 'song/lyric-chars.json').write_text(json.dumps({'lines': [
            {'line': '甲乙丙', 'start': 0.1, 'end': 3.0, 'first_char_t': chars[0],
             'chars': [{'c': u, 't': t, 'source': 'asr'} for u, t in zip(units, chars)]}]}, ensure_ascii=False))
        shutil.copy(REPO / 'examples/make_edl.py', self.dir / 'make_edl_v1.py')

    def project(self, plan, **kw):
        cfg = {'song': 'song/song.wav', 'clips': {'A': 'clips/a.mp4', 'B': 'clips/b.mp4', 'C': 'clips/c.mp4'},
               'avoid': {'B': [[5.0, 5.5]]}, 'grade': 'eq=saturation=1.1', 'plan': plan,
               'title': {'line': 0, 'units': [0, 1, 2], 'pos': [[0.3, 0.3, -8], [0.5, 0.25, 6], [0.7, 0.31, -5]],
                         'style': {'size': 0.2, 'fill': '#FFFFFF', 'stroke': '#000000'}}}
        cfg.update(kw)
        (self.dir / 'project.json').write_text(json.dumps(cfg, ensure_ascii=False))

    def gen(self, version, *extra):
        return sh([PY, self.dir / 'make_edl_v1.py', '--work', self.dir, '--version', version, '--project',
                   self.dir / 'project.json', '--size', '128x72', '--fps', '10', *extra],
                  cwd=self.p_cwd if hasattr(self, 'p_cwd') else self.base, env={'MV_REPLICATE_DIR': str(REPO)})


PLAN_OK = [[None, 'A', 0.0, {}], [2, 'B', 0.0, {}], [6, 'C', 0.0, {'flash_in': 0.1}]]


@media
class Phase4(unittest.TestCase):
    def tearDown(self):
        self.g.cleanup()

    def test_T20_title_leaves_before_the_real_cut(self):
        self.g = GenProject()
        self.g.project(PLAN_OK)
        r = self.g.gen('v1')
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        edl = M.load_json(self.g.dir / 'versions/v1/edl.json')
        card = next(o for o in edl['overlays'] if o['type'] == 'text_pop')
        self.assertLessEqual(card['out'] + card['out_dur'], 1.0)     # the real cut, not the beat after 0.6 + 0.6
        film = F.decode_indices(self.g.dir / 'versions/v1/mv-72p.mp4')
        self.assertEqual(film[10], 60)                                 # first frame after the cut: clean clip B
        g2 = GenProject(chars=(0.2, 0.4, 0.9))
        try:
            g2.project(PLAN_OK)
            r = g2.gen('v1')
            self.assertEqual(r.returncode, 2)
            self.assertIn('next cut is at 1.000s', r.stderr)
            self.assertFalse((g2.dir / 'versions/v1/edl.json').exists())
        finally:
            g2.cleanup()

    def test_T03_generator_no_render_refuses_a_bad_plan(self):
        self.g = GenProject()
        self.g.project([[None, 'A', 0.0, {}], [6, 'B', 0.0, {}], [2, 'C', 0.0, {}]])
        r = self.g.gen('v1', '--no-render')
        self.assertEqual(r.returncode, 2)
        self.assertIn('segments[2]', r.stderr)
        self.assertFalse((self.g.dir / 'versions/v1/edl.json').exists())
        self.g.project([[None, 'B', 4.0, {}], [4, 'C', 0.0, {}]])            # B 4.0..6.0 shows 5.0-5.5 (AVOID)
        r = self.g.gen('v1', '--no-render')
        self.assertEqual(r.returncode, 2)
        self.assertIn('avoided range', r.stderr)

    def test_T26_versions_are_isolated(self):
        self.g = GenProject()
        self.g.project(PLAN_OK)
        self.assertEqual(self.g.gen('v1').returncode, 0)
        v1 = tree_hashes(self.g.dir / 'versions/v1')
        self.assertEqual(M.load_json(self.g.dir / 'versions/v1/manifest.json')['review']['user_approved'], None)
        latest = (self.g.dir / 'versions/latest-passing.json').read_text()
        self.g.project(PLAN_OK, grade='no_such_filter')
        r = self.g.gen('v2')
        self.assertNotEqual(r.returncode, 0)
        r = self.g.gen('v1')
        self.assertEqual(r.returncode, 2)
        self.assertIn('already has a film', r.stderr)
        self.assertEqual(v1, tree_hashes(self.g.dir / 'versions/v1'))
        self.assertEqual(latest, (self.g.dir / 'versions/latest-passing.json').read_text())
        self.assertFalse((self.g.dir / 'versions/v2/mv-72p.mp4').exists())
        self.assertEqual(list((self.g.dir / '.work').iterdir()), [])
        self.assertTrue({'edl.json', 'lyrics.mov', 'mv-72p.mp4', 'mv-72p.qa.json', 'sheet.jpg', 'manifest.json'}
                        <= set(os.listdir(self.g.dir / 'versions/v1')))

    @unittest.skipUnless(FONT, 'no CJK font found: set MV_REPLICATE_FONT')
    def test_T27_styles_layouts_transitions_smoke(self):
        self.g = GenProject()
        d = self.g.dir
        for style in ('karaoke', 'pop', 'terminal', 'bubbles', 'lightbar'):
            with self.subTest(style):
                r = sh([PY, SCRIPTS / 'lyric_layer.py', d / 'song/lyric-chars.json', '--style', style, '--duration', 2,
                        '--size', '128x72', '--fps', 10, '--out', d / f'l-{style}.mov'])
                self.assertEqual(r.returncode, 0, r.stderr)
        segs, t = [], 0.0
        specs = [('A', 0, {'zoom': [1, 1.1], 'focus': [0.3, 0.3]}), ('B', 0, {'punch': 0.3, 'shake': 0.3, 'flash_in': 0.1}),
                 ('A', 1, {'layout': {'frame': 'vertical', 'pos': 'right'}}), ('B', 1, {'layout': {'frame': 'photo'}}),
                 ('A', 2, {'rot': 5, 'duotone': ['#14366E', '#5AAEF5', '#F2FBFF']}), ('B', 2, {'whip_out': 0.2}),
                 ('A', 3, {'whip_in': 0.2}), ('B', 3, {'wipe_in': {'src': 'clips/c.mp4', 'from': 0, 'dur': 0.3}}),
                 ('A', 4, {'freeze': True}), ('B', None, {'end': 5.0, 'speed': 0.8, 'reverse': True})]
        for clip, frm, opt in specs:
            s = {'at': round(t, 3), 'dur': 0.5, 'src': f'clips/{clip.lower()}.mp4', **({'from': frm} if frm is not None else {}), **opt}
            segs.append(s)
            t += 0.5
        style = {'size': 0.3, 'fill': '#FFFFFF', 'stroke': '#1E3A8A', 'marker': '#FFE066'}
        edl = self.g.edl(segs, duration=5.0, loudness={'I': -14, 'TP': -1}, end_fade=0.5, overlays=[
            {'type': 'text_slam', 'at': 0.5, 'dur': 0.5, 'text': 'AB'},
            {'type': 'starburst', 'at': 1.0, 'dur': 0.3}, {'type': 'flash', 'at': 1.5, 'dur': 0.2},
            {'type': 'text_pop', 'at': -0.1, 'dur': 1.0, 'out': 0.7, 'exit': 'whip', 'pop_from': 1.6, 'style': style,
             'pieces': [{'text': '甲', 'at': 0.0, 'x': 0.4, 'y': 0.4}, {'text': '乙', 'at': 0.2, 'x': 0.6, 'y': 0.4}]},
            {'type': 'layer', 'src': 'l-bubbles.mov', 'at': 0, 'dur': 2}])
        self.g.write(edl)
        r = self.g.render()
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        self.assertEqual(M.load_json(d / 'out/film.qa.json')['status'], 'pass')
        self.assertIn('cut off', r.stderr)      # the negative pre-roll warning


def main():
    ids = [a for a in sys.argv[1:] if a.upper().startswith('T') and a[1:].isdigit()]
    rest = [a for a in sys.argv[1:] if a not in ids]
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in loader.loadTestsFromModule(sys.modules[__name__]):
        for t in case:
            for tt in (t if isinstance(t, unittest.TestSuite) else [t]):
                name = tt.id().split('.')[-1]
                if not ids or any(name.startswith(f'test_{i.upper()}_') for i in ids):
                    suite.addTest(tt)
    verbosity = 2 if '-v' in rest else 1
    print(f'python {sys.version.split()[0]}, ffmpeg: {M.tool_versions()["ffmpeg"] if HAVE_FFMPEG else "missing"}, font: {FONT}')
    res = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    sys.exit(0 if res.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
