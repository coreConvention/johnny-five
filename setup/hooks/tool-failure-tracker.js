#!/usr/bin/env node
// tool-failure-tracker.js
// PostToolUse hook. Counts failed tool calls by (tool_name, input-hash) within
// a session. After 3 similar failures, injects an advisory: "consider
// memory_search for prior workarounds."
//
// Design:
//   - Failure detection is heuristic: tool_response.error / isError / is_error
//     presence, or tool_response.stdout/stderr containing "Error" / "Exception"
//     for Bash-shaped responses. Conservative — false positives cost a cheap
//     state-file entry; false negatives just lose the signal.
//   - Per-session state files live at ~/.claude/hooks/state/tool-failures-<session>.json.
//     A best-effort cleanup drops files older than 7 days on each invocation.
//   - Never blocks the tool. Always emits valid JSON (or empty) on stdout.

const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

const FAILURE_THRESHOLD = 3;
const STATE_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

function safeReadStdin() {
  try {
    return JSON.parse(fs.readFileSync(0, 'utf-8'));
  } catch {
    return null;
  }
}

function looksLikeFailure(toolResponse) {
  if (toolResponse == null) return false;
  if (toolResponse === false) return true;
  if (typeof toolResponse !== 'object') return false;
  if (toolResponse.isError === true) return true;
  if (toolResponse.is_error === true) return true;
  if (toolResponse.error) return true;
  // Bash-shaped: non-string stdout or explicit interrupted flag.
  if (toolResponse.interrupted === true) return true;
  // Heuristic on stringified body for tools with less structured errors.
  const body = JSON.stringify(toolResponse);
  if (body.length > 10_000) return false; // avoid false positives on huge outputs
  return /\b(traceback|exception|FAILED|fatal error)\b/i.test(body);
}

function inputSignature(toolName, toolInput) {
  const normalised = JSON.stringify(toolInput ?? {});
  const hash = crypto.createHash('sha256').update(normalised).digest('hex').slice(0, 12);
  return `${toolName}:${hash}`;
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
      if (!f.startsWith('tool-failures-')) continue;
      const p = path.join(dir, f);
      try {
        const st = fs.statSync(p);
        if (now - st.mtimeMs > STATE_TTL_MS) fs.unlinkSync(p);
      } catch {
        // best-effort; ignore per-file errors
      }
    }
  } catch {
    // best-effort; ignore cleanup errors
  }
}

function main() {
  const payload = safeReadStdin();
  if (!payload) return; // no-op

  const toolName = payload.tool_name || 'unknown';
  const toolInput = payload.tool_input || {};
  const toolResponse = payload.tool_response;

  if (!looksLikeFailure(toolResponse)) return;

  const sessionId = payload.session_id || 'unknown';
  const dir = stateDir();
  cleanupOldStateFiles(dir);
  const statePath = path.join(dir, `tool-failures-${sessionId}.json`);

  let state = {};
  if (fs.existsSync(statePath)) {
    try { state = JSON.parse(fs.readFileSync(statePath, 'utf-8')); } catch { state = {}; }
  }

  const sig = inputSignature(toolName, toolInput);
  state[sig] = (state[sig] || 0) + 1;

  try {
    fs.writeFileSync(statePath, JSON.stringify(state));
  } catch {
    // state persistence is best-effort
  }

  if (state[sig] < FAILURE_THRESHOLD) return; // not yet triggered

  // Compose advisory on threshold hit.
  const hash = sig.split(':')[1];
  const advice =
    `Tool-failure tracker: ${toolName} has failed ${state[sig]} times this session with ` +
    `a near-identical input signature (${hash}). Before retrying, consider ` +
    `\`memory_search\` on johnny-five for prior workarounds to this error pattern, or ` +
    `step back and diagnose the root cause rather than retrying.`;

  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PostToolUse',
      additionalContext: advice,
    },
  }));
}

main();
