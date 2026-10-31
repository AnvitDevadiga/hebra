import { useState } from "react";
import { applyTheme, currentTheme, type Theme } from "../lib/theme";
import { MoonIcon, SunIcon } from "./Icons";

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(currentTheme);
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      className="icon-button"
      aria-label={next === "dark" ? "Switch to dark theme" : "Switch to light theme"}
      onClick={() => {
        applyTheme(next);
        setTheme(next);
      }}
    >
      {next === "dark" ? <MoonIcon /> : <SunIcon />}
    </button>
  );
}
