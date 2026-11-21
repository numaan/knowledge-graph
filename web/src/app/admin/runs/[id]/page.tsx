import { notFound } from "next/navigation";
import { RunDetail } from "@/components/admin/RunDetail";
import { controlFetch } from "@/lib/control";
import type { Run } from "@/lib/control-types";

export const dynamic = "force-dynamic";

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const res = await controlFetch<Run>(`api/v1/runs/${encodeURIComponent(id)}`);
  if (!res.ok && res.status === 404) notFound();
  if (!res.ok) return <p role="alert" className="error">Could not load the run: {res.error}</p>;
  return <RunDetail initial={res.data} />;
}
