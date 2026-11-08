"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { adminApi } from "@/lib/admin-client";
import type { Source, SourceKind } from "@/lib/control-types";
import {
  buildConfig, KINDS, SCHEDULE_PRESETS, SECRET_KEYS, validateConfig, validateCron, validateName,
  type FormErrors, type FormValues,
} from "@/lib/admin-validation";

type Field = { name: string; label: string; hint?: string; multiline?: boolean };

// Kind specific fields (docs/streams/admin.md, section 5).
const CONFIG_FIELDS: Record<SourceKind, Field[]> = {
  git: [
    { name: "url", label: "Repository URL", hint: "An https:// URL. Do not put a token in the URL." },
    { name: "branch", label: "Branch", hint: "Default: main." },
  ],
  jira: [
    { name: "base_url", label: "Base URL", hint: "For example https://example.atlassian.net" },
    { name: "projects", label: "Projects (keys)", hint: "Project keys, separated by commas or new lines." },
  ],
  confluence: [
    { name: "base_url", label: "Base URL", hint: "For example https://example.atlassian.net/wiki" },
    { name: "spaces", label: "Spaces (keys)", hint: "Space keys, separated by commas or new lines." },
  ],
  s3: [
    { name: "endpoint", label: "Endpoint", hint: "Optional https:// URL for S3 compatible storage." },
    { name: "bucket", label: "Bucket" },
    { name: "prefix", label: "Prefix", hint: "Optional." },
    { name: "region", label: "Region", hint: "Optional." },
    { name: "include", label: "Include globs", hint: "Optional. Separate with commas or new lines, for example docs/**/*.md", multiline: true },
  ],
};

const SECRET_FIELDS: Record<string, string> = {
  token: "Token", email: "Email", api_token: "API token", access_key_id: "Access key ID", secret_access_key: "Secret access key",
};

const KIND_LABEL: Record<SourceKind, string> = { git: "Git repository", jira: "Jira", confluence: "Confluence", s3: "S3 bucket" };

function presetOf(schedule: string | null): string {
  if (schedule === null) return "0";
  const i = SCHEDULE_PRESETS.findIndex((p) => p.value === schedule);
  return i >= 0 ? String(i) : "custom";
}

function initialValues(source?: Source): FormValues {
  const v: FormValues = {};
  for (const [k, val] of Object.entries(source?.config ?? {})) v[k] = Array.isArray(val) ? val.join(", ") : String(val ?? "");
  return v;
}

type SecretMode = "keep" | "replace" | "clear";

type InputProps = {
  value: string; onChange: (v: string) => void; type?: string; hint?: string; error?: string;
  readOnly?: boolean; multiline?: boolean; autoComplete?: string;
};

function TextField({ id, label, ...props }: { id: string; label: string } & InputProps) {
  const hintId = props.hint ? `${id}-hint` : undefined;
  const errId = props.error ? `${id}-err` : undefined;
  const common = {
    id, name: id, value: props.value, readOnly: props.readOnly,
    onChange: (e: React.ChangeEvent<HTMLInputElement & HTMLTextAreaElement>) => props.onChange(e.target.value),
    "aria-describedby": [hintId, errId].filter(Boolean).join(" ") || undefined,
    "aria-invalid": props.error ? true : undefined,
  };
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {props.multiline ? <textarea {...common} rows={2} /> : <input {...common} type={props.type ?? "text"} autoComplete={props.autoComplete} />}
      {props.hint && <p id={hintId} className="muted small">{props.hint}</p>}
      {props.error && <p id={errId} className="field-error">{props.error}</p>}
    </div>
  );
}

export function SourceForm({ source }: { source?: Source }) {
  const router = useRouter();
  const editing = !!source;
  const [kind, setKind] = useState<SourceKind>(source?.kind ?? "git");
  const [name, setName] = useState(source?.name ?? "");
  const [values, setValues] = useState<FormValues>(initialValues(source));
  const [secret, setSecret] = useState<FormValues>({});
  const [secretMode, setSecretMode] = useState<SecretMode>(source?.has_secret ? "keep" : "replace");
  const [preset, setPreset] = useState(presetOf(source?.schedule ?? null));
  const [cron, setCron] = useState(source?.schedule && presetOf(source.schedule) === "custom" ? source.schedule : "");
  const [enabled, setEnabled] = useState(source?.enabled ?? true);
  const [errors, setErrors] = useState<FormErrors>({});
  const [busy, setBusy] = useState(false);
  const [apiError, setApiError] = useState("");
  const [saved, setSaved] = useState("");

  const set = (f: string, v: string) => setValues((o) => ({ ...o, [f]: v }));
  const secretKeys = SECRET_KEYS[kind];

  function schedule(): string | null {
    if (preset === "custom") return cron.trim() || null;
    return SCHEDULE_PRESETS[Number(preset)].value;
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setApiError("");
    setSaved("");
    const errs: FormErrors = validateConfig(kind, values);
    if (!editing) {
      const ne = validateName(name);
      if (ne) errs.name = ne;
    }
    if (preset === "custom") {
      const ce = validateCron(cron);
      if (ce) errs.cron = ce;
    }
    // Secret: sent only when the user typed a new one (create, or Replace on edit).
    let secretBody: Record<string, string> | undefined;
    if (secretMode === "clear") secretBody = {};
    else if (secretMode === "replace") {
      const filled = secretKeys.filter((k) => (secret[k] ?? "").length > 0);
      const mustFill = (editing && source!.has_secret) || filled.length > 0;
      if (mustFill) {
        for (const k of secretKeys) if (!(secret[k] ?? "")) errs[`secret.${k}`] = "This field is required.";
        secretBody = Object.fromEntries(filled.map((k) => [k, secret[k]]));
      }
    }
    setErrors(errs);
    if (Object.keys(errs).length) return;

    setBusy(true);
    const config = buildConfig(kind, values);
    if (editing) {
      const body: Record<string, unknown> = { config, schedule: schedule(), enabled };
      if (secretBody !== undefined) body.secret = secretBody;
      const res = await adminApi.patch<Source>(`sources/${source!.id}`, body);
      setBusy(false);
      if (!res.ok) return setApiError(res.error);
      setSaved("Saved.");
      setSecret({});
      setSecretMode(res.data?.has_secret ? "keep" : "replace");
      router.refresh();
    } else {
      const body: Record<string, unknown> = { kind, name, config, schedule: schedule(), enabled };
      if (secretBody !== undefined) body.secret = secretBody;
      const res = await adminApi.post<Source>("sources", body);
      setBusy(false);
      if (!res.ok) return setApiError(res.error);
      router.push(`/admin/sources/${res.data.id}`);
    }
  }

  return (
    <form onSubmit={submit} noValidate className="admin-form">
      <fieldset>
        <legend>General</legend>
        <TextField id="name" label="Name" value={name} onChange={setName} error={errors.name} readOnly={editing}
          hint={editing ? "The name cannot change." : "Lowercase slug. It cannot change later."} />
        <div className="field">
          <label htmlFor="kind">Kind</label>
          <select id="kind" name="kind" value={kind} disabled={editing} onChange={(e) => { setKind(e.target.value as SourceKind); setErrors({}); }}>
            {KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
          </select>
        </div>
      </fieldset>

      <fieldset>
        <legend>Connection</legend>
        {kind === "s3" && (
          <p role="note" className="notice">The S3 pipeline is not available yet. You can save and test an S3 source, but Run returns 501 (not implemented).</p>
        )}
        {CONFIG_FIELDS[kind].map((f) => (
          <TextField key={f.name} id={f.name} label={f.label} value={values[f.name] ?? ""} onChange={(v) => set(f.name, v)}
            hint={f.hint} error={errors[f.name]} multiline={f.multiline} />
        ))}
      </fieldset>

      <fieldset>
        <legend>Secret</legend>
        {editing && source!.has_secret && secretMode === "keep" && (
          <div className="row">
            <p>Secret: set ({source!.secret_keys.join(", ") || "values hidden"}). The value is never shown.</p>
            <button type="button" onClick={() => setSecretMode("replace")}>Replace secret</button>
            <button type="button" onClick={() => setSecretMode("clear")}>Clear secret</button>
          </div>
        )}
        {secretMode === "clear" && (
          <div className="row">
            <p role="status">The secret will be removed when you save.</p>
            <button type="button" onClick={() => setSecretMode("keep")}>Undo</button>
          </div>
        )}
        {secretMode === "replace" && (
          <>
            {secretKeys.map((k) => (
              <TextField key={k} id={`secret-${k}`} label={SECRET_FIELDS[k]} value={secret[k] ?? ""} type="password"
                onChange={(v) => setSecret((o) => ({ ...o, [k]: v }))} autoComplete="new-password" error={errors[`secret.${k}`]}
                hint={k === "token" ? "Optional for public repositories." : undefined} />
            ))}
            {editing && source!.has_secret && (
              <button type="button" onClick={() => { setSecretMode("keep"); setSecret({}); }}>Keep existing secret</button>
            )}
          </>
        )}
      </fieldset>

      <fieldset>
        <legend>Schedule</legend>
        <div className="field">
          <label htmlFor="schedule">Schedule</label>
          <select id="schedule" value={preset} onChange={(e) => setPreset(e.target.value)}>
            {SCHEDULE_PRESETS.map((p, i) => <option key={p.label} value={String(i)}>{p.label}</option>)}
            <option value="custom">Custom cron</option>
          </select>
        </div>
        {preset === "custom" && (
          <TextField id="cron" label="Cron expression (UTC)" value={cron} onChange={setCron} error={errors.cron}
            hint="Five fields: minute hour day month weekday. Example: 30 3 * * 1-5" />
        )}
        <label className="row" style={{ marginTop: 10 }}>
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          <span>Enabled (a disabled source does not run on schedule)</span>
        </label>
      </fieldset>

      <div className="row">
        <button type="submit" className="primary" disabled={busy}>{editing ? "Save changes" : "Create source"}</button>
        <span role="status" aria-live="polite">{busy ? "Saving..." : saved}</span>
      </div>
      {apiError && <p role="alert" className="error">Could not save: {apiError}</p>}
    </form>
  );
}
