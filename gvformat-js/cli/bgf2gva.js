#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Convert a BGBlitz `.bgf` to OGXM JSON. See ./README.md for why the whole
// CLI -- not just its entry guard -- lives outside src/.
import { convertBgf } from '../src/bgf2gva.js';

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    console.error("Usage: bgf2gva <file.bgf> [output]");
    process.exit(1);
  }

  const fs = await import("node:fs/promises");

  const bgfPath = args[0];
  const raw = await fs.readFile(bgfPath);
  const ogxm = await convertBgf(raw);

  const outPath = args[1] || bgfPath.replace(/\.bgf$/i, ".gva");
  await fs.writeFile(outPath, JSON.stringify(ogxm));
  console.log(`Wrote ${outPath}`);
}

main().catch(err => { console.error(err); process.exit(1); });
