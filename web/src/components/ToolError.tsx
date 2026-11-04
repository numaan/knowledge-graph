// A failed tool call: 404 means the tool is not registered on this server (another stream's tool, or off).
export function ToolError({ r, what }: { r: { status: number; error: string }; what: string }) {
  if (r.status === 404) return <p className="notice">{what} is not configured on this server.</p>;
  return <p className="error" role="alert">{what} failed: {r.error}</p>;
}
