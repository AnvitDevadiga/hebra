import { useState } from "react";
import { explorer, runKey, type Fragment } from "../lib/data";
import { blob } from "../lib/links";
import { Choices } from "./Choices";
import { SourceView } from "./SourceView";

const fragKey = (f: Fragment) => `${f.path}:${f.start_line}`;
const label = (f: Fragment) => f.symbol ?? f.kind;

export function Explorer() {
  const [task, setTask] = useState(explorer.tasks[0].id);
  const [budget, setBudget] = useState(explorer.budgets[1]);
  const [method, setMethod] = useState("baseline");
  const [focus, setFocus] = useState<Fragment | null>(null);

  const run = explorer.runs[runKey(task, budget, method)];
  const other = explorer.runs[runKey(task, budget, method === "baseline" ? "optimized" : "baseline")];
  const mine = new Set(run.selected.map(fragKey));
  const theirs = new Set(other.selected.map(fragKey));
  const onlyHere = run.selected.filter((f) => !theirs.has(fragKey(f)));
  const onlyThere = other.selected.filter((f) => !mine.has(fragKey(f)));
  const shown = focus ?? run.selected[0] ?? null;

  return (
    <section id="explorer">
      <div className="container">
        <p className="eyebrow">Context explorer</p>
        <h2>See what goes into the context, and why.</h2>
        <p className="lede">
          These are real outputs of <code>hebra pack</code> (version {explorer.hebra_version},
          tokenizer {explorer.tokenizer}), precomputed for every combination below on a small
          synthetic example repository ({" "}
          <a href={blob("examples/demo-shop")}>examples/demo-shop</a>). Choosing an option
          shows a stored result; nothing runs in your browser.
        </p>

        <div className="explorer">
          <div className="panel">
            <Choices
              legend="Task"
              name="task"
              value={task}
              options={explorer.tasks.map((t) => [t.id, t.text])}
              onChange={(v) => {
                setTask(v);
                setFocus(null);
              }}
            />
            <Choices
              legend="Token budget"
              name="budget"
              value={String(budget)}
              options={explorer.budgets.map((b) => [String(b), `${b} tokens`])}
              onChange={(v) => {
                setBudget(Number(v));
                setFocus(null);
              }}
            />
            <Choices
              legend="Method"
              name="method"
              value={method}
              options={Object.entries(explorer.methods)}
              onChange={(v) => {
                setMethod(v);
                setFocus(null);
              }}
            />

            <div
              className="budget-bar"
              role="img"
              aria-label={`${run.used} of ${run.budget} tokens used: ${run.header_tokens} header, ${run.used - run.header_tokens} fragments`}
            >
              <span className="header-part" style={{ width: `${(run.header_tokens / run.budget) * 100}%` }} />
              {run.selected.map((f) => (
                <span
                  key={fragKey(f)}
                  className="fragment-part"
                  style={{ width: `${((f.tokens ?? 0) / run.budget) * 100}%` }}
                />
              ))}
            </div>
            <div className="stats" aria-live="polite">
              <span>
                Used <b>{run.used}</b> / {run.budget} tokens
              </span>
              <span>
                Header <b>{run.header_tokens}</b>
              </span>
              <span>
                Selected <b>{run.selected.length}</b>
              </span>
              <span>
                Excluded <b>{run.excluded.length}</b>
              </span>
            </div>
          </div>

          <div className="two-col">
            <div className="panel">
              <h3>Selected fragments</h3>
              <ul className="fragments">
                {run.selected.map((f) => (
                  <li key={fragKey(f)}>
                    <div className="fragment-head">
                      <button type="button" className="ref-button" onClick={() => setFocus(f)}>
                        {f.path}:{f.start_line}-{f.end_line}
                      </button>
                      <span className="tag">{label(f)}</span>
                      <span className="tag">{f.tokens} tokens</span>
                      {f.sources?.includes("graph") && !f.sources.includes("bm25") && (
                        <span className="tag">via graph</span>
                      )}
                    </div>
                    <p className="reason">{f.reason}</p>
                  </li>
                ))}
              </ul>
              {run.excluded.length > 0 && (
                <>
                  <h3 style={{ marginTop: 20 }}>Excluded candidates</h3>
                  <ul className="fragments">
                    {run.excluded.map((f) => (
                      <li key={fragKey(f)}>
                        <div className="fragment-head">
                          <button type="button" className="ref-button" onClick={() => setFocus(f)}>
                            {f.path}:{f.start_line}-{f.end_line}
                          </button>
                          <span className="tag">{label(f)}</span>
                        </div>
                        <p className="reason">{f.reason}</p>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>

            <div className="panel">
              <h3>Source</h3>
              {shown ? <SourceView fragment={shown} /> : <p className="note">Nothing selected.</p>}
            </div>
          </div>

          <div className="panel">
            <h3>Compared with the other method</h3>
            {onlyHere.length === 0 && onlyThere.length === 0 ? (
              <p className="note">Both methods select the same fragments for this task and budget.</p>
            ) : (
              <p className="note">
                Only in this selection: {onlyHere.map(label).join(", ") || "none"}. Only in{" "}
                {explorer.methods[method === "baseline" ? "optimized" : "baseline"]}:{" "}
                {onlyThere.map(label).join(", ") || "none"}.
              </p>
            )}
            <p className="note">
              On the recorded benchmark, the optimized method did not outperform the default (see
              Benchmarks). Differences here illustrate behaviour, not quality.
            </p>
            <details>
              <summary>Packed Markdown ({run.used} tokens)</summary>
              <pre className="code" style={{ padding: 14 }}>
                {run.context}
              </pre>
            </details>
          </div>
        </div>
      </div>
    </section>
  );
}
