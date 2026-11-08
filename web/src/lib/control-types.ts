// Types of the control API (docs/streams/admin.md, section 4). Shared by browser and server code.

export const SOURCE_KINDS = ["git", "jira", "confluence", "s3"] as const;
export const RUN_STATUSES = ["queued", "running", "succeeded", "failed", "canceled"] as const;
export const RUN_MODES = ["incremental", "full"] as const;
export type SourceKind = (typeof SOURCE_KINDS)[number];
export type RunStatus = (typeof RUN_STATUSES)[number];
export type RunMode = (typeof RUN_MODES)[number];

export type Run = {
  id: string;
  source_id: string | null;
  source_name: string;
  kind: SourceKind;
  status: RunStatus;
  trigger: string;
  mode: RunMode;
  dagster_run_id: string | null;
  dagster_url: string | null;
  stats: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
  ended_at: string | null;
  requested_by: string | null;
  log_tail?: string[];
};

export type Source = {
  id: string;
  kind: SourceKind;
  name: string;
  enabled: boolean;
  schedule: string | null;
  config: Record<string, unknown>;
  has_secret: boolean;
  secret_keys: string[];
  origin: string;
  last_run: Run | null;
  created_at: string;
  updated_at: string;
};

export type AuditEntry = {
  id: string;
  at: string | null;
  actor: string;
  action: string;
  target: string | null;
  detail: Record<string, unknown> | null;
};

/** Result of POST /sources/{id}/test (HTTP 200 also when the check fails). */
export type TestResult = { ok: boolean; message: string; details: Record<string, unknown> };

/** Error body of every non-2xx response. */
export type ApiError = { error: string };

export type MeResult = { user: string; is_admin: boolean };
