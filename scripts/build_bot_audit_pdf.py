from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data_vantage" / "audit_current_bot_all_modules_60sessions_20260812.json"
OUTPUT_PATH = ROOT / "output" / "pdf" / "xao_graal_raport_bota_60_sesji_2026-08-12.pdf"

FONT_DIR = Path("C:/Windows/Fonts")
FONT_REGULAR = "Arial"
FONT_BOLD = "Arial-Bold"
FONT_ITALIC = "Arial-Italic"

INK = colors.HexColor("#18212B")
MUTED = colors.HexColor("#5C6975")
TEAL = colors.HexColor("#087F75")
TEAL_DARK = colors.HexColor("#075E58")
GREEN = colors.HexColor("#18864B")
GREEN_PALE = colors.HexColor("#E8F5ED")
AMBER = colors.HexColor("#B66A00")
AMBER_PALE = colors.HexColor("#FFF3DB")
RED = colors.HexColor("#B43A3A")
RED_PALE = colors.HexColor("#FCEAEA")
BLUE = colors.HexColor("#2667A9")
BLUE_PALE = colors.HexColor("#EAF2FB")
LINE = colors.HexColor("#D8E0E6")
PANEL = colors.HexColor("#F4F7F8")
WHITE = colors.white


def _register_fonts() -> None:
    pdfmetrics.registerFont(TTFont(FONT_REGULAR, str(FONT_DIR / "arial.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(FONT_DIR / "arialbd.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_ITALIC, str(FONT_DIR / "ariali.ttf")))


def _load() -> dict[str, Any]:
    return json.loads(DATA_PATH.read_text(encoding="utf-8-sig"))


def _money(value: float, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:,.2f} USD".replace(",", " ")


def _pct(value: float, signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    return f"{prefix}{value:.2f}%".replace(".", ",")


class SectionRule(Flowable):
    def __init__(self, width: float, color: colors.Color = TEAL):
        super().__init__()
        self.width = width
        self.height = 2.5 * mm
        self.color = color

    def draw(self) -> None:
        self.canv.setFillColor(self.color)
        self.canv.roundRect(0, 0.9 * mm, self.width, 1.2 * mm, 0.6 * mm, fill=1, stroke=0)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "cover_brand": ParagraphStyle(
            "cover_brand",
            parent=base["Normal"],
            fontName=FONT_BOLD,
            fontSize=10,
            textColor=TEAL,
            leading=13,
            spaceAfter=5 * mm,
        ),
        "cover_title": ParagraphStyle(
            "cover_title",
            parent=base["Title"],
            fontName=FONT_BOLD,
            fontSize=27,
            leading=31,
            textColor=INK,
            alignment=TA_LEFT,
            spaceAfter=4 * mm,
        ),
        "cover_subtitle": ParagraphStyle(
            "cover_subtitle",
            parent=base["Normal"],
            fontName=FONT_REGULAR,
            fontSize=12,
            leading=17,
            textColor=MUTED,
            spaceAfter=8 * mm,
        ),
        "h1": ParagraphStyle(
            "h1",
            parent=base["Heading1"],
            fontName=FONT_BOLD,
            fontSize=19,
            leading=23,
            textColor=INK,
            spaceBefore=1 * mm,
            spaceAfter=2 * mm,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontName=FONT_BOLD,
            fontSize=13,
            leading=17,
            textColor=INK,
            spaceBefore=4 * mm,
            spaceAfter=2 * mm,
        ),
        "h3": ParagraphStyle(
            "h3",
            parent=base["Heading3"],
            fontName=FONT_BOLD,
            fontSize=10.5,
            leading=14,
            textColor=TEAL_DARK,
            spaceBefore=2.5 * mm,
            spaceAfter=1 * mm,
        ),
        "body": ParagraphStyle(
            "body",
            parent=base["BodyText"],
            fontName=FONT_REGULAR,
            fontSize=9.5,
            leading=14,
            textColor=INK,
            spaceAfter=2.5 * mm,
        ),
        "small": ParagraphStyle(
            "small",
            parent=base["BodyText"],
            fontName=FONT_REGULAR,
            fontSize=8,
            leading=11,
            textColor=MUTED,
        ),
        "table": ParagraphStyle(
            "table",
            parent=base["BodyText"],
            fontName=FONT_REGULAR,
            fontSize=8.1,
            leading=10.2,
            textColor=INK,
        ),
        "table_bold": ParagraphStyle(
            "table_bold",
            parent=base["BodyText"],
            fontName=FONT_BOLD,
            fontSize=8.1,
            leading=10.2,
            textColor=INK,
        ),
        "table_header": ParagraphStyle(
            "table_header",
            parent=base["BodyText"],
            fontName=FONT_BOLD,
            fontSize=8.1,
            leading=10.2,
            textColor=WHITE,
        ),
        "compact": ParagraphStyle(
            "compact",
            parent=base["BodyText"],
            fontName=FONT_REGULAR,
            fontSize=7.5,
            leading=9.2,
            textColor=INK,
        ),
        "bullet": ParagraphStyle(
            "bullet",
            parent=base["BodyText"],
            fontName=FONT_REGULAR,
            fontSize=8.7,
            leading=11.5,
            textColor=INK,
            spaceAfter=1.2 * mm,
        ),
        "callout": ParagraphStyle(
            "callout",
            parent=base["BodyText"],
            fontName=FONT_BOLD,
            fontSize=11,
            leading=16,
            textColor=INK,
            spaceAfter=0,
        ),
        "quote": ParagraphStyle(
            "quote",
            parent=base["BodyText"],
            fontName=FONT_ITALIC,
            fontSize=10,
            leading=15,
            textColor=INK,
        ),
        "center_small": ParagraphStyle(
            "center_small",
            parent=base["BodyText"],
            fontName=FONT_REGULAR,
            fontSize=8,
            leading=10,
            textColor=MUTED,
            alignment=TA_CENTER,
        ),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(text, style)


def _section(title: str, styles: dict[str, ParagraphStyle], width: float) -> list[Flowable]:
    return [_p(title, styles["h1"]), SectionRule(width), Spacer(1, 3 * mm)]


def _panel(content: Flowable | list[Flowable], width: float, background: colors.Color, border: colors.Color) -> Table:
    table = Table([[content]], colWidths=[width])
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), background),
                ("BOX", (0, 0), (-1, -1), 0.7, border),
                ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 4 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * mm),
            ]
        )
    )
    return table


def _data_table(
    rows: list[list[Any]],
    widths: list[float],
    styles: dict[str, ParagraphStyle],
    aligns: Iterable[str] | None = None,
    highlight_last: bool = False,
) -> Table:
    normalized: list[list[Any]] = []
    for row_index, row in enumerate(rows):
        style = styles["table_header"] if row_index == 0 else styles["table"]
        normalized.append([value if isinstance(value, Flowable) else _p(str(value), style) for value in row])
    table = Table(normalized, colWidths=widths, repeatRows=1, hAlign="LEFT")
    commands: list[tuple[Any, ...]] = [
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.45, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, PANEL]),
        ("TOPPADDING", (0, 0), (-1, -1), 1.7 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.7 * mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 2.3 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2.3 * mm),
    ]
    if aligns:
        for column, align in enumerate(aligns):
            commands.append(("ALIGN", (column, 1), (column, -1), align))
    if highlight_last and len(rows) > 1:
        commands.append(("BACKGROUND", (0, -1), (-1, -1), GREEN_PALE))
    table.setStyle(TableStyle(commands))
    return table


def _kpi_row(items: list[tuple[str, str, colors.Color]], styles: dict[str, ParagraphStyle], width: float) -> Table:
    cells = []
    for label, value, accent in items:
        value_style = ParagraphStyle(
            f"kpi_{label}",
            parent=styles["callout"],
            fontSize=15,
            leading=18,
            textColor=accent,
            alignment=TA_CENTER,
        )
        cells.append(
            [
                _p(value, value_style),
                _p(label, styles["center_small"]),
            ]
        )
    inner = []
    for cell in cells:
        t = Table([[cell[0]], [cell[1]]], colWidths=[width / len(items) - 4 * mm])
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), WHITE),
                    ("BOX", (0, 0), (-1, -1), 0.7, LINE),
                    ("TOPPADDING", (0, 0), (-1, 0), 4 * mm),
                    ("BOTTOMPADDING", (0, 1), (-1, 1), 4 * mm),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2 * mm),
                ]
            )
        )
        inner.append(t)
    outer = Table([inner], colWidths=[width / len(items)] * len(items), hAlign="LEFT")
    outer.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 1.5 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 1.5 * mm)]))
    return outer


def _bar_chart(items: list[tuple[str, float, colors.Color]], width: float, height: float) -> Drawing:
    drawing = Drawing(width, height)
    left = 40 * mm
    right = 9 * mm
    top = 5 * mm
    row_h = (height - top - 4 * mm) / max(1, len(items))
    max_value = max(abs(value) for _, value, _ in items) or 1.0
    bar_width = width - left - right
    for index, (label, value, color) in enumerate(items):
        y = height - top - (index + 1) * row_h + 2.3 * mm
        drawing.add(String(0, y + 1.1 * mm, label, fontName=FONT_REGULAR, fontSize=8, fillColor=INK))
        drawing.add(Rect(left, y, bar_width, 3.5 * mm, fillColor=PANEL, strokeColor=None))
        extent = bar_width * abs(value) / max_value
        drawing.add(Rect(left, y, extent, 3.5 * mm, fillColor=color, strokeColor=None))
        text = ("+" if value > 0 else "") + f"{value:.2f} USD"
        label_color = WHITE if extent >= bar_width * 0.82 else color
        drawing.add(String(width - right, y + 0.8 * mm, text, fontName=FONT_BOLD, fontSize=7.5, fillColor=label_color, textAnchor="end"))
    return drawing


def _architecture(width: float, height: float) -> Drawing:
    drawing = Drawing(width, height)
    boxes = [
        ("Telegram\nPhoenix VIP", 0, 34 * mm, 34 * mm, 17 * mm, BLUE_PALE, BLUE),
        ("Parser i\nwalidacja live", 45 * mm, 34 * mm, 39 * mm, 17 * mm, PANEL, INK),
        ("Market / range /\n6 nog TP1-TP6", 95 * mm, 34 * mm, 43 * mm, 17 * mm, GREEN_PALE, GREEN),
        ("MT5\nXAUUSD", 149 * mm, 34 * mm, 29 * mm, 17 * mm, AMBER_PALE, AMBER),
        ("M1 / M5 / M15", 0, 4 * mm, 34 * mm, 17 * mm, BLUE_PALE, BLUE),
        ("Silnik setupow\nXAU scalper", 45 * mm, 4 * mm, 39 * mm, 17 * mm, PANEL, INK),
        ("3 nogi TP1\nBE i filtry", 95 * mm, 4 * mm, 43 * mm, 17 * mm, GREEN_PALE, GREEN),
    ]
    for text, x, y, w, h, fill, stroke in boxes:
        drawing.add(Rect(x, y, w, h, rx=2 * mm, ry=2 * mm, fillColor=fill, strokeColor=stroke, strokeWidth=0.8))
        lines = text.split("\n")
        for line_index, line in enumerate(lines):
            drawing.add(String(x + w / 2, y + h / 2 + (2.2 - line_index * 4.2) * mm, line, fontName=FONT_BOLD if line_index == 0 else FONT_REGULAR, fontSize=8, fillColor=INK, textAnchor="middle"))
    arrow_color = MUTED
    for y in (42 * mm, 12 * mm):
        for x1, x2 in ((34 * mm, 45 * mm), (84 * mm, 95 * mm), (138 * mm, 149 * mm)):
            if y < 20 * mm and x1 >= 138 * mm:
                continue
            drawing.add(Line(x1 + 1 * mm, y, x2 - 1 * mm, y, strokeColor=arrow_color, strokeWidth=1.1))
            drawing.add(Line(x2 - 3 * mm, y + 1.5 * mm, x2 - 1 * mm, y, strokeColor=arrow_color, strokeWidth=1.1))
            drawing.add(Line(x2 - 3 * mm, y - 1.5 * mm, x2 - 1 * mm, y, strokeColor=arrow_color, strokeWidth=1.1))
    drawing.add(Line(138 * mm, 12 * mm, 149 * mm, 34 * mm, strokeColor=arrow_color, strokeWidth=1.1))
    return drawing


def _page(canvas, doc) -> None:
    canvas.saveState()
    page = canvas.getPageNumber()
    width, height = A4
    if page > 1:
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, height - 14 * mm, width - 18 * mm, height - 14 * mm)
        canvas.setFont(FONT_BOLD, 7.5)
        canvas.setFillColor(TEAL_DARK)
        canvas.drawString(18 * mm, height - 10 * mm, "XAO GRAAL - AUDYT AKTUALNEGO BOTA")
        canvas.setFont(FONT_REGULAR, 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(width - 18 * mm, height - 10 * mm, "60 sesji | 12.08.2026")
    canvas.setStrokeColor(LINE)
    canvas.line(18 * mm, 13 * mm, width - 18 * mm, 13 * mm)
    canvas.setFont(FONT_REGULAR, 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 8.5 * mm, "Raport historyczny - nie stanowi gwarancji przyszlych wynikow")
    canvas.drawRightString(width - 18 * mm, 8.5 * mm, f"Strona {page}")
    canvas.restoreState()


def _doc() -> tuple[BaseDocTemplate, float]:
    width, height = A4
    left = right = 18 * mm
    top = 20 * mm
    bottom = 18 * mm
    doc = BaseDocTemplate(
        str(OUTPUT_PATH),
        pagesize=A4,
        leftMargin=left,
        rightMargin=right,
        topMargin=top,
        bottomMargin=bottom,
        title="XAO Graal - raport bota za 60 sesji",
        author="XAO Graal",
        subject="Audyt strategii Phoenix VIP i XAU scalpera",
    )
    frame = Frame(left, bottom, width - left - right, height - top - bottom, id="normal")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPageEnd=_page)])
    return doc, width - left - right


def build() -> None:
    _register_fonts()
    data = _load()
    styles = _styles()
    doc, width = _doc()
    dynamic = data["normalized_start_1000_dynamic"]
    fixed = data["normalized_start_1000_fixed_001"]
    stability = data["stability_fixed_001"]
    first = stability["first_30_sessions"]
    last = stability["last_30_sessions"]
    source = dynamic["by_source"]
    story: list[Flowable] = []

    # Cover
    story.extend(
        [
            Spacer(1, 16 * mm),
            _p("XAO GRAAL / RAPORT STRATEGII", styles["cover_brand"]),
            _p("Aktualny bot XAU<br/>Audyt 60 sesji tradingowych", styles["cover_title"]),
            _p(
                "Phoenix VIP + moduł range + pre-sygnał góra/dół + XAU scalper<br/>"
                "Okres: 20.05.2026 - 11.08.2026 | Dane M1 VantageMarkets-Demo",
                styles["cover_subtitle"],
            ),
            _panel(
                _p(
                    "<font color='#18864B'>WERDYKT:</font> konfiguracja ma dodatnią wartość oczekiwaną na badanej próbie, "
                    "ale tempo zysku wyraźnie osłabło w drugiej połowie. Realne jest dalsze bycie na plusie; "
                    "wyniku +122,52% nie należy traktować jako prognozy ani gwarancji.",
                    styles["callout"],
                ),
                width,
                GREEN_PALE,
                GREEN,
            ),
            Spacer(1, 7 * mm),
            _kpi_row(
                [
                    ("Wynik dynamiczny", "+1 225,18 USD", GREEN),
                    ("Zamknięty max DD", "-283,56 USD", RED),
                    ("Dodatnie dni", "45 / 60", TEAL),
                ],
                styles,
                width,
            ),
            Spacer(1, 8 * mm),
            _p(
                "Raport przygotowany do udostępnienia. Nie zawiera loginów, haseł ani danych dostępowych do brokera lub Telegrama.",
                styles["small"],
            ),
        ]
    )
    story.append(PageBreak())

    # Executive summary and portfolio
    story.extend(_section("1. Podsumowanie dla kolegi", styles, width))
    story.append(
        _panel(
            _p(
                "Bot automatyzuje dwie niezależne ścieżki: kopiuje i zarządza sygnałami Phoenix VIP oraz samodzielnie "
                "wyszukuje krótkie setupy XAUUSD. Wszystkie aktywne transakcje zostały odtworzone chronologicznie na jednym "
                "saldzie, dzięki czemu dynamiczny lot i równoczesne pozycje wpływają na wynik tak jak w działającym systemie.",
                styles["body"],
            ),
            width,
            BLUE_PALE,
            BLUE,
        )
    )
    story.append(Spacer(1, 4 * mm))
    story.append(_p("Wynik całego portfela", styles["h2"]))
    story.append(
        _data_table(
            [
                ["Wariant", "Start", "Koniec", "Wynik", "Max DD"],
                ["Aktualny lot dynamiczny", _money(dynamic["start_balance"]), _money(dynamic["final_balance"]), f"{_money(dynamic['profit'], True)} ({_pct(dynamic['return_pct'], True)})", f"{_money(dynamic['max_closed_drawdown_usd'])} ({_pct(-dynamic['max_closed_drawdown_pct_start'])})"],
                ["Kontrolny 0,01 lot/nogę", _money(fixed["start_balance"]), _money(fixed["final_balance"]), f"{_money(fixed['profit'], True)} ({_pct(fixed['return_pct'], True)})", f"{_money(fixed['max_closed_drawdown_usd'])} ({_pct(-fixed['max_closed_drawdown_pct_start'])})"],
            ],
            [47 * mm, 28 * mm, 30 * mm, 38 * mm, 31 * mm],
            styles,
            ["LEFT", "RIGHT", "RIGHT", "RIGHT", "RIGHT"],
        )
    )
    story.append(Spacer(1, 4 * mm))
    story.append(
        _kpi_row(
            [
                ("Wszystkie nogi", f"{dynamic['accepted_legs']}", TEAL),
                ("Maks. jednocześnie", f"{dynamic['max_concurrent_positions']} pozycji", BLUE),
                ("Odrzucone przez margin", f"{dynamic['margin_skips']}", GREEN),
            ],
            styles,
            width,
        )
    )
    story.append(Spacer(1, 5 * mm))
    story.append(_p("Co ten wynik oznacza", styles["h2"]))
    story.append(
        _data_table(
            [
                ["Pytanie", "Odpowiedź"],
                ["Czy konfiguracja była historycznie zyskowna?", "Tak. Obie połowy testu zakończyły się dodatnio."],
                ["Czy +122,52% jest realistyczną stałą prognozą?", "Nie. To agresywny wynik in-sample, obciążony dopasowaniem strategii do części tej historii."],
                ["Który moduł tworzy przewagę?", "Przede wszystkim Phoenix. Scalper był dodatni w całości, lecz ujemny w ostatnich 30 sesjach."],
                ["Czego potrzeba przed kontem live?", "20-30 kolejnych sesji forward bez zmieniania reguł, lotów i godzin."],
            ],
            [63 * mm, 111 * mm],
            styles,
        )
    )
    story.append(PageBreak())

    # Modules and stability
    story.extend(_section("2. Wyniki modułów i stabilność", styles, width))
    module_rows = [["Moduł", "Nogi", "Win rate", "PnL dynamiczny"]]
    module_names = {
        "phoenix_full": "Phoenix - pełny sygnał",
        "phoenix_range": "Phoenix - widełki/range",
        "phoenix_direction": "Phoenix - pre-sygnał góra/dół",
        "scalper": "XAU scalper",
    }
    for key in ("phoenix_full", "phoenix_range", "phoenix_direction", "scalper"):
        row = source[key]
        module_rows.append([module_names[key], int(row["legs"]), _pct(row["win_rate_pct"]), _money(row["pnl"], True)])
    story.append(_data_table(module_rows, [68 * mm, 24 * mm, 34 * mm, 48 * mm], styles, ["LEFT", "RIGHT", "RIGHT", "RIGHT"]))
    story.append(Spacer(1, 5 * mm))
    story.append(_bar_chart([(module_names[key], float(source[key]["pnl"]), GREEN if source[key]["pnl"] >= 0 else RED) for key in ("phoenix_full", "phoenix_range", "phoenix_direction", "scalper")], width, 45 * mm))
    story.append(Spacer(1, 3 * mm))
    story.append(_p("Uwaga o liczebności", styles["h2"]))
    story.append(
        _p(
            "1 439 nóg nie oznacza 1 439 niezależnych decyzji. Pełny Phoenix odczytał 268 sygnałów i wykonał 118 z nich. "
            "Scalper utworzył 284 setupy po trzy nogi. Z 264 zapowiedzi góra/dół filtr pullbacku dopuścił 55 wejść. "
            "Win rate nóg jest więc statystyką zarządzania pozycjami, a nie liczbą niezależnych prognoz.",
            styles["body"],
        )
    )
    story.append(_p("Pierwsze i ostatnie 30 sesji", styles["h2"]))
    first_total = sum(float(row["pnl_at_001"]) for row in first.values())
    last_total = sum(float(row["pnl_at_001"]) for row in last.values())
    story.append(
        _data_table(
            [
                ["Moduł przy stałym 0,01", "Pierwsze 30", "Ostatnie 30", "Zmiana"],
                *[
                    [
                        module_names[key],
                        _money(first[key]["pnl_at_001"], True),
                        _money(last[key]["pnl_at_001"], True),
                        _money(last[key]["pnl_at_001"] - first[key]["pnl_at_001"], True),
                    ]
                    for key in ("phoenix_full", "phoenix_range", "phoenix_direction", "scalper")
                ],
                ["Suma", _money(first_total, True), _money(last_total, True), _money(last_total - first_total, True)],
            ],
            [66 * mm, 36 * mm, 36 * mm, 36 * mm],
            styles,
            ["LEFT", "RIGHT", "RIGHT", "RIGHT"],
            highlight_last=True,
        )
    )
    story.append(Spacer(1, 4 * mm))
    story.append(
        _panel(
            _p(
                "Najważniejsza anomalia: scalper przeszedł z +124,11 USD do -47,82 USD przy stałym 0,01. "
                "Phoenix również zwolnił, ale każdy jego aktywny moduł pozostał dodatni w obu połowach.",
                styles["callout"],
            ),
            width,
            AMBER_PALE,
            AMBER,
        )
    )
    story.append(PageBreak())

    # Architecture and settings
    story.extend(_section("3. Jak działa aktualny bot", styles, width))
    story.append(_architecture(width, 52 * mm))
    story.append(Spacer(1, 3 * mm))
    story.append(_p("Listener Phoenix VIP", styles["h2"]))
    story.append(
        _data_table(
            [
                ["Element", "Aktywne ustawienie"],
                ["Nasłuch", "Tylko Phoenix VIP, odświeżanie i watchdog co 0,5 sekundy"],
                ["Parser", "Łączy komunikat góra/dół, następną wiadomość z widełkami i pełny sygnał; obsługuje edycje oraz typową pomyłkę cyfry setek"],
                ["Pełny sygnał", "Do 6 nóg TP1-TP6, wejścia cyklicznie rozłożone na trzech poziomach widełek"],
                ["Ochrona", "TP1 bez BE; TP2-TP5 BE po TP1; TP6 z drabinką SL; zachowany SL autora"],
                ["Pendingi", "Wygaszenie po 15 minutach; usunięcie po TP2, secure, BE lub cancel"],
                ["Range", "Market, gdy cena jest użyteczna, oraz do 2 pendingów; TP 1,25/2/5 USD; SL 6 USD; BE +0,10 po +0,30"],
                ["Pre-sygnał", "EMA9/21 + krótkie cofnięcie M1; TP 1 USD; SL 6 USD; maks. 15 minut"],
                ["Lot", "Pełny Phoenix: 0,01/nogę przy 1 000 USD i +0,01 co 500 USD ponad bazę; range 0,02; pre-sygnał 0,01"],
            ],
            [45 * mm, 129 * mm],
            styles,
        )
    )
    story.append(Spacer(1, 4 * mm))
    story.append(_p("XAU scalper", styles["h2"]))
    story.append(
        _data_table(
            [
                ["Element", "Aktywne ustawienie"],
                ["Setupy", "TwoBar, ADX Breakout, SMC MSS, liquidity sweep, FVG retest i order block"],
                ["Dane", "M1/M5/M15, wyłącznie zamknięte świece, bez look-ahead wyższych interwałów"],
                ["Godziny UTC", "02:00-02:59, 05:00-05:59, 07:00-08:59, 11:00-12:59"],
                ["Pozycje", "3 nogi do TP1; TP 1,50 USD; efektywny SL 3,75 USD; BE +0,15 po ruchu +1 USD"],
                ["Filtry", "Jakość świecy, separacja i nachylenie EMA, przeciwny impuls, pogoń za ruchem i chwilowa jakość kierunku"],
                ["Lot", "0,01 lota na nogę za każde pełne 500 USD aktualnego balance"],
                ["Wyłączone", "Martingale, trailing, profit lock, kontrscalper, BTC scalper i zespoły agentów"],
            ],
            [45 * mm, 129 * mm],
            styles,
        )
    )
    story.append(PageBreak())

    # Improvements
    story.extend(_section("4. Droga do obecnej wersji", styles, width))
    story.append(
        _panel(
            _p(
                "Nie ma historii Git, dlatego nie istnieje uczciwa liczba pojedynczych edycji. Zakres można jednak policzyć "
                "jako <b>25 dużych pakietów funkcjonalnych</b>, 206 parametrów aktywnej konfiguracji, 68 skryptów badawczych "
                "i backtestowych oraz 147 testów regresji.",
                styles["callout"],
            ),
            width,
            PANEL,
            TEAL,
        )
    )
    story.append(Spacer(1, 5 * mm))
    correction_groups = [
        (
            "Parser i Telegram - 6 pakietów",
            [
                "1. Wnioskowanie BUY/SELL z relacji entry, TP i SL.",
                "2. Rozpoznawanie zapowiedzi „wrzucę teraz, góra/dół”.",
                "3. Łączenie zapowiedzi z wiadomością zawierającą same widełki.",
                "4. Naprawa typowej pomyłki cyfry setek względem ceny rynkowej.",
                "5. Walidacja poziomów sygnału względem aktualnego marketu.",
                "6. Obsługa edycji, odpowiedzi, fingerprintów i duplikatów.",
            ],
        ),
        (
            "Egzekucja Phoenix - 7 pakietów",
            [
                "7. Decyzja market kontra pending na podstawie położenia ceny.",
                "8. Osobny moduł staged range z marketem i pendingami.",
                "9. Cykliczne mapowanie trzech wejść na kolejne TP.",
                "10. Rozszerzenie pełnego sygnału do TP1-TP6.",
                "11. Zachowanie oryginalnego SL i fallback dla błędnego SL.",
                "12. Profile BE oraz drabinka SL dla dalszej nogi.",
                "13. Wygaszanie i kasowanie pendingów po komunikatach kanału.",
            ],
        ),
        (
            "Lot i zarządzanie pozycją - 4 pakiety",
            [
                "14. Dynamiczny lot pełnych sygnałów Phoenix.",
                "15. Osobne loty modułu range i pre-sygnału.",
                "16. Reconcile wcześniejszych wejść oraz wyjście ze starego zysku.",
                "17. Model spreadu, prowizji, marginu i równoczesnych pozycji.",
            ],
        ),
        (
            "XAU scalper - 5 pakietów",
            [
                "18. Silnik wielu strategii zamiast pojedynczego triggera.",
                "19. Synchronizacja M1/M5/M15 tylko po zamknięciu świecy.",
                "20. Filtr jakości świecy, EMA i struktury ruchu.",
                "21. Blokady pogoni, przeciwnego impulsu i słabego kierunku.",
                "22. Kalibracja godzin, trzech nóg, TP, SL oraz BE.",
            ],
        ),
        (
            "Niezawodność - 3 pakiety",
            [
                "23. Polling i watchdog kanału co 0,5 sekundy.",
                "24. Logowanie decyzji, dashboard i raportowanie PnL.",
                "25. Nadzór procesów, restart profili i testy regresji.",
            ],
        ),
    ]
    for title, items in correction_groups:
        story.append(_p(title, styles["h3"]))
        rows = [[_p(item, styles["compact"])] for item in items]
        table = Table(rows, colWidths=[width])
        table.setStyle(
            TableStyle(
                [
                    ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, PANEL]),
                    ("BOX", (0, 0), (-1, -1), 0.45, LINE),
                    ("INNERGRID", (0, 0), (-1, -1), 0.25, LINE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 0.9 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0.9 * mm),
                ]
            )
        )
        story.append(table)
    story.append(PageBreak())

    # Methodology and handoff
    story.extend(_section("5. Metodologia i wiarygodność", styles, width))
    story.append(
        _data_table(
            [
                ["Obszar", "Zastosowana zasada"],
                ["Dane", "60 zakończonych sesji XAUUSD na świecach M1 brokera VantageMarkets-Demo"],
                ["Czas Telegrama", "Wiadomości dopasowane do zegara brokera; wykonanie od następnej pełnej świecy M1"],
                ["Koszty", "Historyczny spread każdej świecy oraz prowizja 0,06 USD na 0,01 lota"],
                ["Konflikt TP/SL", "Jeśli oba poziomy wystąpiły w tej samej świecy M1, konserwatywnie liczony był najpierw SL"],
                ["Kapitał", "Wspólne saldo, chronologiczne zamknięcia, przeliczanie lota przy każdym wejściu i kontrola marginu"],
                ["Drawdown", "Dokładny dla zamkniętych transakcji; bez pełnej rekonstrukcji tickowego floating DD"],
            ],
            [45 * mm, 129 * mm],
            styles,
        )
    )
    story.append(Spacer(1, 5 * mm))
    story.append(_p("Ograniczenia, których nie wolno pomijać", styles["h2"]))
    limitations = [
        "Strategia była rozwijana na części tej historii, więc jest to test in-sample, a nie czysta walidacja out-of-sample.",
        "M1 nie odtwarza kolejności wszystkich ticków; konserwatywna zasada SL-first ogranicza, ale nie usuwa tej niepewności.",
        "Nie da się historycznie odtworzyć każdego opóźnienia sieci, poślizgu, requote ani odrzucenia zlecenia.",
        "Zamknięty DD -28,36% w dynamicznym wariancie oznacza agresywny profil; rzeczywisty floating DD mógł być wyższy.",
        "Spadek wyniku drugiej połowy pokazuje zmianę warunków rynku i ryzyko degradacji edge'u.",
    ]
    story.append(
        _panel(
            [_p(f"• {item}", styles["bullet"]) for item in limitations],
            width,
            RED_PALE,
            RED,
        )
    )
    story.append(Spacer(1, 5 * mm))
    story.append(_p("Rekomendacja", styles["h2"]))
    story.append(
        _panel(
            _p(
                "Zamrozić aktualną konfigurację na 20-30 kolejnych sesji demo. Raportować osobno Phoenix full, range, "
                "pre-sygnał i scalper. Decyzję o live oprzeć na wyniku forward, maksymalnym floating DD oraz zgodności "
                "realnych zleceń z backtestem. W obecnych danych Phoenix zasługuje na dalszy test; scalper nie powinien "
                "otrzymać większego lota, dopóki jego ostatnia połowa pozostaje ujemna.",
                styles["callout"],
            ),
            width,
            GREEN_PALE,
            GREEN,
        )
    )
    story.append(Spacer(1, 7 * mm))
    story.append(_p("Krótki opis do przekazania", styles["h2"]))
    story.append(
        _panel(
            _p(
                "XAO Graal to modułowy bot MT5 do XAUUSD. Łączy listener Phoenix VIP z samodzielnym scalperem M1/M5/M15. "
                "Listener rozpoznaje kierunek, widełki, pełny sygnał, edycje i błędne cyfry, a następnie rozkłada pozycję "
                "na TP1-TP6 z BE i drabinką SL. Scalper wykorzystuje TwoBar, ADX Breakout i wybrane setupy SMC. W teście "
                "60 sesji od 1 000 USD aktualny dynamiczny profil zakończył się saldem 2 225,18 USD, lecz ze znacznym "
                "zamkniętym DD 283,56 USD i wyraźnie słabszą drugą połową. To działający model wymagający dalszego forward testu, "
                "a nie obietnica zysku.",
                styles["quote"],
            ),
            width,
            PANEL,
            LINE,
        )
    )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    doc.build(story)
    print(OUTPUT_PATH)


if __name__ == "__main__":
    build()
