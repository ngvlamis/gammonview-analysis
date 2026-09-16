# Third-party notices

This project is MIT-licensed (see `LICENSE`). It incorporates the third-party
material below, each under its own terms. **These notices must be preserved in
any copy or substantial portion of this software.**

---

## HedgeHog — the OGXM format and reference codec

OGXM is HedgeHog's format. `gvformat` is an independent implementation of it,
but several modules are direct ports of HedgeHog's C++ sources rather than
clean-room implementations of the published spec:

| This repo | Ported from |
|---|---|
| `gvformat/binary.py`, `gvformat-js/src/binary.js` | `src/match/ogxm_io.cpp`, `ogxm_format.hpp`, `ogxm_json.cpp` — chunk struct layouts, `encode_ply`, fixed-point probability encoding, and `parse_ply_analysis_json` field-for-field |
| `gvformat/export.py`, `gvformat-js/src/export.js` | `src/match/ogxm_replay.cpp` — `_TurnState` is a port of `replay_game()`; the OGID state/action string constants are taken byte-for-byte from `src/ogid.h` |

Source: <https://gitlab.com/eranlambooij/hedgehog-public>

```
MIT License

Copyright (c) 2026 Eran Lambooij

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Kazaross-XG2 match-equity table

The MET data shipped in `gvformat/met.py` and `gvformat-js/src/met.js` (the
25x25 pre-Crawford table and the post-Crawford vector) is transcribed from GNU
Backgammon's `met/Kazaross-XG2.xml`. The table is separately licensed under the
permissive notice below — it is **not** covered by GNU Backgammon's GPL.

```
Copyright (C) 2011 Neil Kazaross

Table rolled up to 9 point match by eXtreme Gammon. Then uses R/K MET
which was rolled up to 15 and extrapolated to 25 points.

Transcribed for use by GNUbg by Michael Petch <mpetch@capp-sysware.com>.

This file is distributed as a part of the GNU Backgammon program.
Copying and distribution of this file, with or without modification, are
permitted in any medium without royalty provided the copyright notice and
this notice are preserved. This file is offered as-is, without any warranty.
```

---

## Not bundled

For completeness, since both are commonly assumed to be vendored here:

- **bgsage** (MPL-2.0) — the analysis engine. An optional `[engine]` extra
  resolved from PyPI at install time; no bgsage source is included in this
  repository, and none of its files are modified.
- **pako** (MIT) — used by the JS mirror for zlib deflate. A declared npm
  dependency, installed by `npm install`; not vendored, and excluded from the
  Python sdist.
