import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { App } from "./App";
import { benchmarks, explorer, runKey } from "./lib/data";

beforeEach(() => {
  document.documentElement.dataset.theme = "light";
  window.localStorage.clear();
});
afterEach(() => document.body.replaceChildren());

const explorerSection = () => document.getElementById("explorer")!;

describe("context explorer", () => {
  it("shows the stored run for every control combination", () => {
    render(<App />);
    const section = explorerSection();
    for (const task of explorer.tasks) {
      fireEvent.click(within(section).getByLabelText(task.text));
      for (const budget of explorer.budgets) {
        fireEvent.click(within(section).getByLabelText(`${budget} tokens`));
        for (const [method, label] of Object.entries(explorer.methods)) {
          fireEvent.click(within(section).getByLabelText(label));
          const run = explorer.runs[runKey(task.id, budget, method)];
          expect(run.used).toBeLessThanOrEqual(budget);
          const stats = section.querySelector(".stats")!.textContent!;
          expect(stats).toContain(`Used ${run.used} / ${budget} tokens`);
          expect(stats).toContain(`Selected ${run.selected.length}`);
          expect(stats).toContain(`Excluded ${run.excluded.length}`);
        }
      }
    }
  });

  it("highlights exactly the referenced lines of a clicked fragment", () => {
    render(<App />);
    const section = explorerSection();
    const run = explorer.runs[runKey(explorer.tasks[0].id, explorer.budgets[1], "baseline")];
    const f = run.selected[1];
    fireEvent.click(within(section).getByText(`${f.path}:${f.start_line}-${f.end_line}`));
    const hits = section.querySelectorAll(".code .line.hit .num");
    const numbers = Array.from(hits, (n) => Number(n.textContent));
    expect(numbers[0]).toBe(f.start_line);
    expect(numbers[numbers.length - 1]).toBe(f.end_line);
  });
});

describe("theme", () => {
  it("toggles, persists, and labels the action", () => {
    render(<App />);
    const toggle = screen.getByRole("button", { name: "Switch to dark theme" });
    fireEvent.click(toggle);
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem("hebra-theme")).toBe("dark");
    fireEvent.click(screen.getByRole("button", { name: "Switch to light theme" }));
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});

describe("benchmarks", () => {
  it("displays the recorded numbers and the negative findings", () => {
    render(<App />);
    const section = document.getElementById("benchmarks")!;
    const ctx = benchmarks.runs["v2-heldout"].summary.subsets.all.context;
    const row = within(section).getByRole("row", { name: /BM25 \+ greedy \(default\)/ });
    expect(row.textContent).toContain(`${(ctx.bm25_greedy["8000"].symbol_recall * 100).toFixed(1)}%`);
    expect(section.textContent).toContain("no coding-agent experiments have been run");
    expect(section.textContent).toContain("did not improve");
  });

  it("switches run and task subset", () => {
    render(<App />);
    const section = document.getElementById("benchmarks")!;
    fireEvent.click(within(section).getByLabelText("v1 (original optimizer)"));
    fireEvent.click(within(section).getByLabelText("Task text does not name the target"));
    const v1 = benchmarks.runs["v1-heldout"].summary.subsets.subject_does_not_name_a_label;
    const row = within(section).getByRole("row", { name: /BM25 \+ graph \+ optimized/ });
    expect(row.textContent).toContain(`${(v1.context.hebra_optimized["1000"].symbol_recall * 100).toFixed(1)}%`);
    expect(section.textContent).toContain(`${v1.tasks} held-out tasks`);
  });
});
