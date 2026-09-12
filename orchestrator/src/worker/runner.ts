/**
 * Headless skill runner — the worker's engine room. Unlike Kairos (which
 * ran the Agent SDK in-process), RevenueOS's automations are the Python
 * CLI: this module spawns it as a child process, watches it with a
 * timeout, and turns its stdout into a RunOutcome the scheduler can
 * journal.
 *
 * Supervision (same shape as Kairos' runner): a watchdog timeout kills a
 * runaway run (SIGTERM, then SIGKILL if it won't go), and failures are
 * classified so the scheduler retries transient ones (rate limits,
 * network blips, 5xx) exactly once instead of blindly retrying real bugs.
 */
import { spawn } from 'node:child_process';

export interface RunOutcome {
  ok: boolean;
  summary: string;
  error?: string;
  /** Rate limits, network blips, 5xx, timeouts — worth one retry. */
  retryable?: boolean;
}

const TRANSIENT_PATTERNS = [
  /rate.?limit/i,
  /\b429\b/,
  // 5xx only in a status-ish context — "pid 511" is not a server error.
  /(status|failed|error|code)\D{0,10}\b5\d{2}\b/i,
  /timed?.?out/i,
  /econnreset|econnrefused|enotfound|eai_again|etimedout|epipe/i,
  /network|socket hang up/i,
  /overloaded/i,
];

export function isTransientError(message: string): boolean {
  return TRANSIENT_PATTERNS.some((pattern) => pattern.test(message));
}

export const DEFAULT_RUN_TIMEOUT_MS = 20 * 60_000;

/** How long a killed process gets to exit on SIGTERM before SIGKILL. */
const SIGKILL_GRACE_MS = 5_000;

export interface RunSkillHeadlessOptions {
  workspaceRoot: string;
  skill: string;
  /** Names the run — passed through as REVENUEOS_WORKFLOW. */
  workflow: string;
  timeoutMs?: number;
  /** Override the spawned argv — used by tests; defaults to the CLI below. */
  command?: string[];
}

/** The one JSON line the CLI prints to stdout when it finishes. */
interface SkillRunPayload {
  ok: boolean;
  summary: string;
  actions_created?: number;
  error?: string;
}

function defaultCommand(skill: string): string[] {
  // REVENUEOS_BIN lets a workspace point at "uv run revenueos" instead of
  // a globally installed "revenueos" — everything after it is unchanged.
  const override = process.env.REVENUEOS_BIN?.trim();
  const base = override ? override.split(/\s+/) : ['revenueos'];
  return [...base, 'run', skill, '--json'];
}

function lastNonEmptyLine(text: string): string | null {
  const lines = text.split('\n').map((line) => line.trim()).filter(Boolean);
  return lines.length ? lines[lines.length - 1]! : null;
}

function parsePayload(stdout: string): SkillRunPayload | null {
  const line = lastNonEmptyLine(stdout);
  if (!line) return null;
  try {
    const parsed = JSON.parse(line) as SkillRunPayload;
    return parsed && typeof parsed.ok === 'boolean' ? parsed : null;
  } catch {
    return null;
  }
}

export async function runSkillHeadless(opts: RunSkillHeadlessOptions): Promise<RunOutcome> {
  const timeoutMs = opts.timeoutMs ?? DEFAULT_RUN_TIMEOUT_MS;
  const command = opts.command ?? defaultCommand(opts.skill);
  const [bin, ...args] = command;
  if (!bin) {
    return { ok: false, summary: '', error: 'runSkillHeadless: empty command', retryable: false };
  }

  return new Promise<RunOutcome>((resolve) => {
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    let settled = false;

    const child = spawn(bin, args, {
      cwd: opts.workspaceRoot,
      env: {
        ...process.env,
        REVENUEOS_WORKFLOW: opts.workflow,
      },
    });

    let killTimer: NodeJS.Timeout | undefined;
    const watchdog = setTimeout(() => {
      timedOut = true;
      child.kill('SIGTERM');
      killTimer = setTimeout(() => {
        child.kill('SIGKILL');
      }, SIGKILL_GRACE_MS);
    }, timeoutMs);

    const finish = (outcome: RunOutcome): void => {
      if (settled) return;
      settled = true;
      clearTimeout(watchdog);
      if (killTimer) clearTimeout(killTimer);
      resolve(outcome);
    };

    child.stdout?.on('data', (chunk: Buffer) => {
      stdout += chunk.toString();
    });
    child.stderr?.on('data', (chunk: Buffer) => {
      stderr += chunk.toString();
    });

    child.on('error', (error) => {
      const message = error.message;
      finish({ ok: false, summary: '', error: message, retryable: isTransientError(message) });
    });

    child.on('close', (code) => {
      if (timedOut) {
        finish({
          ok: false,
          summary: '',
          error: `Run exceeded ${Math.round(timeoutMs / 60_000)} minute(s) — timeout, process killed.`,
          retryable: false,
        });
        return;
      }

      const payload = parsePayload(stdout);
      if (code !== 0 || !payload) {
        const tail = stderr.trim().slice(-500);
        const error = tail || `process exited with code ${code ?? 'null'} and produced no parseable result`;
        finish({ ok: false, summary: '', error, retryable: isTransientError(error) });
        return;
      }

      finish({
        ok: payload.ok,
        summary: payload.summary ?? '',
        error: payload.error,
        retryable: payload.ok ? undefined : isTransientError(payload.error ?? ''),
      });
    });
  });
}
