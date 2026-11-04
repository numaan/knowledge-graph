import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { AskChat } from "@/components/AskChat";
import { FeedbackButtons } from "@/components/FeedbackButtons";

vi.mock("next/link", () => ({ default: ({ href, children, ...p }: { href: string; children: React.ReactNode }) => <a href={href} {...p}>{children}</a> }));

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async (_url: string, _init: RequestInit) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("FeedbackButtons", () => {
  it("sends helpful at once", async () => {
    const fetch = mockFetch(202, { status: "accepted" });
    render(<FeedbackButtons nodeId="jira:PREC-1" />);
    await userEvent.click(screen.getByRole("button", { name: "Helpful" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Your feedback was sent");
    expect(JSON.parse(String(fetch.mock.calls[0][1].body))).toEqual({ node_id: "jira:PREC-1", kind: "helpful", comment: "" });
  });

  it("asks for a comment for wrong, from the keyboard", async () => {
    const fetch = mockFetch(202, {});
    render(<FeedbackButtons nodeId="n1" />);
    screen.getByRole("button", { name: "Wrong" }).focus();
    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "Wrong" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.type(screen.getByLabelText("What is wrong? (optional)"), "uses 7 days");
    await userEvent.click(screen.getByRole("button", { name: "Send feedback" }));
    await screen.findByRole("status");
    expect(JSON.parse(String(fetch.mock.calls[0][1].body))).toMatchObject({ kind: "wrong", comment: "uses 7 days" });
  });

  it("shows an error when the API rejects it", async () => {
    mockFetch(400, { error: "bad" });
    render(<FeedbackButtons nodeId="n1" />);
    await userEvent.click(screen.getByRole("button", { name: "Helpful" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not send feedback");
  });
});

describe("AskChat", () => {
  it("renders the answer with citation links", async () => {
    mockFetch(200, { data: { answer: "Done by f [1].", citations: [{ id: "precept:a.py#f", title: "f" }] } });
    render(<AskChat />);
    await userEvent.type(screen.getByLabelText("Your question"), "who does it?");
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    const cite = await screen.findByRole("link", { name: "Source 1: f" });
    expect(cite).toHaveAttribute("href", "/entity?id=precept%3Aa.py%23f");
    expect(screen.getByRole("group", { name: "Was this answer right?" })).toBeInTheDocument();
  });

  it("says not configured when the ask tool is missing", async () => {
    mockFetch(404, { error: 'tool "ask" is not configured on this server' });
    render(<AskChat />);
    await userEvent.type(screen.getByLabelText("Your question"), "hi{Enter}");
    expect(await screen.findByText(/The ask tool is not configured/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows server errors as alerts", async () => {
    mockFetch(422, { error: "LLM unavailable" });
    render(<AskChat />);
    await userEvent.type(screen.getByLabelText("Your question"), "hi{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("LLM unavailable");
  });
});
