// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// JavaScript ESM port of gvformat/share.py — encode a whole match as a compact,
// URL-safe string (and back).
//
// One rung above write_gvab: where that is match <-> bytes, this is match <->
// URL-safe text — the same `.gvab` bytes, zlib-deflated and base64url-encoded so
// a full match can ride inside a URL, a QR code, or any text-only channel with
// no hosted file. Deliberately site-agnostic: the codec knows nothing about any
// particular viewer's URL scheme; a caller composes its own link around
// encode_match (see GammonView's sharelink.js, and gvanalysis/share.py).
//
// pako.deflate emits a zlib stream (RFC 1950), the exact counterpart of Python's
// zlib.compress, so a string encoded on either side decodes on the other.
// base64url is URL-safe (-/_) with `=` padding stripped.

import { deflate, inflate } from "pako";
import { write_gvab } from "./binary.js";
import { readGvab } from "./reader.js";

// base64url over raw bytes, without Buffer — btoa/atob exist in browsers and in
// Node >= 18. Chunked so a large match doesn't blow String.fromCharCode's
// argument limit.
function bytesToBase64url(bytes) {
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64urlToBytes(str) {
  const padded = str.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/**
 * Encode an OGXM match into a URL-safe base64url string.
 * @param {object} match  an OGXM match (write_gvab input / readGvab output)
 * @returns {string} base64url payload (URL-safe; needs no further escaping)
 */
export function encode_match(match) {
  return bytesToBase64url(deflate(write_gvab(match)));
}

/**
 * Decode an encode_match string back into an OGXM match (its inverse).
 * @param {string} payload  a base64url string from encode_match
 * @returns {object} OGXM match
 */
export function decode_match(payload) {
  return readGvab(inflate(base64urlToBytes(payload)));
}
