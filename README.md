# kugi

> Storage-minimal, content-addressed tool & workspace manager built in [Kizu](https://github.com/kizu-lang/kizu) — designed for git worktrees and AI-parallel development.

`kugi` (釘 — *nail / pin*) pins reproducible toolchains, environment variables, and dotfile links without a central package registry, without plugin bloat, and without duplicating binaries across worktrees.

## Why kugi?

Heavy git-worktree and parallel AI-agent workflows quickly burn through local SSDs:

- Multiple worktrees duplicate toolchains, build artifacts, and caches.
- Deleted worktrees leave stale GC roots behind, letting stores grow unbounded.
- Self-updating CLI agents conflict with managers that try to own their install directories.
- Central registries and multi-backend managers hide where binaries actually come from.

`kugi` takes a narrow, explicit design:

1. **Zero registry, zero magic**: Every tool URL and SHA-256 hash lives in `kugi.toml` + `kugi.lock`.
2. **One copy across all worktrees + strip extraction**: Binaries live once in a content-addressed store (`~/.local/share/kugi/store/<name>-<version>-<sha256[:12]>/`). Only declared binaries are extracted; archives and unneeded files are discarded immediately.
3. **Concurrent-safe**: `flock` + atomic `rename` let parallel worktree agents run `kugi sync` simultaneously without races or duplicate downloads.
4. **Dead-worktree-aware GC & storage budget**: `kugi gc` automatically drops roots pointing to deleted worktrees and enforces an optional `max_size` LRU budget.
5. **First-class self-updating tools (`mode = "bootstrap"`)**: Reproducibly install fast-moving CLIs on fresh machines without rolling back or blocking their built-in updaters.

## Comparison

| Aspect | mise | Nix / Home Manager | **kugi** |
|---|---|---|---|
| Central registry | Huge built-in registry + backends | nixpkgs | **None (explicit URLs)** |
| Hash verification | Varies by backend | Strict | **Always SHA-256** |
| Worktree deduplication | Per-tool installs, manual prune | Store shared, stale roots accumulate | **Single store + auto dead-root GC + budget** |
| Self-updating CLIs | Often blocked / conflicts | Disabled by read-only store | **Native `mode = "bootstrap"`** |
| Config symlinks | Not included | Included | **Included (`[links]`)** |
| Scope | Builds & manages everything | Full OS / source-build graphs | **Prebuilt binaries, env, and links** |

## Configuration

### `kugi.toml` (human-written manifest)

```toml
[store]
max_size = "4GB"

[tools.ripgrep]
github = "BurntSushi/ripgrep"
version = "14.1.1"
tag = "{version}"
asset = "ripgrep-{version}-{arch}-{os}.tar.gz"
os_map = { darwin = "apple-darwin", linux = "unknown-linux-musl" }
bin = ["rg"]

[tools.zig]
version = "0.16.0"
url = "https://ziglang.org/download/{version}/zig-{arch}-{os}-{version}.tar.xz"
os_map = { darwin = "macos" }
extract = "all"
bin = ["zig"]

[tools.agent]
version = "2.3.2"
url = "https://example.com/releases/agent-{version}-{os}-{arch}.tar.gz"
bin = ["agent"]
mode = "bootstrap"
install_dir = "~/.agent/bin"

[assets.fonts]
version = "1.2.0"
url = "https://example.com/packs/fonts-{version}.tar.gz"

[assets.dictionary]
version = "1.0.0"
url = "https://example.com/packs/dict-{version}.dat"
archive = "none"

[env]
EDITOR = "nvim"
FONT_DIR = "{asset.fonts}"
DICT_FILE = "{asset.dictionary}/dict-1.0.0.dat"
ZIG_LIB_DIR = "{tool.zig}/lib"
PROJECT_DATA = "{manifest_dir}/data"

[links]
"~/.config/nvim" = "~/my-configs/nvim"
"~/.config/ghostty/config" = "~/my-configs/ghostty/config"
```

### `kugi.lock` (generated, committed)

```json
{
  "version": 1,
  "tools": {
    "ripgrep": {
      "version": "14.1.1",
      "targets": {
        "darwin-aarch64": {
          "url": "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-aarch64-apple-darwin.tar.gz",
          "sha256": "..."
        },
        "linux-x86_64": {
          "url": "https://github.com/BurntSushi/ripgrep/releases/download/14.1.1/ripgrep-14.1.1-x86_64-unknown-linux-musl.tar.gz",
          "sha256": "..."
        }
      }
    }
  },
  "assets": {
    "fonts": {
      "version": "1.2.0",
      "targets": {
        "darwin-aarch64": {
          "url": "https://example.com/packs/fonts-1.2.0.tar.gz",
          "sha256": "..."
        }
      }
    }
  }
}
```

## Commands (v0.1)

- `kugi lock [--tool NAME] [--asset NAME] [--target OS-ARCH]... [--sync]` — Resolve URLs for the host or specified targets and merge SHA-256 hashes into `kugi.lock` (pass `--sync` to also extract, store, and link the host target in a single download).
- `kugi sync` — Verify, fetch missing tools and assets, extract into the shared store, register this manifest root, and apply `[links]`.
- `kugi exec -- <cmd> [args...]` — Run `<cmd>` with manifest tools prepended to `PATH` and `[env]` applied (expanding `~/`, `{manifest_dir}`, `{asset.<name>}`, and `{tool.<name>}`).
- `kugi env` — Print shell `export` lines for the current manifest.
- `kugi gc` — Remove roots whose manifest/worktree no longer exists, delete unreferenced tool/asset store entries, and enforce `max_size`.
- `kugi status` — Inspect resolved tool/asset store paths, total disk usage, and reclaimable space.
- `kugi version` — Print version.

## Development

Requires [Kizu](https://github.com/kizu-lang/kizu) `v0.8.0+` on `PATH` (or via `KIZU` / `KIZU_LIB_DIR`).

```sh
sh scripts/lint.sh              # format check, hygiene checks, kizu lint, kizu check, kizu test
sh scripts/build.sh             # build optimized native binary to dist/kugi
sh scripts/test-integration.sh  # end-to-end CLI integration tests over loopback HTTP
./dist/kugi --help
```

See [SPEC.md](SPEC.md) for the full v0.1 specification and [AGENTS.md](AGENTS.md) for contributor guidelines.

## License

[MIT](LICENSE)
