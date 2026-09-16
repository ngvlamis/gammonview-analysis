# CLI runners

The converters' command-line front ends, kept **out of `src/`** on purpose.

`src/` is a browser library. Two things break a *consumer's* build if they leak
into it, and both used to:

- **`import.meta`** — a syntax error the moment a toolchain rewrites the module
  to CommonJS, which is what Babel does under Jest. The old entry guard
  (`import.meta.url === \`file://${process.argv[1]}\``) sat at the bottom of each
  converter, so a consumer could not so much as `import` one from a test without
  it blowing up at parse time, before any of their code ran.
- **`node:*` builtins** — `main()` does `await import("node:fs/promises")`,
  which resolves nowhere in a browser. Bundlers stub it and warn
  ("externalized for browser compatibility").

So the whole CLI — argument parsing, file I/O, `main()`, and the invocation —
lives here, and `src/` holds nothing but the converter itself.
`test/test-robustness.js` fails if either pattern reappears in `src/`.

These files are executed, never imported:

```bash
node cli/mat2gva.js match.mat [out.gva]
node cli/xg2gva.js  match.xg  [out.gva]
node cli/bgf2gva.js match.bgf [out.gva]
```
