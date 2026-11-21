import { buildConfig, parseList, validateConfig, validateCron, validateKeyList, validateName, validateUrl } from "@/lib/admin-validation";

describe("validateName", () => {
  it("accepts slugs", () => {
    for (const n of ["a", "my-repo", "0abc", "a".repeat(63)]) expect(validateName(n)).toBeNull();
  });
  it("rejects bad names", () => {
    for (const n of ["", "-a", "A", "a_b", "a b", "a".repeat(64)]) expect(validateName(n)).not.toBeNull();
  });
});

describe("validateUrl", () => {
  it("requires https", () => {
    expect(validateUrl("https://github.com/o/r.git")).toBeNull();
    expect(validateUrl("http://x.org")).toMatch(/https/);
    expect(validateUrl("not a url")).not.toBeNull();
    expect(validateUrl("https://user:pw@x.org")).toMatch(/credentials/);
  });
  it("handles empty by required flag", () => {
    expect(validateUrl("")).toMatch(/required/);
    expect(validateUrl("  ", false)).toBeNull();
  });
});

describe("lists", () => {
  it("parses commas, spaces and lines", () => {
    expect(parseList("A, B\nC  D,,")).toEqual(["A", "B", "C", "D"]);
  });
  it("validates keys", () => {
    expect(validateKeyList("PROJ, OPS")).toBeNull();
    expect(validateKeyList("")).toMatch(/at least one/);
    expect(validateKeyList("1ABC")).not.toBeNull();
  });
});

describe("validateCron", () => {
  it("accepts empty and 5 fields", () => {
    expect(validateCron("")).toBeNull();
    expect(validateCron("*/15 * * * *")).toBeNull();
    expect(validateCron("0 2 * * 1-5")).toBeNull();
  });
  it("rejects other shapes", () => {
    expect(validateCron("* * * *")).toMatch(/5 fields/);
    expect(validateCron("* * * * * *")).toMatch(/5 fields/);
    expect(validateCron("a;b * * * *")).toMatch(/invalid/);
  });
});

describe("validateConfig and buildConfig", () => {
  it("git", () => {
    expect(validateConfig("git", { url: "" }).url).toBeTruthy();
    expect(validateConfig("git", { url: "https://h/r" })).toEqual({});
    expect(buildConfig("git", { url: " https://h/r ", branch: "" })).toEqual({ url: "https://h/r", branch: "main" });
  });
  it("jira and confluence", () => {
    expect(Object.keys(validateConfig("jira", { base_url: "http://x", projects: "" }))).toEqual(["base_url", "projects"]);
    expect(validateConfig("confluence", { base_url: "https://x", spaces: "ENG" })).toEqual({});
    expect(buildConfig("jira", { base_url: "https://x", projects: "A, B" })).toEqual({ base_url: "https://x", projects: ["A", "B"] });
    expect(buildConfig("confluence", { base_url: "https://x", spaces: "ENG" })).toEqual({ base_url: "https://x", spaces: ["ENG"] });
  });
  it("s3 omits empty optional keys", () => {
    expect(validateConfig("s3", {}).bucket).toBeTruthy();
    expect(validateConfig("s3", { bucket: "b", endpoint: "http://x" }).endpoint).toBeTruthy();
    expect(buildConfig("s3", { bucket: "b", prefix: "", include: "*.md, *.txt" })).toEqual({ bucket: "b", include: ["*.md", "*.txt"] });
  });
});
