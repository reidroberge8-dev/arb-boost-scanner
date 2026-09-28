"""
Reads Reid's manually-maintained bankroll tracking Google Sheet (deposits,
current live balance per book, running all-time profit/loss per book, plus
a week-by-week log below that). Published as CSV via the standard
/export?format=csv URL -- same no-auth-needed mechanism boost_trigger_poller.py
already uses for its own (different) sheet.

This is a flat grid, not a Forms response table, so it's read with plain
csv.reader (row-label-keyed) rather than boost_trigger_poller's
csv.DictReader (header-keyed) -- there is no per-row header here, just a
label in column 0:

    ,DraftKings,FanDuel,Fanatics,Kalshi,Total,
    Starting Balance,$190.00,$65.00,$100.00,$95.00,$450.00,
    Withdrawls,$0.00,$0.00,$0.00,$0.00,$0.00,
    LIVE,$246.80,$251.00,$447.34,$167.57,"$1,112.71",
    LIVE +/-,$56.80,$186.00,$347.34,$72.57,$662.71,
    (blank spacer row)
    Weekly,DraftKings,FanDuel,Fanatics,Kalshi,Total,Running Profit,Return %
    9/21,...  (weekly log continues below)

Rows above 'LIVE'/'LIVE +/-' (or their exact count) can and do change --
confirmed 9/27 when a 'Deposits - WDs' row got split into 'Starting Balance'
+ 'Withdrawls', pushing LIVE from row 3 to row 4. Matching by LABEL (below)
rather than a hardcoded row number/index is what makes that a non-event.

'LIVE' is the real current balance sitting in each account right now -- what
Cash Available should be set to. 'LIVE +/-' is all-time profit/loss per book
(including bets placed outside this tool entirely, since it's driven off
real account balances vs. real deposits) -- a different, broader number than
bets_store's own totals, which only cover bets placed through this app.
"""
import csv
import io
import os
import urllib.request

# Overridable via ARB_BANKROLL_CSV_URL -- REQUIRED here (no hardcoded
# fallback): this repo is public, so the sheet's URL (a "publish to web"
# link -- already unauthenticated/fetchable by anyone who has it, same as
# boost_trigger_poller.py's Form-response sheet) must not sit in committed
# source where it becomes far more discoverable than an unlisted link. Set
# as a GitHub Actions secret, same pattern as the email credentials.
BANKROLL_CSV_URL = os.environ["ARB_BANKROLL_CSV_URL"]

# Column order after the row-label column -- matches cash_store.BOOKS exactly
# (Kalshi included: it's tracked in this sheet too, even though it's not a
# traditional sportsbook) followed by the sheet's own Total column.
BANKROLL_BOOKS = ('draftkings', 'fanduel', 'fanatics', 'kalshi')

LIVE_ROW_LABEL = 'LIVE'
PL_ROW_LABEL = 'LIVE +/-'


def _parse_dollar(cell):
    """'$1,112.71' -> 1112.71; blank/unparseable -> None (never 0 -- a truly
    blank cell means "no data," not "zero dollars," and the two must not be
    confused when this feeds cash_store, where 0 explicitly zeroes out every
    play needing that book)."""
    cell = (cell or '').strip().replace('$', '').replace(',', '')
    if not cell:
        return None
    try:
        return float(cell)
    except ValueError:
        return None


def fetch_bankroll():
    """Live fetch + parse. Returns:
        {'cash': {book: dollars_or_None, ...},       # the 'LIVE' row
         'pl':   {book: dollars_or_None, ...},        # the 'LIVE +/-' row
         'pl_total': dollars_or_None}                 # that row's own Total column
    Raises on a network failure or a sheet shape the caller doesn't
    recognize (missing either row, or no header row with all 4 book names)
    -- callers should surface that as a real error rather than silently
    showing stale/zeroed numbers.

    Column positions (book -> index, and Total's index) are looked up
    dynamically from whichever row contains all 4 book names, rather than
    hardcoded -- a 'Novig' column landed between Kalshi and Total on 9/28,
    which would have silently corrupted a fixed pl_total column index.
    The LIVE row label is matched loosely (startswith 'LIVE', excluding the
    '+/-' row) since it drifted from 'LIVE' to 'LIVE $' the same day --
    exact-string row-label matching (the ORIGINAL design rationale here)
    only protects against ROW reordering, not the label text itself
    changing, so this loosens that one specific match."""
    req = urllib.request.Request(BANKROLL_CSV_URL, headers={"User-Agent": "arb-tracker-bankroll"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8", errors="ignore")

    rows = [r for r in csv.reader(io.StringIO(text)) if r]

    header = None
    for row in rows:
        lower = [c.strip().lower() for c in row]
        if all(book in lower for book in BANKROLL_BOOKS):
            header = lower
            break
    if header is None:
        raise ValueError("bankroll sheet header row (book names) not found -- layout changed more than expected")
    book_col = {book: header.index(book) for book in BANKROLL_BOOKS}
    total_col = header.index("total") if "total" in header else None

    by_label = {}
    for row in rows:
        label = (row[0] or '').strip()
        if label and label not in by_label:  # first occurrence wins (weekly log below reuses no labels, but be safe)
            by_label[label] = row

    live = next((row for lbl, row in by_label.items()
                 if lbl.upper().startswith("LIVE") and "+/-" not in lbl), None)
    pl = by_label.get(PL_ROW_LABEL)
    if not live or not pl:
        raise ValueError(f"bankroll sheet is missing a 'LIVE...' or '{PL_ROW_LABEL}' row "
                          f"-- check it hasn't been renamed/reordered")

    def _cell(row, col):
        return _parse_dollar(row[col]) if col is not None and col < len(row) else None

    cash = {book: _cell(live, book_col[book]) for book in BANKROLL_BOOKS}
    pl_by_book = {book: _cell(pl, book_col[book]) for book in BANKROLL_BOOKS}
    pl_total = _cell(pl, total_col)
    return {'cash': cash, 'pl': pl_by_book, 'pl_total': pl_total}
