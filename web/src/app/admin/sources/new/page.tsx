import { SourceForm } from "@/components/admin/SourceForm";

export const metadata = { title: "Add source" };

export default async function NewSourcePage() {
  return (
    <>
      <h1>Add source</h1>
      <SourceForm />
    </>
  );
}
