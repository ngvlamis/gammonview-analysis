# @gammonview/gvformat

The OGXM/GVA codec in pure JavaScript — read and write `.gvab` backgammon match
files, convert `.xg` / `.bgf` / `.mat` / OpenGammon sources to `.gva` JSON, and
encode or parse OGIDs. **No engine, no WebAssembly, no build step.** One runtime
dependency (`pako`, for zlib).

This is the browser half of
[gammonview-analysis](https://github.com/ngvlamis/gammonview-analysis): Python
writes `.gvab` server-side, this reads it client-side. It is an ESM mirror of the
Python `gvformat` package and **the definitive copy of the JavaScript** — the
port is maintained here, not downstream.

## Install

```bash
npm install @gammonview/gvformat
```

Node 18 or newer. ESM only — there is no CommonJS build and no bundling step;
`src/` is what ships, and it is the same source the tests run against. `pako`
(zlib) is the one runtime dependency.

Vendoring `src/` directly still works if you would rather not take a dependency
— it is plain ESM with no build step — but the package is the supported path.

## Use

```js
import { read_gvab, write_gvab, convert_mat, convert_xg, convert_bgf, parse_ogid } from '@gammonview/gvformat';

// .gvab bytes -> .gva JSON
const ogxm = read_gvab(bytes);
console.log(ogxm.match_length, ogxm.games.length);

// rewriting is idempotent: one pass canonicalizes, and it is byte-stable
// from then on. It does not preserve non-canonical slack in a foreign file.
const canonical = write_gvab(read_gvab(bytes));
write_gvab(read_gvab(canonical));   // === canonical

// a dropped .mat -> .gva JSON, no analysis, no engine
const converted = convert_mat(matText);            // text in, sync

// the binary source formats are async (they inflate internally)
const fromXg  = await convert_xg(fileBytes);
const fromBgf = await convert_bgf(fileBytes);

// plies carry OGID strings, so reading a position is parse_ogid
const { ogid_before } = ogxm.games[0].plies[0];
const pos = parse_ogid(ogid_before);   // -> board, cube, dice, score, state
```

`board_to_ogid(board, { moverIsWhite: true })` is the inverse, for when you hold
a board array rather than an OGID — an engine's output, say. A complete OGID
needs the cube, dice, score and state options too; `src/ogid.js` lists them all
with their defaults.

`convert_og` is the odd one out — it is not a source converter but a merge:
`convert_og(base, payload)` folds an OpenGammon analysis payload into an OGXM
document you already have (normally from `convert_mat`), leaving its games,
plies and OGIDs untouched.

Analysis statistics the format computes on read are in
`compute_aggregates(ogxm)`, and the match-equity helpers (`eq2mwc`, `mwc2eq`,
`mwcAnchors`) live in `met.js`.

Every export is available under both `snake_case` and `camelCase`, so it reads
naturally from either side of the Python/JS boundary.

## Layout

| Path | What |
|---|---|
| `src/` | The library. Browser-safe: no `node:*` imports, no `import.meta`. |
| `cli/` | Node front ends for the converters — executed, never imported. |
| `test/` | 24 standalone test scripts, chained by `npm test`. |

The `src/` / `cli/` split is load-bearing rather than cosmetic: `import.meta` is
a syntax error once a toolchain rewrites the module to CommonJS, and `node:*`
builtins resolve nowhere in a browser. Both used to live in `src/` and broke
consumers' builds. `test/test-robustness.js` fails if either pattern reappears.
See [`cli/README.md`](cli/README.md).

```bash
npm test        # all 24, no network and no fixtures outside the repo
```

Each script is standalone and runnable on its own (`node test/test-ogid.js`);
`npm test` chains them with `&&`, so the run stops at the first failure and each
prints its own check tally rather than one combined total.

## Parity with Python

The Python `gvformat` and this package must agree byte-for-byte — the same
`.gvab` in gives the same `.gva` JSON out, and the MET tables and OGID encoders are
kept numerically identical. A fix to one is a fix to both. The Python suite and
`npm test` cover the same ground from either side.

Version numbers follow the monorepo tag that ships them (`vX.Y.Z` → `X.Y.Z`);
the JS line skips releases where no JavaScript changed.

## Licence

[MIT](LICENSE). `src/binary.js` and `src/export.js` are ports of HedgeHog's C++
codec (MIT, © 2026 Eran Lambooij) and carry its notice — see
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md), whose notices must be preserved
in copies of this package. `src/met.js` embeds the Kazaross-XG2 match-equity
table under its own permissive terms, reproduced at the top of that file.
