import Link from "next/link";
import { SearchForm } from "@/components/SearchForm";

export default function Home() {
  return (
    <>
      <h1>Knowledge base</h1>
      <p className="muted">Code, tickets, documents, requirements and the people behind them, in one graph.</p>
      <SearchForm autoFocus />
      <div className="grid-2" style={{ marginTop: 24 }}>
        <section className="card">
          <h2 style={{ marginTop: 0 }}><Link href="/ask">Ask a question</Link></h2>
          <p className="muted">Get an answer with numbered citations to code, tickets and documents.</p>
        </section>
        <section className="card">
          <h2 style={{ marginTop: 0 }}><Link href="/domain">Domain overview</Link></h2>
          <p className="muted">Capabilities, concepts, business rules and endpoints per repository.</p>
        </section>
        <section className="card">
          <h2 style={{ marginTop: 0 }}><Link href="/requirements">Requirement coverage</Link></h2>
          <p className="muted">Verdicts and evidence for each checked requirement.</p>
        </section>
      </div>
    </>
  );
}
