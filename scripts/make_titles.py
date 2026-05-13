#!/usr/bin/env python3
"""Create title-card MP4s using PIL + ffmpeg for the demo video combine step."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "Pillow", "-q"], check=False)
    from PIL import Image, ImageDraw, ImageFont

W   = int(os.environ.get("SOURCE_W",   "1400"))
H   = int(os.environ.get("SOURCE_H",   "900"))
FPS = int(os.environ.get("SOURCE_FPS", "25"))
BG     = (40, 42, 54)    # Dracula background
GREY   = (98, 114, 164)  # Dracula comment colour

TITLES = [
    ("title_intro", "Agentic Self-Healing Kubernetes",
     "Perceive  →  Reason (multi-agent)  →  Plan  →  Act  →  Verify",
     (80, 250, 123)),   # green

    ("title_s1", "SCENARIO 1  —  High Error Rate",
     "5xx surge detected  ·  Agent investigates  ·  Restarts pods",
     (255, 85, 85)),    # red

    ("title_s2", "SCENARIO 2  —  P99 Latency Spike",
     "600ms delay  ·  P99 > 200ms SLO  ·  Agent scales up",
     (241, 250, 140)),  # yellow

    ("title_s3", "SCENARIO 3  —  Code Bug  (ZeroDivisionError)",
     "Empty window  ·  CodePatchAgent writes fix  ·  GitHub PR opened",
     (189, 147, 249)),  # purple

    ("title_s4", "SCENARIO 4  —  Stats Bug  (IndexError)",
     "Wrong percentile multiplier  ·  CodePatchAgent patches index calc  ·  PR opened",
     (255, 184, 108)),  # orange

    ("title_outro", "Four breaches — detected and fixed autonomously",
     "Evidence-gated diagnosis  ·  Structural confidence scoring  ·  HITL gates",
     (80, 250, 123)),   # green
]

DOCS = Path(__file__).parent.parent / "docs"
DOCS.mkdir(exist_ok=True)

# Try to find a monospace font
FONT_PATHS = [
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/Library/Fonts/Courier New.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]

def _font(size: int):
    for p in FONT_PATHS:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def make_frame(title: str, subtitle: str, color: tuple) -> Image.Image:
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    f_big = _font(52)
    f_sub = _font(28)

    # Title line — centred, ~40% from top
    bbox = draw.textbbox((0, 0), title, font=f_big)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((W - tw) // 2, H // 2 - 70), title, fill=color, font=f_big)

    # Subtitle line
    bbox2 = draw.textbbox((0, 0), subtitle, font=f_sub)
    sw = bbox2[2] - bbox2[0]
    draw.text(((W - sw) // 2, H // 2 + 20), subtitle, fill=GREY, font=f_sub)

    # Thin accent bar at top
    draw.rectangle([(0, 0), (W, 4)], fill=color)
    draw.rectangle([(0, H - 4), (W, H)], fill=color)

    return img


def make_title_mp4(name: str, title: str, subtitle: str, color: tuple, duration: int = 4):
    out_path = DOCS / f"{name}.mp4"
    frame    = make_frame(title, subtitle, color)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        frame.save(tmp.name)
        frame_path = tmp.name

    subprocess.run([
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", frame_path,
        "-t", str(duration),
        "-r", str(FPS),       # match VHS source clip framerate
        "-c:v", "libx264",
        "-crf", "17",         # high quality — title cards are mostly static
        "-preset", "slow",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ], capture_output=True, check=True)

    os.unlink(frame_path)
    print(f"  ✓ {out_path.name}")
    return out_path


if __name__ == "__main__":
    print("Creating title cards...")
    for name, title, subtitle, color in TITLES:
        make_title_mp4(name, title, subtitle, color)
    print("Done.")
