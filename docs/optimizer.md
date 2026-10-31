# Context selection

`hebra pack` turns ranked candidates into a context document that fits a token
budget. Two strategies exist: `greedy` (the default) and `optimized`. This page
defines both and separates what is proven from what is heuristic or measured.

## Setting

- Candidates `F`: fragments returned by retrieval (`bm25` or `bm25_graph`),
  after removing stale fragments (source changed since indexing) and exact
  duplicates. Optionally, verified memory records.
- `rank(f)`: 1-based retrieval rank.
- `T`: query terms; `w_t` their IDF weights over all indexed chunks,
  normalised so that `sum_t w_t = 1`.
- `G`: graph symbols reached during `bm25_graph` retrieval; `g_s` their graph
  weights (`1 / bm25_rank` of the seed that reached them, halved per hop),
  normalised so that the weights of symbols defined in candidates sum to 1.
- `terms(f)`: query terms that occur in `f`; `symbols(f)`: graph symbols
  defined inside `f`.
- `doc(S)`: the rendered Markdown for an ordered selection `S`, header
  included; `tokens(x)`: exact token count under the chosen tiktoken encoding.

## Constraint (proven, by construction)

    tokens(doc(S)) <= B

Every fragment is accepted only after the *complete* document, with that
fragment appended, has been re-counted with the real tokenizer. The final
document is counted once more before it is returned. The guarantee therefore
covers headers, fences, reason lines, and token merges across fragment
boundaries. It holds for the selected encoding only; other tokenizers count
differently.

## Objective of `optimized` (heuristic)

    U(S) = alpha * sum_{f in S} rank(f) ** -k
         + beta  * sum_{t in T} w_t * [t in terms(f) for some f in S]
         + gamma * sum_{s in G} g_s * [s in symbols(f) for some f in S]

with non-negative `alpha, beta, gamma, k`.

Properties that hold (proven):

- `U` is **monotone** (adding a fragment never lowers it) and **submodular**
  (a fragment's marginal gain can only shrink as the selection grows): it is
  a non-negative modular term plus weighted coverage functions. The test
  suite checks both properties on random instances.
- Redundancy is the loss of marginal gain: a fragment that repeats terms or
  symbols already covered adds only its rank prior. The trace reports
  `redundancy = standalone gain - marginal gain` at the moment of selection.

What is heuristic (not proven to measure usefulness):

- That rank, query-term coverage, and graph-symbol coverage are the right
  ingredients, and the functional form `rank ** -k`.
- The weights. They were chosen by a pre-registered grid search on the
  benchmark's dev split only (see `docs/benchmarks.md`).
- `U` is a score, not a probability that the context will help.

## Algorithm of `optimized`

1. **Cost-benefit greedy.** Repeatedly take the candidate with the highest
   `marginal gain / estimated tokens` (estimate: tokens of the fragment
   rendered alone); accept it if the exact full-document count still fits.
2. **Best single fragment.** The feasible fragment with the highest stand-alone
   gain.
3. Return whichever of the two has the higher `U`.

For a monotone submodular objective with additive costs, the better of (1)
and (2) is a constant-factor approximation of the budgeted optimum (Khuller,
Moss, and Naor 1999 for coverage; Krause and Guestrin 2005 in general;
Leskovec et al. 2007). Here the true cost of a fragment, its effect on the
token count of the whole rendered document, is only approximately additive
(boundary merges, position numbers), so the guarantee applies to the
estimated cost model, not to the exact rendered cost. Feasibility, by
contrast, is exact.

Selected fragments are emitted in retrieval-rank order when that order still
fits the budget, otherwise in the (verified) selection order.

## `greedy` (Phase 1)

Walk candidates in rank order and keep each one that still fits. The same
exact feasibility check applies.

## Complexity

With `n` candidates (50 by default, at most 1000), selection evaluates at most
`n` gains per step for at most `n` steps, and performs at most `n` exact
document counts of at most `B` tokens each.

## Tie-breaking

All choices are deterministic: higher ratio, then higher gain, then better
rank, then the fragment key (path and start line).

## Empirical status

See `docs/benchmarks.md` for measured results. In short: on the v1 held-out
set the original `optimized` objective lost to rank-order `greedy`; the
diagnosis and the revised prior are documented there, together with the
re-run. `greedy` remains the default.
