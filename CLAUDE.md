# packages/flywire — scoped rules

- Spec: `../../specs/flywire_implementation_spec.pdf` (text: `.txt` alongside). Contracts: `../../contracts/SHARED_CONTRACTS.md` — **contracts win** over the spec where they differ (see DEVIATION Dn markers).
- Write ONLY inside `packages/flywire/`. Never import the other two packages; interoperate only via contract file formats.
- All standards in the root `CLAUDE.md` apply (strict TDD, simplicity, zero fake tests, atomic/leak-free I/O).
- Verify from this directory, all clean before any handoff:
  `uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src && uv run pytest -v`
