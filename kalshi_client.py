"""
Kalshi public market data client (no auth needed for read-only market data).
Pulls single-game moneyline-equivalent (team-to-win) binary markets.
"""
import re
import urllib.request
import json
from team_map import CODE_TABLES, split_team_codes, nickname_for_code

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {'MLB': 'KXMLBGAME', 'NFL': 'KXNFLGAME', 'NCAAF': 'KXNCAAFGAME'}
TOTALS_SERIES = {'MLB': 'KXMLBTOTAL', 'NFL': 'KXNFLTOTAL', 'NCAAF': 'KXNCAAFTOTAL'}

# NCAAF has ~130+ FBS/FCS schools -- no static code table (unlike MLB/NFL). Kalshi's
# own NCAAF GAME markets carry the team name directly in 'yes_sub_title', so we read
# that instead of maintaining one, and reuse it to decode the TOTALS series' ticker
# codes (which don't carry names) via a code table built fresh from the GAME series.
_STATE_ABBR_RE = re.compile(r'\bSt\.$')


def _normalize_team_name(name):
    """Kalshi abbreviates 'State' -> 'St.' (e.g. 'Fresno St.') while VegasInsider
    spells it out ('Fresno State'). Canonicalize to VI's spelling so arb_engine's
    frozenset((team_a, team_b)) matching -- already exact-match, untouched here --
    lines up. No-op for MLB/NFL names (none end in 'St.')."""
    return _STATE_ABBR_RE.sub('State', name.strip()) if name else name

# Player-prop ladder series (Over/Under a stat threshold, one ladder per player per game).
# Kalshi's market 'title' carries the full player name (e.g. "Patrick Mahomes: 350+ passing
# yards") so no name-matching table is needed -- much simpler than the team-code tables above.
PLAYER_PROP_SERIES = {
    'NFL': [
        ('KXNFLPASSYDS', 'passing yards'),
        ('KXNFLRECYDS', 'receiving yards'),
        ('KXNFLRSHYDS', 'rushing yards'),
        ('KXNFLREC', 'receptions'),
    ],
    'MLB': [
        ('KXMLBHR', 'home runs'),
        ('KXMLBHIT', 'hits'),
        ('KXMLBTB', 'total bases'),
        ('KXMLBRBI', 'RBIs'),
        ('KXMLBSB', 'stolen bases'),
    ],
}

# matches the date/time prefix in an event ticker, e.g. 26SEP162138 -> leaves team codes
_DATE_RE = re.compile(r'^\d{2}[A-Z]{3}\d{2}(\d{4})?')


def _fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _code_table_from_game_markets(series_ticker):
    """Builds a {ticker_suffix_code: normalized_team_name} table from a GAME-style
    series' open markets, reading names straight from 'yes_sub_title' (e.g. 'Stanford'
    for ticker 'KXNCAAFGAME-26SEP26GTSTAN-STAN' -> code 'STAN'). Used to decode the
    TOTALS series for sports with no static CODE_TABLES entry (currently just NCAAF),
    since totals markets don't carry team names directly but use the same per-team
    codes in their event ticker as the GAME series does."""
    url = f"{BASE}/markets?series_ticker={series_ticker}&status=open&limit=1000"
    try:
        data = _fetch_json(url)
    except Exception:
        return {}
    table = {}
    for m in data.get('markets', []):
        code = m.get('ticker', '').rsplit('-', 1)[-1]
        name = _normalize_team_name(m.get('yes_sub_title', ''))
        if code and name:
            table[code] = name
    return table


def fetch_games(sport):
    """Returns list of dicts: {event_ticker, team_a, team_b,
    team_a_yes_ask, team_a_yes_bid, team_b_yes_ask, team_b_yes_bid, close_time}.
    Sports with a static CODE_TABLES entry (MLB/NFL) decode team codes from the
    ticker via split_team_codes/nickname_for_code. Sports without one (NCAAF, too
    many schools for a hand-built table) read the name straight from each market's
    'yes_sub_title' instead -- confirmed present/non-empty on every NCAAF GAME market."""
    series_ticker = SERIES[sport]
    code_table = CODE_TABLES.get(sport)
    url = f"{BASE}/markets?series_ticker={series_ticker}&status=open&limit=1000"  # NCAAF alone can exceed 200 open markets (478 seen); MLB/NFL are always well under 1000 too, so this is a no-op widening for them
    try:
        data = _fetch_json(url)
    except Exception:
        return []

    by_event = {}
    for m in data.get('markets', []):
        et = m.get('event_ticker', '')
        by_event.setdefault(et, []).append(m)

    games = []
    for et, markets in by_event.items():
        if len(markets) != 2:
            continue

        if code_table is not None:
            suffix = et[len(series_ticker) + 1:]  # strip 'KXMLBGAME-'
            date_match = _DATE_RE.match(suffix)
            if not date_match:
                continue
            blob = suffix[date_match.end():]
            c1, c2 = split_team_codes(blob, code_table)
            if not c1:
                continue
            nick1 = nickname_for_code(c1, code_table)
            nick2 = nickname_for_code(c2, code_table)

            # each market's ticker ends in -<CODE>; match to the right nickname
            team_data = {}
            for mkt in markets:
                code = mkt['ticker'].rsplit('-', 1)[-1]
                nick = nickname_for_code(code, code_table)
                team_data[nick] = {
                    'yes_ask': float(mkt.get('yes_ask_dollars') or 0),
                    'yes_bid': float(mkt.get('yes_bid_dollars') or 0),
                    'no_ask': float(mkt.get('no_ask_dollars') or 0),
                    'no_bid': float(mkt.get('no_bid_dollars') or 0),
                    'volume': mkt.get('volume_fp', 0),
                    'ticker': mkt['ticker'],
                }
        else:
            # no static code table (NCAAF) -- read the name straight off each
            # market's own 'yes_sub_title' field, keyed by that market's own
            # ticker-suffix code (no need to parse/split the event blob at all).
            team_data = {}
            names_by_code = {}
            for mkt in markets:
                code = mkt['ticker'].rsplit('-', 1)[-1]
                nick = _normalize_team_name(mkt.get('yes_sub_title', ''))
                if not nick:
                    continue
                names_by_code[code] = nick
                team_data[nick] = {
                    'yes_ask': float(mkt.get('yes_ask_dollars') or 0),
                    'yes_bid': float(mkt.get('yes_bid_dollars') or 0),
                    'no_ask': float(mkt.get('no_ask_dollars') or 0),
                    'no_bid': float(mkt.get('no_bid_dollars') or 0),
                    'volume': mkt.get('volume_fp', 0),
                    'ticker': mkt['ticker'],
                }
            if len(names_by_code) != 2:
                continue
            nick1, nick2 = list(names_by_code.values())

        if nick1 not in team_data or nick2 not in team_data:
            continue
        games.append({
            'event_ticker': et,
            'close_time': markets[0].get('close_time'),
            'team_a': nick1, 'team_a_odds': team_data[nick1],
            'team_b': nick2, 'team_b_odds': team_data[nick2],
        })
    return games


def fetch_totals(sport):
    """Returns list of dicts: {event_ticker, team_a, team_b, close_time,
    strikes: [{floor_strike, yes_ask, yes_bid, no_ask, no_bid, ticker}, ...]}.
    'Yes' on a strike = Over that floor_strike; 'No' = Under/at-or-below it.
    Totals markets never carry team names directly (only 'Over 70.5 points'-style
    titles), so sports without a static CODE_TABLES entry (NCAAF) get a code table
    built fresh from that sport's GAME series first -- confirmed the GAME and TOTALS
    series use the exact same per-team ticker codes for the same matchup."""
    series_ticker = TOTALS_SERIES[sport]
    code_table = CODE_TABLES.get(sport) or _code_table_from_game_markets(SERIES[sport])
    url = f"{BASE}/markets?series_ticker={series_ticker}&status=open&limit=500"
    try:
        data = _fetch_json(url)
    except Exception:
        return []

    by_event = {}
    for m in data.get('markets', []):
        et = m.get('event_ticker', '')
        by_event.setdefault(et, []).append(m)

    events = []
    for et, markets in by_event.items():
        suffix = et[len(series_ticker) + 1:]  # strip 'KXMLBTOTAL-' / 'KXNFLTOTAL-'
        date_match = _DATE_RE.match(suffix)
        if not date_match:
            continue
        blob = suffix[date_match.end():]
        c1, c2 = split_team_codes(blob, code_table)
        if not c1:
            continue
        nick1, nick2 = nickname_for_code(c1, code_table), nickname_for_code(c2, code_table)
        if not nick1 or not nick2:
            continue

        strikes = []
        for mkt in markets:
            fs = mkt.get('floor_strike')
            if fs is None:
                continue
            strikes.append({
                'floor_strike': float(fs),
                'yes_ask': float(mkt.get('yes_ask_dollars') or 0),
                'yes_bid': float(mkt.get('yes_bid_dollars') or 0),
                'no_ask': float(mkt.get('no_ask_dollars') or 0),
                'no_bid': float(mkt.get('no_bid_dollars') or 0),
                'ticker': mkt['ticker'],
            })
        if not strikes:
            continue
        events.append({
            'event_ticker': et,
            'close_time': markets[0].get('close_time'),
            'team_a': nick1, 'team_b': nick2,
            'strikes': strikes,
        })
    return events


def fetch_player_prop_ladders(series_ticker):
    """Generic fetch for any Kalshi player-prop ladder series. One Kalshi 'event' covers
    a whole game and bundles every player's ladder together, so the real grouping key is
    (event_ticker, player_code) -- parsed out of the ticker's middle segment, e.g.
    'KXNFLPASSYDS-26SEP14DENKC-KCPMAHOMES15-350' -> event 'KXNFLPASSYDS-26SEP14DENKC',
    player_code 'KCPMAHOMES15'. Player name comes straight from the market title
    ("Patrick Mahomes: 350+ passing yards" -> "Patrick Mahomes"), not a lookup table.
    Returns list of {event_ticker, player_code, player_name, close_time, strikes: [...]}."""
    url = f"{BASE}/markets?series_ticker={series_ticker}&status=open&limit=1000"
    try:
        data = _fetch_json(url)
    except Exception:
        return []

    by_player = {}
    for m in data.get('markets', []):
        et = m.get('event_ticker', '')
        ticker = m.get('ticker', '')
        if not et or not ticker.startswith(et + '-'):
            continue
        fs = m.get('floor_strike')
        if fs is None:
            continue
        rest = ticker[len(et) + 1:]
        player_code = rest.rsplit('-', 1)[0]
        title = m.get('title', '') or ''
        player_name = title.split(':')[0].strip() if ':' in title else (player_code or title)
        key = (et, player_code)
        entry = by_player.setdefault(key, {
            'event_ticker': et, 'player_code': player_code, 'player_name': player_name,
            'close_time': m.get('close_time'), 'strikes': [],
        })
        entry['strikes'].append({
            'floor_strike': float(fs),
            'yes_ask': float(m.get('yes_ask_dollars') or 0),
            'no_ask': float(m.get('no_ask_dollars') or 0),
            'ticker': ticker,
        })
    return [v for v in by_player.values() if len(v['strikes']) >= 2]


def event_matchup(event_ticker, series_ticker, sport):
    """Best-effort 'Team A @ Team B' label parsed from a prop event ticker, for display only
    -- falls back to the bare event ticker if parsing fails (never blocks the arb check)."""
    code_table = CODE_TABLES.get(sport)
    if not code_table or not event_ticker.startswith(series_ticker + '-'):
        return event_ticker
    suffix = event_ticker[len(series_ticker) + 1:]
    date_match = _DATE_RE.match(suffix)
    if not date_match:
        return event_ticker
    blob = suffix[date_match.end():]
    c1, c2 = split_team_codes(blob, code_table)
    if not c1:
        return event_ticker
    n1, n2 = nickname_for_code(c1, code_table), nickname_for_code(c2, code_table)
    return f"{n1} @ {n2}" if n1 and n2 else event_ticker


if __name__ == '__main__':
    for sport in ('MLB', 'NFL'):
        gs = fetch_games(sport)
        print(sport, len(gs), 'games')
        for g in gs[:3]:
            print(' ', g['team_a'], g['team_a_odds']['yes_ask'], 'vs',
                  g['team_b'], g['team_b_odds']['yes_ask'])
