#!/usr/bin/env node
// memory-discipline-enforce.js
// Stop hook. Reads per-session counters written by memory-discipline-track.js.
// Blocks Stop when the current turn had ≥3 file edits and 0 johnny-five
// memory_store calls — forcing the assistant to either store a lesson or
// explicitly justify the turn before ending.
//
// Pairs with memory-discipline-track.js (PostToolUse).
// State file: ~/.claude/hooks/state/memory-discipline-<sessionId>.json
//
// NOTE: this is a COMMAND-type Stop hook. It only reads a local state file and
// never calls an MCP tool, so it works even though MCP servers are shut down at
// stop time (which is why a PROMPT-type Stop hook that tries to call memory_store
// cannot work — see docs/INTEGRATION.md).
//
// Bypass / override:
//   - If state already shows stop_overrides field set to true, the hook
//     allows the Stop and clears it (one-shot bypass set by the assistant).
//   - The block can be lifted permanently for a session by writing
//     {"disabled": true} into the state file.
//
// Output protocol (Stop hook):
//   - {"decision": "block", "reason": "..."} → blocks Stop, reason injected
//   - exit 0 with no output → allows Stop
//
// Counters reset on every successful (allowed) Stop.

const fs = require('fs');
const path = require('path');
const os = require('os');

const HEAVY_EDIT_THRESHOLD = 3;

function safeReadStdin() {
  try {
    return JSON.parse(fs.readFileSync(0, 'utf-8'));
  } catch {
    return null;
  }
}

function statePathFor(sessionId) {
  const d = path.join(os.homedir(), '.claude', 'hooks', 'state');
  fs.mkdirSync(d, { recursive: true });
  return path.join(d, `memory-discipline-${sessionId}.json`);
}

function readState(p) {
  if (!fs.existsSync(p)) return { edits: 0, stores: 0 };
  try { return JSON.parse(fs.readFileSync(p, 'utf-8')); } catch { return { edits: 0, stores: 0 }; }
}

function writeState(p, s) {
  try { fs.writeFileSync(p, JSON.stringify(s)); } catch {}
}

function resetCounters(p) {
  writeState(p, { edits: 0, stores: 0, searches: 0, turnStart: Date.now() });
}

function main() {
  const payload = safeReadStdin();
  if (!payload) { process.exit(0); }

  // Only enforce on the FIRST stop attempt of a turn, not on re-entry where
  // the assistant is already responding to our block (avoids loops).
  if (payload.stop_hook_active === true) { process.exit(0); }

  const sessionId = payload.session_id || 'unknown';
  const sp = statePathFor(sessionId);
  const state = readState(sp);

  // Honor session-level disable
  if (state.disabled === true) { process.exit(0); }

  // Honor one-shot bypass
  if (state.stop_overrides === true) {
    state.stop_overrides = false;
    writeState(sp, state);
    process.exit(0);
  }

  const edits = Number(state.edits || 0);
  const stores = Number(state.stores || 0);
  const searches = Number(state.searches || 0);
  const correctionSeen = state.correction_seen === true;
  const searchesAtCorrection = Number(state.searches_at_correction || 0);

  const heavyTurn = edits >= HEAVY_EDIT_THRESHOLD;
  const stored = stores >= 1;

  // Gate 1: heavy turn without store. Existing behaviour.
  if (heavyTurn && !stored) {
    const reason =
      `Memory-discipline check: this turn made ${edits} file edits but did not call ` +
      `\`mcp__johnny-five__memory_store\` once. Before ending, decide:\n\n` +
      `  (a) Store at least one lesson/project memory now (preferred). Search first ` +
      `with \`memory_search\` to avoid duplicates, then \`memory_store\` with ` +
      `project_dir set to the current repo and importance ≥6 for genuine lessons. ` +
      `What to capture: user corrections, non-obvious bug root causes, silent ` +
      `typo/config-mismatch gotchas, ★ Insight architectural connections, patterns ` +
      `likely to recur. Then end the turn — the next Stop will pass.\n\n` +
      `  (b) If this turn really had nothing worth remembering (pure refactor with ` +
      `no surprises, mechanical rename, etc.), bypass the check by writing ` +
      `\`{"stop_overrides": true}\` into ` +
      `\`~/.claude/hooks/state/memory-discipline-${sessionId}.json\` (merge with ` +
      `existing state) before re-attempting Stop. Use sparingly — bypass means ` +
      `the lesson capture trail goes silent for this turn.`;

    process.stdout.write(JSON.stringify({ decision: 'block', reason }));
    process.exit(0);
  }

  // Gate 2: correction happened, no search since. Enforces SEARCH-FIRST
  // when the user corrects you. The user-prompt-correction.sh hook sets
  // `correction_seen` and snapshots `searches_at_correction` when it fires;
  // this gate refuses Stop until at least one new memory_search has been
  // logged AFTER the correction.
  if (correctionSeen && searches <= searchesAtCorrection) {
    const reason =
      `Memory-discipline check: a correction signal fired this turn ` +
      `(user-prompt-correction.sh detected one of "actually", "does not match", ` +
      `"i wanted", "why is", "are you using", etc.) but no \`mcp__johnny-five__memory_search\` ` +
      `call has happened since. Before ending, run a memory_search relevant to ` +
      `the user's correction (search for the topic + the project_dir of cwd) so ` +
      `you can check whether you already had the answer stored.\n\n` +
      `Search-first is the load-bearing rule of the self-improvement loop. ` +
      `When the user corrects you, the most common cause is that you skipped a ` +
      `lesson that was already in johnny-five — searching turns "I should have ` +
      `known" into "I'll fix this and avoid the same trap next time."\n\n` +
      `If this correction genuinely needs no memory context (e.g., a typo fix ` +
      `or a stylistic nit), bypass with \`{"stop_overrides": true}\` in ` +
      `\`~/.claude/hooks/state/memory-discipline-${sessionId}.json\`.`;

    process.stdout.write(JSON.stringify({ decision: 'block', reason }));
    process.exit(0);
  }

  // All gates pass — allow stop, reset counters for next turn.
  resetCounters(sp);
  process.exit(0);
}

main();
