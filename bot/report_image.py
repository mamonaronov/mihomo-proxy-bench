"""PNG report with every scheme's numbers. Telegram text cannot fit them."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1680
PAD = 36
BG = (16, 18, 24)
CARD = (28, 32, 42)
HEAD = (36, 42, 56)
TEXT = (236, 238, 242)
MUTED = (156, 164, 178)
AMBER = (224, 176, 72)
GREEN = (88, 196, 140)
RED = (224, 104, 98)

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

COLUMNS: list[tuple[str, str, int]] = [
    ("rank", "#", 48),
    ("id", "Схема", 168),
    ("success", "Успех", 100),
    ("samples", "Замеры", 168),
    ("errors", "Ошибки", 150),
    ("avg", "Средняя", 110),
    ("p50", "p50", 100),
    ("p95", "p95", 100),
    ("p99", "p99", 100),
    ("stdev", "Разброс", 120),
    ("timeouts", "Таймауты", 120),
    ("switches", "Смена", 90),
    ("streak", "Простой", 110),
]


def _font_path(candidates: tuple[str, ...]) -> str:
    for path in candidates:
        if Path(path).is_file():
            return path
    raise RuntimeError("no TTF font found for the report image")


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(_font_path(_BOLD_CANDIDATES if bold else _FONT_CANDIDATES), size)


def _wrap(font: ImageFont.FreeTypeFont, text: str, width: int) -> list[str]:
    words = str(text).split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if font.getlength(trial) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _success_color(value: float | None) -> tuple[int, int, int]:
    if not isinstance(value, (int, float)):
        return MUTED
    if value >= 0.99:
        return GREEN
    if value >= 0.95:
        return AMBER
    return RED


class _Sheet:
    def __init__(self) -> None:
        self.y = PAD
        self.ops: list[tuple[Any, ...]] = []
        self.title = _font(34, bold=True)
        self.section = _font(22, bold=True)
        self.body = _font(20)
        self.small = _font(18)
        self.table = _font(18)
        self.table_bold = _font(18, bold=True)

    def gap(self, pixels: int) -> None:
        self.y += pixels

    def text_at(
        self,
        x: int,
        y: int,
        value: str,
        font: ImageFont.FreeTypeFont,
        color: tuple[int, int, int],
    ) -> None:
        self.ops.append(("text", x, y, value, font, color))

    def line(self, value: str, font: ImageFont.FreeTypeFont, color: tuple[int, int, int], x: int = PAD) -> None:
        self.text_at(x, self.y, value, font, color)
        self.y += int(font.size * 1.4)

    def wrapped(self, value: str, font: ImageFont.FreeTypeFont, color: tuple[int, int, int], width: int, x: int = PAD) -> None:
        for part in _wrap(font, value, width):
            self.line(part, font, color, x)

    def card(self, y0: int, y1: int, fill: tuple[int, int, int]) -> None:
        self.ops.insert(0, ("rect", PAD, y0, WIDTH - PAD, y1, fill))

    def finish(self) -> bytes:
        image = Image.new("RGB", (WIDTH, self.y + PAD), BG)
        draw = ImageDraw.Draw(image)
        for op in self.ops:
            if op[0] == "rect":
                _, x0, y0, x1, y1, fill = op
                draw.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=fill)
            else:
                _, x, y, value, font, color = op
                draw.text((x, y), value, font=font, fill=color)
        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def render_report_png(report: dict[str, Any]) -> bytes:
    sheet = _Sheet()
    inner = WIDTH - PAD * 2
    sheet.line(str(report["title"]), sheet.title, TEXT)
    sheet.gap(4)
    sheet.line(f"За {report['period']}", sheet.body, MUTED)
    sheet.gap(6)
    for item in report.get("meta") or []:
        sheet.wrapped(str(item), sheet.small, MUTED, inner)
    sheet.gap(18)

    notes = [str(item) for item in report.get("notes") or [] if str(item).strip()]
    axes = [str(item) for item in report.get("axes") or [] if str(item).strip()]
    if notes or axes:
        top = sheet.y
        sheet.gap(16)
        sheet.line("Рекомендации", sheet.section, TEXT, PAD + 16)
        for item in notes:
            sheet.wrapped(item, sheet.body, TEXT, inner - 32, PAD + 16)
        for item in axes:
            sheet.wrapped(item, sheet.small, MUTED, inner - 32, PAD + 16)
        sheet.gap(14)
        sheet.card(top, sheet.y, CARD)
        sheet.gap(20)

    rows: list[dict[str, Any]] = list(report.get("rows") or [])
    if rows:
        sheet.line("Все схемы", sheet.section, TEXT)
        sheet.gap(8)
        _table(sheet, rows)
        sheet.gap(22)
        sheet.line("Подробно", sheet.section, TEXT)
        sheet.gap(10)
        for row in rows:
            _detail(sheet, row, inner)
            sheet.gap(12)
    return sheet.finish()


def _table(sheet: _Sheet, rows: list[dict[str, Any]]) -> None:
    row_h = 42
    y = sheet.y
    sheet.card(y, y + row_h, HEAD)
    x = PAD + 14
    for _key, label, width in COLUMNS:
        sheet.text_at(x, y + 10, label, sheet.table_bold, MUTED)
        x += width
    y += row_h
    for index, row in enumerate(rows):
        fill = (24, 28, 38) if index % 2 == 0 else (20, 23, 32)
        sheet.card(y, y + row_h, fill)
        x = PAD + 14
        tone = _success_color(row.get("success_value"))
        for key, _label, width in COLUMNS:
            value = str(row.get(key) or "—")
            color = tone if key == "success" else TEXT
            font = sheet.table_bold if key in {"id", "success"} else sheet.table
            sheet.text_at(x, y + 10, value, font, color)
            x += width
        y += row_h
    sheet.y = y


def _detail(sheet: _Sheet, row: dict[str, Any], inner: int) -> None:
    top = sheet.y
    sheet.gap(14)
    title = f"{row.get('rank', '')}. {row.get('id', '')}".strip()
    sheet.line(title, sheet.section, TEXT, PAD + 16)
    advice = str(row.get("advice") or "").strip()
    if advice:
        sheet.wrapped(advice, sheet.body, TEXT, inner - 32, PAD + 16)
    note = str(row.get("note") or "").strip()
    if note:
        sheet.wrapped(note, sheet.small, AMBER, inner - 32, PAD + 16)
    for item in row.get("lines") or []:
        sheet.wrapped(str(item), sheet.small, MUTED, inner - 32, PAD + 16)
    sheet.gap(12)
    sheet.card(top, sheet.y, CARD)
