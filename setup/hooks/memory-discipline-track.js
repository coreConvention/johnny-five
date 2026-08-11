#!/usr/bin/env node
// memory-discipline-track.js
// PostToolUse hook. Tracks per-session counts of:
//   - File-modifying tools (Edit, Write, apply_patch) → "edits"
//   - johnny-five memory_store calls → "stores"
//   - johnny-five memory_search calls → "searches"
//
// Pairs with memory-discipline-enforce.js (Stop hook), which reads the same
// state file and blocks Stop when:
//   (a) a turn had ≥3 edits and 0 stores, OR
//   (b) a correction signal fired this turn (correction_seen=true,
//       set by user-prompt-correction.sh) and 0 memory_search calls
//       have happened since.
//
// State files live below the deployed hook directory.
// TTL: 7 days (mirrors tool-failure-tracker.js).
//
// Never blocks. Always emits empty-or-valid JSON on stdout.

const {
  cleanupOldStateFiles,
  readState,
  safeReadStdin,
  statePath,
  writeState,
} = require('./lib/j5-runtime');

const EDIT_TOOLS = new Set(['Edit', 'Write', 'apply_patch']);
const STORE_TOOLS = new Set([
  'mcp__johnny-five__memory_store',
  'mcp__johnny_five__memory_store',
]);
const SEARCH_TOOLS = new Set([
  'mcp__johnny-five__memory_search',
  'mcp__johnny_five__memory_search',
]);

function main() {
  const payload = safeReadStdin();
  if (!payload) return;

  const toolName = payload.tool_name || '';
  const sessionId = payload.session_id || 'unknown';

  const isEdit = EDIT_TOOLS.has(toolName);
  const isStore = STORE_TOOLS.has(toolName);
  const isSearch = SEARCH_TOOLS.has(toolName);
  if (!isEdit && !isStore && !isSearch) return;

  cleanupOldStateFiles('memory-discipline');
  const stateFile = statePath('memory-discipline', sessionId);
  const state = readState(
    stateFile,
    { edits: 0, stores: 0, searches: 0, turnStart: Date.now() },
  );

  if (isEdit) state.edits = (state.edits || 0) + 1;
  if (isStore) state.stores = (state.stores || 0) + 1;
  if (isSearch) state.searches = (state.searches || 0) + 1;

  writeState(stateFile, state);
}

main();
