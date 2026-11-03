import type { Metadata } from "next";
import { AskChat } from "@/components/AskChat";

export const metadata: Metadata = { title: "Ask" };

export default function AskPage() {
  return (
    <>
      <h1>Ask</h1>
      <p className="muted">Answers come from code, tickets and documents. Each claim links to its source.</p>
      <AskChat />
    </>
  );
}
