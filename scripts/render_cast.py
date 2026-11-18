"""Convierte un .cast (asciicast v2) en un GIF animado y una captura PNG del final.

    python scripts/render_cast.py demo.cast demo.gif demo.png [--max-idle 1.2]

Es un emulador de terminal mínimo (texto, saltos de línea, ``\\r`` y colores SGR), suficiente
para la salida de las demos. Las pausas largas se acortan a ``--max-idle`` segundos en el GIF;
los tiempos que imprime la propia demo (``[ 12.3s]``) son los reales.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
BG, FG, BAR = (24, 26, 33), (214, 219, 228), (44, 47, 58)
COLORS = {
    30: (60, 64, 75), 31: (240, 113, 120), 32: (152, 214, 120), 33: (240, 198, 116), 34: (110, 170, 245),
    35: (205, 140, 235), 36: (102, 214, 222), 37: (214, 219, 228), 90: (125, 132, 150),
}
SGR = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")


@dataclass(frozen=True)
class Style:
    fg: tuple[int, int, int] = FG
    bold: bool = False
    dim: bool = False


class Screen:
    def __init__(self, cols: int) -> None:
        self.cols = cols
        self.lines: list[list[tuple[str, Style]]] = [[]]
        self.col = 0
        self.style = Style()

    def feed(self, text: str) -> None:
        pos = 0
        for m in SGR.finditer(text):
            self._text(text[pos : m.start()])
            if m.group(2) == "m":
                self._sgr(m.group(1))
            pos = m.end()
        self._text(text[pos:])

    def _sgr(self, params: str) -> None:
        fg, bold, dim = self.style.fg, self.style.bold, self.style.dim
        for p in [int(x) for x in params.split(";") if x] or [0]:
            if p == 0:
                fg, bold, dim = FG, False, False
            elif p == 1:
                bold = True
            elif p == 2:
                dim = True
            elif p in COLORS:
                fg = COLORS[p]
        self.style = Style(fg, bold, dim)

    def _text(self, text: str) -> None:
        for ch in text:
            if ch == "\r":
                self.col = 0
            elif ch == "\n":
                self.lines.append([])
                self.col = 0
            elif ch == "\x1b" or ord(ch) < 32:
                continue
            else:
                if self.col >= self.cols:
                    self.lines.append([])
                    self.col = 0
                line = self.lines[-1]
                if self.col < len(line):
                    line[self.col] = (ch, self.style)
                else:
                    line.append((ch, self.style))
                self.col += 1

    def snapshot(self) -> list[list[tuple[str, Style]]]:
        return [list(line) for line in self.lines]


class Painter:
    def __init__(self, cols: int, title: str, size: int = 15) -> None:
        self.font = ImageFont.truetype(FONT, size)
        self.bold = ImageFont.truetype(FONT_BOLD, size)
        self.cw = round(self.font.getlength("M"))
        self.lh = size + 5
        self.cols, self.title = cols, title
        self.pad, self.bar = 16, 34

    def paint(self, lines: list[list[tuple[str, Style]]], rows: int) -> Image.Image:
        w = self.pad * 2 + self.cw * self.cols
        h = self.bar + self.pad * 2 + self.lh * rows
        img = Image.new("RGB", (w, h), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, w, self.bar], fill=BAR)
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([14 + i * 22, 11, 26 + i * 22, 23], fill=c)
        tw = self.font.getlength(self.title)
        d.text(((w - tw) / 2, 8), self.title, font=self.font, fill=(170, 176, 190))
        visible = lines[-rows:]
        for r, line in enumerate(visible):
            y = self.bar + self.pad + r * self.lh
            for c, (ch, st) in enumerate(line):
                if ch == " ":
                    continue
                fg = tuple((a + b) // 2 for a, b in zip(st.fg, BG)) if st.dim else st.fg
                d.text((self.pad + c * self.cw, y), ch, font=self.bold if st.bold else self.font, fill=fg)
        return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cast")
    ap.add_argument("gif")
    ap.add_argument("png")
    ap.add_argument("--max-idle", type=float, default=1.2)
    ap.add_argument("--rows", type=int, default=0, help="filas visibles en el GIF (0 = las del .cast)")
    ap.add_argument("--hold", type=float, default=6.0, help="segundos que se mantiene el último fotograma")
    a = ap.parse_args()

    with open(a.cast, encoding="utf-8") as fh:
        header = json.loads(fh.readline())
        events = [json.loads(line) for line in fh if line.strip()]
    cols, rows = header["width"], a.rows or header["height"]
    painter = Painter(cols, header.get("title") or "install-mcp")
    screen = Screen(cols)

    # Agrupa eventos cercanos (<80 ms) en un fotograma.
    groups: list[tuple[float, str]] = []
    for t, kind, data in events:
        if kind != "o":
            continue
        if groups and t - groups[-1][0] < 0.08:
            groups[-1] = (groups[-1][0], groups[-1][1] + data)
        else:
            groups.append((t, data))

    frames: list[Image.Image] = []
    durations: list[int] = []
    for i, (t, data) in enumerate(groups):
        gap = (groups[i + 1][0] - t) if i + 1 < len(groups) else a.hold
        # Las líneas que llegan juntas se muestran una a una (60 ms) para que se puedan seguir.
        parts = [p for p in re.split(r"(?<=\n)", data) if p]
        for j, part in enumerate(parts):
            screen.feed(part)
            last = j == len(parts) - 1
            dur = (a.hold if i + 1 == len(groups) else min(gap, a.max_idle)) if last else 0.06
            frames.append(painter.paint(screen.snapshot(), rows))
            durations.append(max(40, int(dur * 1000)))

    pal = [f.convert("P", palette=Image.Palette.ADAPTIVE, colors=128) for f in frames]
    pal[0].save(a.gif, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True, disposal=1)
    full = screen.snapshot()
    while full and not full[-1]:
        full.pop()
    painter.paint(full, len(full)).save(a.png, optimize=True)
    print(f"{a.gif}: {len(frames)} fotogramas; {a.png}: {len(full)} líneas")


if __name__ == "__main__":
    main()
