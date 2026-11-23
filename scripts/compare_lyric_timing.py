#!/usr/bin/env python3
"""Compare two lyric_chars.py results (e.g. large-v3-turbo vs small) unit by unit.

Lists every unit whose times differ by more than --threshold (default 0.12 s) and every unit that is
estimated (not a direct ASR or hand-set time) in either file. Agreement between two models shows
consistency only; it does not prove the times match the real singing - check flagged units by ear.

  python compare_lyric_timing.py turbo.json small.json [--threshold 0.12] [--json report.json]
Exit code 0 when nothing is over the threshold, 1 otherwise, 2 when the files do not match up.
"""
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvr_common as M   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('a')
    ap.add_argument('b')
    ap.add_argument('--threshold', type=float, default=0.12)
    ap.add_argument('--json')
    x = ap.parse_args()
    A, B = M.load_json(x.a)['lines'], M.load_json(x.b)['lines']
    if [l['line'] for l in A] != [l['line'] for l in B]:
        M.die('[compare] the two files are for different lyrics (line texts differ)')
    over, estimated = [], []
    for i, (la, lb) in enumerate(zip(A, B)):
        if [c['c'] for c in la['chars']] != [c['c'] for c in lb['chars']]:
            M.die(f'[compare] line {i}: units differ between the files')
        for k, (ca, cb) in enumerate(zip(la['chars'], lb['chars'])):
            d = round(cb['t'] - ca['t'], 3)
            item = {'line': i, 'unit': k, 'c': ca['c'], 'a': ca['t'], 'b': cb['t'], 'diff': d,
                    'source_a': ca.get('source'), 'source_b': cb.get('source')}
            if abs(d) > x.threshold:
                over.append(item)
            if ca.get('guessed') or cb.get('guessed'):
                estimated.append(item)
    rep = {'a': x.a, 'b': x.b, 'threshold': x.threshold, 'over_threshold': over, 'estimated': estimated,
           'note': 'consistency between two transcriptions only; not proof of the true sung times'}
    if x.json:
        M.atomic_write_json(x.json, rep)
    for it in over:
        print(f"over   line {it['line']:3d} {it['c']}: {it['a']:.3f} vs {it['b']:.3f} ({it['diff']:+.3f})")
    for it in estimated:
        print(f"guess  line {it['line']:3d} {it['c']}: {it['a']:.3f} ({it['source_a']}) vs {it['b']:.3f} ({it['source_b']})")
    print(f'{len(over)} over {x.threshold}s, {len(estimated)} estimated')
    sys.exit(1 if over else 0)


if __name__ == '__main__':
    main()
