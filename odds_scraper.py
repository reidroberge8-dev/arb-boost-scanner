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

def extract_row(row):
    tds = row.find_all('td')
    out = {}
    for bi, col in enumerate(ALL_COLS):
        if bi + 1 >= len(tds):
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
                'team_a': team_a, 'a': extract_row(row_a),
                'team_b': team_b, 'b': extract_row(row_b),
            })
    return games

def book_lines(side, book):
    val, price = side.get(book, (None, None))
    return parse_number(val), parse_price(price)

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
