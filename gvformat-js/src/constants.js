// SPDX-License-Identifier: MIT
// Copyright (C) 2026 Nicholas Vlamis

// Shared constants (mirrors gvformat/binary.py)

export const OGXM_MAGIC = 0x4D58474F;
export const VERSION_MAJOR = 1;
export const VERSION_MINOR = 3;

export const CHUNK_MHDR = 0x5244484D;
export const CHUNK_GAME = 0x454D4147;
export const CHUNK_ANAL = 0x4C414E41;
export const CHUNK_EVAL = 0x4C415645;
export const CHUNK_ALTS = 0x53544C41;
export const CHUNK_CUBE = 0x45425543;
export const CHUNK_GVAN = 0x4E415647;
export const CHUNK_CSUM = 0x4D555343;
// Base-spec chunks gvformat does not decode. They are carried through a
// read/write cycle verbatim (see _unknown_chunks): SIGN binds to its
// analysis block, CLCK/VIDO trail every block.
export const CHUNK_SIGN = 0x4E474953;
export const CHUNK_CLCK = 0x4B434C43;
export const CHUNK_VIDO = 0x4F444956;
export const END_MAGIC = 0x21444E45;

export const CHUNK_FLAG_CRITICAL = 0x0001;
export const HEADER_FLAG_HAS_ANALYSIS = 0x0001;

// GVAN chunk version (GammonView's own, independent of the OGXM version).
//   v1: per-decision mwc_on_win/mwc_on_loss anchors
//   v2: anchors blanked (MWC became compute-on-read) but the bytes kept
//   v3: the blanked bytes dropped -- checker record 7->3 B, cube record 6->2 B
export const GVAN_VERSION = 3;

export const MAX_ALTS_PER_DECISION = 50;
export const MAX_PLAYER_NAME = 255;
export const DEFAULT_BOARD_SENTINEL = 0xFF;
export const WINNER_INCOMPLETE = 0xFF;

export const ACTION_DOUBLE = 21;
export const ACTION_TAKE = 22;
export const ACTION_DROP = 23;
export const ACTION_RESIGN_GAME = 27;
export const ACTION_RESIGN_MATCH = 28;
export const ACTION_SET_POSITION = 31;

// The terminal actions that are *acts*. Every other end-of-game marker (24
// game over, 26 final, 29 forfeit) has no actor, so writers stamp it with the
// winner; a resignation has one, and it is the player who lost. Readers use
// this to decide whose row a terminal ply belongs to.
export const RESIGN_ACTIONS = new Set([ACTION_RESIGN_GAME, ACTION_RESIGN_MATCH]);
export const SET_POSITION_DICE_FLAG = 0x40;

export const CUBE_TYPE_DOUBLE_DECISION = 0;
export const CUBE_TYPE_TAKE_PASS = 1;
export const CUBE_TYPE_MISSED_DOUBLE = 2;
export const CUBE_TYPE_RESIGN = 3;
export const CUBE_TYPE_LIVE_CHECKER = 4;

export const CUBE_ACTION_NO_DOUBLE = 0;
export const CUBE_ACTION_DOUBLE = 1;
export const CUBE_ACTION_TAKE = 2;
export const CUBE_ACTION_PASS = 3;

export const CUBE_ACTION_CODES = {
  no_double: CUBE_ACTION_NO_DOUBLE,
  double: CUBE_ACTION_DOUBLE,
  take: CUBE_ACTION_TAKE,
  pass: CUBE_ACTION_PASS,
};

// Format cap on analysis blocks per match (binary spec: MAX_ANALYSES). A file
// carrying more than one sets min_reader_minor = 3.
export const MAX_ANALYSES = 16;

// action_id -> [d1, d2, num_move_bytes]; index = action_id (0-20).
export const DICE_TABLE = [
  [1, 1, 4], [1, 2, 2], [1, 3, 2], [1, 4, 2], [1, 5, 2], [1, 6, 2],
  [2, 2, 4], [2, 3, 2], [2, 4, 2], [2, 5, 2], [2, 6, 2],
  [3, 3, 4], [3, 4, 2], [3, 5, 2], [3, 6, 2],
  [4, 4, 4], [4, 5, 2], [4, 6, 2],
  [5, 5, 4], [5, 6, 2],
  [6, 6, 4],
];

// action_code (0-3) -> OGXM cube-action label (inverse of CUBE_ACTION_CODES)
export const ACTION_NAMES = {
  0: "no_double",
  1: "double",
  2: "take",
  3: "pass",
};
