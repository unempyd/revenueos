/**
 * The RevenueOS orchestrator — the always-on service that runs every
 * scheduled automation for one workspace in a single process.
 *
 *   npm run worker                       long-running tick loop + status server
 *   npm run worker -- --once <name>      run one automation now and exit
 *
 * Reads data/automations.json, computes next-run times, executes each due
 * automation SERIALLY through the headless runner (one at a time — no
 * bursts, no log races), journals every run through the storage port, and
 * serves /health + /runs for the dashboard.
 *
 * Env: REVENUEOS_ROOT (workspace root, defaults to cwd), REVENUEOS_BIN
 * (override the `revenueos` CLI invocation, e.g. "uv run revenueos"),
 * REVENUEOS_WORKER_TOKEN (protects the status routes), TZ (the user's
 * timezone so cron hours mean their hours), PORT (defaults to 8790).
 */
import { JsonlStore } from '../storage/jsonlStore.js';
import { loadWorkerAutomations, type WorkerAutomation } from './automations.js';
import { nextRun } from './schedule.js';
import { runSkillHeadless, type RunOutcome } from './runner.js';
import { createWorkerServer, type WorkerHealth } from './server.js';

const TICK_MS = 30_000;
const RETRY_DELAY_MS = 60_000;

/** Same shape as WorkerHealth, but named for this project instead of Kairos. */
interface OrchestratorHealth extends Omit<WorkerHealth, 'service'> {
  service: 'revenueos-orchestrator';
}

async function main(): Promise<void> {
  const root = process.env.REVENUEOS_ROOT ?? process.cwd();
  const store = new JsonlStore(root);
  const startedAt = new Date().toISOString();

  // A 'running' record surviving boot means the last worker died mid-run.
  for (const stale of await store.listRuns({ status: 'running', limit: 20 })) {
    await store.recordRun({ ...stale, status: 'failed', finishedAt: startedAt, error: 'worker restarted mid-run' });
  }

  let automations: WorkerAutomation[] = await loadWorkerAutomations(root);
  const nextAt = new Map<string, Date>();
  const scheduleAll = (from: Date): void => {
    nextAt.clear();
    for (const automation of automations) {
      if (!automation.enabled) continue;
      try {
        nextAt.set(automation.name, nextRun(automation.schedule, from));
      } catch (error) {
        console.error(`worker: bad schedule on ${automation.name}: ${(error as Error).message}`);
      }
    }
  };
  scheduleAll(new Date());

  let running: string | null = null;

  const execute = async (automation: WorkerAutomation, attempt = 1): Promise<RunOutcome> => {
    running = automation.name;
    const startedRun = new Date().toISOString();
    const id = `${automation.name}-${startedRun}`;
    await store.recordRun({
      id,
      automation: automation.name,
      skill: automation.skill,
      startedAt: startedRun,
      status: 'running',
      source: 'worker',
    });
    console.log(`worker: ${automation.name} started (attempt ${attempt})`);
    const outcome = await runSkillHeadless({
      workspaceRoot: root,
      skill: automation.skill,
      workflow: automation.name,
    });
    await store.recordRun({
      id,
      automation: automation.name,
      skill: automation.skill,
      startedAt: startedRun,
      finishedAt: new Date().toISOString(),
      status: outcome.ok ? 'ok' : 'failed',
      summary: outcome.summary.slice(0, 500) || undefined,
      error: outcome.error,
      source: 'worker',
    });
    console.log(`worker: ${automation.name} ${outcome.ok ? 'ok' : `FAILED — ${outcome.error}`}`);
    running = null;
    if (!outcome.ok && outcome.retryable && attempt === 1) {
      console.log(`worker: ${automation.name} failure looks transient — one retry in ${RETRY_DELAY_MS / 1000}s`);
      await new Promise((resolve) => setTimeout(resolve, RETRY_DELAY_MS));
      return execute(automation, 2);
    }
    return outcome;
  };

  // One-shot mode: `tsx src/worker/index.ts --once <automation-name>` runs
  // that automation immediately and exits — used by tests and `revenueos
  // doctor` to prove the pathway works without standing up the tick loop.
  const onceIndex = process.argv.indexOf('--once');
  if (onceIndex !== -1) {
    const name = process.argv[onceIndex + 1];
    if (!name) {
      console.error('worker: --once requires an automation name');
      process.exit(1);
      return;
    }
    const automation = automations.find((a) => a.name === name);
    if (!automation) {
      console.error(`worker: no automation named "${name}" in data/automations.json`);
      process.exit(1);
      return;
    }
    const outcome = await execute(automation);
    process.exit(outcome.ok ? 0 : 1);
    return;
  }

  let ticking = false;
  const tick = async (): Promise<void> => {
    if (ticking) return;
    ticking = true;
    try {
      // Pick up edits to automations.json without a redeploy.
      automations = await loadWorkerAutomations(root);
      for (const automation of automations) {
        if (!automation.enabled) {
          nextAt.delete(automation.name);
          continue;
        }
        if (!nextAt.has(automation.name)) {
          try {
            nextAt.set(automation.name, nextRun(automation.schedule, new Date()));
          } catch {
            continue;
          }
        }
        const due = nextAt.get(automation.name)!;
        if (Date.now() >= due.getTime()) {
          // Reschedule from NOW before running: a missed window while the
          // worker was down or busy fires once, never in a backlog burst.
          nextAt.set(automation.name, nextRun(automation.schedule, new Date()));
          await execute(automation); // serial — the tick waits
        }
      }
    } finally {
      ticking = false;
    }
  };
  setInterval(() => void tick(), TICK_MS);

  const getHealth = (): OrchestratorHealth => ({
    service: 'revenueos-orchestrator',
    startedAt,
    timezone: process.env.TZ ?? 'UTC',
    automations: automations.map((a) => ({
      name: a.name,
      schedule: a.schedule,
      skill: a.skill,
      enabled: a.enabled,
      nextRun: nextAt.get(a.name)?.toISOString() ?? null,
    })),
    running,
  });

  const token = process.env.REVENUEOS_WORKER_TOKEN;
  if (!token) console.warn('worker: REVENUEOS_WORKER_TOKEN not set — /health and /runs are unauthenticated.');
  const port = Number(process.env.PORT ?? 8790);
  createWorkerServer({
    token,
    // server.ts is vendored from Kairos and types this as the literal
    // 'kairos-worker' — cast so the wire payload can say what this
    // service actually is without touching the vendored file.
    getHealth: getHealth as unknown as () => WorkerHealth,
    store,
    workspaceRoot: root,
  }).listen(port, () => {
    console.log(`revenueos-orchestrator up on :${port} — ${automations.filter((a) => a.enabled).length} automation(s) scheduled.`);
  });
}

void main();
