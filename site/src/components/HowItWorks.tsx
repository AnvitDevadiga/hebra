import { blob } from "../lib/links";

const STAGES = [
  ["Repository", "Your Git repository, read locally. Nothing is uploaded."],
  ["Source indexing", "Tracked files, Python symbols and chunks, stored in SQLite."],
  ["Retrieval and code relationships", "Keyword ranking (BM25), optionally expanded through resolved calls and references."],
  ["Budget-aware context selection", "Fragments chosen until the exact token budget is reached, never beyond."],
  ["Source-grounded memory", "Optional facts tied to source lines, re-checked on every read."],
  ["Inspectable context package", "Markdown for the agent, JSON with the reason for every decision."],
] as const;

export function HowItWorks() {
  return (
    <section id="how">
      <div className="container">
        <p className="eyebrow">How it works</p>
        <h2>From repository to a bounded, explained context.</h2>
        <p className="lede">
          Who it is for: developers who give coding agents context and want to know exactly
          what went in, why, and whether it is still the current code.
        </p>
        <ol className="pipeline" aria-label="Hebra pipeline">
          {STAGES.map(([title, text]) => (
            <li key={title}>
              <strong>{title}</strong>
              <span>{text}</span>
            </li>
          ))}
        </ol>
        <details className="technical">
          <summary>Technical details</summary>
          <ul>
            <li>Indexing uses <code>git ls-files</code>, skips credential-like files, rejects symlinks, and updates incrementally in one SQLite transaction.</li>
            <li>The graph covers Python only. Calls it cannot resolve statically are recorded as uncertain, dynamic, or unresolved, not guessed.</li>
            <li><code>bm25_graph</code> merges keyword and graph rankings with reciprocal rank fusion. There is no semantic or embedding search.</li>
            <li>Every fragment is accepted only after the whole rendered document is re-counted with the real tokenizer (tiktoken <code>o200k_base</code> by default).</li>
            <li>Every file behind a result is re-hashed; files changed since indexing are excluded.</li>
          </ul>
          <p className="note">
            Read more: <a href={blob("docs/retrieval.md")}>retrieval</a>,{" "}
            <a href={blob("docs/optimizer.md")}>context selection</a>,{" "}
            <a href={blob("docs/memory.md")}>memory</a>,{" "}
            <a href={blob("docs/security.md")}>security model</a>.
          </p>
        </details>
      </div>
    </section>
  );
}
