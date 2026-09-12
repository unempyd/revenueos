import { describe, expect, it } from 'vitest';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { nextRun, parseCron } from '../src/worker/schedule.js';
import { isTransientError, runSkillHeadless } from '../src/worker/runner.js';
import { loadWorkerAutomations, upsertWorkerAutomation } from '../src/worker/automations.js';

async function tmpRoot(): Promise<string> {
  return mkdtemp(join(tmpdir(), 'revenueos-worker-'));
}

// nextRun works in the process's local time on purpose (the worker runs
// with TZ set to the user's zone) — so tests build and assert LOCAL dates.
const local = (y: number, mo: number, d: number, h: number, mi: number) => new Date(y, mo - 1, d, h, mi);
const stamp = (date: Date) =>
  `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()} ${date.getHours()}:${String(date.getMinutes()).padStart(2, '0')}`;

describe('cron schedule math (local time)', () => {
  it('daily at 10:00', () => {
    expect(stamp(nextRun('0 10 * * *', local(2026, 7, 18, 9, 59)))).toBe('2026-7-18 10:00');
    expect(stamp(nextRun('0 10 * * *', local(2026, 7, 18, 10, 0)))).toBe('2026-7-19 10:00');
  });

  it('list of hours (engagement-sweep: 9,15,21)', () => {
    expect(stamp(nextRun('0 9,15,21 * * *', local(2026, 7, 18, 9, 0)))).toBe('2026-7-18 15:00');
    expect(stamp(nextRun('0 9,15,21 * * *', local(2026, 7, 18, 22, 0)))).toBe('2026-7-19 9:00');
  });

  it('day-of-week (Sunday 17:00) — 2026-07-18 is a Saturday', () => {
    expect(stamp(nextRun('0 17 * * 0', local(2026, 7, 18, 12, 0)))).toBe('2026-7-19 17:00');
  });

  it('day 7 normalizes to Sunday, steps and ranges work', () => {
    expect(stamp(nextRun('0 17 * * 7', local(2026, 7, 18, 12, 0)))).toBe('2026-7-19 17:00');
    expect(stamp(nextRun('*/15 * * * *', local(2026, 7, 18, 10, 7)))).toBe('2026-7-18 10:15');
    expect(stamp(nextRun('0 9-11 * * *', local(2026, 7, 18, 10, 30)))).toBe('2026-7-18 11:00');
  });

  it('rejects malformed expressions loudly', () => {
    expect(() => parseCron('0 10 * *')).toThrow(/5 fields/);
    expect(() => parseCron('0 25 * * *')).toThrow(/hour/);
    expect(() => parseCron('0 10 * * MON')).toThrow(/day-of-week/);
  });
});

describe('transient error classification (retry once, not forever)', () => {
  it('flags rate limits, 5xx, network blips, timeouts', () => {
    for (const message of [
      'Rate limit exceeded, retry after 60s',
      'CreatorOS request failed (503)',
      'fetch failed: ECONNRESET',
      'request timed out',
      'Overloaded',
    ]) {
      expect(isTransientError(message)).toBe(true);
    }
  });

  it('does not flag real bugs as transient', () => {
    for (const message of [
      'A funnel needs a DM message — that is the whole point of the funnel.',
      "That endpoint isn't part of RevenueOS.",
      'Comment replies are not supported on TikTok',
    ]) {
      expect(isTransientError(message)).toBe(false);
    }
  });
});

describe('runSkillHeadless (spawns the revenueos CLI)', () => {
  it('parses the last stdout line as the JSON result on a clean exit', async () => {
    const outcome = await runSkillHeadless({
      workspaceRoot: process.cwd(),
      skill: 'discover',
      workflow: 'test-discover',
      command: [
        'node',
        '-e',
        "console.log('starting run'); console.log(JSON.stringify({ok: true, summary: 'found 3 leads', actions_created: 3}));",
      ],
    });
    expect(outcome.ok).toBe(true);
    expect(outcome.summary).toBe('found 3 leads');
    expect(outcome.error).toBeUndefined();
  });

  it('flags a non-zero exit with ECONNRESET on stderr as retryable', async () => {
    const outcome = await runSkillHeadless({
      workspaceRoot: process.cwd(),
      skill: 'discover',
      workflow: 'test-econnreset',
      command: ['node', '-e', "console.error('fetch failed: ECONNRESET'); process.exit(1);"],
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.error).toContain('ECONNRESET');
    expect(outcome.retryable).toBe(true);
  });

  it('kills a run that outlives its timeout and reports it as such (not retryable)', async () => {
    const outcome = await runSkillHeadless({
      workspaceRoot: process.cwd(),
      skill: 'discover',
      workflow: 'test-timeout',
      timeoutMs: 300,
      command: ['node', '-e', 'setTimeout(() => { process.exit(0); }, 5000);'],
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.error?.toLowerCase()).toContain('timeout');
    expect(outcome.retryable).toBe(false);
  }, 10_000);
});

describe('worker automations file round trip', () => {
  it('upsertWorkerAutomation writes data/automations.json and loadWorkerAutomations reads it back', async () => {
    const root = await tmpRoot();
    await upsertWorkerAutomation(root, {
      name: 'discover',
      schedule: '0 7 * * *',
      skill: 'discover',
      enabled: true,
      description: 'Daily lead discovery sweep.',
    });
    await upsertWorkerAutomation(root, {
      name: 'monitor',
      schedule: '0 */6 * * *',
      skill: 'monitor',
      enabled: true,
      description: 'Every-6h health monitor.',
    });
    const automations = await loadWorkerAutomations(root);
    expect(automations).toHaveLength(2);
    expect(automations.find((a) => a.name === 'discover')?.schedule).toBe('0 7 * * *');
    expect(automations.find((a) => a.name === 'monitor')?.skill).toBe('monitor');
  });
});
