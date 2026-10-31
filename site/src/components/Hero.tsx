import { REPO } from "../lib/links";

export function Hero() {
  return (
    <section className="hero" id="top">
      <div className="container">
        <h1>hebra.</h1>
        <p className="tagline">The right context. Nothing extra.</p>
        <p className="lede">
          A context engine for AI coding agents. Given a repository, a task, and a token
          budget, Hebra finds the relevant code, follows the relationships between symbols,
          and packs a context document that never exceeds the budget, with the file, lines,
          and reason for every fragment. It runs locally and never sends repository content anywhere.
        </p>
        <div className="actions">
          <a className="button primary" href="#explorer">
            Explore demonstration
          </a>
          <a className="button" href={REPO}>
            View GitHub
          </a>
        </div>
        <div className="install">
          <code>pip install "git+https://github.com/AnvitDevadiga/hebra"</code>
        </div>
      </div>
    </section>
  );
}
