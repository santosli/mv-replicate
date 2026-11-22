#!/usr/bin/env python3
"""Per-character lyric timing: put whisper's token times onto the correct lyric text.

whisper hears the song through the music and gets many characters wrong, but its DTW token times
(`t_dtw`, needs `-dtw <model preset>` and `-nfa`: flash attention silently disables DTW) are good.
Do NOT use the token `offsets`: on a real song they put the first sung syllable 0.8 s early and
drifted by a syllable inside lines ("sometimes on time, sometimes half a beat late"); the DTW
times of two different models agreed within 0.06 s and matched the vocal-band energy rises.

This takes the known lyric lines (line start times, see examples/lyrics.example.json) and the
whisper JSON (whisper-cli -ojf, Chinese, one character per token), and for each line assigns the
whisper characters to the real ones by a global sequence alignment; characters whisper skipped
get interpolated times and are marked `guessed` (check those by ear or against onsets).

  whisper-cli -m ggml-large-v3-turbo-q5_0.bin -dtw large.v3.turbo -nfa -l zh -ojf -of <out>/whisper -f song16k.wav
  python3 lyric_chars.py <out>/whisper.json --lyrics lyrics.json --duration 68 --out lyric-chars.json
"""
import argparse, json, re

PUNCT = re.compile(r'[\s，。、！？!?,.“”"‘’…～~—\-：:；;（）()]')


def chars_of(line):
    """Displayable units: CJK characters one by one, Latin words / numbers kept whole."""
    return re.findall(r'[A-Za-z0-9]+|[^\sA-Za-z0-9]', PUNCT.sub(' ', line))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('whisper')
    ap.add_argument('--lyrics', required=True, help='JSON with "lines": [{start, line}]')
    ap.add_argument('--lyrics-key', help='read the lines from this top-level key instead')
    ap.add_argument('--duration', type=float, required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--time', choices=['dtw', 'offsets'], default='dtw')
    ap.add_argument('--spread', action='append', default=[],
                    help='line containing this text gets its characters spread evenly over the line '
                         '(hummed lines whisper cannot hear, e.g. 呼噜噜)')
    a = ap.parse_args()

    lyr = json.load(open(a.lyrics))
    lines = (lyr[a.lyrics_key] if a.lyrics_key else lyr)['lines']
    flat = []
    for seg in json.load(open(a.whisper))['transcription']:
        for t in seg['tokens']:
            if t['text'].startswith('[') or not PUNCT.sub('', t['text']):
                continue
            at = t['t_dtw'] / 100 if a.time == 'dtw' and t.get('t_dtw', -1) >= 0 else t['offsets']['from'] / 1000
            flat.append((at, PUNCT.sub('', t['text'])))
    toks = []
    for i, (at, txt) in enumerate(flat):
        # a token may hold several characters ("看看"); spread them up to the next token
        nxt = flat[i + 1][0] if i + 1 < len(flat) else at + 0.25 * len(txt)
        step = min(0.3, max(0.0, nxt - at) / len(txt))
        for k, ch in enumerate(txt):
            toks.append((round(at + step * k, 3), ch))

    # global alignment of the real characters against what whisper heard (Needleman-Wunsch:
    # same character +2, different character 0, gap -1); whisper hears homophones, so most
    # pairs are mismatches that still carry the right time
    units, owner = [], []
    for i, ln in enumerate(lines):
        for u in chars_of(ln['line']):
            units.append(u)
            owner.append(i)
    n, m = len(units), len(toks)
    S = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        S[i][0] = -i
    for j in range(1, m + 1):
        S[0][j] = -j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            S[i][j] = max(S[i - 1][j - 1] + (2 if units[i - 1] == toks[j - 1][1] else 0),
                          S[i - 1][j] - 1, S[i][j - 1] - 1)
    times, i, j = [None] * n, n, m
    while i and j:
        if S[i][j] == S[i - 1][j - 1] + (2 if units[i - 1] == toks[j - 1][1] else 0):
            times[i - 1] = toks[j - 1][0]
            i, j = i - 1, j - 1
        elif S[i][j] == S[i - 1][j] - 1:
            i -= 1
        else:
            j -= 1
    # characters whisper skipped: interpolate between neighbours (or the line start)
    known = [k for k in range(n) if times[k] is not None]
    guessed = set(k for k in range(n) if times[k] is None)
    for k in sorted(guessed):
        p = max((q for q in known if q < k), default=None)
        q = min((q for q in known if q > k), default=None)
        lo = times[p] if p is not None else lines[owner[k]]['start']
        if q is None:
            times[k] = round(lo + 0.25 * (k - (p if p is not None else k - 1)), 3)
        else:
            p0 = p if p is not None else k - 1
            times[k] = round(lo + (times[q] - lo) * (k - p0) / (q - p0), 3)

    # a character pushed outside its own line (hummed lines whisper writes as other syllables)
    # is spread evenly between the last good time in the line and the line end
    for i, ln in enumerate(lines):
        end = lines[i + 1]['start'] if i + 1 < len(lines) else a.duration
        ks = [k for k in range(n) if owner[k] == i]
        bad = [k for k in ks if not (ln['start'] - 0.6 <= times[k] < end)]
        if bad:
            good = [times[k] for k in ks if k not in bad and times[k] < times[bad[0]]] if bad[0] != ks[0] else []
            lo = good[-1] if good else ln['start']
            for j, k in enumerate(bad):
                times[k] = round(lo + (end - 0.2 - lo) * (j + (1 if good else 0)) / (len(bad) + (1 if good else 0)), 3)
                guessed.add(k)

    for i, ln in enumerate(lines):
        if any(x in ln['line'] for x in a.spread):
            end = lines[i + 1]['start'] if i + 1 < len(lines) else a.duration
            ks = [k for k in range(n) if owner[k] == i]
            for j, k in enumerate(ks):
                times[k] = round(ln['start'] + (end - 0.3 - ln['start']) * j / len(ks), 3)
                guessed.add(k)

    out = []
    for i, ln in enumerate(lines):
        end = lines[i + 1]['start'] if i + 1 < len(lines) else a.duration
        cs = [{'c': units[k], 't': times[k], **({'guessed': True} if k in guessed else {})}
              for k in range(n) if owner[k] == i]
        out.append({'line': ln['line'], 'start': ln['start'], 'end': round(end, 3),
                    'first_char_t': cs[0]['t'], 'chars': cs})
    json.dump({'source': a.whisper, 'lines': out}, open(a.out, 'w'), ensure_ascii=False, indent=1)
    for o in out:
        print(f"{o['start']:6.2f} " + ' '.join(f"{c['c']}{'?' if c.get('guessed') else ''}{c['t']:.2f}" for c in o['chars']))


if __name__ == '__main__':
    main()
