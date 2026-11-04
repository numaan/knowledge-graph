"use client";

import { useEffect, useState } from "react";

type Theme = "system" | "light" | "dark";
const NEXT: Record<Theme, Theme> = { system: "light", light: "dark", dark: "system" };

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("system");

  useEffect(() => {
    const t = document.documentElement.dataset.theme;
    if (t === "light" || t === "dark") setTheme(t);
  }, []);

  function cycle() {
    const next = NEXT[theme];
    setTheme(next);
    if (next === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = next;
    try {
      if (next === "system") localStorage.removeItem("kg-theme");
      else localStorage.setItem("kg-theme", next);
    } catch {
      // storage blocked: the choice lasts for this page only
    }
  }

  return (
    <button type="button" onClick={cycle} aria-label={`Theme: ${theme}. Change theme`} title="Change theme">
      {theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"} <span className="small">{theme}</span>
    </button>
  );
}
