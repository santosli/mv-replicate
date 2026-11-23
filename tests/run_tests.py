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
