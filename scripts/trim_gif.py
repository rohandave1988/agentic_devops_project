#!/usr/bin/env python3
"""Trim demo_short_raw.gif → demo_short.gif for LinkedIn upload.

Three segments kept (total ~30s):
  Seg 1: banner + Cycle 1 healthy + Cycle 2 breach detected      (~10s)
  Seg 2: LLM tool calls + diagnosis + scale_up action confirmed  (~16s)
  Seg 3: Recovery — Cycle 3 all-green SLOs healthy               (~4s)

Run: python3 scripts/trim_gif.py
"""
import sys
import numpy as np
import imageio.v3 as iio

RAW = "docs/demo_short_raw.gif"
OUT = "docs/demo_short.gif"

# Calibrated for the ~2079-frame (139s) raw output.
# Scale proportionally if frame count differs between runs.
SEG1_START, SEG1_END   = 280,  440   # banner → breach + LLM starting
SEG2_START, SEG2_END   = 1200, 1470  # LLM diagnosis + scale_up confirmed
SEG3_START, SEG3_END   = 1920, 1985  # Cycle 3 all-green recovery

CALIBRATED_TOTAL = 2079

frames = iio.imread(RAW, plugin="pillow", index=...)
total  = len(frames)
print(f"Raw GIF: {total} frames ({total*67//1000}s at 15fps)")

if abs(total - CALIBRATED_TOTAL) > 100:
    ratio = total / CALIBRATED_TOTAL
    SEG1_START = int(SEG1_START * ratio); SEG1_END = int(SEG1_END * ratio)
    SEG2_START = int(SEG2_START * ratio); SEG2_END = int(SEG2_END * ratio)
    SEG3_START = int(SEG3_START * ratio); SEG3_END = int(SEG3_END * ratio)
    print(f"Scaled cut points for {total}-frame input")

seg1 = frames[SEG1_START:SEG1_END]
seg2 = frames[SEG2_START:SEG2_END]
seg3 = frames[SEG3_START:SEG3_END]
kept = np.concatenate([seg1, seg2, seg3], axis=0)

duration_s = len(kept) * 67 // 1000
print(f"Segments: {len(seg1)}f + {len(seg2)}f + {len(seg3)}f = {len(kept)} frames ({duration_s}s)")

iio.imwrite(OUT, kept, plugin="pillow", duration=67, loop=0)
print(f"Saved: {OUT}")
