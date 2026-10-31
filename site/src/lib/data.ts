import benchmarksJson from "../data/benchmarks.json";
import explorerJson from "../data/explorer.json";
import memoryJson from "../data/memory.json";

export interface Fragment {
  rank: number;
  path: string;
  start_line: number;
  end_line: number;
  kind: string;
  symbol: string | null;
  tokens?: number;
  reason: string;
  matched_terms?: string[];
  sources?: string[];
  evidence?: string[];
}

export interface Run {
  used: number;
  budget: number;
  header_tokens: number;
  utility: number;
  selected: Fragment[];
  excluded: Fragment[];
  context: string;
}

export interface ExplorerData {
  hebra_version: string;
  tokenizer: string;
  repository: string;
  tasks: { id: string; text: string }[];
  budgets: number[];
  methods: Record<string, string>;
  runs: Record<string, Run>;
  files: Record<string, string>;
}

export const explorer = explorerJson as unknown as ExplorerData;
export const runKey = (task: string, budget: number, method: string) =>
  `${task}|${budget}|${method}`;

export interface MemoryRef {
  path: string;
  start_line: number;
  end_line: number;
  status: string;
  detail: string;
  span_sha256: string;
}
export interface MemoryStep {
  title: string;
  explanation: string;
  record: {
    id: string;
    kind: string;
    content: string;
    provenance: string;
    status: string;
    git_revision: string | null;
    revision_note: string;
    refs: MemoryRef[];
  };
}
export const memory = memoryJson as unknown as { steps: MemoryStep[] };

export interface ContextMetrics {
  symbol_recall: number;
  any_hit: number;
  token_precision: number | null;
  utilization: number;
  redundancy: number;
  references_correct: number;
  latency_ms_median: number;
}
export interface Paired {
  mean_recall_difference: number;
  ci95: [number, number];
  wins: number;
  ties: number;
  losses: number;
}
export interface Subset {
  tasks: number;
  ranking: Record<string, Record<string, number>>;
  context: Record<string, Record<string, ContextMetrics>>;
  paired: Record<string, Record<string, Paired>>;
}
export interface BenchmarkRun {
  hebra_commit: string;
  split: string;
  source: string;
  summary: { tasks: number; task_errors: string[]; subsets: Record<string, Subset> };
}
export interface Benchmarks {
  dataset: string;
  tasks_sha256: string;
  tokenizer: string;
  budgets: number[];
  repositories: Record<string, { url: string; pinned_head: string }>;
  runs: Record<string, BenchmarkRun>;
  memory_freshness: {
    scenarios: number;
    correct: number;
    results: { scenario: string; expected_status: string; status: string; correct: boolean }[];
  };
  index_timing: Record<string, { files_indexed: number; seconds_median: number }>;
  failures_at_8000: {
    count: number;
    of: number;
    examples: { task_id: string; task: string; labels: string[] }[];
  };
  agent_experiments: string;
  docs: string;
}
export const benchmarks = benchmarksJson as unknown as Benchmarks;
