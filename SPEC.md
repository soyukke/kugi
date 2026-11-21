# kugi v0.1 Specification

`kugi` is a storage-minimal, content-addressed tool and workspace manager written in Kizu.
This document defines the v0.1 contract. Implementations must not deviate silently from these rules.

## 1. Design Goals & Non-Goals

### Goals
- **Explicit provenance**: No central registry or third-party plugin ecosystem. Every artifact URL and SHA-256 digest is declared in `kugi.toml` + `kugi.lock`.
- **Minimal disk footprint**: Content-addressed shared store across all worktrees and projects, strip extraction of declared binaries only, immediate deletion of downloaded archives, automatic dead-worktree root pruning, and optional LRU disk budget.
- **Parallel safety**: Concurrent `kugi sync` calls from multiple worktrees/agents must never corrupt the store or duplicate final entries.
- **Zero shell-magic requirement**: Usable via explicit `kugi exec -- <cmd>` and global `~/.local/share/kugi/bin`, with optional `eval "$(kugi env)"`.
- **Self-updater compatibility**: `mode = "bootstrap"` installs a verified initial binary into a writable directory and never overwrites or downgrades it on subsequent syncs.

### Non-Goals (v0.1)
- Compiling C/C++ dependency graphs from source.
- Supporting Windows native paths/locks in v0.1 (macOS and Linux POSIX targets first).
- Interactive TUI or daemon processes.

## 2. Directory Layout

Respects XDG base directories with POSIX defaults:

- Global manifest: `${XDG_CONFIG_HOME:-$HOME/.config}/kugi/kugi.toml` (and sibling `kugi.lock`)
- Data root (`KUGI_HOME`, default `${XDG_DATA_HOME:-$HOME/.local/share}/kugi`):
  - `store/<name>-<version>-<sha256[0..12]>/` — immutable extracted tool directories
  - `store/asset-<name>-<version>-<sha256[0..12]>/` — immutable extracted non-executable asset directories
  - `bin/` — global symlinks for tools declared in the global manifest (`mode = "store"`)
  - `roots/<root-id>.json` — registered manifest/lockfile paths (for GC)
  - `locks/<key>.lock` — `flock` lockfiles for concurrent sync/GC safety
  - `tmp/` — staging directory on the same filesystem as `store/` so `rename` is atomic

## 3. Manifest Format (`kugi.toml`)

A clean, comment-friendly TOML subset:

- Top-level sections: `[store]`, `[tools.<name>]`, `[assets.<name>]`, `[env]`, `[links]`.
- Comments start with `#`.
- Values: double-quoted strings, string arrays `["a", "b"]`, inline flat string maps `{ k = "v", ... }`, integers, booleans.

### `[store]` (optional)
- `max_size`: human byte budget (e.g. `"4GB"`, `"512MB"`). Unreferenced entries are evicted LRU during `kugi gc` (and optionally post-sync) until under budget.

### `[tools.<name>]`
Required fields:
- `version`: string (explicit version; no unpinned `"latest"` in `kugi.toml`).
- Either `url` OR (`github` + `asset`):
  - `url`: direct HTTPS URL template.
  - `github`: `"<owner>/<repo>"`, expands with `asset` to `https://github.com/<owner>/<repo>/releases/download/<tag>/<asset>` (`tag` defaults to `v{version}` unless `tag` is specified, e.g. `tag = "{version}"`).
- `bin`: non-empty array of relative paths or basenames inside the archive (or single-file binary name when `archive = "none"`). Supports `"src:dest"` renaming if the extracted filename differs from the exposed command name.

Optional fields:
- `os_map`: inline map overriding `{os}` (default: `darwin` / `linux`).
- `arch_map`: inline map overriding `{arch}` (default: `aarch64` / `x86_64`).
- `archive`: `"auto"` (default — infer from URL extension) or `"none"` (treat as a raw single binary).
- `extract`: `"bin"` (default — strip-extract only declared `bin` entries) or `"all"` (keep full extracted tree for tools like `zig` that need sibling `lib/` files).
- `mode`: `"store"` (default — immutable store entry + symlink) or `"bootstrap"` (extract once into writable `install_dir` only if destination binary does not already exist).
- `install_dir`: required when `mode = "bootstrap"` (e.g. `"~/.agent/bin"`).
- `sha256`: optional inline lowercase hex SHA-256 (overrides/bypasses `kugi.lock` lookup for single-platform manifests).

### `[assets.<name>]`
Non-executable content-addressed distributions (fonts, browser bundles, dictionaries, static data packs) stored under `store/asset-<name>-<version>-<sha256[0..12]>/` and managed by the same SHA-256 verification, root registration, dead-worktree GC, and `max_size` LRU budget as tools.

Required fields:
- `version`: string (explicit pinned version; `"latest"` is rejected).
- Either `url` OR (`github` + `asset`, with optional `tag` defaulting to `v{version}`).

Optional fields:
- `os_map`: inline map overriding `{os}` (default: `darwin` / `linux`).
- `arch_map`: inline map overriding `{arch}` (default: `aarch64` / `x86_64`).
- `archive`: `"auto"` (default) or `"none"` (place raw single file at `store/asset-<name>-<version>-<sha256[0..12]>/<filename>`).
- `unwrap`: boolean (default `true` — when extracting an archive, if extraction produces a single non-symlink top-level directory, unwrap its contents directly into the store directory).
- `sha256`: optional inline lowercase hex SHA-256 (overrides/bypasses `kugi.lock` lookup).

Fields such as `bin`, `extract`, `mode`, and `install_dir` are not defined on `[assets.<name>]` and fail manifest validation.

Template placeholders expanded in `url`, `asset`, `tag`:
- `{version}`, `{os}`, `{arch}`.

### `[env]` (optional)
- Key-value map of environment variables exported by `kugi env` and applied by `kugi exec`.
- Supports leading `~` / `~/` expansion using `$HOME`.
- Expands the following placeholders anywhere in the value in a single pass:
  - `{manifest_dir}` — canonical directory containing `kugi.toml`.
  - `{asset.<name>}` — absolute store directory path for `[assets.<name>]` (`store/asset-<name>-<version>-<sha256[0..12]>`).
  - `{tool.<name>}` — absolute store directory path for `[tools.<name>]` (`store/<name>-<version>-<sha256[0..12]>`, or the expanded `install_dir` when `mode = "bootstrap"`).
- Referencing an undeclared `<name>` or malformed placeholder fails `kugi env` / `kugi exec` with exit code `2` and a diagnostic message; referencing a declared but unlocked asset/tool fails with exit code `1`.

### `[links]` (optional)
- Map of `"<dest_path>" = "<source_path>"`.
- Leading `~/` expands to `$HOME/`.
- On `kugi sync`:
  - Creates parent directories of `<dest_path>`.
  - If `<dest_path>` is already a symlink pointing to `<source_path>`, no-op.
  - If `<dest_path>` is a symlink pointing elsewhere, atomically replaces it.
  - If `<dest_path>` is a regular file or non-empty directory, refuses to overwrite and exits non-zero (never destroys untracked user files without `--force`).

## 4. Lockfile Format (`kugi.lock`)

JSON document (UTF-8, pretty-printed with 2-space indent, sorted keys for clean git diffs):

```json
{
  "version": 1,
  "tools": {
    "ripgrep": {
      "version": "14.1.1",
      "targets": {
        "darwin-aarch64": {
          "url": "https://...",
          "sha256": "64-char-lowercase-hex"
        },
        "linux-x86_64": {
          "url": "https://...",
          "sha256": "64-char-lowercase-hex"
        }
      }
    }
  },
  "assets": {
    "fonts": {
      "version": "1.0.0",
      "targets": {
        "darwin-aarch64": {
          "url": "https://...",
          "sha256": "64-char-lowercase-hex"
        }
      }
    }
  }
}
```

The `"assets"` top-level object is omitted when empty so manifests without assets remain byte-for-byte compatible with v1 tool-only lockfiles, and lockfiles without `"assets"` decode with an empty asset map.

Target key is `<os>-<arch>` (`darwin-aarch64`, `darwin-x86_64`, `linux-aarch64`, `linux-x86_64`).

## 5. Store, Extraction, and Concurrency Invariants

1. **Hash before extract & HTTPS transport**:
   - Downloads prioritize Kizu's native `std::http` + `std::tls` (TLS 1.3) client, streaming into `tmp/<pid>-<rand>.part` while computing SHA-256 and following up to 10 redirects while rejecting `https://` -> `http://` downgrades.
   - If an HTTPS connection fails at the TLS/network layer (such as a TLS 1.2-only host), `kugi` falls back to OS `curl` restricted to HTTPS (`--proto =https --proto-redir =https --max-redirs <remaining>`) into the same `.part` staging file and then computes the SHA-256 digest via Kizu's streaming hasher.
   - If the computed digest does not match `kugi.lock` (or inline `sha256`), delete the `.part` file immediately and exit non-zero. Never execute or extract unverified bytes.
   - Download failures report the specific cause (`HTTP <status>`, `TLS / network error`, `too many redirects`, `insecure redirect (https -> http)`, or `no trusted CA certificates found`).
2. **Archive handling**:
   - `.tar.gz`, `.tgz`, `.tar.xz`, `.txz`, `.tar.bz2`, `.tbz2`, `.tar`, `.zip`: extract into `tmp/stage-<pid>-<rand>/` (using OS `tar` with path-traversal rejection: reject absolute paths, `..` segments, and escaping symlinks/hardlinks).
   - Raw single binary (tool with no archive extension or `archive = "none"`): place in `stage-final/bin/<dest>` and `chmod 0755`.
   - Raw single file (asset with no archive extension or `archive = "none"`): place directly in `stage-final/<filename>` (derived from the URL path basename, falling back to `<name>` if absent or unsafe) with standard non-executable file mode.
3. **Tool extraction (`extract = "bin"`, default, and `extract = "all"`) & Asset extraction**:
   - Both tool modes locate declared `bin` entries by matching regular files or valid internal symlinks whose resolved `realpath` is a regular file within the extraction root.
   - `extract = "bin"`: copies only the resolved target files of declared `bin` entries into `stage-final/bin/<cmd>` with mode `0755`, and deletes the full archive + staging dir immediately.
   - `extract = "all"`: if extraction produced a single top-level non-symlink directory, unwraps its contents directly into `stage-final/` (re-verifying that all symlinks stay inside `stage-final/`). Marks each located binary's real target file `0755`, preserves any valid regular file or internal symlink already at `bin/<cmd>`, and creates relative symlinks in `bin/<cmd>` for binaries located elsewhere in the tree.
   - `[assets.<name>]` archives: validate, extract, and when `unwrap = true` (default) unwrap a single top-level non-symlink directory directly into `stage-final/` (re-verifying all internal symlinks); when `unwrap = false`, preserve the extracted directory hierarchy as-is.
4. **Atomic publish & `flock`**:
   - Acquire exclusive `flock` on `locks/store-<key>.lock` before fetching/staging `<key>` (where `<key>` is `<name>-<version>-<hash12>` for tools and `asset-<name>-<version>-<hash12>` for assets).
   - Re-check whether `store/<key>` already exists after acquiring the lock (if a parallel worktree finished first, skip download completely).
   - Atomically move `stage-final` to `store/<key>` via `rename(2)`.
   - Always clean up `tmp/` artifacts on both success and error paths.

## 6. Root Registration & Worktree-Aware GC

- Every successful `kugi sync` writes/updates `roots/<sha256(realpath(manifest))[:16]>.json` containing:
  - `manifest`: absolute path to `kugi.toml`
  - `lockfile`: absolute path to `kugi.lock`
  - `synced_at`: unix timestamp
  - `store_keys`: list of `store/<key>` entries required by this manifest (including both `store/<tool-...>` and `store/asset-<...>`)
- `kugi gc`:
  1. Acquires exclusive GC lock (`locks/gc.lock`).
  2. Scans all `roots/*.json`. If `manifest` no longer exists on disk (e.g., deleted git worktree), removes the stale root record.
  3. For live roots, re-reads their `kugi.toml` and `kugi.lock` (falling back to stored `store_keys`) to build the live set of tool and asset store keys.
  4. Deletes every directory in `store/` not in the live set (and cleans orphaned `tmp/` files older than 1 hour).
  5. Reports deleted roots, deleted store entries, and freed bytes.
  6. Supports `--dry-run` to preview without deleting.

## 7. CLI Commands & Exit Codes

Manifest resolution order:
1. `--manifest <path>` if explicitly passed.
2. Nearest `kugi.toml` walking upward from current working directory.
3. Global manifest `${XDG_CONFIG_HOME:-$HOME/.config}/kugi/kugi.toml`.

Commands:
- `kugi lock [--manifest PATH] [--tool NAME]... [--asset NAME]... [--target OS-ARCH]... [--sync]`
  - Downloads artifact(s) for each requested target (defaulting to the current host target), computes SHA-256, merges target entries into `kugi.lock` without dropping other existing targets, writes `kugi.lock` atomically, and deletes downloaded temp archives.
  - `--tool NAME` and/or `--asset NAME` restrict locking to the named tools/assets while preserving all other lock entries.
  - With `--sync`, locks all specified targets and reuses the already-downloaded temp archive for the host target without downloading a second time to publish local store entries, install missing `bootstrap` tools, update `bin/` symlinks (for global manifest), apply `[links]`, register the root, and enforce `[store] max_size`. Non-host targets are only recorded in `kugi.lock` and never staged locally.
- `kugi sync [--manifest PATH] [--force]`
  - Requires locked SHA-256 for every declared tool and asset (fails clearly telling the user to run `kugi lock` if missing/mismatched version or URL drift).
  - Fetches missing tool and asset store entries, verifies SHA-256, extracts, updates `bin/` symlinks (for global manifest), runs `bootstrap` installs if absent, applies `[links]`, registers root.
- `kugi exec [--manifest PATH] -- <cmd> [args...]`
  - Resolves store `bin/` paths for manifest tools, prepends to `PATH`, expands and applies `[env]` (including `~/`, `{manifest_dir}`, `{asset.<name>}`, and `{tool.<name>}`), and `execvp`s `<cmd>` directly (no wrapper process overhead).
- `kugi env [--manifest PATH] [--shell sh|zsh|bash|fish]`
  - Emits shell statements updating `PATH` and `[env]` (with full placeholder expansion).
- `kugi gc [--dry-run]`
  - Prunes dead roots and unreferenced tool/asset store entries; reports bytes freed.
- `kugi status [--manifest PATH]`
  - Prints each declared tool (`<name> <version> <status> <path>`) and asset (`asset:<name> <version> <status> <path>`), with status (`ok` / `missing` / `bootstrap` / `unlocked`), total store size, and reclaimable size.
- `kugi version` / `--version` / `-V`

Exit codes:
- `0`: success
- `1`: runtime / I/O / network / hash-mismatch error
- `2`: CLI usage or manifest parse error
