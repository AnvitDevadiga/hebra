import { memory } from "../lib/data";
import { blob } from "../lib/links";

export function MemoryDemo() {
  return (
    <section id="memory">
      <div className="container">
        <p className="eyebrow">Source-grounded memory</p>
        <h2>Facts stay tied to the code that supports them.</h2>
        <p className="lede">
          A memory record stores the exact source lines it relies on and their SHA-256. Every
          read re-checks them, so a record whose code changed is reported as stale and kept
          out of packed context. Below is a real record from <code>hebra memory</code>, followed
          through two edits of the example repository (its random id is replaced for display).
        </p>
        <ol className="steps" aria-label="Memory record over time" style={{ listStyle: "none", padding: 0 }}>
          {memory.steps.map((step, i) => {
            const r = step.record;
            const ref = r.refs[0];
            return (
              <li key={step.title} className="card">
                <div className="fragment-head">
                  <h3 style={{ margin: 0 }}>
                    {i + 1}. {step.title}
                  </h3>
                  <span className={r.status === "stale" ? "status stale" : "status"}>{r.status}</span>
                </div>
                <p style={{ marginTop: 8 }}>{step.explanation}</p>
                <dl className="record">
                  <dt>content</dt>
                  <dd>{r.content}</dd>
                  <dt>provenance</dt>
                  <dd>{r.provenance}</dd>
                  <dt>reference</dt>
                  <dd>
                    {ref.path}:{ref.start_line}-{ref.end_line} ({ref.status}
                    {ref.detail ? `: ${ref.detail}` : ""})
                  </dd>
                  <dt>span hash</dt>
                  <dd>{ref.span_sha256.slice(0, 16)}…</dd>
                  <dt>revision</dt>
                  <dd>{r.revision_note || `created at ${r.git_revision?.slice(0, 12)}`}</dd>
                </dl>
              </li>
            );
          })}
        </ol>
        <p className="note">
          Decisions are stored as user decisions, never as verified source facts, and records
          without references are never included as current. See{" "}
          <a href={blob("docs/memory.md")}>docs/memory.md</a>.
        </p>
      </div>
    </section>
  );
}
