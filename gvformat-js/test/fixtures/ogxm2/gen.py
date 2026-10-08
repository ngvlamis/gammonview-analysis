# Builds the gvformat-js OGXM v2 fixtures from a real HedgeHog export, through
# HedgeHog's own reference writer (ogxm_convert --to-binary).
#
# `sample.json` is that export as the reference reader projects it
# (`ogxm_convert --to-json < export.ogxm`). It is not committed: it names real
# players. Everything written here renames them and drops the signatures,
# which the renaming would break anyway. Build the converter with
# `make ogxm-tools` in a hedgehog-public checkout, and point CONV at it.
import json, copy, subprocess, sys
CONV = '/Users/nicholas/projects/hedgehog-public/build/ogxm_convert'
src = json.load(open('sample.json'))

def base(n_games):
    d = copy.deepcopy(src)
    d.pop('match_signatures', None)
    d['analysis_info'].pop('signature', None)
    d['player_white'] = 'Alice'
    d['player_black'] = 'Bob'
    d['games'] = d['games'][:n_games]
    w = b = 0
    for g in d['games']:
        if g['winner'] == 0: w += min(g['points_won'], d['match_length'] - w)
        elif g['winner'] == 1: b += min(g['points_won'], d['match_length'] - b)
    d['white_score'], d['black_score'] = w, b
    d['result'] = 0
    return d

def write(d, name):
    out = subprocess.run([CONV, '--to-binary'], input=json.dumps(d).encode(), capture_output=True)
    if out.returncode: sys.exit(f'{name}: {out.stderr.decode()}')
    open(name + '.ogxm', 'wb').write(out.stdout)
    back = subprocess.run([CONV, '--to-json'], input=out.stdout, capture_output=True, check=True)
    ref = json.loads(back.stdout)
    # What the reference replays each ply to: the oracle the test checks against.
    exp = {'ogids': [[[p.get('ogid_before'), p.get('ogid_after')] for p in g['plies']] for g in ref['games']]}
    json.dump(exp, open(name + '.expected.json', 'w'), separators=(',', ':'))
    print(name, len(out.stdout))

# 1. The match itself, three games (the third has a missed double), analysed.
d = base(3); d['checksum'] = 'crc32'; write(d, 'match')

# 2. A second, deeper block over three decisions -- HedgeHog's re-run shape.
d = base(2)
info1 = copy.deepcopy(d['analysis_info'])
info1['analysis_id'] = '00000000-0000-4000-8000-000000000001'
info1['level'] = {'checker_ply': 3, 'cube_ply': 3}
info1['ply'] = 3
d['analyses_info'] = [d['analysis_info'], info1]
def strip_levels(a):
    a.pop('level', None); a.pop('ply', None)
    for k in ('cube_decision', 'missed_double', 'roll'):
        if k in a: a[k].pop('level', None)
    for x in a.get('alternatives', []): x.pop('level', None); x.pop('ply', None)
    return a
for g in d['games']:
    for pi, p in enumerate(g['plies']):
        if 'analysis' not in p: continue
        p['analyses'] = [dict(copy.deepcopy(p['analysis']), analysis_index=0)]
        if g is d['games'][0] and pi in (2, 4, 6):
            a1 = strip_levels(copy.deepcopy(p['analysis'])); a1.pop('roll', None)
            p['analyses'].append(dict(a1, analysis_index=1))
write(d, 'two-blocks')

# 3. A game from a set-up position: game 1 from its ninth ply on.
d = base(2)
g = d['games'][1]
import re
board = [0] * 26
board[1], board[12], board[17], board[19] = 2, 5, 3, 5
board[24], board[13], board[8], board[6] = -2, -5, -3, -5
for p in g['plies'][:8]:
    white = p['color'] == 1
    for m in p['moves']:
        f, pips = m['from'], m['pips']
        sgn = 1 if white else -1
        board[f] -= sgn
        t = f + pips if white else f - pips
        if white and t >= 25 or (not white) and t <= 0: continue
        if board[t] * sgn < 0: board[t] = 0; board[25 if white else 0] -= sgn
        board[t] += sgn
g['initial_board'] = board
g['plies'] = g['plies'][8:]
write(d, 'set-up')

# 4. Something the v1 shape cannot replay: a game opening with the cube on 2.
d = base(1); d['games'][0]['initial_cube_value'] = 2; write(d, 'cube-on-two')

# 5. A resignation: game 0 cut after six plies, and the side on roll resigns.
d = base(1)
g = d['games'][0]
g['plies'] = g['plies'][:6]
c = 1 - g['plies'][-1]['color']
g['plies'].append({'action_id': 27, 'color': c, 'resign_value': 'single'})
g['winner'] = 1 if c == 1 else 0
g['points_won'] = 1
g['termination'] = 'resigned'
d['white_score'], d['black_score'] = (1, 0) if g['winner'] == 0 else (0, 1)
write(d, 'resign')
