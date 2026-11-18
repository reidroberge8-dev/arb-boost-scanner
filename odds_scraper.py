"""
Shared VegasInsider odds table scraper + arbitrage/middle detector.
Used both for on-demand runs and the recurring scheduled scan.
"""
import urllib.request
from bs4 import BeautifulSoup

BOOKS = ['bet365', 'betmgm', 'draftkings', 'caesars', 'fanduel', 'hardrock', 'fanatics', 'riverscasino']
ALL_COLS = ['open'] + BOOKS + ['consensus']

# Per-process cache -- boosted_scan/free_bet_scan/dual_boost_combo_scan/
# scan_market_wide each fetch the same sport page independently, so a single
# run_boost_scan.py invocation with e.g. 2 NFL boosts + 1 NFL free bet used
# to re-fetch the identical live NFL page 5 separate times (measured 9/28:
# 23 total HTTP calls for a 3-item scan, 15 of them exact duplicates). One
# process = one live snapshot of the market anyway, so caching by URL for
# the life of the process is strictly more correct, not just faster --
# every caller now sees the SAME odds instant instead of whatever changed
# in the seconds between separate re-fetches.
_html_cache = {}

def fetch_html(url):
    if url in _html_cache:
        return _html_cache[url]
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', errors='ignore')
    _html_cache[url] = html
    return html

def parse_price(price_str):
    if price_str is None:
        return None
    p = price_str.strip().lower()
    if p == 'even':
        return 100
    try:
        return int(p.replace('+', ''))
    except ValueError:
        return None

def parse_number(val_str):
    if val_str is None:
        return None
    v = val_str.strip()
    for prefix in ('o', 'u'):
        if v.lower().startswith(prefix) and len(v) > 1 and v[1].isdigit():
            v = v[1:]
            break
    if v.lower() == 'pk':
        return 0.0
    try:
        return float(v.replace('+', ''))
    except ValueError:
        return None

def implied_prob(american):
    if american is None:
        return None
    if american > 0:
        return 100.0 / (american + 100.0)
    return -american / (-american + 100.0)

_LABEL_TO_COL = {
    'open': 'open', 'bet365': 'bet365', 'betmgm': 'betmgm',
    'draftkings': 'draftkings', 'caesars': 'caesars', 'fanduel': 'fanduel',
    'hardrock': 'hardrock', 'fanatics': 'fanatics',
    'riverscasino': 'riverscasino', 'consensus': 'consensus',
}

def _detect_col_order(table):
    """Reads the REAL column sequence for THIS page's odds table from its own
    <thead> instead of assuming every sport offers the same fixed 8 books in
    the same order (the old ALL_COLS-position assumption). Real bug this
    fixes (WNBA, found 9/30): VegasInsider doesn't offer Fanatics odds for
    WNBA at all -- confirmed via a live fetch, its <thead> reads Time, Open,
    Bet365, BetMGM, DraftKings, Caesars, FanDuel, HardRock, RiversCasino,
    Consensus -- no Fanatics column exists on that page. The old hardcoded
    ALL_COLS ('...,hardrock,fanatics,riverscasino,consensus') assumed
    Fanatics always sits between HardRock and RiversCasino, silently
    shifting every column after HardRock by one for any sport missing it:
    RiversCasino's real price got mislabeled 'fanatics', and the Consensus
    average (not a real, bookable price at all) got mislabeled
    'riverscasino'. Verified via VI's own per-cell background-color/deeplink
    coding (which uniquely identifies the true book per cell) that this
    detection is correct and the shift was real, not a coincidence.

    Returns an ordered list of internal column keys (a subset/reordering of
    ALL_COLS, one entry per real data column after the team-name column),
    skipping the leading 'Time' header (that's the team-name column, handled
    separately) and any trailing unlabeled column. Falls back to the
    historical ALL_COLS order if no <thead> is found at all (unexpected
    layout change) rather than crashing.
    """
    thead = table.find('thead')
    if thead is None:
        return ALL_COLS
    order = []
    for th in thead.find_all('th'):
        key = _LABEL_TO_COL.get(th.get_text(strip=True).lower())
        if key:
            order.append(key)
    return order or ALL_COLS

def extract_row(row, col_order):
    tds = row.find_all('td')
    out = {}
    for col in ALL_COLS:
        bi = col_order.index(col) if col in col_order else None
        if bi is None or bi + 1 >= len(tds):
            out[col] = (None, None)
            continue
        td = tds[bi + 1]
        dv = td.find('span', class_='data-value')
        do = td.find('small', class_='data-odds')
        dm = td.find('span', class_='data-moneyline')
        if dm is not None:
            # pure moneyline cell: no separate line number, just a price
            out[col] = (None, dm.get_text(strip=True))
            continue
        val = dv.get_text(strip=True) if dv else None
        price = do.get_text(strip=True) if do else None
        out[col] = (val, price)
    return out

def parse_sport_page(html, sections):
    """sections: ordered list of market names matching the page's stacked tables,
    e.g. NFL -> ['spread','total','moneyline'], MLB -> ['moneyline','total','runline']"""
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find_all('table')[0]
    col_order = _detect_col_order(table)
    rows = table.find_all('tr')
    divided = [r for r in rows if r.get('class') and 'divided' in r.get('class')]
    footer = [r for r in rows if r.get('class') and 'footer' in r.get('class')]
    n_total = min(len(divided), len(footer))
    if n_total % len(sections) != 0:
        # Every market section is assumed to list the SAME number of games,
        # in perfectly even contiguous blocks (idx = sec_idx * n_games + g,
        # below) -- if VI ever shows an uneven count across sections (one
        # market temporarily pulled for a single game, say), this silently
        # misaligns team names/odds onto the WRONG game instead of erroring.
        # Not observed happening yet; this is a visible paper trail in the
        # Actions log if it ever does, not a hard failure -- one page's
        # layout hiccup shouldn't take down the whole scan.
        print(f"WARNING: parse_sport_page got {n_total} game-rows across "
              f"{len(sections)} sections {sections} -- doesn't divide evenly, "
              f"VI's page layout may have changed. Games may be misaligned "
              f"across market sections this run.")
    n_games = n_total // len(sections)

    # map each divided-row's position in `rows` -> whether the game already finished
    # (VegasInsider prefixes the sub-header row right before a finished game with 'Final'),
    # plus its own scheduled start time (UTC ISO8601, in a <span data-role="localtime"
    # data-value="..."> inside that same sub-header row -- present whether the game is
    # upcoming, live, or final). This is the ONLY reliable, self-contained "has this game
    # started" signal VI gives us: 'Final' alone only ever catches games that have fully
    # ENDED, never ones merely in progress, and Kalshi's close_time (used elsewhere as a
    # cross-check) isn't always available -- Kalshi drops a market from its API the moment
    # it closes, so a game with no matching *open* Kalshi market looks identical to one
    # Kalshi never listed at all. Real bug this fixed: live in-progress games (started,
    # not final, no open Kalshi market for that specific date) kept generating alerts.
    row_index = {id(r): i for i, r in enumerate(rows)}
    final_flags = []
    start_times = []
    for r in divided:
        idx = row_index.get(id(r))
        is_final = False
        start_time = None
        if idx is not None and idx > 0:
            subheader = rows[idx - 1]
            is_final = 'Final' in subheader.get_text(' ', strip=True)
            time_span = subheader.find('span', attrs={'data-role': 'localtime'})
            start_time = time_span.get('data-value') if time_span else None
        final_flags.append(is_final)
        start_times.append(start_time)

    games = []
    for sec_idx, sec_name in enumerate(sections):
        for g in range(n_games):
            idx = sec_idx * n_games + g
            row_a, row_b = divided[idx], footer[idx]
            ta = row_a.find('a', class_='team-name')
            tb = row_b.find('a', class_='team-name')
            team_a = ta.get_text(strip=True) if ta else f'A{g}'
            team_b = tb.get_text(strip=True) if tb else f'B{g}'
            games.append({
                'market': sec_name,
                'game_id': g,
                'final': final_flags[idx],
                'start_time': start_times[idx],
                'team_a': team_a, 'a': extract_row(row_a, col_order),
                'team_b': team_b, 'b': extract_row(row_b, col_order),
            })
    return games

# Stale-price guard (Reid's report, 11/18): VegasInsider showed Fanatics
# Pelicans +200 all afternoon while Fanatics' own app had +155 -- VI's column
# for one book had frozen on an old line. The engine trusted it and called it
# a 1.63% "true arb" (FanDuel Pistons -186 / Fanatics Pelicans +200) that
# didn't exist. A frozen line shows up as one book offering a price far
# BETTER than every other book on the exact same bet, so: compare each
# book's implied probability against the median of the other books quoting
# the same side at the same number, and drop it if it's more than
# STALE_GAP_PTS percentage points better. Only "better" is filtered -- a
# price worse than market can't create a fake arb/hedge, it just loses.
# Needs >= STALE_MIN_PEERS other books to judge; with fewer, keep the price.
# Real arbs come from a book being 1-3 pts off the pack; +200 vs a ~+160
# market is ~5.5 pts. Lives in book_lines so every path (true arb, Kalshi
# arb/ladders vs books, boosts, free bets, hedges, combos) gets it at once.
STALE_GAP_PTS = 4.0
STALE_MIN_PEERS = 3
_stale_logged = set()

def _is_stale_outlier(side, book, num, price):
    ip = implied_prob(price)
    if ip is None:
        return False
    peers = []
    for other in BOOKS:
        if other == book:
            continue
        oval, oprice = side.get(other, (None, None))
        op = parse_price(oprice)
        if op is None:
            continue
        onum = parse_number(oval)
        if (onum is None) != (num is None):
            continue
        if num is not None and abs(onum - num) > 1e-6:
            continue  # different line number -> not the same bet, not comparable
        peers.append(implied_prob(op))
    if len(peers) < STALE_MIN_PEERS:
        return False
    peers.sort()
    n = len(peers)
    median = peers[n // 2] if n % 2 else (peers[n // 2 - 1] + peers[n // 2]) / 2
    return (median - ip) * 100 > STALE_GAP_PTS

def book_lines(side, book):
    val, price = side.get(book, (None, None))
    num, p = parse_number(val), parse_price(price)
    if p is not None and _is_stale_outlier(side, book, num, p):
        key = (book, tuple(sorted((k, v) for k, v in side.items() if isinstance(v, tuple))))  # content key -- id() gets reused
        if key not in _stale_logged:
            _stale_logged.add(key)
            print(f"STALE PRICE SKIPPED: {book} {val or ''} {price} is >{STALE_GAP_PTS} pts "
                  f"better than the other books on the same bet -- likely a frozen VI line")
        return num, None
    return num, p

def find_true_arb(games):
    """Same-number, cross-book price arbitrage (zero-risk if found)."""
    hits = []
    for game in games:
        for bi in BOOKS:
            na, pa = book_lines(game['a'], bi)
            if pa is None:
                continue
            if game['market'] != 'moneyline' and na is None:
                continue  # malformed/unparseable line on this book -> can't trust it
            for bj in BOOKS:
                if bi == bj:
                    continue
                nb, pb = book_lines(game['b'], bj)
                if pb is None:
                    continue
                if game['market'] != 'moneyline':
                    if nb is None:
                        continue
                    if game['market'] in ('spread', 'runline'):
                        if abs(na + nb) > 1e-6:
                            continue  # numbers don't cancel out -> not same line (opposite-sign fav/dog)
                    elif game['market'] == 'total':
                        if abs(abs(na) - abs(nb)) > 1e-6:
                            continue  # Over/Under share sign convention, magnitude-only compare is correct here
                ia, ib = implied_prob(pa), implied_prob(pb)
                total = ia + ib
                if total < 1.0:
                    margin = (1.0 - total) * 100
                    hits.append({
                        'market': game['market'], 'team_a': game['team_a'], 'team_b': game['team_b'],
                        'book_a': bi, 'line_a': na, 'price_a': pa,
                        'book_b': bj, 'line_b': nb, 'price_b': pb,
                        'edge_pct': round(margin, 2),
                    })
    return sorted(hits, key=lambda h: -h['edge_pct'])

def find_middles(games):
    """Cross-book number divergence creating a 'both bets win' window (spread/total/runline only)."""
    hits = []
    for game in games:
        if game['market'] not in ('spread', 'total', 'runline'):
            continue
        for bi in BOOKS:
            na, pa = book_lines(game['a'], bi)
            if na is None or pa is None:
                continue
            if abs(na) > 60:
                continue  # malformed parse (e.g. '--4' style scraper artifact), sanity-bound
            for bj in BOOKS:
                if bi == bj:
                    continue
                nb, pb = book_lines(game['b'], bj)
                if nb is None or pb is None:
                    continue
                if abs(nb) > 60:
                    continue
                if game['market'] == 'spread':
                    gap = na + nb  # A's points + B's (negative) points; >0 = middle
                else:
                    # total/runline: side 'a' row is Over/underdog(+), side 'b' is Under/favorite(-)
                    gap = nb - na if game['market'] == 'total' else na + nb
                if gap > 0.4:  # ignore trivial half-point noise, keep meaningful windows
                    hits.append({
                        'market': game['market'], 'team_a': game['team_a'], 'team_b': game['team_b'],
                        'book_a': bi, 'line_a': na, 'price_a': pa,
                        'book_b': bj, 'line_b': nb, 'price_b': pb,
                        'gap': round(gap, 1),
                    })
    return sorted(hits, key=lambda h: -h['gap'])

SPORT_PAGES = {
    'NFL': ('https://www.vegasinsider.com/nfl/odds/las-vegas/', ['spread', 'total', 'moneyline']),
    'MLB': ('https://www.vegasinsider.com/mlb/odds/las-vegas/', ['moneyline', 'total', 'runline']),
    'NCAAF': ('https://www.vegasinsider.com/college-football/odds/las-vegas/', ['spread', 'total', 'moneyline']),
    'NHL': ('https://www.vegasinsider.com/nhl/odds/las-vegas/', ['moneyline', 'total', 'spread']),
    # NBA re-enabled (10/16/2026) -- was disabled 10/5 because VegasInsider's
    # preseason NBA coverage was stale (confirmed then: off by 50-70 cents on
    # a game hours from tipoff, vs. NFL/MLB matching exactly on the same
    # check). Re-checked live today: VI shows a full 12-game regular-season-
    # opening-night slate (not preseason), and a cross-book spot-check
    # (Celtics @ 76ers, Heat @ Magic, Bucks @ Pacers, Raptors @ Pistons)
    # shows DK/FD/Fanatics prices in normal few-cents-of-vig agreement with
    # each other -- the exact "matched exactly" health signal NFL/MLB passed
    # back on 10/5, not the 50-70 cent gap that caused the disable.
    'NBA': ('https://www.vegasinsider.com/nba/odds/las-vegas/', ['spread', 'total', 'moneyline']),
    'WNBA': ('https://www.vegasinsider.com/wnba/odds/las-vegas/', ['spread', 'total', 'moneyline']),
    'NCAAB': ('https://www.vegasinsider.com/college-basketball/odds/las-vegas/', ['spread', 'total', 'moneyline']),
}

def scan_all():
    report = {}
    for sport, (url, sections) in SPORT_PAGES.items():
        try:
            html = fetch_html(url)
            games = parse_sport_page(html, sections)
            arb = find_true_arb(games)
            mid = find_middles(games)
            report[sport] = {'n_games': len(games) // len(sections), 'arb': arb, 'middles': mid}
        except Exception as e:
            report[sport] = {'error': str(e)}
    return report

if __name__ == '__main__':
    import json
    print(json.dumps(scan_all(), indent=2)[:3000])
