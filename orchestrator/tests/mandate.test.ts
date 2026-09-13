import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { loadMandate } from '../src/worker/mandate.js';

describe('loadMandate', () => {
  it('returns null when the workspace has no mandate file', async () => {
    const root = await mkdtemp(join(tmpdir(), 'revenueos-mandate-'));
    expect(await loadMandate(root)).toBeNull();
  });

  it('reads REVENUEOS_OPERATOR_MANDATE.md and reports its title, size and digest', async () => {
    const root = await mkdtemp(join(tmpdir(), 'revenueos-mandate-'));
    const text = 'RevenueOS — Non-Negotiable Commercial Operating Mandate\n\nPurpose\n\nGenerate real revenue.\n';
    await writeFile(join(root, 'REVENUEOS_OPERATOR_MANDATE.md'), text);
    const m = await loadMandate(root);
    expect(m).not.toBeNull();
    expect(m?.title).toBe('RevenueOS — Non-Negotiable Commercial Operating Mandate');
    expect(m?.chars).toBe(text.length);
    expect(m?.sha).toMatch(/^[0-9a-f]{12}$/);
    expect(m?.path.endsWith('REVENUEOS_OPERATOR_MANDATE.md')).toBe(true);
  });

  it('falls back to MANDATE.md', async () => {
    const root = await mkdtemp(join(tmpdir(), 'revenueos-mandate-'));
    await writeFile(join(root, 'MANDATE.md'), '# Grow bookings\n');
    expect((await loadMandate(root))?.title).toBe('Grow bookings');
  });
});
