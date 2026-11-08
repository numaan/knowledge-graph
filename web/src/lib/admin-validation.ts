import type { SourceKind } from "./control-types";

// Field-level validation for the source form. Mirrors pipeline/kg_pipeline/control/validate.py;
// the backend stays the authority, this only gives fast feedback.

export const NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;
export const PROJECT_KEY_RE = /^[A-Za-z][A-Za-z0-9_~-]{0,63}$/;

export const KINDS: SourceKind[] = ["git", "jira", "confluence", "s3"];

export const SECRET_KEYS: Record<SourceKind, string[]> = {
  git: ["token"],
  jira: ["email", "api_token"],
  confluence: ["email", "api_token"],
  s3: ["access_key_id", "secret_access_key"],
};

export const SCHEDULE_PRESETS: { label: string; value: string | null }[] = [
  { label: "Manual only", value: null },
  { label: "Hourly", value: "0 * * * *" },
  { label: "Every 15 minutes", value: "*/15 * * * *" },
  { label: "Daily 02:00 UTC", value: "0 2 * * *" },
];

/** Returns an error text, or null when valid. */
export function validateName(name: string): string | null {
  return NAME_RE.test(name) ? null : "Use lowercase letters, digits and hyphens. Start with a letter or digit. Maximum 63 characters.";
}

export function validateUrl(value: string, required = true): string | null {
  const v = value.trim();
  if (!v) return required ? "This field is required." : null;
  let u: URL;
  try {
    u = new URL(v);
  } catch {
    return "Enter a full URL that starts with https://";
  }
  if (u.protocol !== "https:" || !u.hostname) return "The URL must start with https://";
  if (u.username || u.password) return "Do not put credentials in the URL. Use the secret fields.";
  return null;
}

/** Split a free text list on commas, spaces and new lines. Drops empty entries. */
export function parseList(text: string): string[] {
  return text.split(/[\s,]+/).filter(Boolean);
}

export function validateKeyList(text: string, label = "key"): string | null {
  const items = parseList(text);
  if (!items.length) return `Enter at least one ${label}.`;
  if (!items.every((i) => PROJECT_KEY_RE.test(i))) return `Each ${label} must start with a letter and use letters, digits, _ ~ or -.`;
  return null;
}

/** Empty string means manual only. A cron expression needs 5 fields (UTC). */
export function validateCron(value: string): string | null {
  const v = value.trim();
  if (!v) return null;
  const fields = v.split(/\s+/);
  if (fields.length !== 5) return "A cron expression has 5 fields: minute hour day month weekday.";
  if (!fields.every((f) => /^[0-9A-Za-z*/,\-?#LW]+$/.test(f))) return "The cron expression has an invalid character.";
  return null;
}

export type FormValues = Record<string, string>;
export type FormErrors = Record<string, string>;

/** Validate the config fields of a kind. Keys of the result are field names. */
export function validateConfig(kind: SourceKind, v: FormValues): FormErrors {
  const e: FormErrors = {};
  const need = (f: string) => {
    if (!(v[f] ?? "").trim()) e[f] = "This field is required.";
  };
  if (kind === "git") {
    const err = validateUrl(v.url ?? "");
    if (err) e.url = err;
  } else if (kind === "jira" || kind === "confluence") {
    const err = validateUrl(v.base_url ?? "");
    if (err) e.base_url = err;
    const f = kind === "jira" ? "projects" : "spaces";
    const le = validateKeyList(v[f] ?? "", kind === "jira" ? "project key" : "space key");
    if (le) e[f] = le;
  } else {
    need("bucket");
    const err = validateUrl(v.endpoint ?? "", false);
    if (err) e.endpoint = err;
  }
  return e;
}

/** Build the `config` object for the API from form values. */
export function buildConfig(kind: SourceKind, v: FormValues): Record<string, unknown> {
  const t = (f: string) => (v[f] ?? "").trim();
  if (kind === "git") return { url: t("url"), branch: t("branch") || "main" };
  if (kind === "jira") return { base_url: t("base_url"), projects: parseList(v.projects ?? "") };
  if (kind === "confluence") return { base_url: t("base_url"), spaces: parseList(v.spaces ?? "") };
  const out: Record<string, unknown> = { bucket: t("bucket") };
  if (t("endpoint")) out.endpoint = t("endpoint");
  if (t("prefix")) out.prefix = t("prefix");
  if (t("region")) out.region = t("region");
  const include = parseList(v.include ?? "");
  if (include.length) out.include = include;
  return out;
}
