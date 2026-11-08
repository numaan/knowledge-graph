import { AdminNav } from "@/components/admin/AdminNav";
import { getMe } from "@/lib/me";

export const metadata = { title: "Admin" };

// Guard: children (and their control API calls) render only for an admin.
export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const me = await getMe(5000);
  if (me.state === "forbidden") {
    return (
      <div className="notice" role="alert">
        <h1>Admin only</h1>
        <p>Your account is not in the admin group. Ask an administrator for access.</p>
      </div>
    );
  }
  if (me.state === "unavailable") {
    return (
      <div className="error" role="alert">
        <h1>Control API unavailable</h1>
        <p>The control service did not answer. Wait a moment and try again (reload this page).</p>
      </div>
    );
  }
  return (
    <>
      <AdminNav />
      {children}
    </>
  );
}
