import { useState } from "react";
import { benchmarks } from "../lib/data";
import { blob } from "../lib/links";
import { Choices } from "./Choices";

const CONFIGS: [string, string][] = [
  ["truncation", "Truncate BM25 results at the budget"],
  ["bm25_greedy", "BM25 + greedy (default)"],
  ["graph_greedy", "BM25 + graph + greedy"],
  ["hebra_optimized", "BM25 + graph + optimized"],
  ["ablation_no_graph", "Ablation: optimized without graph"],
  ["ablation_no_coverage", "Ablation: optimized without coverage terms"],
];
const SUBSETS: [string, string][] = [
  ["all", "All held-out tasks"],
  ["subject_does_not_name_a_label", "Task text does not name the target"],
];
const PAIRS = [
  "hebra_optimized vs bm25_greedy",
  "graph_greedy vs bm25_greedy",
  "bm25_greedy vs truncation",
];

const pct = (x: number) => `${(x * 100).toFixed(1)}%`;
const signed = (x: number) => `${x > 0 ? "+" : ""}${(x * 100).toFixed(1)}`;

export function Benchmarks() {
  const [runName, setRunName] = useState("v2-heldout");
  const [subset, setSubset] = useState("all");
  const run = benchmarks.runs[runName];
  const data = run.summary.subsets[subset];
  const budgets = benchmarks.budgets.map(String);
  const repos = Object.keys(benchmarks.repositories).join(", ");
  const fail = benchmarks.failures_at_8000;
  const mem = benchmarks.memory_freshness;

  return (
    <section id="benchmarks">
      <div className="container">
        <p className="eyebrow">Benchmarks</p>
        <h2>Measured evidence, including what did not work.</h2>
        <p className="lede">
          Localization benchmark: {repos} at pinned commits. Each task is the subject line of a
          real fix commit; the target is the functions that fix changed; each task is indexed at
          the commit before the fix. The metric is the share of target functions present in the
          packed context. It measures finding the right code, not whether an agent then
          succeeds: <strong>no coding-agent experiments have been run</strong>.
        </p>

        <div className="panel" style={{ marginBottom: 24 }}>
          <Choices
            legend="Run"
            name="run"
            value={runName}
            options={[
              ["v2-heldout", "v2 (current)"],
              ["v1-heldout", "v1 (original optimizer)"],
            ]}
            onChange={setRunName}
          />
          <Choices legend="Tasks" name="subset" value={subset} options={SUBSETS} onChange={setSubset} />
          <p className="note" style={{ margin: 0 }}>
            {data.tasks} held-out tasks; {run.summary.task_errors.length} task errors; Hebra
            commit <code>{run.hebra_commit.slice(0, 12)}</code>;{" "}
            <a href={run.source}>raw records and summary</a>.
          </p>
        </div>

        <h3>Target functions found in the packed context</h3>
        <div className="table-wrap" style={{ marginBottom: 32 }}>
          <table>
            <caption>
              Mean over tasks, by token budget ({benchmarks.tokenizer}). Bars are scaled to 100%.
            </caption>
            <thead>
              <tr>
                <th scope="col">Configuration</th>
                {budgets.map((b) => (
                  <th key={b} scope="col" className="num">
                    {b}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {CONFIGS.map(([id, name]) => (
                <tr key={id}>
                  <th scope="row" style={{ fontWeight: 400, background: "transparent" }}>
                    {name}
                  </th>
                  {budgets.map((b) => {
                    const v = data.context[id][b].symbol_recall;
                    return (
                      <td key={b} className="num">
                        <span className="bar" style={{ width: `${v * 48}px` }} aria-hidden="true" />
                        {pct(v)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <h3>Paired differences</h3>
        <div className="table-wrap" style={{ marginBottom: 16 }}>
          <table>
            <caption>
              Mean per-task difference in percentage points, 95% bootstrap interval, and
              wins/ties/losses over the same tasks.
            </caption>
            <thead>
              <tr>
                <th scope="col">Comparison</th>
                {budgets.map((b) => (
                  <th key={b} scope="col">
                    {b}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {PAIRS.map((pair) => (
                <tr key={pair}>
                  <th scope="row" style={{ fontWeight: 400, background: "transparent" }}>
                    {pair}
                  </th>
                  {budgets.map((b) => {
                    const p = data.paired[pair][b];
                    return (
                      <td key={b}>
                        {signed(p.mean_recall_difference)} [{signed(p.ci95[0])}, {signed(p.ci95[1])}]{" "}
                        <span className="note">
                          {p.wins}/{p.ties}/{p.losses}
                        </span>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="note" style={{ marginBottom: 32 }}>
          Reading: graph expansion and the optimizer did not improve this metric; the optimizer is
          worse at 8000 tokens. Simple truncation scores highest because it keeps the start of
          large functions that whole-fragment packing skips. Because of these results, the
          default stays BM25 + greedy. <a href={benchmarks.docs}>Methodology and diagnosis</a>.
        </p>

        <div className="grid three" style={{ marginBottom: 32 }}>
          <div className="card">
            <h3>Ranking</h3>
            <p>
              BM25 recall@10 {pct(data.ranking.bm25["recall@10"])}, recall@20{" "}
              {pct(data.ranking.bm25["recall@20"])}, MRR {data.ranking.bm25.mrr.toFixed(3)}. With
              graph expansion: recall@20 {pct(data.ranking.bm25_graph["recall@20"])}.
            </p>
          </div>
          <div className="card">
            <h3>Budget and references</h3>
            <p>
              At 4000 tokens the default used {pct(data.context.bm25_greedy["4000"].utilization)} of
              the budget, in a median {data.context.bm25_greedy["4000"].latency_ms_median} ms. No run
              exceeded its budget; {pct(data.context.bm25_greedy["4000"].references_correct)} of
              references matched their source lines.
            </p>
          </div>
          <div className="card">
            <h3>Memory freshness</h3>
            <p>
              {mem.correct} of {mem.scenarios} constructed scenarios (edits, moves, renames,
              deletions, branch switches, resets, foreign stores) were classified as intended.
              Labels are correct by construction; this checks behaviour, not usefulness.
            </p>
          </div>
          <div className="card">
            <h3>Indexing</h3>
            <p>
              Cold full index:{" "}
              {Object.entries(benchmarks.index_timing)
                .map(([n, t]) => `${n} ${t.seconds_median.toFixed(2)} s (${t.files_indexed} files)`)
                .join(", ")}
              . One laptop; small repositories only.
            </p>
          </div>
          <div className="card">
            <h3>Agent success</h3>
            <p>
              Not measured. A harness exists, but no model was run, so nothing here shows that
              agents succeed more often.
            </p>
          </div>
        </div>

        <h3>Failure cases</h3>
        <p className="note">
          In the v2 run, {fail.count} of {fail.of} held-out tasks had no target function in the
          context under any configuration, even at 8000 tokens. Examples:
        </p>
        <ul className="fragments">
          {fail.examples.map((f) => (
            <li key={f.task_id}>
              <div className="fragment-head">
                <code>{f.task_id}</code>
              </div>
              <p className="reason">
                “{f.task}” — target: {f.labels.join(", ")}
              </p>
            </li>
          ))}
        </ul>
        <p className="note">
          Reproduce everything with the scripts in{" "}
          <a href={blob("docs/benchmarks.md#reproducing")}>docs/benchmarks.md</a>.
        </p>
      </div>
    </section>
  );
}
