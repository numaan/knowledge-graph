import { handlers, oidcEnabled } from "@/auth";

const off = () => Response.json({ error: "login is off (local mode)" }, { status: 404 });

export const GET = oidcEnabled ? handlers.GET : off;
export const POST = oidcEnabled ? handlers.POST : off;
