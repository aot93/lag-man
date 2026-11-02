# Code strip format (v2)

The strip under the clock carries the count in a form software can decode. The analysis
extension reads the strip, not the digits (SPEC.md, "Clock content"). The encoder and the
reference decoder are in `src/lagman/core/strip.py`.

## Layout

One row of 30 equal, adjacent cells, left to right:

| Cells | Content |
| --- | --- |
| 0, 1 | Reference: white, black |
| 2–25 | Count mod 2^24 in Gray code, most significant bit first (white = 1) |
| 26 | Parity: makes the ones in cells 2–26 even |
| 27, 28, 29 | Reference: black, white, white |

The references are asymmetric, so the decoder can tell which way round it is reading. A
region marked right to left, or a strip seen in a mirror, decodes correctly and is reported
as reversed. (v1 had symmetric `W B … B W` references, so a reversed read could produce a
plausible wrong count. v1 was never used on a recording.)

- Cells are always pure white (#ffffff) and pure black (#000000), whatever the clock colours.
- Cell width is `floor(clock width / 30)` pixels; strip height is the larger of one cell
  width and 0.4 of the digit size. The strip is centred under the digits.
- 2^24 ticks is 46.6 hours at 100Hz and 38.8 hours at 120Hz. Differences between two counts
  are taken mod 2^24 (`count_difference`), so a wrap does not upset a latency figure.
- The count is ticks of the source refresh (10ms at 100Hz, 8.333ms at 120Hz, or 20ms and
  16.667ms at the 50 and 60Hz fallbacks), whatever the time code rate. The run log records
  the refresh rate, so the analysis converts ticks to milliseconds with the rate from the log.

## Why Gray code

Consecutive counts differ in exactly one data cell, plus the parity cell. A camera frame
that catches the screen mid-change therefore shows at most one data cell at an intermediate
level. The decoder flags it as `transition` with the two candidate counts, rather than
guessing (SPEC.md, "Processing").

## Decoding

1. Sample the mean level of the inner half of each of the 30 cells across the region, in
   linear light: video code values are converted with a 2.2 gamma first, so a cell lit for
   half an exposure reads as half, not about 0.73. The analysis also fits a bow along the
   strip, so the cells stay centred where lens distortion curves it.
2. White level = mean of cells 0, 28 and 29; black level = mean of cells 1 and 27. Reject
   the frame if the difference is under 15% of full scale.
3. Normalise each cell to 0–1 between those levels. Below 0.3 is 0, above 0.7 is 1, between
   is ambiguous. The reference cells must read 1, 0 … 0, 1, 1; if they do not, try the
   levels in reverse order before giving up.
4. No ambiguous cells: check parity, then convert Gray to binary.
5. One ambiguous data cell whose two readings are adjacent counts: `transition`. If only
   the parity cell is ambiguous, the data cell nearest 0.5 (within 0.35 of it) is taken as
   the changing one: an exposure split unevenly across a change can push that cell just
   past a threshold while the parity cell stays in between.
6. Anything else: `unreadable`, with a reason.
