#!/usr/bin/env node
// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Convert a JellyFish `.mat` to OGXM JSON. See ./README.md for why the whole
// CLI -- not just its entry guard -- lives outside src/.
import { convertMat } from '../src/mat2gva.js';

async function main() {
  const args = process.argv.slice(2);
  if (args.length === 0) {
    console.error("Usage: mat2gva <file.mat> [output]");
    process.exit(1);
  }

  const fs = await import("node:fs/promises");

  const matPath = args[0];
  const text = await fs.readFile(matPath, "utf-8");
  const ogxm = convertMat(text);

  const outPath = args[1] || matPath.replace(/\.mat$/i, ".gva");
  await fs.writeFile(outPath, JSON.stringify(ogxm));
  console.log(`Wrote ${outPath}`);
}

main().catch(err => { console.error(err); process.exit(1); });
