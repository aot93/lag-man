# cam-lag: rules for Claude Code

- Read SPEC.md first. Build in the phase order in SPEC.md ("Build phases"), one phase per PR.
- The displayed count advances only from displayed frames (`Clock.frame_displayed`), never
  from the system clock. Do not add time-based catch-up.
- Pure logic lives in `src/camlag/core` with no third-party imports, so it runs anywhere and
  the analysis extension can reuse it. Graphics code lives in `src/camlag/generator`.
- The strip format is a contract with the analysis extension: any change updates
  `docs/strip-format.md`, bumps its version and keeps the decoder tests passing.
- Write the test first for every rule. Hardware behaviour that cannot be tested here
  (Pi 5 refresh rates, vsync blocking, GPIO timing) is marked TODO(verify) in code.
- Commands: `uv sync --extra generator`, `uv run pytest`, `uv run ruff check .`,
  `uv run camlag-generator --help`; render a frame without a display with
  `uv run camlag-generator --snapshot out.png`.
- Ask before adding a runtime dependency.
- Web UI and any generated HTML follow `docs/brand.md`; colours and type come from
  `design/tokens.css`, never hard-coded. The UI must work with no internet.
