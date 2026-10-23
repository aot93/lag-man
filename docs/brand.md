# Brand and style guide

The lag-man web control UI and its printable reports share one look, taken from the
"Fidelity Under Load" report: a dark navy and white base, one red-to-orange
accent, Raleway headings, Roboto body text and IBM Plex Mono for every byte,
number and field name. All values live in [`design/tokens.css`](../design/tokens.css);
components reference tokens, never raw colours.

## Colour

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--bg` | `#f5f5f8` | `#05062b` | Page background |
| `--surface` | `#ffffff` | `#0d0e3d` | Cards, tables, panels |
| `--raised` | `#fafafd` | `#141553` | Inline code, tags, hover wells |
| `--ink` | `#05062b` | `#ffffff` | Headings, primary text |
| `--ink2` | `#454668` | `#b7b8ca` | Body text, labels |
| `--muted` | `#84859f` | `#8081a0` | Units, secondary metadata |
| `--line` | `#e4e4ed` | `#1e1f56` | Table rules, dividers |
| `--border` | navy 11% | white 13% | Card and control outlines |
| `--accent` | `#ee2737` | `#ff4453` | Brand red: eyebrows, active state, focus |
| `--accent2` | `#ff6a39` | `#ff8256` | Gradient end, paired with `--accent` |
| `--good` | `#0f7a52` | `#35c98c` | Pass, fits, verified |
| `--s1`…`--s4` | red, blue, green, violet | brighter variants | Chart series, in order |

The accent gradient (`--accent` to `--accent2`, left to right) is the signature
mark. It appears as the 3px masthead rule, the 52px bar under each section
heading, and the fill of an active toggle button. Use it nowhere else.

**Status mapping for lag-man** (proposed; the source report has no
warning colour): error uses `--accent`, warning uses `--accent2`, pass uses
`--good`. Every status also carries a text label or icon, so colour is never
the only signal. Dropped or repeated frames show as a warning until acknowledged.

## Type

| Role | Font | Style |
| --- | --- | --- |
| Page title (h1) | Raleway 800 | `clamp(2rem, 4.4vw, 3.05rem)`, line-height 1.08, tracking -0.015em |
| Section heading (h2) | Raleway 700 | 1.06rem, uppercase, tracking 0.085em, gradient bar beneath |
| Sub-heading (h3) | Raleway 700 | 1.02rem |
| Eyebrow | Raleway 700 | 0.7rem, uppercase, tracking 0.17em, `--accent` |
| Body | Roboto 400 | 16px / 1.62, `--ink2`, max 74ch |
| Lede | Roboto 400 | 1.14rem, max 70ch |
| Table header | Raleway 700 | 0.67rem, uppercase, tracking 0.04em |
| Hex, values, field names | IBM Plex Mono 400/500 | 0.86em inline, 0.75rem in tables |

Numbers in tables are right-aligned with `font-variant-numeric: tabular-nums`.

## Components

- **Masthead:** product name left, 3px gradient rule below.
- **Key facts:** grid of cards (min 215px), Raleway 800 number at 1.92rem
  over a one-line label. Use for headline numbers such as count, refresh rate
  and latency.
- **Cards:** `--surface`, 1px `--border`, `--radius`, 18px padding; a mono
  unit line in `--muted` under the title.
- **Tables:** inside a scrolling `--surface` container with a sticky header;
  rows tint faintly with the accent on hover.
- **Tags:** pill, Raleway 700 0.64rem uppercase; `.ok` variant in `--good`.
- **Notes / callouts:** 3px `--accent` left border on `--surface`.
- **Toolbar buttons:** outlined, Raleway 600; hover turns `--accent`; the
  active one is filled with the gradient and white text.
- **Bars and meters:** 7px bars, rounded on the value end, series colours in
  order. Link meters show the limit as a marked line.

## Layout and themes

- Max content width 1280px, 22px side padding (16px under 640px).
- Light and dark themes follow `prefers-color-scheme`, with a manual override
  through `data-theme="light"` or `data-theme="dark"` on the root element.
- A print style drops to black on white with a darker red accent.

## Offline

The UI works on a local network with no internet (SPEC.md, "Web control front end"). It bundles the
three font families locally instead of loading them from Google Fonts.

## Logos

[`design/vyv-logo.png`](../design/vyv-logo.png) is the VYV mark from the
reference report (271 x 120, transparent, orange-to-red gradient). It reads on
both themes, so there is no dark variant. It sits at the left of the masthead,
34px high, before the product name. Do not recolour or stretch it. The
report's product logos are not used.
