# AGENTS.md

- Express behavior and invariants in types, explicit names, lints, and tests — not prose or code comments (`kizu lint .` enforces zero `//` comments in `src/`).
- When a mistake happens, encode the fix as a type, lint rule, or test rather than adding text here.
- Model non-trivial state transitions, concurrency, and failure paths with `std::machine`, assert `problems()` exhaustively in unit tests, and keep TLA+ specs derived from `to_json()` so implementation and formal verification never drift.
- Keep local CI fast to maximize iteration speed: isolate tests in temporary `KUGI_HOME` dirs over loopback HTTP, avoid live third-party network calls, and never commit local paths, usernames, private names, or secrets.
- Before committing, run:
  ```sh
  sh scripts/lint.sh
  sh scripts/build.sh
  sh scripts/test-integration.sh
  ```
