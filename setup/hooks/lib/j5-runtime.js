'use strict';

const fs = require('fs');
const path = require('path');

const STATE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const HOOKS_DIR = path.resolve(__dirname, '..');

function safeReadStdin() {
  try {
    return JSON.parse(fs.readFileSync(0, 'utf-8'));
  } catch {
    return null;
  }
}

function safeSessionId(sessionId) {
  return String(sessionId || 'unknown').replace(/[^A-Za-z0-9._-]/g, '_');
}

function stateDir() {
  const dir = path.join(HOOKS_DIR, 'state');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function statePath(prefix, sessionId) {
  return path.join(stateDir(), `${prefix}-${safeSessionId(sessionId)}.json`);
}

function cleanupOldStateFiles(prefix) {
  try {
    const dir = stateDir();
    const now = Date.now();
    for (const file of fs.readdirSync(dir)) {
      if (!file.startsWith(`${prefix}-`)) continue;
      const candidate = path.join(dir, file);
      try {
        if (now - fs.statSync(candidate).mtimeMs > STATE_TTL_MS) {
          fs.unlinkSync(candidate);
        }
      } catch {
        // Per-file cleanup is best-effort.
      }
    }
  } catch {
    // State cleanup must never block a hook.
  }
}

function readState(candidate, fallback = {}) {
  if (!fs.existsSync(candidate)) return { ...fallback };
  try {
    return JSON.parse(fs.readFileSync(candidate, 'utf-8'));
  } catch {
    return { ...fallback };
  }
}

function writeState(candidate, state) {
  try {
    fs.writeFileSync(candidate, JSON.stringify(state));
  } catch {
    // State persistence is best-effort.
  }
}

function isStopHookReentry(payload) {
  return payload?.stop_hook_active === true;
}

module.exports = {
  cleanupOldStateFiles,
  isStopHookReentry,
  readState,
  safeReadStdin,
  stateDir,
  statePath,
  writeState,
};
