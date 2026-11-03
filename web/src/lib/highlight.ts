import "server-only";

import { createHighlighterCore, type HighlighterCore } from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";

// Server-side syntax highlighting with explicit languages (static imports, so the standalone build
// traces them) and dual light/dark themes switched by CSS variables.

let highlighter: Promise<HighlighterCore> | undefined;

function get() {
  highlighter ??= createHighlighterCore({
    themes: [import("shiki/themes/github-light.mjs"), import("shiki/themes/github-dark.mjs")],
    langs: [
      import("shiki/langs/typescript.mjs"), import("shiki/langs/tsx.mjs"), import("shiki/langs/javascript.mjs"),
      import("shiki/langs/jsx.mjs"), import("shiki/langs/python.mjs"), import("shiki/langs/go.mjs"),
      import("shiki/langs/java.mjs"), import("shiki/langs/kotlin.mjs"), import("shiki/langs/csharp.mjs"),
      import("shiki/langs/sql.mjs"), import("shiki/langs/json.mjs"), import("shiki/langs/yaml.mjs"),
      import("shiki/langs/markdown.mjs"), import("shiki/langs/bash.mjs"), import("shiki/langs/html.mjs"),
      import("shiki/langs/css.mjs"), import("shiki/langs/vue.mjs"),
    ],
    engine: createJavaScriptRegexEngine(),
  });
  return highlighter;
}

export async function highlight(code: string, lang: string, startLine = 1): Promise<string> {
  const h = await get();
  return h.codeToHtml(code, {
    lang: h.getLoadedLanguages().includes(lang) ? lang : "text",
    themes: { light: "github-light", dark: "github-dark" },
    defaultColor: false,
    transformers: [
      {
        pre(node) {
          node.properties.style = `--start: ${startLine - 1}`;
          node.properties.tabindex = "0";
        },
      },
    ],
  });
}
