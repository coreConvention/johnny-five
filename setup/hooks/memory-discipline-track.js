#!/usr/bin/env node
// memory-discipline-track.js
// PostToolUse hook. Tracks per-session counts of:
//   - File-modifying tools (Edit, Write, NotebookEdit) → "edits"
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
// State files: ~/.claude/hooks/state/memory-discipline-<sessionId>.json
// TTL: 7 days (mirrors tool-failure-tracker.js).
//
// Never blocks. Always emits empty-or-valid JSON on stdout.

const fs = require('fs');
const path = require('path');
const os = require('os');

const STATE_TTL_MS = 7 * 24 * 60 * 60 * 1000;

const EDIT_TOOLS = new Set(['Edit', 'Write', 'NotebookEdit']);
const STORE_TOOL = 'mcp__johnny-five__memory_store';
const SEARCH_TOOL = 'mcp__johnny-five__memory_search';

function safeReadStdin() {
  try {
    return JSON.parse(fs.readFileSync(0, 'utf-8'));
  } catch {
    return null;
  }
}

function stateDir() {
  const d = path.join(os.homedir(), '.claude', 'hooks', 'state');
  fs.mkdirSync(d, { recursive: true });
  return d;
}

function cleanupOldStateFiles(dir) {
  try {
    const now = Date.now();
    for (const f of fs.readdirSync(dir)) {
      if (!f.startsWith('memory-discipline-')) continue;
      const p = path.join(dir, f);
      try {
        const st = fs.statSync(p);
        if (now - st.mtimeMs > STATE_TTL_MS) fs.unlinkSync(p);
      } catch {}
    }
  } catch {}
}

function main() {
  const payload = safeReadStdin();
  if (!payload) return;

  const toolName = payload.tool_name || '';
  const sessionId = payload.session_id || 'unknown';

  const isEdit = EDIT_TOOLS.has(toolName);
  const isStore = toolName === STORE_TOOL;
  const isSearch = toolName === SEARCH_TOOL;
  if (!isEdit && !isStore && !isSearch) return;

  const dir = stateDir();
  cleanupOldStateFiles(dir);
  const statePath = path.join(dir, `memory-discipline-${sessionId}.json`);

  let state = { edits: 0, stores: 0, searches: 0, turnStart: Date.now() };
  if (fs.existsSync(statePath)) {
    try { state = JSON.parse(fs.readFileSync(statePath, 'utf-8')); } catch {}
  }

  if (isEdit) state.edits = (state.edits || 0) + 1;
  if (isStore) state.stores = (state.stores || 0) + 1;
  if (isSearch) state.searches = (state.searches || 0) + 1;

  try {
    fs.writeFileSync(statePath, JSON.stringify(state));
  } catch {}
}

main();
