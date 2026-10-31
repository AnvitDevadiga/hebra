# Security model and indexing policy

Hebra treats every repository as untrusted input. This page describes what is indexed, how consistency is maintained while indexing, and the protections and limits of the security model.

## What gets indexed

In a Git repository (default), Hebra indexes the paths in Git's index, as listed by `git ls-files --cached`, reading each one's **current working-tree content**:

| Path state | Indexed? |
| --- | --- |
| Committed and unchanged | Yes |
| Committed with unstaged edits | Yes, with the edited working-tree content |
| Newly staged, never committed | Yes |
| Tracked but matching `.gitignore` (force-added) | Yes; `.gitignore` does not apply to tracked files in Git |
| Deleted from the working tree but still in Git's index | No (skipped as missing) |
| Untracked, not ignored | Only with `--include-untracked` |
| Untracked and ignored by `.gitignore` | Never |
| Credential-like file name, or contains a PEM private key | Never, whatever its Git state |

"Tracked" here means "in Git's index", not "in the latest commit". If the repository has Git metadata but the listing fails, indexing stops rather than walking the filesystem.

Without Git metadata, or with `--no-git`, Hebra walks the filesystem instead (`.gitignore` is **not** applied). The walk is pre-order depth-first: in each directory, files are taken in name order, then each subdirectory in name order. `max_scan_entries` (default 200,000) caps the directory entries read from the operating system, counting every entry, including excluded and symlinked ones, and memory use is bounded by the same cap. If the cap is reached inside a directory, which of that directory's entries were read depends on the order the OS returns them, not on their names; that is stable for an unchanged directory on common filesystems but not guaranteed. In Git mode, the same cap limits the paths read from `git ls-files`, in Git's order.

## Consistency while indexing

The index database is updated atomically, but a live working tree cannot be snapshotted. Before committing, Hebra checks that HEAD still points to the same commit, that re-listing candidates gives the same paths, and that every file it read still has the same device, inode, size, and modification time. If anything changed, the scan is retried (at most 3 attempts in total); if the repository keeps changing, indexing fails and the previous index is kept. Not covered: edits in the short window between this check and the commit, same-size rewrites within one filesystem timestamp tick, and files that were skipped rather than read. Query-time source checks re-hash result files, so anything that slips through is still flagged as stale.

## Security model

The repository memory store (`.hebra/memory.sqlite3`, see [memory.md](memory.md)) follows the same index-location policy: no symlinked paths, no foreign SQLite databases, and a store from another repository is refused.

Hebra treats every repository as untrusted input.

- **Credentials.** Files whose names commonly hold secrets are never indexed: `.env`, `.env.*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`, `id_rsa*`, `id_dsa*`, `id_ecdsa*`, `id_ed25519*`, `.npmrc`, `.pypirc`, `.netrc`, `.git-credentials`, `credentials*` (except source-code files such as `credentials.py`), and `secrets.{yaml,yml,json}`, as well as anything under `.ssh/`, `.aws/`, or `.gnupg/`. This applies even to files that are committed or staged. Files containing a PEM private-key header are skipped regardless of name. These are filename and pattern filters, **not secret scanning**: an API key or password inside an ordinary source or config file will still be indexed. Review what you index before handing packed context to a hosted model.
- **Index location.** The default index lives in `<repo>/.hebra/`, which a malicious repository could pre-populate. Hebra opens `.hebra`, its database, and its `.gitignore` marker relative to directory descriptors with `O_NOFOLLOW`, rejects symlinks, non-regular files, and symlinked SQLite sidecars, re-checks file identity after SQLite opens the database, and refuses to modify any existing SQLite database that is not a Hebra index. For an explicit `--db PATH`, you choose the location: its parent directories may be anywhere (and may be symlinks), but the database file itself must not be a symlink or a foreign database. These checks fully cover hostile repository content; they narrow, but cannot eliminate, races against a concurrent local attacker who can rewrite `.hebra` while Hebra runs, because SQLite opens the database by path. Windows lacks the needed `O_NOFOLLOW`/`dir_fd` support and gets weaker, `lstat`-based checks.
- **Output structure.** The task text and file contents appear in packed Markdown only inside fenced code blocks whose fence is longer than any backtick run they contain, and paths and symbol names only inside escaped inline code spans, so neither the task nor repository data can add headings, quotes, lists, or HTML to the document's structure. This protects structure only: text inside a fence can still contain instructions, and a model may follow them. It is not protection against prompt injection. Terminal output escapes control characters in paths.
- **Output files.** `-o FILE` writes a uniquely named temporary file beside the destination (created exclusively, without following symlinks), `fsync`s it, and renames it into place relative to a pinned directory descriptor, so the destination is either the old file or the complete new one. Symlinks, directories, and special files at the destination are refused; failures and Ctrl-C remove the temporary file. A process killed outright can leave a hidden `.<name>.<random>.tmp` file. On platforms without `renameat`, the final rename is by path after re-checking the directory's identity, which leaves a narrow window for a concurrent directory swap.
- **Execution.** Repository code is parsed, never imported or run. Git is invoked with a fixed argument list, no shell, a sanitised environment, and `core.fsmonitor` disabled.
