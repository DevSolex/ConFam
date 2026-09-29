"""
services/messaging/statement.py — merchant sales statement (PDF).

Read-only rendering of a merchant's confirmed sales, delivered as a PDF over
WhatsApp by the LEDGER / STATEMENT command in services/messaging/main.py.

Design constraints:

  - Read-only. This module SELECTs from ledger_entries and never writes. The
    ledger is append-only (Engineering Rule 2) and has no UPDATE/DELETE grant
    for confam_app, so there is deliberately no "mark as read" side effect and
    no summary/state table to keep in sync.
  - Integer money. amount_minor_units is BIGINT kobo and the DB forbids
    amounts <= 0, so a ledger row is always a positive credit that
    increases the merchant's balance. format_naira() converts with divmod on
    ints only — never `/ 100`, never a float (Engineering Rule 6).
  - No native dependencies. fpdf2 with its built-in core fonts is pure Python;
    no Cairo/Pango/reportlab, so the deploy image gains nothing native.

Why the amounts are described as "collected" and not "paid out": a ledger
entry is written when the rail confirms the *buyer's* payment was collected.
It does not prove the merchant's bank account was credited. OQ-020 tracks that
gap, so every statement carries a disclaimer saying so. Do not soften that
wording until OQ-020 is resolved.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from fpdf import FPDF

# Rule 6 input is kobo (NGN's smallest unit). 100 kobo = 1 naira.
KIBO_PER_NAIRA = 100

# Most recent entries are kept; the PDF then prints them oldest-first. A
# merchant with more sales than this gets an explicit "showing the most recent
# 500 of N total sales" note rather than a silently truncated statement.
LEDGER_DISPLAY_LIMIT = 500

# Rows are drawn oldest-first after taking the most recent LEDGER_DISPLAY_LIMIT.
_RUNNING_TOTAL_LABEL = "Total confirmed sales"


@dataclass(frozen=True)
class LedgerRow:
    """One confirmed sale as displayed in the statement.

    status is derived from entry_type rather than payment_links.status: the
    link lifecycle (created/opened/paid/settling/logged/expired/failed) is
    internal plumbing, and "Logged" means nothing to a shop owner. A sale reads
    "Confirmed"; a reconciliation correction reads "Correction" so a merchant
    can see the row is an adjustment and not new revenue.
    """

    confirmed_at: datetime
    description: str
    amount_minor_units: int
    status: str
    entry_type: str
    rail: str


def format_naira(amount_minor_units: int) -> str:
    """Format kobo as a grouped naira string using integer arithmetic only.

    75000 -> 'NGN 750.00', 1 -> 'NGN 0.01', 1234567 -> 'NGN 12,345.67'

    Uses divmod on ints rather than dividing by 100.0 so large or long-running
    balances never pick up float rounding error (Engineering Rule 6).

    The PDF uses the 'NGN' code rather than the '₦' sign: fpdf2's built-in
    Helvetica core font is latin-1 only, and '₦' (U+20A6) is outside that
    range, so a naira sign would raise FPDFUnicodeEncodingException.
    """
    if not isinstance(amount_minor_units, int):
        raise TypeError(
            f"amount_minor_units must be int kobo, got {type(amount_minor_units).__name__}"
        )

    sign = "-" if amount_minor_units < 0 else ""
    whole, kobo = divmod(abs(amount_minor_units), KIBO_PER_NAIRA)
    return f"{sign}NGN {whole:,}.{kobo:02d}"


def ledger_row_status(entry_type: str) -> str:
    """Merchant-facing status label for a ledger entry."""
    return "Correction" if entry_type == "correction" else "Confirmed"


def _pdf_safe(text: str) -> str:
    """Coerce text to the latin-1 range fpdf2's core fonts can encode.

    A merchant's business name or a payment description is arbitrary
    user input and may contain emoji, CJK, or other characters that the core
    fonts cannot encode — fpdf2 raises FPDFUnicodeEncodingException on those
    and the whole statement would fail to render. Losing a glyph is strictly
    better than failing to deliver the document.
    """
    return (
        str(text)
        .replace("\n", " ")
        .replace("\r", " ")
        .encode("latin-1", "replace")
        .decode("latin-1")
        .strip()
    )


def _truncate(text: str, max_chars: int) -> str:
    """Shorten over-long cell text so one row cannot break the table layout.

    The ellipsis is three ASCII dots, not U+2026: fpdf2's core fonts encode to
    latin-1, where U+2026 is not representable.
    """
    text = _pdf_safe(text)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def format_row_date(confirmed_at: datetime) -> str:
    """'17 Sep 2026' — day-level precision, which is all a statement needs."""
    return confirmed_at.strftime("%d %b %Y")


def format_generated_at(generated_at: datetime) -> str:
    """'17 Sep 2026, 14:05 UTC' — when this specific PDF was produced."""
    stamp = generated_at.strftime("%d %b %Y, %H:%M")
    tz = generated_at.strftime("%Z") or generated_at.strftime("%z")
    return f"{stamp} {tz}".strip()


# ---------------------------------------------------------------------------
# Read-only retrieval
# ---------------------------------------------------------------------------


def fetch_ledger_rows(
    conn, merchant_id, limit: int = LEDGER_DISPLAY_LIMIT
) -> tuple[list[LedgerRow], int]:
    """Load a merchant's most recent confirmed sales, newest-first selection.

    Returns (rows, total_count) where rows holds at most `limit` entries in
    display order (oldest first) and total_count is the merchant's full number
    of ledger entries. Callers use total_count to disclose truncation; it is
    the count for this merchant only.

    Rows are selected DESC (newest first) so LIMIT keeps the most recent
    `limit` entries, then reversed so the PDF reads chronologically top to
    bottom with the running total climbing.

    Merchant scoping happens in the WHERE clause, not in Python, so a bug
    elsewhere cannot widen the result set: one merchant can never see another
    merchant's sales.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*)
                 FROM ledger_entries
                WHERE merchant_id = %s""",
            (merchant_id,),
        )
        total_count = int(cur.fetchone()[0])

        cur.execute(
            """SELECT e.confirmed_at,
                      l.description,
                      e.amount_minor_units,
                      e.entry_type,
                      e.rail
                 FROM ledger_entries e
                 JOIN payment_links l ON l.link_id = e.link_id
                WHERE e.merchant_id = %s
                ORDER BY e.confirmed_at DESC, e.created_at DESC
                LIMIT %s""",
            (merchant_id, limit),
        )
        raw = cur.fetchall()

    rows = [
        LedgerRow(
            confirmed_at=confirmed_at,
            description=description,
            amount_minor_units=int(amount_minor_units),
            status=ledger_row_status(entry_type),
            entry_type=entry_type,
            rail=rail,
        )
        for confirmed_at, description, amount_minor_units, entry_type, rail in raw
    ]
    rows.reverse()  # oldest first for display

    return rows, total_count


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

_PAGE_MARGIN_MM = 15
_ROW_HEIGHT_MM = 7.0
_HEADING_HEIGHT_MM = 9.0

# A4 portrait is 210mm wide; margins leave 180mm of usable width.
_COL_DATE_MM = 26.0
_COL_AMOUNT_MM = 32.0
_COL_STATUS_MM = 24.0
_COL_DESCRIPTION_MM = 180.0 - _COL_DATE_MM - _COL_AMOUNT_MM - _COL_STATUS_MM

# ASCII punctuation only: the em dash this would naturally use (U+2014) is not
# encodable by fpdf2's latin-1 core fonts and would raise during rendering.
_DISCLAIMER = (
    "These amounts are payments collected on your behalf, not a bank "
    "statement. ConFam records a sale when the buyer's payment is confirmed, "
    "which is not proof that the money has reached your bank account. "
    "Settlement runs on your banking partner's schedule. Figures may still "
    "change after refunds, reversals, or failed settlement runs."
)

_TRUNCATION_NOTE_PREFIX = "Showing the most recent"


class _StatementPDF(FPDF):
    """A4 statement with a column header repeated on every page.

    fpdf2 calls header() automatically for the first page and for each page
    added by an automatic page break, so the column titles stay visible on a
    long statement without any manual bookkeeping.
    """

    def header(self) -> None:
        self.set_font("Helvetica", "B", 9)
        self.set_fill_color(238, 238, 238)
        self.cell(_COL_DATE_MM, _ROW_HEIGHT_MM, "DATE", border=1, fill=True)
        self.cell(_COL_DESCRIPTION_MM, _ROW_HEIGHT_MM, "DESCRIPTION", border=1, fill=True)
        self.cell(_COL_AMOUNT_MM, _ROW_HEIGHT_MM, "AMOUNT", border=1, fill=True, align="R")
        self.cell(_COL_STATUS_MM, _ROW_HEIGHT_MM, "STATUS", border=1, fill=True)
        self.ln(_ROW_HEIGHT_MM)


def _statement_pdf_header(
    pdf: _StatementPDF, business_name: str, generated_at: datetime
) -> None:
    """Title block: what this document is, whose it is, and when it was made."""
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, _HEADING_HEIGHT_MM, "ConFam Sales Statement", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 11)
    pdf.cell(
        0,
        _ROW_HEIGHT_MM,
        f"Business: {_pdf_safe(business_name) or 'Unnamed business'}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.cell(
        0,
        _ROW_HEIGHT_MM,
        f"Generated: {format_generated_at(generated_at)}",
        new_x="LMARGIN",
        new_y="NEXT",
    )
    pdf.ln(2)


def _add_table_row(pdf: _StatementPDF, row: LedgerRow) -> None:
    """Draw one sale, starting a new page first if it would not fit whole."""
    # Advance the page ourselves before a row can straddle the footer margin;
    # a row is never split across pages.
    if pdf.get_y() + _ROW_HEIGHT_MM > pdf.h - _PAGE_MARGIN_MM:
        pdf.add_page()

    pdf.set_font("Helvetica", "", 9)
    pdf.cell(_COL_DATE_MM, _ROW_HEIGHT_MM, format_row_date(row.confirmed_at), border=1)
    pdf.cell(
        _COL_DESCRIPTION_MM,
        _ROW_HEIGHT_MM,
        _truncate(row.description, 46),
        border=1,
    )
    pdf.cell(
        _COL_AMOUNT_MM,
        _ROW_HEIGHT_MM,
        format_naira(row.amount_minor_units),
        border=1,
        align="R",
    )
    pdf.cell(_COL_STATUS_MM, _ROW_HEIGHT_MM, row.status, border=1)
    pdf.ln(_ROW_HEIGHT_MM)


def _add_truncation_note(pdf: _StatementPDF, rows_shown: int, total_count: int) -> None:
    """Disclose that the table is a window onto a longer history.

    The number rendered is len(rows), not LEDGER_DISPLAY_LIMIT, so the note
    can never claim a limit the document did not actually apply.
    """
    note = f"{_TRUNCATION_NOTE_PREFIX} {rows_shown:,} of {total_count:,} total sales."
    pdf.set_font("Helvetica", "I", 9)
    pdf.cell(0, _ROW_HEIGHT_MM, note, new_x="LMARGIN", new_y="NEXT")


def _add_running_total(pdf: _StatementPDF, running_total_minor_units: int) -> None:
    """The running total, labelled so it cannot be read as a payout balance."""
    pdf.ln(1)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(
        0,
        _ROW_HEIGHT_MM,
        f"{_RUNNING_TOTAL_LABEL}: {format_naira(running_total_minor_units)}",
        new_x="LMARGIN",
        new_y="NEXT",
    )


def _add_disclaimer(pdf: _StatementPDF) -> None:
    pdf.ln(1)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 4, _pdf_safe(_DISCLAIMER), new_x="LMARGIN", new_y="NEXT")


def build_statement_pdf(
    business_name: str,
    rows: list[LedgerRow],
    total_count: int,
    generated_at: datetime,
) -> bytes:
    """Render the statement PDF and return its bytes, ready to upload to Meta.

    rows must be non-empty; callers check for an empty ledger first and reply
    in plain text rather than sending a document with nothing in it.

    Rows are drawn in exactly the order given, so the caller controls display
    order: fetch_ledger_rows() returns them oldest-first, which is the order
    the running total should climb in.

    The running total is a plain integer sum of the displayed rows. Because
    ledger_entries.amount_minor_units is CHECK (> 0) and a correction is a new
    positive row rather than a mutation of the original (Rule 2), every row
    adds to the total — a correction is an adjustment that itself credits,
    never a subtraction of an earlier row.

    fpdf2 core fonts are latin-1, so all dynamic text is passed through
    _pdf_safe() before drawing.
    """
    if not rows:
        raise ValueError("build_statement_pdf requires at least one row")

    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)

    pdf = _StatementPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=_PAGE_MARGIN_MM)
    pdf.set_title("ConFam Sales Statement")
    pdf.set_creator("ConFam")
    pdf.add_page()

    _statement_pdf_header(pdf, business_name, generated_at)

    if total_count > len(rows):
        _add_truncation_note(pdf, len(rows), total_count)

    running_total = 0
    for row in rows:
        _add_table_row(pdf, row)
        running_total += row.amount_minor_units

    _add_running_total(pdf, running_total)
    _add_disclaimer(pdf)

    return bytes(pdf.output())


def statement_filename(business_name: str, generated_at: datetime) -> str:
    """'confam-statement-solex-shoes-2026-09-17.pdf'

    The name goes into a multipart upload to Meta, so it is reduced to
    lowercase ASCII with dashes: an apostrophe, a slash, or a non-latin
    character in a business name must not produce a rejected upload or a
    mangled filename on the merchant's phone.
    """
    safe_name = _pdf_safe(business_name)
    slug = "".join(
        ch if (ch.isascii() and (ch.isalnum() or ch in " -_")) else " " for ch in safe_name
    )
    slug = "-".join(slug.split()).strip("-").lower()
    slug = slug[:48].strip("-") or "business"

    return f"confam-statement-{slug}-{generated_at.strftime('%Y-%m-%d')}.pdf"
