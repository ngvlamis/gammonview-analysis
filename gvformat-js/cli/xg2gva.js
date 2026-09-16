#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Convert an eXtreme Gammon `.xg` to OGXM JSON. See ./README.md for why the
// whole CLI -- not just its entry guard -- lives outside src/.
import { convertXg } from '../src/xg2gva.js';

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    console.error("Usage: xg2gva <file.xg> [output]");
    process.exit(1);
  }

  const fs = await import("node:fs/promises");

  const xgPath = args[0];
  const raw = new Uint8Array(await fs.readFile(xgPath));
  const ogxm = await convertXg(raw);

  const outPath = args[1] || xgPath.replace(/\.xg$/i, ".gva");
  await fs.writeFile(outPath, JSON.stringify(ogxm));
  console.log(`Wrote ${outPath}`);
}

main().catch(err => { console.error(err); process.exit(1); });
