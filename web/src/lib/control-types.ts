// Types of the control API (docs/streams/admin.md, section 4). Shared by browser and server code.

export type SourceKind = "git" | "jira" | "confluence" | "s3";
export type RunStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export type Run = {
  id: string;
  source_id: string | null;
  source_name: string;
  kind: SourceKind;
  status: RunStatus;
  trigger: string;
  mode: "incremental" | "full";
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
  ts?: string;
  created_at?: string;
  actor: string;
  action: string;
  target?: string | null;
  detail?: Record<string, unknown> | null;
  [key: string]: unknown;
};

export type MeResult = { user: string; is_admin: boolean };
