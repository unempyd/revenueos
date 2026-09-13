/**
 * The workspace's governing mandate (RevenueOS-owned, not vendored).
 *
 * A workspace may carry `REVENUEOS_OPERATOR_MANDATE.md` (or `MANDATE.md`) at its root: the
 * document that states what the operation exists to achieve. The orchestrator reads it once at
 * start-up so the running service can say which mandate it operates under — the same file the
 * Python heartbeat derives the workspace objective from — and reports it on /health. Nothing here
 * interprets the text; that is the heartbeat's job.
 */
import { readFile, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';

export const MANDATE_FILES = ['REVENUEOS_OPERATOR_MANDATE.md', 'MANDATE.md'] as const;

export interface MandateInfo {
  path: string;
  chars: number;
  sha: string; // first 12 hex chars of sha256 over the file bytes
  title: string; // first non-empty line
  loadedAt: string;
}

export async function loadMandate(root: string): Promise<MandateInfo | null> {
  for (const name of MANDATE_FILES) {
    const path = join(root, name);
    try {
      const s = await stat(path);
      if (!s.isFile()) continue;
    } catch {
      continue;
    }
    const buf = await readFile(path);
    const text = buf.toString('utf8');
    const title = text.split(/\r?\n/).map((l) => l.trim()).find((l) => l.length > 0) ?? '';
    return {
      path,
      chars: text.length,
      sha: createHash('sha256').update(buf).digest('hex').slice(0, 12),
      title: title.replace(/^#+\s*/, ''),
      loadedAt: new Date().toISOString(),
    };
  }
  return null;
}
