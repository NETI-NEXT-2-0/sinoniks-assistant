import { isRecord } from '../api/client';

const PREFIX = 'partner-clarification-draft-v1:';
const TTL = 24 * 60 * 60 * 1000;
export interface ClarificationDraft {
  values: Record<string, string>;
  selected: Record<string, string[]>;
  unknowns: Record<string, boolean>;
  other: Record<string, boolean>;
  freeText: Record<string, string>;
}
export function draftKey(owner: string, chat: string, branch: string, revision: string): string {
  return PREFIX + JSON.stringify([owner, chat, branch, revision]);
}
export function readDraft(key: string): ClarificationDraft | null {
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw || raw.length > 100000) return null;
    const row: unknown = JSON.parse(raw);
    if (!isRecord(row) || row.version !== 1 || typeof row.savedAt !== 'number'
      || Date.now() - row.savedAt > TTL || row.savedAt > Date.now() + 60000) {
      sessionStorage.removeItem(key); return null;
    }
    for (const name of ['values', 'selected', 'unknowns', 'other', 'freeText']) {
      const fields = row[name];
      if (!isRecord(fields) || Object.keys(fields).length > 128) return null;
      if (!Object.entries(fields).every(([path, value]) => path.startsWith('/') && path.length < 240
        && (name === 'selected' ? Array.isArray(value) && value.length <= 256
          && value.every(item => typeof item === 'string' && item.length <= 120)
          : name === 'unknowns' || name === 'other' ? typeof value === 'boolean'
          : typeof value === 'string' && value.length <= 8000))) return null;
    }
    return row as unknown as ClarificationDraft;
  } catch { return null; }
}
export function saveDraft(key: string, draft: ClarificationDraft): boolean {
  try {
    const value = JSON.stringify({ version: 1, savedAt: Date.now(), ...draft });
    if (value.length > 100000) return false;
    sessionStorage.setItem(key, value);
    return true;
  } catch { return false; }
}
export function removeDraft(key: string): void {
  try { sessionStorage.removeItem(key); } catch { /* Storage may be disabled. */ }
}
export function clearClarificationDrafts(): void {
  try {
    const keys = Array.from({ length: sessionStorage.length }, (_, i) => sessionStorage.key(i));
    keys.filter((key): key is string => !!key && key.startsWith(PREFIX))
      .forEach(key => sessionStorage.removeItem(key));
  } catch { /* Logout still succeeds when storage is disabled. */ }
}
