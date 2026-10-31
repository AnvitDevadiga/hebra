import { useState } from "react";
import { REPO } from "../lib/links";
import { MenuIcon } from "./Icons";
import { ThemeToggle } from "./ThemeToggle";

const LINKS = [
  ["#how", "How it works"],
  ["#explorer", "Explorer"],
  ["#benchmarks", "Benchmarks"],
  ["#memory", "Memory"],
] as const;

export function Header() {
  const [open, setOpen] = useState(false);
  return (
    <header className="header">
      <div className="container header-inner">
        <a className="wordmark" href="#top" aria-label="hebra, back to top">
          hebra.
        </a>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <nav id="site-nav" className={open ? "nav open" : "nav"} aria-label="Main">
            {LINKS.map(([href, label]) => (
              <a key={href} href={href} onClick={() => setOpen(false)}>
                {label}
              </a>
            ))}
            <a href={REPO}>GitHub</a>
          </nav>
          <ThemeToggle />
          <button
            type="button"
            className="icon-button menu-button"
            aria-label="Menu"
            aria-expanded={open}
            aria-controls="site-nav"
            onClick={() => setOpen(!open)}
          >
            <MenuIcon />
          </button>
        </div>
      </div>
    </header>
  );
}
