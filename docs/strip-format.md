# Code strip format (v1)

The strip under the clock carries the count in a form software can decode. The analysis
extension reads the strip, not the digits (SPEC.md, "Clock content"). The encoder and the
reference decoder are in `src/camlag/core/strip.py`.

## Layout

One row of 29 equal, adjacent cells, left to right:

| Cells | Content |
| --- | --- |
| 0, 1 | Reference: white, black |
| 2–25 | Count mod 2^24 in Gray code, most significant bit first (white = 1) |
| 26 | Parity: makes the ones in cells 2–26 even |
| 27, 28 | Reference: black, white |

- Cells are always pure white (#ffffff) and pure black (#000000), whatever the clock colours.
- Cell width is `floor(clock width / 29)` pixels; strip height is the larger of one cell
  width and 0.4 of the digit size. The strip is centred under the digits.
- 2^24 ticks is 46.6 hours at 100Hz and 38.8 hours at 120Hz. Differences between two counts
  are taken mod 2^24 (`count_difference`), so a wrap does not upset a latency figure.
- The count is ticks of the source refresh (10ms at 100Hz, 8.333ms at 120Hz), whatever the
  time code rate. The run log records the rate set, so the analysis converts ticks to
  milliseconds with the refresh rate from the log.

## Why Gray code

Consecutive counts differ in exactly one data cell, plus the parity cell. A camera frame
that catches the screen mid-change therefore shows at most one data cell at an intermediate
level. The decoder flags it as `transition` with the two candidate counts, rather than
guessing (SPEC.md, "Processing").

## Decoding

1. Sample the mean luma of the inner half of each of the 29 cells across the region.
2. White level = mean of cells 0 and 28; black level = mean of cells 1 and 27. Reject the
   frame if the difference is under 15% of full scale.
3. Normalise each cell to 0–1 between those levels. Below 0.3 is 0, above 0.7 is 1, between
   is ambiguous. The reference cells must read 1, 0 … 0, 1.
4. No ambiguous cells: check parity, then convert Gray to binary.
5. One ambiguous data cell whose two readings are adjacent counts: `transition`.
6. Anything else: `unreadable`, with a reason.
