import { controlFetch } from "@/lib/control";
import { controlPath, csrfOk, requestOrigin, methodAllowed, readLimited, declaredTooLarge } from "@/lib/admin-proxy";

// Browser-facing proxy to the control API. It adds the caller's access token on the server and
// forwards only the allow-listed admin paths. Cookies and the browser Authorization header are
// never forwarded, and no upstream header except content-type is returned.

const err = (error: string, status: number) => Response.json({ error }, { status });

async function forward(req: Request, path: string[]) {
  const method = req.method.toUpperCase();
  const match = controlPath(path);
  if (!match) return err("not found", 404);
  if (!methodAllowed(match, method)) return err("method not allowed", 405);
  if (!csrfOk(method, req.headers, requestOrigin(req.headers, req.url))) return err("forbidden", 403);
  let body: string | undefined;
  if (method === "POST" || method === "PATCH") {
    if (declaredTooLarge(req.headers)) return err("body too large", 413);
    const bytes = await readLimited(req.body);
    if (bytes === null) return err("body too large", 413);
    body = bytes.byteLength ? new TextDecoder().decode(bytes) : undefined;
  }
  const res = await controlFetch<unknown>(match.path + new URL(req.url).search, { method, body });
  if (!res.ok) return err(res.error, res.status);
  if (res.status === 204) return new Response(null, { status: 204 }); // delete source: no body
  return Response.json(res.data, { status: res.status });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: Request, { params }: Ctx) {
  return forward(req, (await params).path);
}
export async function POST(req: Request, { params }: Ctx) {
  return forward(req, (await params).path);
}
export async function PATCH(req: Request, { params }: Ctx) {
  return forward(req, (await params).path);
}
export async function DELETE(req: Request, { params }: Ctx) {
  return forward(req, (await params).path);
}
