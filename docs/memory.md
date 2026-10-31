# Source-grounded memory

`hebra memory` stores short statements about a repository that should carry
into future context: architectural facts, conventions, failure causes,
relationships, test observations, and decisions. It is a small local SQLite
store, not a conversation log, and nothing is learned or added automatically.
Records are added by a person (or a tool acting for one).

```bash
hebra memory add . --kind fact --text "Retries are capped in fetch_with_retry" --ref src/net.py:40-58
hebra memory add . --kind decision --text "Money is always Decimal, never float"
hebra memory list .
hebra memory show . mem_3f2a9c1b7d4e
hebra memory invalidate . mem_3f2a9c1b7d4e --reason "retry logic moved to the client"
hebra pack . --task "..." --budget 8000 --memory
```

## Storage

`<repo>/.hebra/memory.sqlite3`, separate from the index. The index is a cache
that is rebuilt when its format changes; memory is user data and must survive
that. The same path-safety policy as the index applies (no symlinks, no
foreign SQLite databases, see the README's security model). `--memory-db PATH`
selects another location.

## Record fields

`id`, `kind`, `content` (at most 4,000 characters), `provenance`, `git_revision`
(HEAD when added), `created_at`, `updated_at`, `status` (`active` or
`invalidated`, with a reason), and up to 20 source references. Each reference
stores `path`, `start_line`, `end_line`, the SHA-256 of the whole file, and the
SHA-256 of the referenced lines. References are read from the working tree when
the record is added, without following symlinks; a reference to a missing
file, a line range beyond the end of the file, or a path outside the
repository is rejected.

## Provenance (fixed when a record is added)

| provenance | when | treated as |
|---|---|---|
| `source-verified` | non-decision record with references | a claim about the code, checked against it on every read |
| `user-decision` | `--kind decision` | an explicit engineering decision; shown as a decision, never as verified source fact |
| `unverified` | non-decision record without references | listed, but never included in packed context |

The distinction matters: a fact derived from code is only as good as the code
it points to, so it must be re-verified; a decision is valid because a person
made it, and its references (if any) are supporting context.

## Freshness (recomputed on every read)

Each reference is checked against the working tree:

| reference status | meaning |
|---|---|
| `current` | file unchanged, or the referenced lines are byte-identical at the same position |
| `moved` | the referenced lines exist elsewhere in the file (new position reported) |
| `changed` | the referenced lines are no longer present |
| `renamed` | the file is gone, but a file with identical content is in the index |
| `missing` | the file is gone |
| `replaced` | the path is now a symlink or special file |

Record status: `invalidated` if invalidated; `decision` for decisions;
`unverifiable` without references; `current` if every reference is current;
otherwise `stale`. Only `current` facts and decisions are eligible for packed
context, and only if they share a term with the task.

## Branches, resets, and repository identity

- The commit a record was added at is kept. If HEAD differs, the record notes
  it; if that commit is no longer in HEAD's history (a branch switch or reset),
  the note says so. Status stays content-based: on another branch, a reference
  whose lines are unchanged is still accurate evidence, and one whose lines
  differ is stale.
- Each store records its repository's identity: its root commit(s), or its
  resolved path if it has no Git history. Opening a store from another
  repository (for example, a copied `.hebra` directory) fails.

## Trust boundary

Memory content is rendered into packed context as fenced, labelled data, with
its provenance and freshness. It is never treated as an instruction to Hebra,
and it cannot add structure to the Markdown document. A model reading the
context may still be influenced by what a record says.
