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
- The analysis (`src/camlag/analysis`) decodes with `core.strip`, never its own copy. Test it
  on synthetic recordings (`analysis.synth`) until real S5 footage is in `tests/`; any change
  to sampling or decoding must keep `tests/test_analysis.py` exact on clean frames.
- Commands: `uv sync --extra generator --extra analysis`, `uv run camlag-analyse --help`, `uv run pytest`, `uv run ruff check .`,
  `uv run camlag-generator --help`; render a frame without a display with
  `uv run camlag-generator --snapshot out.png`; web UI without a display:
  `uv run camlag-generator --headless 1920x1080`.
- The web server and UI use only the standard library and plain JS (no build step). The
  render loop owns all generator state: other threads change it only via `Engine.submit`.
- The analysis app (`analysis/app.py`, `analysis/static/`) listens on 127.0.0.1 only. Every
  `/api/` call checks the session token and the Host header; keep it that way for new routes.
  Long work runs through `Jobs`, never on a request thread.
- Theme switching lives in `web/static/theme.js`; the report has an inline copy
  (`report.THEME_SCRIPT`). Keep them in step.
- `src/camlag/web/static/tokens.css` is a copy of `design/tokens.css`; a test keeps them equal.
- Ask before adding a runtime dependency.
- Web UI and any generated HTML follow `docs/brand.md`; colours and type come from
  `design/tokens.css`, never hard-coded. The UI must work with no internet.
