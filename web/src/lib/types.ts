// Shapes of the kg-mcp REST layer (mcp/rest.go) and of the tool results the UI reads.

export type Props = Record<string, unknown>;

export interface GraphNode {
  labels: string[];
  properties: Props;
}

export interface Neighbor {
  id: string;
  labels: string[];
  name: string | null;
  path?: string | null;
  distance: number;
  via: string[];
}

export interface NodeResponse {
  node: GraphNode;
  neighbors: { results: Neighbor[]; note?: string };
}

export interface ToolInfo {
  name: string;
  title?: string;
  description: string;
  input_schema: unknown;
}

/** Result of a REST call: data, or an error with the HTTP status (404 = tool not configured). */
export type Result<T> = { ok: true; data: T } | { ok: false; status: number; error: string };

export interface CodeHit {
  path: string;
  start_line?: number;
  end_line?: number;
  symbol_id: string | null;
  symbol: string;
  kind: string;
  role?: string | null;
  route?: string | null;
  concepts?: string[];
  callers?: string[];
  callees?: string[];
  tests?: string[];
  code?: string;
  score?: number;
}

export interface KnowledgeHit {
  id: string;
  kind: string; // Ticket or Section
  key?: string | null;
  type?: string | null;
  status?: string | null;
  title: string;
  url?: string | null;
  text?: string;
}

export interface Citation {
  n: number;
  id?: string;
  title: string;
  url?: string;
  kind?: string;
  snippet?: string;
}

export interface Answer {
  answer: string;
  citations: Citation[];
  confidence?: number;
  note?: string;
}

export interface RequirementRow {
  id: string;
  ref?: string | null;
  statement: string;
  type?: string | null;
  source?: string | null;
  section?: string | null;
  repo?: string | null;
  verdict?: string | null;
  confidence?: number | null;
  gaps?: string[] | string | null;
  checked_at?: string | null;
  evidence: { link: string; id: string; symbol: string; path: string; confidence?: number }[];
  tests: number;
}

export interface RequirementsResponse {
  counts: Record<string, number>;
  total: number;
  requirements: RequirementRow[];
}

export const FEEDBACK_KINDS = ["helpful", "wrong", "outdated", "missing"] as const;
export type FeedbackKind = (typeof FEEDBACK_KINDS)[number];
