#!/usr/bin/env python3
"""Per-character lyric timing: put whisper's token times onto the correct lyric text.

whisper hears the song through the music and gets many characters wrong, but its DTW token times
(`t_dtw`, centiseconds; needs `-dtw <model preset>` and `-nfa`: flash attention silently disables DTW
and leaves t_dtw = -1) are good. Do NOT use the token `offsets` (milliseconds): on a real song they
put the first sung syllable 0.8 s early and drifted by a syllable inside lines.

Method (references/conventions.md):
  1. Every whisper text token must carry a finite t_dtw >= 0 (default) - otherwise this stops and names
     the token. `--time offsets` uses offsets.from instead and says so in `timing_source`.
  2. Tokens are split into characters; a multi-character token spreads its characters up to the next
     token (source "split"). The lyric text is aligned to those characters (Needleman-Wunsch, same
     character +2, different 0, gap -1). A display unit (one CJK character, or a whole Latin word or
     number) takes the time of its first character.
  3. Per line, a time outside [line start - onset tolerance, next line start) is dropped. Remaining
     ASR times must be non-decreasing; a conflict stops the run (or, with --drop-conflicting-anchors,
     the fewest anchors are dropped). Gaps are filled linearly between the neighbouring reliable times
     or the line boundaries (source "interpolated"). Times are never sorted and re-dealt.
  4. --spread TEXT spreads a whole line evenly (hummed lines whisper cannot hear; source "spread").
  5. --overrides FILE applies hand-set times (source "override"). The file records the lyric file's
     sha256; a changed lyric file makes it stale and the run stops. Re-running keeps the overrides.
Empty or punctuation-only lines are kept with no characters and "skipped" (or --empty-lines error).
Every character records its `source`; `guessed` is true for anything that is not a direct ASR or
hand-set time. Matching whisper is evidence of consistency, not proof of the true sung time.

  whisper-cli -m ggml-large-v3-turbo-q5_0.bin -dtw large.v3.turbo -nfa -l zh -ojf -of <out>/whisper -f song16k.wav
  python3 lyric_chars.py <out>/whisper.json --lyrics lyrics.json --duration 68 --out lyric-chars.json
  python3 lyric_chars.py ... --override-template overrides.json     # start a hand-correction file
"""
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mvr_common as M   # noqa: E402

PUNCT = re.compile(r'[\s，。、！？!?,.“”"‘’…～~—\-：:；;（）()「」『』《》·]')


def units_of(line):
    """Displayable units: CJK characters one by one, Latin words / numbers kept whole."""
    return re.findall(r'[A-Za-z0-9]+|[^\sA-Za-z0-9]', PUNCT.sub(' ', line))


def fail(where, why):
    raise M.MvrError('lyric_chars', where, why)


def load_tokens(path, mode):
    data = json.loads(Path(path).read_text())          # whisper may write NaN; every value is checked below
    toks = []
    for si, seg in enumerate(data.get('transcription', [])):
        for ti, t in enumerate(seg.get('tokens', [])):
            text = t.get('text', '')
            if text.strip().startswith('[') or not PUNCT.sub('', text):
                continue
            where = f'whisper segment {si} token {ti} {text.strip()!r}'
            if mode == 'dtw':
                v = t.get('t_dtw')
                if v is None:
                    fail(where, 'has no t_dtw: run whisper-cli with -dtw <preset> -nfa, or pass --time offsets explicitly')
                if not M.finite(v):
                    fail(where, f't_dtw is {v!r}, not a finite number')
                if v < 0:
                    fail(where, f't_dtw is {v}: DTW was disabled (whisper-cli needs -nfa with -dtw)')
                at = v / 100
            else:
                v = (t.get('offsets') or {}).get('from')
                if not M.finite(v) or v < 0:
                    fail(where, f'offsets.from is {v!r}')
                at = v / 1000
            toks.append((at, PUNCT.sub('', text).lower()))
    if not toks:
        fail(str(path), 'no text tokens')
    chars = []
    for i, (at, txt) in enumerate(toks):
        nxt = toks[i + 1][0] if i + 1 < len(toks) else at + 0.25 * len(txt)
        step = min(0.3, max(0.0, nxt - at) / len(txt))
        for k, ch in enumerate(txt):
            chars.append((at + step * k, ch, 'asr' if k == 0 else 'split'))
    return chars


def align(a, b):
    """Needleman-Wunsch over characters; returns the matched index in b (or None) for each item of a."""
    n, m = len(a), len(b)
    S = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        S[i][0] = -i
    for j in range(1, m + 1):
        S[0][j] = -j
    for i in range(1, n + 1):
        ai, Si, Sp = a[i - 1], S[i], S[i - 1]
        for j in range(1, m + 1):
            Si[j] = max(Sp[j - 1] + (2 if ai == b[j - 1] else 0), Sp[j] - 1, Si[j - 1] - 1)
    out, i, j = [None] * n, n, m
    while i and j:
        if S[i][j] == S[i - 1][j - 1] + (2 if a[i - 1] == b[j - 1] else 0):
            out[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif S[i][j] == S[i - 1][j] - 1:
            i -= 1
        else:
            j -= 1
    return out


def best_monotone(items):
    """items: [(time, weight)]; positions of the max-weight non-decreasing subsequence."""
    n = len(items)
    if not n:
        return set()
    best, prev = [w for _, w in items], [-1] * n
    for i in range(n):
        for j in range(i):
            if items[j][0] <= items[i][0] and best[j] + items[i][1] > best[i]:
                best[i], prev[i] = best[j] + items[i][1], j
    k = max(range(n), key=lambda i: best[i])
    keep = set()
    while k >= 0:
        keep.add(k)
        k = prev[k]
    return keep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('whisper')
    ap.add_argument('--lyrics', required=True, help='JSON with "lines": [{start, line}]')
    ap.add_argument('--lyrics-key', help='read the lines from this top-level key instead')
    ap.add_argument('--duration', type=float, required=True, help='song length in seconds (end of the last line)')
    ap.add_argument('--out', required=True)
    ap.add_argument('--time', choices=['dtw', 'offsets'], default='dtw')
    ap.add_argument('--onset-tolerance', type=float, default=0.6,
                    help='seconds a sung character may start before its line start (recorded in the output)')
    ap.add_argument('--spread', action='append', default=[],
                    help='line containing this text gets its characters spread evenly over the line')
    ap.add_argument('--overrides', help='hand-set times (see --override-template)')
    ap.add_argument('--override-template', help='write an overrides file for these lyrics and exit')
    ap.add_argument('--drop-conflicting-anchors', action='store_true',
                    help='drop the fewest ASR times needed to keep a line in order, instead of stopping')
    ap.add_argument('--empty-lines', choices=['skip', 'error'], default='skip')
    a = ap.parse_args()
    try:
        run(a)
    except M.MvrError as e:
        M.die(e)
    except (ValueError, OSError, KeyError, TypeError) as e:
        M.die(f'[lyric_chars] {e}')


def run(a):
    if not (M.finite(a.duration) and a.duration > 0) or not (M.finite(a.onset_tolerance) and a.onset_tolerance >= 0):
        fail('arguments', '--duration must be > 0 and --onset-tolerance >= 0')
    lyr = M.load_json(a.lyrics)
    raw = (lyr[a.lyrics_key] if a.lyrics_key else lyr)['lines']
    lyrics_sha = M.sha256(a.lyrics)
    lines = []
    for i, ln in enumerate(raw):
        if not isinstance(ln.get('line'), str) or not M.finite(ln.get('start')) or ln['start'] < 0:
            fail(f'lyrics line {i}', 'needs "line" text and a finite "start" >= 0')
        if lines and ln['start'] < lines[-1]['start']:
            fail(f'lyrics line {i}', f'start {ln["start"]} is before line {i - 1} ({lines[-1]["start"]}): lines must be in order')
        if ln['start'] >= a.duration:
            fail(f'lyrics line {i}', f'start {ln["start"]} is not before --duration {a.duration}')
        lines.append({'line': ln['line'], 'start': ln['start'], 'units': units_of(ln['line'])})
    for i, ln in enumerate(lines):
        ln['end'] = lines[i + 1]['start'] if i + 1 < len(lines) else a.duration
        if not ln['units'] and a.empty_lines == 'error':
            fail(f'lyrics line {i}', 'has no displayable characters')

    if a.override_template:
        tpl = {'lyrics_sha256': lyrics_sha, 'note': 'copy units you want to pin into items with a time t (song seconds)',
               'items': [], 'units': [{'line': i, 'unit': k, 'c': u} for i, ln in enumerate(lines) for k, u in enumerate(ln['units'])]}
        M.atomic_write_json(a.override_template, tpl)
        print('wrote', a.override_template)
        return

    toks = load_tokens(a.whisper, a.time)
    flat, owner = [], []          # lyric characters and their (line, unit, position in unit)
    for i, ln in enumerate(lines):
        for k, u in enumerate(ln['units']):
            for p, ch in enumerate(u.lower()):
                flat.append(ch)
                owner.append((i, k, p))
    match = align(flat, [c for _, c, _ in toks])
    first = {}
    for f, j in enumerate(match):
        i, k, p = owner[f]
        if p == 0 and j is not None:
            first[(i, k)] = (toks[j][0], toks[j][2])

    overrides = {}
    if a.overrides:
        ov = M.load_json(a.overrides)
        if ov.get('lyrics_sha256') != lyrics_sha:
            fail(a.overrides, 'stale: it was made for a different lyrics file (lyrics_sha256 differs); re-check and update it')
        for it in ov.get('items', []):
            i, k = it.get('line'), it.get('unit')
            if not (isinstance(i, int) and 0 <= i < len(lines) and isinstance(k, int) and 0 <= k < len(lines[i]['units'])):
                fail(a.overrides, f'item {it} points outside the lyrics')
            if it.get('c') != lines[i]['units'][k]:
                fail(a.overrides, f'stale: line {i} unit {k} is {lines[i]["units"][k]!r}, override says {it.get("c")!r}')
            if not M.finite(it.get('t')):
                fail(a.overrides, f'item {it}: t must be finite')
            overrides[(i, k)] = float(it['t'])

    stats = {'asr': 0, 'split': 0, 'interpolated': 0, 'spread': 0, 'override': 0,
             'dropped_out_of_line': 0, 'dropped_conflict': 0, 'skipped_lines': 0}
    out_lines, prev_last = [], 0.0
    for i, ln in enumerate(lines):
        n, units = len(ln['units']), ln['units']
        start, end = ln['start'], ln['end']
        rec = {'line': ln['line'], 'start': start, 'end': round(end, 3)}
        if not n:
            stats['skipped_lines'] += 1
            out_lines.append({**rec, 'first_char_t': None, 'chars': [], 'skipped': 'no displayable characters'})
            continue
        lo_bound = max(start - a.onset_tolerance, prev_last)
        times, src = [None] * n, [None] * n
        spread = any(x in ln['line'] for x in a.spread)
        for k in range(n):
            if (i, k) in overrides:
                times[k], src[k] = overrides[(i, k)], 'override'
                if not (start - a.onset_tolerance <= times[k] < end):
                    fail(f'override line {i} unit {k}', f'{times[k]} is outside the line [{start}, {end})')
            elif not spread and (i, k) in first:
                t, s = first[(i, k)]
                if lo_bound <= t < end:
                    times[k], src[k] = t, s
                else:
                    stats['dropped_out_of_line'] += 1
        anchors = [k for k in range(n) if times[k] is not None]
        keep = best_monotone([(times[k], 10 ** 6 if src[k] == 'override' else 1) for k in anchors])
        bad = [anchors[x] for x in range(len(anchors)) if x not in keep]
        if bad:
            if any(src[k] == 'override' for k in bad):
                fail(f'lyrics line {i}', 'hand-set override times are out of order: ' +
                     ', '.join(f'{units[k]}@{times[k]}' for k in anchors if src[k] == 'override'))
            if not a.drop_conflicting_anchors:
                fail(f'lyrics line {i} {ln["line"]!r}', 'ASR times are out of order: ' +
                     ', '.join(f'{units[k]}@{times[k]:.2f}' for k in anchors) +
                     ' (fix with --overrides, or --drop-conflicting-anchors)')
            for k in bad:
                times[k] = src[k] = None
                stats['dropped_conflict'] += 1
        # fill runs of missing units between reliable neighbours / line boundaries
        lo_line = max(start, prev_last)
        hi_line = max(lo_line, end - (0.3 if spread else 0.2))
        how = 'spread' if spread else 'interpolated'
        k = 0
        while k < n:
            if times[k] is not None:
                k += 1
                continue
            j = k
            while j < n and times[j] is None:
                j += 1
            left = times[k - 1] if k > 0 else None
            right = times[j] if j < n else None
            m = j - k
            if left is None:
                hi = max(right if right is not None else hi_line, lo_line)
                for q in range(m):
                    times[k + q] = lo_line + (hi - lo_line) * q / m
            else:
                hi = right if right is not None else max(hi_line, left)
                for q in range(m):
                    times[k + q] = left + (hi - left) * (q + 1) / (m + 1)
            for q in range(m):
                src[k + q] = how
            k = j
        rounded = [round(t, 3) for t in times]
        for q in range(1, n):
            if rounded[q] < rounded[q - 1]:
                fail(f'lyrics line {i}', f'times out of order after repair: {rounded}')
        if rounded[0] < round(prev_last, 3):
            fail(f'lyrics line {i}', f'starts at {rounded[0]}, before the previous line\'s last character ({prev_last:.3f})')
        prev_last = times[-1]
        chars = []
        for k in range(n):
            stats[src[k]] += 1
            chars.append({'c': units[k], 't': rounded[k], 'source': src[k],
                          **({'guessed': True} if src[k] not in ('asr', 'override') else {})})
        out_lines.append({**rec, 'first_char_t': chars[0]['t'], 'chars': chars})

    res = {'schema_version': M.SCHEMA_VERSION, 'source': str(a.whisper), 'timing_source': a.time,
           'onset_tolerance': a.onset_tolerance,
           'units': 'CJK character or whole Latin word/number; time of its first character',
           'inputs': {'whisper_sha256': M.sha256(a.whisper), 'lyrics_sha256': lyrics_sha,
                      'overrides': {'path': str(a.overrides), 'sha256': M.sha256(a.overrides)} if a.overrides else None},
           'stats': stats, 'lines': out_lines}
    M.atomic_write_json(a.out, res)
    for o in out_lines:
        print(f"{o['start']:6.2f} " + (' '.join(f"{c['c']}{'?' if c.get('guessed') else ''}{c['t']:.2f}" for c in o['chars'])
                                       or f"({o.get('skipped')})"))
    print('sources:', json.dumps(stats))


if __name__ == '__main__':
    main()
