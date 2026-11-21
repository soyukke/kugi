# AGENTS.md

`kugi` is a storage-minimal, content-addressed tool & workspace manager written in Kizu, built for git worktrees and AI-parallel development.

## Source of Truth

- Read `SPEC.md` before changing behavior or adding features.
- Keep `SPEC.md` strictly aligned with implemented behavior; do not add undocumented flags, hidden fallbacks, or registry lookups.
- Keep `README.md` concise and accurate.

## Coding & Repository Rules

- **Language**: Write `kugi` in Kizu (`src/`). Keep external runtime dependencies to zero beyond POSIX libc (`extern "C"` for `flock`, `symlink`, `readlink`, `chmod`, `execvp` where missing from `std::fs` / `std::process`) and OS `tar` for archive extraction.
- **Comments**: Keep `src/**/*.kizu` free of explanatory code comments — enforced mechanically by `kizu lint .` in `scripts/lint.sh`.
  - Do not leave `//` comments (including `// SAFETY:`) in Kizu source files.
  - Express non-obvious invariants via types, explicit helper names, and tests; explain *why* in commit messages and `SPEC.md`.
- **Public OSS hygiene**:
  - Never hardcode local machine paths, usernames, private project names, or secrets in any committed file (code, tests, scripts, docs, or commit messages).
  - Scripts must locate Kizu via `KIZU` env var (falling back to `command -v kizu`) and honor `KIZU_LIB_DIR` when set.
- **Safety & Atomicity**:
  - Always verify SHA-256 before extraction or execution.
  - Stage downloads and extractions inside `KUGI_HOME/tmp/` on the same filesystem, publish via atomic `rename(2)`, and clean up temp files on error.
  - Reject archive path traversal (`..` segments, absolute paths, symlink escapes).
  - Never overwrite non-symlink user files in `[links]` without explicit `--force`.
  - Never overwrite an existing binary in `mode = "bootstrap"`.

## Testing & Verification

- Keep tests fast (< 15s total) and completely isolated using temporary `KUGI_HOME` directories — never touch the real `$HOME/.local/share/kugi` during tests.
- Test network flows against a local loopback mock HTTP server, not live third-party URLs.
- Before every commit, run:
  ```sh
  sh scripts/lint.sh
  sh scripts/build.sh
  ```
- Every new subsystem must ship with tests covering both happy paths and failure invariants (hash mismatch, concurrent sync, dead-worktree GC, non-clobbering links/bootstrap).
