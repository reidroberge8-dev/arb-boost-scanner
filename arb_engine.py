"""
Combined arbitrage/middle detector across DraftKings, FanDuel, Fanatics (via
VegasInsider) and Kalshi (direct public API).

Produces a flat list of "opportunity" dicts, each fully self-contained with
exact bet instructions, ready for the frontend to render and expand.
"""
import time
import itertools
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    _EASTERN = ZoneInfo("America/New_York")
except Exception:
    _EASTERN = None
from odds_scraper import (
    fetch_html, parse_sport_page, implied_prob, book_lines, SPORT_PAGES,
)
import kalshi_client

# The three traditional sportsbooks actually usable (legal in Connecticut) --
# VegasInsider's page carries several more (bet365, BetMGM, Caesars, Hard Rock,
# Rivers Casino) but there's no point finding an "opportunity" on a book that
# isn't actually available to bet on.
SPORTSBOOKS = ('draftkings', 'fanduel', 'fanatics')


def _game_has_started(close_time):
    """close_time is Kalshi's ISO8601 UTC close timestamp (e.g.
    '2026-09-20T00:15:00Z'), which is also the game's scheduled kickoff/first
    pitch -- once wagering closes, the game has started and any odds still
    showing a pre-game price (stale scrape, slow refresh, etc.) are no longer
    real and must not be alerted on. Unparseable/missing close_time returns
    False (can't prove it started, so don't block on this check alone --
    the caller should still have the 'final' flag as a second safety net)."""
    if not close_time:
        return False
    try:
        ct = datetime.fromisoformat(close_time.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return False
    return datetime.now(timezone.utc) >= ct


def _to_eastern_date(dt_utc):
    """Best-effort local-calendar-date conversion, used for "which day is
    this game on" bucketing against a boost's expiration date. Falls back
    to a fixed UTC-4 (EDT) offset if the `tzdata` package isn't installed
    (a bare python.org Windows install doesn't bundle IANA tz data, unlike
    this sandbox's Python) -- close enough for date-bucketing; the 1-hour
    EST-season inaccuracy only matters for a game starting right at the US
    midnight boundary, which doesn't happen for MLB/NFL/NCAAF."""
    if _EASTERN is not None:
        try:
            return dt_utc.astimezone(_EASTERN).date()
        except Exception:
            pass
    return (dt_utc - timedelta(hours=4)).date()


def _game_after_expiration(expires, start_time):
    """expires is a boost's optional ISO 'YYYY-MM-DD' expiration date
    ('' / None -- never expires, always allowed here). start_time is the
    game's ISO8601 UTC kickoff. True means this boost must NOT apply to
    this particular game because the game's (US Eastern) calendar date is
    after the expiration date -- e.g. a boost that "expires 9/18" still
    covers every 9/18 game but none of 9/19's, even though the boost
    itself doesn't get removed from the saved list until the day AFTER
    (see boosts_store.load()) -- those are two different, deliberately
    separate checks: one is "is this boost still worth showing at all,"
    the other is "does this boost apply to this specific game." """
    if not expires or not start_time:
        return False
    try:
        st = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        exp_date = datetime.strptime(expires, '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        return False
    return _to_eastern_date(st) > exp_date


def _disambiguate_labels(games):
    """Maps game_id -> display label ('Team A @ Team B'), adding a
    '(Game N of M)' suffix whenever more than one distinct game_id shares
    the same team pair -- an MLB doubleheader, mainly. Without this, two
    real, distinct games (e.g. a doubleheader's already-finished opener and
    its still-upcoming nightcap) render with an IDENTICAL label, which
    looks exactly like stale data from the game that already ended even
    though the play itself is for the other, legitimately still-open game
    (confirmed 9/28: a Cubs @ Red Sox play was reported as "the game
    that's already over" -- it was actually game 2 of a same-day
    doubleheader, correctly excluded as final was game 1)."""
    ids_by_pair = {}
    for g in games:
        key = frozenset((g['team_a'], g['team_b']))
        ids_by_pair.setdefault(key, set()).add(g['game_id'])
    labels = {}
    for g in games:
        key = frozenset((g['team_a'], g['team_b']))
        ids = sorted(ids_by_pair[key])
        base = f"{g['team_a']} @ {g['team_b']}"
        labels[g['game_id']] = (f"{base} (Game {ids.index(g['game_id']) + 1} of {len(ids)})"
                                 if len(ids) > 1 else base)
    return labels


def _build_close_time_lookup(*kalshi_lists):
    """team-pair -> close_time, built from any number of Kalshi game/total
    lists (each entry has team_a/team_b/close_time). Used to time-gate the
    VegasInsider-scraped games, which have no close_time of their own."""
    lookup = {}
    for lst in kalshi_lists:
        for g in lst:
            key = frozenset((g.get('team_a'), g.get('team_b')))
            ct = g.get('close_time')
            if ct and key not in lookup:
                lookup[key] = ct
    return lookup


def _fmt_price(p):
    return f"+{p}" if p is not None and p > 0 else str(p)


def _payout_per_100(price):
    """Profit on a $100 stake at American odds `price`."""
    if price is None or price == 0:
        return None
    return round(price if price > 0 else 100 * (100 / -price), 2)


def kalshi_effective_cost(price):
    """Kalshi's REAL cost per $1 of payout, including its standard taker trading fee
    -- fee = 0.07 * price * (1 - price), added on top of the raw ask/bid. Every arb
    calc in this module used to treat a Kalshi yes_ask/no_ask as if it were the
    actual cost per $1 of payout with no fee, which overstated guaranteed profit on
    every Kalshi leg (this is what a boosted play's hedge always is, and it's one
    side of several of the no-sportsbook-needed opportunity types below). Confirmed
    against a live example: a 0.68 ask showed a 1.44x payout multiplier on Kalshi's
    own site, not the naive 1/0.68 = 1.47x this module used to assume --
    0.68 + 0.07*0.68*0.32 = 0.6952, and 1/0.6952 = 1.438, matching Kalshi's 1.44x
    within display rounding. Used for every profit/stake CALCULATION; the raw
    unadjusted price is still what gets DISPLAYED, since that's the number that
    actually appears on Kalshi's own order ticket."""
    if price is None:
        return None
    return price + 0.07 * price * (1 - price)


def kalshi_multiplier(price):
    """Payout multiplier Kalshi itself displays next to a contract's price (e.g.
    a 0.68 ask shows '1.44x' on Kalshi's own site) -- the inverse of
    kalshi_effective_cost, i.e. how many dollars back per dollar risked, fee
    included. Every place this module used to display a Kalshi price as a raw
    '0.68 cost/$1' string now shows this multiplier instead, per request, since
    it's the number that actually matches what Kalshi shows and is more directly
    useful ('how much do I get back') than a bare cost fraction."""
    cost = kalshi_effective_cost(price)
    if not cost:
        return None
    return 1 / cost


def dk_fd_moneyline_and_lines(sport, games):
    """True zero-risk arb + middles, across every pair of the 3 legal sportsbooks
    (not just DraftKings/FanDuel -- name kept for historical continuity with the
    rest of this module, but it now checks all of SPORTSBOOKS)."""
    opps = []
    for game in games:
        if game.get('final'):
            continue
        for book_a, book_b in itertools.permutations(SPORTSBOOKS, 2):
            na, pa = book_lines(game['a'], book_a)
            if pa is None:
                continue
            nb, pb = book_lines(game['b'], book_b)
            if pb is None:
                continue
            if game['market'] != 'moneyline':
                if na is None or nb is None:
                    continue
                # spread AND runline use opposite-sign favorite/dog conventions
                # (fav -1.5 / dog +1.5) -- same_line requires na + nb == 0, NOT just
                # matching magnitudes, otherwise two books that disagree about WHICH
                # team is favored look like a matched line when they aren't (both legs
                # can lose). 'total' is different: Over/Under share the same sign
                # convention after parsing, so magnitude-only comparison is correct there.
                same_line = (
                    abs(na + nb) < 1e-6 if game['market'] in ('spread', 'runline')
                    else abs(abs(na) - abs(nb)) < 1e-6
                )
            else:
                same_line = True
            ia, ib = implied_prob(pa), implied_prob(pb)
            if same_line:
                total = ia + ib
                if total < 1.0:
                    # Proportional stake split (units, 100 total) -- flat equal stakes on both
                    # legs only guarantee profit both ways when ia == ib, which is rare. See
                    # _proportional_arb docstring.
                    stake_a, stake_b, profit = _proportional_arb(ia, ib)
                    opps.append({
                        'sport': sport, 'type': 'true_arb', 'market': game['market'],
                        'game': f"{game['team_a']} @ {game['team_b']}",
                        'edge_pct': round((1.0 - total) * 100, 2),
                        'guaranteed_profit': profit,
                        'legs': [
                            {'book': book_a, 'side': (f"Over {na}" if game['market'] == 'total' else game['team_a']), 'line': (None if game['market'] == 'total' else na), 'price': pa,
                             'payout_per_100': _payout_per_100(pa), 'stake': stake_a},
                            {'book': book_b, 'side': (f"Under {nb}" if game['market'] == 'total' else game['team_b']), 'line': (None if game['market'] == 'total' else nb), 'price': pb,
                             'payout_per_100': _payout_per_100(pb), 'stake': stake_b},
                        ],
                    })
            elif game['market'] in ('spread', 'total', 'runline') and na is not None and nb is not None and abs(na) < 60 and abs(nb) < 60:
                gap = (na + nb) if game['market'] == 'spread' else (nb - na if game['market'] == 'total' else na + nb)
                if gap > 0.4:
                    stake = 100
                    win_a = _payout_per_100(pa)
                    win_b = _payout_per_100(pb)
                    both_win = round(win_a + win_b, 2)
                    only_a = round(win_a - stake, 2)
                    only_b = round(win_b - stake, 2)
                    opps.append({
                        'sport': sport, 'type': 'middle', 'market': game['market'],
                        'game': f"{game['team_a']} @ {game['team_b']}",
                        'gap': gap, 'worst_case': min(only_a, only_b), 'best_case': both_win,
                        'legs': [
                            {'book': book_a, 'side': (f"Over {na}" if game['market'] == 'total' else game['team_a']), 'line': (None if game['market'] == 'total' else na), 'price': pa,
                             'payout_per_100': win_a},
                            {'book': book_b, 'side': (f"Under {nb}" if game['market'] == 'total' else game['team_b']), 'line': (None if game['market'] == 'total' else nb), 'price': pb,
                             'payout_per_100': win_b},
                        ],
                    })
    return opps


def dk_fd_vs_kalshi(sport, ml_games, kalshi_games):
    """Cross-market arb between any of SPORTSBOOKS' moneyline and Kalshi's
    team-to-win contracts."""
    opps = []
    kalshi_by_teams = {}
    for g in kalshi_games:
        key = frozenset((g['team_a'], g['team_b']))
        existing = kalshi_by_teams.get(key)
        if existing is None or (g['close_time'] or '') < (existing['close_time'] or ''):
            kalshi_by_teams[key] = g  # keep the soonest-closing game (today's, not a later series date)

    for game in ml_games:
        key = frozenset((game['team_a'], game['team_b']))
        kg = kalshi_by_teams.get(key)
        if not kg:
            continue
        for book in SPORTSBOOKS:
            _, price_a = book_lines(game['a'], book)
            _, price_b = book_lines(game['b'], book)
            if price_a is None or price_b is None:
                continue
            ip_a, ip_b = implied_prob(price_a), implied_prob(price_b)

            # sportsbook side = team_a, Kalshi side = team_b (buy Yes on team_b)
            kalshi_b_ask = kg['team_b_odds']['yes_ask'] if kg['team_a'] == game['team_a'] else kg['team_a_odds']['yes_ask']
            kalshi_a_ask = kg['team_a_odds']['yes_ask'] if kg['team_a'] == game['team_a'] else kg['team_b_odds']['yes_ask']
            if kalshi_a_ask and kalshi_b_ask:
                for sb_side, sb_price, sb_prob, kal_side, kal_ask in (
                    (game['team_a'], price_a, ip_a, game['team_b'], kalshi_b_ask),
                    (game['team_b'], price_b, ip_b, game['team_a'], kalshi_a_ask),
                ):
                    kal_cost = kalshi_effective_cost(kal_ask)
                    edge = 1.0 - (sb_prob + kal_cost)
                    if edge > 0:
                        stake_sb, stake_kal, profit = _proportional_arb(sb_prob, kal_cost)
                        opps.append({
                            'sport': sport, 'type': 'sportsbook_kalshi_arb', 'market': 'moneyline',
                            'game': f"{game['team_a']} @ {game['team_b']}",
                            'edge_pct': round(edge * 100, 2),
                            'guaranteed_profit': profit,
                            'legs': [
                                {'book': book.capitalize(), 'side': sb_side, 'line': None,
                                 'price': sb_price, 'payout_per_100': _payout_per_100(sb_price), 'stake': stake_sb},
                                {'book': 'Kalshi', 'side': kal_side, 'line': None,
                                 'price': f"{kalshi_multiplier(kal_ask):.2f}x", 'payout_per_100': round((1 - kal_cost) / kal_cost * 100, 2),
                                 'stake': stake_kal},
                            ],
                        })
    return opps


def dk_fd_totals_vs_kalshi(sport, total_games, kalshi_totals):
    """Cross-market arb between any of SPORTSBOOKS' Over/Under totals and Kalshi's
    per-strike total-ladder contracts. Requires an EXACT floor_strike match to the
    sportsbook's line - no approximating across different strikes, that
    would not be real arbitrage."""
    opps = []
    kalshi_by_teams = {}
    for ev in kalshi_totals:
        key = frozenset((ev['team_a'], ev['team_b']))
        existing = kalshi_by_teams.get(key)
        if existing is None or (ev['close_time'] or '') < (existing['close_time'] or ''):
            kalshi_by_teams[key] = ev  # soonest-closing game, not a later series date

    for game in total_games:
        key = frozenset((game['team_a'], game['team_b']))
        kev = kalshi_by_teams.get(key)
        if not kev:
            continue
        strikes_by_line = {s['floor_strike']: s for s in kev['strikes']}

        for book in SPORTSBOOKS:
            over_line, over_price = book_lines(game['a'], book)
            under_line, under_price = book_lines(game['b'], book)
            if over_price is None or under_price is None:
                continue

            if over_line is not None:
                strike = strikes_by_line.get(over_line)
                if strike and strike['no_ask']:
                    no_cost = kalshi_effective_cost(strike['no_ask'])
                    edge = 1.0 - (implied_prob(over_price) + no_cost)
                    if edge > 0:
                        stake_sb, stake_kal, profit = _proportional_arb(implied_prob(over_price), no_cost)
                        opps.append({
                            'sport': sport, 'type': 'sportsbook_kalshi_arb', 'market': 'total',
                            'game': f"{game['team_a']} @ {game['team_b']}",
                            'edge_pct': round(edge * 100, 2),
                            'guaranteed_profit': profit,
                            'legs': [
                                {'book': book.capitalize(), 'side': f"Over {over_line}", 'line': None,
                                 'price': over_price, 'payout_per_100': _payout_per_100(over_price), 'stake': stake_sb},
                                {'book': 'Kalshi', 'side': f"Under {over_line} (buy No)", 'line': None,
                                 'price': f"{kalshi_multiplier(strike['no_ask']):.2f}x",
                                 'payout_per_100': round((1 - no_cost) / no_cost * 100, 2),
                                 'stake': stake_kal},
                            ],
                        })

            if under_line is not None:
                strike = strikes_by_line.get(under_line)
                if strike and strike['yes_ask']:
                    yes_cost = kalshi_effective_cost(strike['yes_ask'])
                    edge = 1.0 - (implied_prob(under_price) + yes_cost)
                    if edge > 0:
                        stake_sb, stake_kal, profit = _proportional_arb(implied_prob(under_price), yes_cost)
                        opps.append({
                            'sport': sport, 'type': 'sportsbook_kalshi_arb', 'market': 'total',
                            'game': f"{game['team_a']} @ {game['team_b']}",
                            'edge_pct': round(edge * 100, 2),
                            'guaranteed_profit': profit,
                            'legs': [
                                {'book': book.capitalize(), 'side': f"Under {under_line}", 'line': None,
                                 'price': under_price, 'payout_per_100': _payout_per_100(under_price), 'stake': stake_sb},
                                {'book': 'Kalshi', 'side': f"Over {under_line} (buy Yes)", 'line': None,
                                 'price': f"{kalshi_multiplier(strike['yes_ask']):.2f}x",
                                 'payout_per_100': round((1 - yes_cost) / yes_cost * 100, 2),
                                 'stake': stake_kal},
                            ],
                        })
    return opps


def _proportional_arb(p1, p2, total_stake=100):
    """Given two mutually-exclusive-outcome costs-per-$1 (p1 + p2 < 1), returns
    (stake1, stake2, guaranteed_profit) splitting a fixed total_stake so the
    payout is IDENTICAL regardless of which side hits. This is the correct
    way to size a true 2-outcome arb -- flat equal stakes on both legs only
    guarantee profit both ways when the two prices happen to be symmetric."""
    total_p = p1 + p2
    stake1 = round(total_stake * p1 / total_p, 2)
    stake2 = round(total_stake * p2 / total_p, 2)
    profit = round(total_stake * (1.0 - total_p) / total_p, 2)
    return stake1, stake2, profit


def kalshi_internal_arb(sport, kalshi_games, kalshi_totals):
    """Arbitrage entirely WITHIN Kalshi's own market structure -- no
    sportsbook needed at all. Two independent mechanisms:

    1. Team two-way: exactly one team wins a given game (barring a tie), so
       Kalshi's separate Yes/No market for each team is really pricing the
       same underlying coin flip from two different tickers. If
       yes_ask(team_a) + yes_ask(team_b) < 1.0, Kalshi's own two markets for
       the same game disagree with each other -- buying Yes on both is free
       money, split proportionally so the payout is equal either way.

    2. Totals-ladder calendar arb: 'Over 8.5' implies 'Over 6.5' (same game,
       same stat, higher bar is strictly harder), so buying Yes on a lower
       strike + No on a higher strike guarantees at least $1 back per
       contract-pair, and MORE if the actual result lands between the two
       strikes. This is not a middle: there is no outcome where both legs
       lose, unlike a genuine cross-book line disagreement.
    """
    opps = []

    for g in kalshi_games:
        ya = g['team_a_odds'].get('yes_ask')
        yb = g['team_b_odds'].get('yes_ask')
        if not ya or not yb:
            continue
        # Both legs of this one are on Kalshi -- the trading fee applies to BOTH trades,
        # not just one, so both go through kalshi_effective_cost before checking the arb.
        ya_cost, yb_cost = kalshi_effective_cost(ya), kalshi_effective_cost(yb)
        total_p = ya_cost + yb_cost
        if total_p >= 1.0:
            continue
        stake_a, stake_b, profit = _proportional_arb(ya_cost, yb_cost)
        note = ("NFL games can (rarely) end in a tie, which would make both legs "
                "lose since neither team 'wins' -- small non-zero risk, not a pure "
                "mathematical guarantee like the totals-ladder check below."
                ) if sport == 'NFL' else None
        opps.append({
            'sport': sport, 'type': 'kalshi_internal_arb', 'market': 'moneyline',
            'game': f"{g['team_a']} @ {g['team_b']}",
            'edge_pct': round((1.0 - total_p) * 100, 2),
            'guaranteed_profit': profit,
            'note': note,
            'legs': [
                {'book': 'Kalshi', 'side': f"{g['team_a']} to win (buy Yes)", 'line': None,
                 'price': f"{kalshi_multiplier(ya):.2f}x", 'stake': stake_a},
                {'book': 'Kalshi', 'side': f"{g['team_b']} to win (buy Yes)", 'line': None,
                 'price': f"{kalshi_multiplier(yb):.2f}x", 'stake': stake_b},
            ],
        })

    for ev in kalshi_totals:
        strikes = sorted(ev['strikes'], key=lambda s: s['floor_strike'])
        for i, lo in enumerate(strikes):
            lo_ask = lo.get('yes_ask')
            if not lo_ask:
                continue
            for hi in strikes[i + 1:]:
                hi_ask = hi.get('no_ask')
                if not hi_ask:
                    continue
                lo_cost, hi_cost = kalshi_effective_cost(lo_ask), kalshi_effective_cost(hi_ask)
                total_p = lo_cost + hi_cost
                if total_p >= 1.0:
                    continue
                stake_lo, stake_hi, profit = _proportional_arb(lo_cost, hi_cost)
                opps.append({
                    'sport': sport, 'type': 'kalshi_internal_arb', 'market': 'total',
                    'game': f"{ev['team_a']} @ {ev['team_b']}",
                    'edge_pct': round((1.0 - total_p) * 100, 2),
                    'guaranteed_profit': profit,
                    'note': (f"Guaranteed minimum shown above; pays more than that if the "
                             f"actual total lands between {lo['floor_strike']} and {hi['floor_strike']}."),
                    'legs': [
                        {'book': 'Kalshi', 'side': f"Over {lo['floor_strike']} (buy Yes)", 'line': None,
                         'price': f"{kalshi_multiplier(lo_ask):.2f}x", 'stake': stake_lo},
                        {'book': 'Kalshi', 'side': f"Under {hi['floor_strike']} (buy No)", 'line': None,
                         'price': f"{kalshi_multiplier(hi_ask):.2f}x", 'stake': stake_hi},
                    ],
                })
    return opps


def kalshi_player_prop_arb(sport):
    """Same calendar-arb mechanism as kalshi_internal_arb's totals-ladder check, applied to
    Kalshi's player-prop ladders (passing/receiving/rushing yards, receptions, HR/hits/TB/RBI/
    steals, etc). 'Over higher threshold' implies 'Over lower threshold' for the same player in
    the same game, so buying Yes on a lower strike + No on a higher strike guarantees at least
    $1 back always, more if the real stat lands between the two strikes. Kalshi-only, no
    sportsbook needed -- and no player-name-matching table needed either, since Kalshi's own
    market title carries the player's full name directly.
    """
    opps = []
    for series_ticker, stat_label in kalshi_client.PLAYER_PROP_SERIES.get(sport, []):
        try:
            ladders = kalshi_client.fetch_player_prop_ladders(series_ticker)
        except Exception:
            continue
        for ladder in ladders:
            if _game_has_started(ladder.get('close_time')):
                continue
            strikes = sorted(ladder['strikes'], key=lambda s: s['floor_strike'])
            matchup = kalshi_client.event_matchup(ladder['event_ticker'], series_ticker, sport)
            for i, lo in enumerate(strikes):
                lo_ask = lo.get('yes_ask')
                if not lo_ask:
                    continue
                for hi in strikes[i + 1:]:
                    hi_ask = hi.get('no_ask')
                    if not hi_ask:
                        continue
                    lo_cost, hi_cost = kalshi_effective_cost(lo_ask), kalshi_effective_cost(hi_ask)
                    total_p = lo_cost + hi_cost
                    if total_p >= 1.0:
                        continue
                    stake_lo, stake_hi, profit = _proportional_arb(lo_cost, hi_cost)
                    opps.append({
                        'sport': sport, 'type': 'kalshi_internal_arb', 'market': stat_label,
                        'game': f"{ladder['player_name']} \u2014 {matchup}",
                        'edge_pct': round((1.0 - total_p) * 100, 2),
                        'guaranteed_profit': profit,
                        'note': (f"Guaranteed minimum shown above; pays more than that if "
                                 f"{ladder['player_name']}'s actual {stat_label} lands between "
                                 f"{lo['floor_strike']} and {hi['floor_strike']}."),
                        'legs': [
                            {'book': 'Kalshi', 'side': f"Over {lo['floor_strike']} {stat_label} (buy Yes)",
                             'line': None, 'price': f"{kalshi_multiplier(lo_ask):.2f}x", 'stake': stake_lo},
                            {'book': 'Kalshi', 'side': f"Under {hi['floor_strike']} {stat_label} (buy No)",
                             'line': None, 'price': f"{kalshi_multiplier(hi_ask):.2f}x", 'stake': stake_hi},
                        ],
                    })
    return opps


def _boosted_leg_return(stake, american_price, boost_pct):
    """Total return (stake back + boosted profit) if a boosted book leg wins.
    boost_pct is a fraction (0.5 = 50% boost), matching how every boost was
    modeled by hand this session: raw profit * (1 + boost_pct)."""
    if american_price is None:
        return None
    raw = (stake * (american_price / 100.0) if american_price > 0
           else stake * (100.0 / abs(american_price)))
    return stake + raw * (1 + boost_pct)


def _best_hedge(boosted_return, stake, hedge_candidates):
    """hedge_candidates: list of dicts with a 'cost_per_dollar' key (Kalshi's
    yes_ask/no_ask, or implied_prob() of another book's price -- both are
    'cost to buy $1 of payout on that side', same shape either way). Sizes
    the hedge to equalize profit across both outcomes (same math as
    _proportional_arb, just stake-fixed on the boosted leg instead of
    optimizing both legs) and returns whichever candidate gives the highest
    guaranteed profit, or None if none are actually profitable -- a boost
    doesn't guarantee an arb exists, it just improves the price."""
    best = None
    for c in hedge_candidates:
        cost = c.get('cost_per_dollar')
        if not cost:
            continue
        hedge_stake = boosted_return * cost
        profit = boosted_return - stake - hedge_stake
        if profit <= 0:
            continue
        if best is None or profit > best['guaranteed_profit']:
            best = {**c, 'hedge_stake': round(hedge_stake, 2), 'guaranteed_profit': round(profit, 2)}
    return best


def _cash_capped_leg(stake, boosted_return, hedge, cash_available):
    """Given the boosted leg's stake/return and the chosen hedge dict, scales
    BOTH legs down proportionally if the hedge book's available cash can't
    cover hedge['hedge_stake'] as-is (arb math is linear in stake, so a
    uniform scale-down keeps it profitable, just smaller -- edge_pct is
    untouched since it's a ratio). Returns (stake, boosted_return, hedge)
    with hedge_stake/guaranteed_profit updated to match, or None if the
    hedge book's cash is 0 (no play possible at all)."""
    if not cash_available:
        return stake, boosted_return, hedge
    hedge_cash = cash_available.get(hedge['book'].lower())
    if hedge_cash is None or hedge['hedge_stake'] <= hedge_cash:
        return stake, boosted_return, hedge
    if hedge_cash <= 0:
        return None
    scale = hedge_cash / hedge['hedge_stake']
    new_stake = round(stake * scale, 2)
    new_hedge_stake = round(hedge['hedge_stake'] * scale, 2)
    new_profit = round(hedge['guaranteed_profit'] * scale, 2)
    if new_stake <= 0 or new_hedge_stake <= 0 or new_profit <= 0:
        # Scaled down to a fraction of a cent -- rounds to $0 on both legs.
        # That's not a real bet, it's noise; drop it instead of recommending
        # (and emailing) a "$0 wager, $0 return" play.
        return None
    new_hedge = dict(hedge, hedge_stake=new_hedge_stake, guaranteed_profit=new_profit)
    return new_stake, boosted_return * scale, new_hedge


def _free_bet_winnings(stake, american_price):
    """Profit ONLY (stake NOT returned) if a free bet's leg wins -- that's
    the entire point of a free bet: the stake is the sportsbook's
    promotional credit, never your money, so a win pays out the winnings
    portion only and a loss costs you nothing at all (there's no 'stake'
    of yours to lose). Same raw-profit math as _boosted_leg_return, just
    without the '+ stake' (stake back) or boost multiplier."""
    if american_price is None:
        return None
    return (stake * (american_price / 100.0) if american_price > 0
            else stake * (100.0 / abs(american_price)))


def _best_free_bet_hedge(winnings, hedge_candidates):
    """Free-bet analog of _best_hedge. Because the free leg has NO real
    money at risk (a loss costs nothing), the hedge only needs to be sized
    against the WINNINGS, not stake+return like a real-money boosted leg:
    hedge_stake = winnings * cost_per_dollar. That equalizes total profit
    whichever side wins -- if the free leg wins, profit = winnings -
    hedge_stake (the hedge's real stake is lost); if the hedge wins,
    profit = hedge_stake * (decimal_odds - 1), which reduces to the exact
    same value by construction (see reference/arb_tracker_notes.md for the
    algebra). Unlike a boosted leg, this is ALWAYS profitable for any
    hedge_candidates entry with cost < 1 (i.e. any real odds at all) --
    a free bet doesn't need a genuine cross-book arb to guarantee profit,
    unlike a boost. In a perfectly fair (no-vig) 50/50 market this extracts
    exactly 50% of the free bet's face value -- the textbook matched-
    betting number. Still picks whichever candidate maximizes profit
    (lowest cost_per_dollar, i.e. best odds on the hedge side)."""
    best = None
    for c in hedge_candidates:
        cost = c.get('cost_per_dollar')
        if not cost:
            continue
        hedge_stake = winnings * cost
        profit = winnings - hedge_stake
        if profit <= 0:
            continue
        if best is None or profit > best['guaranteed_profit']:
            best = {**c, 'hedge_stake': round(hedge_stake, 2), 'guaranteed_profit': round(profit, 2)}
    return best


def _scale_leg(leg, scale):
    leg = dict(leg)
    leg['stake'] = round(leg['stake'] * scale, 2)
    if 'boosted_return' in leg:
        leg['boosted_return'] = round(leg['boosted_return'] * scale, 2)
    if 'winnings' in leg:
        leg['winnings'] = round(leg['winnings'] * scale, 2)
    return leg


def _scale_play(p, scale, key_a, key_b):
    p = dict(p)
    p[key_a] = _scale_leg(p[key_a], scale)
    p[key_b] = _scale_leg(p[key_b], scale)
    p['guaranteed_profit'] = round(p.get('guaranteed_profit', 0) * scale, 2)
    if 'best_case_profit' in p:
        p['best_case_profit'] = round(p['best_case_profit'] * scale, 2)
    p['total_staked'] = round(p.get('total_staked', 0) * scale, 2)
    # edge_pct is a ratio (profit / total_staked) -- unaffected by uniform scaling.
    return p


def apply_cash_pool(plays, cash_available):
    """Takes a list of ALREADY-SELECTED final plays about to be recommended
    TOGETHER in one batch -- a solo boosted_scan play (boosted_leg/hedge_leg)
    or a dual_boost_combo_scan play (leg_a/leg_b), one per boost plus any
    combos -- and rations each book's cash_available across ALL of them.

    This is different from (and layered on top of) boosted_scan's/
    dual_boost_combo_scan's own per-play cap: those only guard a SINGLE play
    against exceeding the WHOLE stated balance in isolation. Two different
    boosts both hedging on Kalshi can each individually pass that check
    while their COMBINED real-dollar asks blow through the actual Kalshi
    balance -- that's exactly what this catches. Processes highest-
    guaranteed-profit-first (best plays get first claim on limited cash),
    scales a play down proportionally if what's left for one of its books
    can't fully cover it (same linear-arb-math scaling boosted_scan's own
    hedge cap uses), and drops it entirely once a book is fully spent -- OR,
    for a free bet marked non-splitable (p['splitable'] is False), drops it
    entirely as soon as ANY scale-down would be needed at all, since that
    kind of free bet can't be placed at a reduced size either way.
    Returns a new list, highest profit first -- caller re-splits/re-sorts
    solo vs combo plays as needed."""
    cash_available = cash_available or {}
    if not any(v is not None for v in cash_available.values()):
        return list(plays)  # nothing constrained -- don't touch anything

    remaining = {k: v for k, v in cash_available.items() if v is not None}
    ordered = sorted(plays, key=lambda p: p.get('guaranteed_profit', 0), reverse=True)
    kept = []
    for p in ordered:
        # A free bet's own leg is NEVER real money -- it's the sportsbook's
        # promotional credit, not a draw against that book's cash balance --
        # so unlike a combo (both legs real) or a solo boost (both legs
        # real), only the HEDGE side of a free-bet play gets checked/
        # decremented against remaining real cash. leg_a_is_real=False is
        # the only thing that differs from the boost/combo cases below.
        if p.get('combo'):
            key_a, key_b, leg_a_is_real = 'leg_a', 'leg_b', True
        elif p.get('free_bet'):
            key_a, key_b, leg_a_is_real = 'free_bet_leg', 'hedge_leg', False
        else:
            key_a, key_b, leg_a_is_real = 'boosted_leg', 'hedge_leg', True
        leg_a, leg_b = p[key_a], p[key_b]
        book_a, book_b = leg_a['book'].lower(), leg_b['book'].lower()
        stake_a, stake_b = leg_a['stake'], leg_b['stake']

        scale = 1.0
        if leg_a_is_real and book_a in remaining and stake_a > 0:
            scale = min(scale, remaining[book_a] / stake_a)
        if book_b in remaining and stake_b > 0:
            scale = min(scale, remaining[book_b] / stake_b)
        scale = max(scale, 0.0)
        if scale <= 0:
            continue

        if scale < 1.0:
            # A non-splitable free bet can't be partially placed (it must
            # go on ONE bet in full, per the sportsbook's own rule for that
            # promo) -- same principle free_bet_scan()'s own per-play cap
            # already applies, just re-checked here since THIS scale comes
            # from pooling across multiple simultaneous plays, a different
            # cash constraint than the single-play one.
            if p.get('free_bet') and not p.get('splitable', True):
                continue
            p = _scale_play(p, scale, key_a, key_b)

        if (p[key_a]['stake'] <= 0 or p[key_b]['stake'] <= 0
                or p.get('guaranteed_profit', 0) <= 0):
            # Scaled down to a fraction of a cent by a near-empty remaining
            # balance -- rounds to $0 on a leg (or profit). Not a real bet;
            # drop it instead of recommending/emailing a "$0 wager" play.
            continue

        if leg_a_is_real and book_a in remaining:
            remaining[book_a] -= p[key_a]['stake']
        if book_b in remaining:
            remaining[book_b] -= p[key_b]['stake']
        kept.append(p)
    return kept


def boosted_scan(book, boost_pct, max_wager, min_odds=-100000, sport='ALL', game_filter='', limit=25, expires='', cash_available=None, allowed_books=None, restrict_sports=None):
    """Given a profit-boost offer (which book, boost %, max wager it allows,
    and the minimum odds it's eligible on), find the best real-dollar hedge
    for every qualifying side/game/market -- checking Kalshi AND every other
    legal sportsbook as the hedge and keeping whichever pays more (with 3
    books now, that's 2 sportsbook candidates plus Kalshi, not just 1+1). Unlike
    scan()'s opportunities (proportional 100-unit basis), these use the
    ACTUAL max_wager dollar amount, since that's a fixed cap from the boost
    offer, not something to optimize -- always use the full allowed wager,
    that's what makes the boost worth the most (established by hand this
    session for the MLB/NFL boost examples). Returns a flat list sorted by
    guaranteed profit descending. Covers moneyline and totals -- the two
    market types actually used this session; spread/runline would use the
    identical mechanism if ever wanted.

    cash_available: optional {book: dollars_or_None} (see cash_store.py) --
    caps the boosted leg's stake at whatever's actually sitting in that
    book's account (never mind that the boost allows more), and separately
    caps the hedge leg the same way against WHATEVER book ends up hedging
    it. Both legs scale down together proportionally when capped (arb math
    is linear in stake, so this can't turn a profitable play unprofitable --
    it just shrinks it), and edge_pct is unaffected since it's a ratio. A
    book with no entry (or this whole arg left None) means unlimited, same
    as before this existed. Per-play cap only -- doesn't account for
    multiple simultaneous plays sharing the same book's one real balance.

    allowed_books: optional set of lowercase book names a HEDGE candidate is
    allowed to use (None = no restriction) -- from the mobile page's own
    book filter chips, so a filtered-out book never gets picked as the
    hedge even if it would've paid the most; the scan re-maximizes profit
    among whatever's left instead of just hiding an already-chosen play.
    restrict_sports: optional set of sport codes to scan (None = no
    restriction) -- same idea for the sport filter chips, intersected with
    this boost's own  setting rather than overriding it.
    """
    other_books = [b for b in SPORTSBOOKS if b != book]
    if allowed_books is not None:
        other_books = [b for b in other_books if b in allowed_books]
    sports = [sport] if sport != 'ALL' else ('MLB', 'NFL', 'NCAAF')
    if restrict_sports is not None:
        sports = [sp for sp in sports if sp in restrict_sports]
    plays = []

    cash_available = cash_available or {}
    book_cash = cash_available.get(book)
    if book_cash is not None:
        max_wager = min(max_wager, book_cash)
    if max_wager <= 0:
        return []

    for sp in sports:
        url, sections = SPORT_PAGES[sp]
        try:
            html = fetch_html(url)
        except Exception:
            continue
        raw_games = parse_sport_page(html, sections)
        # Labels computed from the RAW, pre-filter scrape -- a doubleheader's
        # already-finished opener still counts toward "how many games share
        # this team pair today" even after it gets dropped by the .get('final')
        # filter below, so the surviving nightcap still reads "(Game 2 of 2)"
        # instead of looking identical to (and thus like stale data from) the
        # game that already ended.
        labels = _disambiguate_labels(raw_games)
        games = [g for g in raw_games if not g.get('final')]

        try:
            kalshi_games = kalshi_client.fetch_games(sp)
        except Exception:
            kalshi_games = []
        try:
            kalshi_totals = kalshi_client.fetch_totals(sp)
        except Exception:
            kalshi_totals = []
        kalshi_games = [g for g in kalshi_games if not _game_has_started(g.get('close_time'))]
        kalshi_totals = [g for g in kalshi_totals if not _game_has_started(g.get('close_time'))]
        close_lookup = _build_close_time_lookup(kalshi_games, kalshi_totals)
        games = [
            g for g in games
            if not _game_has_started(g.get('start_time'))
            and not _game_has_started(close_lookup.get(frozenset((g['team_a'], g['team_b']))))
            and not _game_after_expiration(expires, g.get('start_time'))
        ]

        kg_by_teams = {frozenset((g['team_a'], g['team_b'])): g for g in kalshi_games}
        kt_by_teams = {frozenset((g['team_a'], g['team_b'])): g for g in kalshi_totals}

        for g in games:
            label = labels[g['game_id']]
            if game_filter and game_filter.lower() not in label.lower():
                continue
            key = frozenset((g['team_a'], g['team_b']))

            if g['market'] == 'moneyline':
                kg = kg_by_teams.get(key)
                for side_key, opp_key, side_team, opp_team in (
                    ('a', 'b', g['team_a'], g['team_b']),
                    ('b', 'a', g['team_b'], g['team_a']),
                ):
                    _, price = book_lines(g[side_key], book)
                    if price is None or price < min_odds:
                        continue
                    boosted_return = _boosted_leg_return(max_wager, price, boost_pct)
                    candidates = []
                    if kg and (allowed_books is None or 'kalshi' in allowed_books):
                        opp_odds = kg['team_a_odds'] if kg['team_a'] == opp_team else kg['team_b_odds']
                        ask = (opp_odds or {}).get('yes_ask')
                        if ask:
                            candidates.append({'book': 'Kalshi', 'side': f"{opp_team} to win (buy Yes)",
                                                'price_display': f"{kalshi_multiplier(ask):.2f}x", 'cost_per_dollar': kalshi_effective_cost(ask)})
                    for ob in other_books:
                        _, opp_price = book_lines(g[opp_key], ob)
                        if opp_price is not None:
                            candidates.append({'book': ob.capitalize(), 'side': f"{opp_team} to win",
                                                'price_display': opp_price, 'cost_per_dollar': implied_prob(opp_price)})
                    hedge = _best_hedge(boosted_return, max_wager, candidates)
                    if hedge:
                        capped = _cash_capped_leg(max_wager, boosted_return, hedge, cash_available)
                        if capped is None:
                            continue
                        leg_stake, leg_return, hedge = capped
                        total_staked = leg_stake + hedge['hedge_stake']
                        plays.append({
                            'sport': sp, 'market': 'moneyline', 'game': label,
                            'boosted_leg': {'book': book.capitalize(), 'side': f"{side_team} to win",
                                            'price': price, 'stake': leg_stake, 'boosted_return': round(leg_return, 2)},
                            'hedge_leg': {'book': hedge['book'], 'side': hedge['side'],
                                          'price': hedge['price_display'], 'stake': hedge['hedge_stake']},
                            'guaranteed_profit': hedge['guaranteed_profit'],
                            'total_staked': round(total_staked, 2),
                            'edge_pct': round(hedge['guaranteed_profit'] / total_staked * 100, 2),
                        })

            elif g['market'] == 'total':
                kt = kt_by_teams.get(key)
                strikes_by_line = {s['floor_strike']: s for s in kt['strikes']} if kt else {}
                for side_key, opp_key, side_label, opp_label, kalshi_field in (
                    ('a', 'b', 'Over', 'Under', 'no_ask'),
                    ('b', 'a', 'Under', 'Over', 'yes_ask'),
                ):
                    line, price = book_lines(g[side_key], book)
                    if price is None or price < min_odds or line is None:
                        continue
                    boosted_return = _boosted_leg_return(max_wager, price, boost_pct)
                    candidates = []
                    strike = strikes_by_line.get(line)
                    if strike and strike.get(kalshi_field) and (allowed_books is None or 'kalshi' in allowed_books):
                        candidates.append({'book': 'Kalshi', 'side': f"{opp_label} {line} (buy {'No' if kalshi_field=='no_ask' else 'Yes'})",
                                            'price_display': f"{kalshi_multiplier(strike[kalshi_field]):.2f}x",
                                            'cost_per_dollar': kalshi_effective_cost(strike[kalshi_field])})
                    for ob in other_books:
                        opp_line, opp_price = book_lines(g[opp_key], ob)
                        if opp_price is not None and opp_line is not None and abs(opp_line - line) < 1e-6:
                            candidates.append({'book': ob.capitalize(), 'side': f"{opp_label} {line}",
                                                'price_display': opp_price, 'cost_per_dollar': implied_prob(opp_price)})
                    hedge = _best_hedge(boosted_return, max_wager, candidates)
                    if hedge:
                        capped = _cash_capped_leg(max_wager, boosted_return, hedge, cash_available)
                        if capped is None:
                            continue
                        leg_stake, leg_return, hedge = capped
                        total_staked = leg_stake + hedge['hedge_stake']
                        plays.append({
                            'sport': sp, 'market': 'total', 'game': label,
                            'boosted_leg': {'book': book.capitalize(), 'side': f"{side_label} {line}",
                                            'price': price, 'stake': leg_stake, 'boosted_return': round(leg_return, 2)},
                            'hedge_leg': {'book': hedge['book'], 'side': hedge['side'],
                                          'price': hedge['price_display'], 'stake': hedge['hedge_stake']},
                            'guaranteed_profit': hedge['guaranteed_profit'],
                            'total_staked': round(total_staked, 2),
                            'edge_pct': round(hedge['guaranteed_profit'] / total_staked * 100, 2),
                        })

    plays.sort(key=lambda p: p['guaranteed_profit'], reverse=True)
    plays = plays[:limit]
    for i, p in enumerate(plays):
        p['id'] = i
    return plays


def free_bet_scan(book, free_bet_amount, min_odds=-100000, sport='ALL', game_filter='', limit=25, expires='', cash_available=None, splitable=True, allowed_books=None, restrict_sports=None):
    """Free-bet analog of boosted_scan(). A free bet ('site credit', 'risk-
    free bet' from a promo/referral) is stake-not-returned: win it and you
    get the winnings only (never the stake back, since it was never your
    money), lose it and it simply costs nothing (again, never your money).
    That means, unlike a boost, NO genuine cross-book arb is required to
    guarantee profit -- _best_free_bet_hedge finds a real-money hedge on
    every qualifying side/game/market, checking Kalshi and every other
    sportsbook exactly like boosted_scan does, and it's ALWAYS profitable
    for any hedge with real odds (see _best_free_bet_hedge's docstring).

    free_bet_amount is the free bet's face value, used as an upper bound
    exactly like a boost's max_wager -- it can get scaled down (never up)
    by _cash_capped_leg if the best hedge's cash-available can't cover
    the hedge stake at full size (same linear-arb scaling boosted_scan
    uses). Unlike max_wager, free_bet_amount is NEVER capped against
    cash_available[book] itself -- a free bet's face value is promotional
    credit, not a draw against that book's real cash balance, so it isn't
    constrained by how much real money happens to be sitting there.

    splitable: whether the sportsbook lets this free bet's credit be split
    across multiple separate wagers (True) or requires it be placed in full
    on ONE bet (False). When False, a hedge that can't fully cover the
    FACE-VALUE hedge stake can't be partially used either -- the play is
    dropped entirely (this game/market combo simply isn't playable with
    this free bet right now) rather than shown at a reduced, technically-
    unplaceable size. apply_cash_pool() applies the same rule when pooling
    cash across multiple simultaneous plays.

    Covers moneyline and totals, same as boosted_scan. cash_available caps
    only the HEDGE leg (the free leg never touches real cash either way).

    allowed_books/restrict_sports: same meaning as boosted_scan's -- the
    mobile page's book/sport filter chips, so a filtered play gets properly
    RE-MAXIMIZED within the filter (a different hedge book, or dropped if
    this free bet's own sport has no overlap with the filter) instead of
    just being hidden after the fact with no chance to pick a better one.
    """
    other_books = [b for b in SPORTSBOOKS if b != book]
    if allowed_books is not None:
        other_books = [b for b in other_books if b in allowed_books]
    sports = [sport] if sport != 'ALL' else ('MLB', 'NFL', 'NCAAF')
    if restrict_sports is not None:
        sports = [sp for sp in sports if sp in restrict_sports]
    plays = []

    if free_bet_amount <= 0:
        return []

    for sp in sports:
        url, sections = SPORT_PAGES[sp]
        try:
            html = fetch_html(url)
        except Exception:
            continue
        raw_games = parse_sport_page(html, sections)
        labels = _disambiguate_labels(raw_games)
        games = [g for g in raw_games if not g.get('final')]

        try:
            kalshi_games = kalshi_client.fetch_games(sp)
        except Exception:
            kalshi_games = []
        try:
            kalshi_totals = kalshi_client.fetch_totals(sp)
        except Exception:
            kalshi_totals = []
        kalshi_games = [g for g in kalshi_games if not _game_has_started(g.get('close_time'))]
        kalshi_totals = [g for g in kalshi_totals if not _game_has_started(g.get('close_time'))]
        close_lookup = _build_close_time_lookup(kalshi_games, kalshi_totals)
        games = [
            g for g in games
            if not _game_has_started(g.get('start_time'))
            and not _game_has_started(close_lookup.get(frozenset((g['team_a'], g['team_b']))))
            and not _game_after_expiration(expires, g.get('start_time'))
        ]

        kg_by_teams = {frozenset((g['team_a'], g['team_b'])): g for g in kalshi_games}
        kt_by_teams = {frozenset((g['team_a'], g['team_b'])): g for g in kalshi_totals}

        for g in games:
            label = labels[g['game_id']]
            if game_filter and game_filter.lower() not in label.lower():
                continue
            key = frozenset((g['team_a'], g['team_b']))

            if g['market'] == 'moneyline':
                kg = kg_by_teams.get(key)
                for side_key, opp_key, side_team, opp_team in (
                    ('a', 'b', g['team_a'], g['team_b']),
                    ('b', 'a', g['team_b'], g['team_a']),
                ):
                    _, price = book_lines(g[side_key], book)
                    if price is None or price < min_odds:
                        continue
                    winnings = _free_bet_winnings(free_bet_amount, price)
                    candidates = []
                    if kg and (allowed_books is None or 'kalshi' in allowed_books):
                        opp_odds = kg['team_a_odds'] if kg['team_a'] == opp_team else kg['team_b_odds']
                        ask = (opp_odds or {}).get('yes_ask')
                        if ask:
                            candidates.append({'book': 'Kalshi', 'side': f"{opp_team} to win (buy Yes)",
                                                'price_display': f"{kalshi_multiplier(ask):.2f}x", 'cost_per_dollar': kalshi_effective_cost(ask)})
                    for ob in other_books:
                        _, opp_price = book_lines(g[opp_key], ob)
                        if opp_price is not None:
                            candidates.append({'book': ob.capitalize(), 'side': f"{opp_team} to win",
                                                'price_display': opp_price, 'cost_per_dollar': implied_prob(opp_price)})
                    hedge = _best_free_bet_hedge(winnings, candidates)
                    if hedge:
                        capped = _cash_capped_leg(free_bet_amount, winnings, hedge, cash_available)
                        if capped is None:
                            continue
                        leg_stake, leg_winnings, hedge = capped
                        if not splitable and leg_stake < free_bet_amount - 0.01:
                            continue  # can't place a smaller portion -- must be full or nothing
                        plays.append({
                            'sport': sp, 'market': 'moneyline', 'game': label, 'free_bet': True,
                            'splitable': splitable,
                            'free_bet_leg': {'book': book.capitalize(), 'side': f"{side_team} to win",
                                             'price': price, 'stake': leg_stake, 'winnings': round(leg_winnings, 2)},
                            'hedge_leg': {'book': hedge['book'], 'side': hedge['side'],
                                          'price': hedge['price_display'], 'stake': hedge['hedge_stake']},
                            'guaranteed_profit': hedge['guaranteed_profit'],
                            'total_staked': hedge['hedge_stake'],
                            'edge_pct': round(hedge['guaranteed_profit'] / leg_stake * 100, 2) if leg_stake else 0,
                        })

            elif g['market'] == 'total':
                kt = kt_by_teams.get(key)
                strikes_by_line = {s['floor_strike']: s for s in kt['strikes']} if kt else {}
                for side_key, opp_key, side_label, opp_label, kalshi_field in (
                    ('a', 'b', 'Over', 'Under', 'no_ask'),
                    ('b', 'a', 'Under', 'Over', 'yes_ask'),
                ):
                    line, price = book_lines(g[side_key], book)
                    if price is None or price < min_odds or line is None:
                        continue
                    winnings = _free_bet_winnings(free_bet_amount, price)
                    candidates = []
                    strike = strikes_by_line.get(line)
                    if strike and strike.get(kalshi_field) and (allowed_books is None or 'kalshi' in allowed_books):
                        candidates.append({'book': 'Kalshi', 'side': f"{opp_label} {line} (buy {'No' if kalshi_field=='no_ask' else 'Yes'})",
                                            'price_display': f"{kalshi_multiplier(strike[kalshi_field]):.2f}x",
                                            'cost_per_dollar': kalshi_effective_cost(strike[kalshi_field])})
                    for ob in other_books:
                        opp_line, opp_price = book_lines(g[opp_key], ob)
                        if opp_price is not None and opp_line is not None and abs(opp_line - line) < 1e-6:
                            candidates.append({'book': ob.capitalize(), 'side': f"{opp_label} {line}",
                                                'price_display': opp_price, 'cost_per_dollar': implied_prob(opp_price)})
                    hedge = _best_free_bet_hedge(winnings, candidates)
                    if hedge:
                        capped = _cash_capped_leg(free_bet_amount, winnings, hedge, cash_available)
                        if capped is None:
                            continue
                        leg_stake, leg_winnings, hedge = capped
                        if not splitable and leg_stake < free_bet_amount - 0.01:
                            continue  # can't place a smaller portion -- must be full or nothing
                        plays.append({
                            'sport': sp, 'market': 'total', 'game': label, 'free_bet': True,
                            'splitable': splitable,
                            'free_bet_leg': {'book': book.capitalize(), 'side': f"{side_label} {line}",
                                             'price': price, 'stake': leg_stake, 'winnings': round(leg_winnings, 2)},
                            'hedge_leg': {'book': hedge['book'], 'side': hedge['side'],
                                          'price': hedge['price_display'], 'stake': hedge['hedge_stake']},
                            'guaranteed_profit': hedge['guaranteed_profit'],
                            'total_staked': hedge['hedge_stake'],
                            'edge_pct': round(hedge['guaranteed_profit'] / leg_stake * 100, 2) if leg_stake else 0,
                        })

    plays.sort(key=lambda p: p['guaranteed_profit'], reverse=True)
    plays = plays[:limit]
    for i, p in enumerate(plays):
        p['id'] = i
    return plays


def _combo_from_sides(bA, price_a, side_a_desc, bB, price_b, side_b_desc, sport, market, game_label):
    """One candidate dual-boost combo: bA's boost staked on one side, bB's
    boost staked on the OPPOSITE side of the same market -- no external hedge,
    the two boosted bets cover each other directly. Both stakes use their own
    full max_wager (same "always use the full allowed amount" convention as
    boosted_scan/_best_hedge). Unlike a balanced hedge, the two outcomes
    usually pay different amounts since neither leg's size was chosen to
    equalize them -- so this reports the guaranteed (worst-case) floor AND
    the better-case upside, not one flat number. Returns None if either price
    is missing/below its own boost's min_odds, or if the worst case isn't
    actually profitable (a boost improves the price, it doesn't guarantee
    this pairing beats the vig)."""
    if price_a is None or price_b is None:
        return None
    if price_a < bA['min_odds'] or price_b < bB['min_odds']:
        return None
    stake_a, stake_b = bA['max_wager'], bB['max_wager']
    return_a = _boosted_leg_return(stake_a, price_a, bA['boost_pct'])
    return_b = _boosted_leg_return(stake_b, price_b, bB['boost_pct'])
    total_staked = stake_a + stake_b
    profit_if_a = return_a - total_staked
    profit_if_b = return_b - total_staked
    guaranteed = min(profit_if_a, profit_if_b)
    if guaranteed <= 0:
        return None
    best_case = max(profit_if_a, profit_if_b)
    best_case_side = side_a_desc if profit_if_a >= profit_if_b else side_b_desc
    return {
        'sport': sport, 'market': market, 'game': game_label, 'combo': True,
        'leg_a': {'book': bA['book'].capitalize(), 'side': side_a_desc, 'price': price_a,
                  'stake': stake_a, 'boosted_return': round(return_a, 2),
                  'boost_pct': round(bA['boost_pct'] * 100), 'boost_id': bA.get('_id')},
        'leg_b': {'book': bB['book'].capitalize(), 'side': side_b_desc, 'price': price_b,
                  'stake': stake_b, 'boosted_return': round(return_b, 2),
                  'boost_pct': round(bB['boost_pct'] * 100), 'boost_id': bB.get('_id')},
        'guaranteed_profit': round(guaranteed, 2),
        'best_case_profit': round(best_case, 2),
        'best_case_side': best_case_side,
        'total_staked': round(total_staked, 2),
        'edge_pct': round(guaranteed / total_staked * 100, 2),
    }


def dual_boost_combo_scan(boosts, limit=25, cash_available=None, restrict_sports=None):
    """Given ALL the user's currently-saved boost offers at once (not one at a
    time like boosted_scan), look for pairs that land on OPPOSITE sides of the
    SAME game+market+line at TWO DIFFERENT books -- e.g. one boost used on
    DK's Under, another on FD's Over, same total line. Unlike boosted_scan's
    single-leg-plus-hedge pattern, neither leg here needs a Kalshi/other-book
    hedge: the two boosted bets hedge each other directly. `boosts` is a list
    of dicts: book, sport ('ALL' or specific), game (filter substring, '' for
    any), min_odds, max_wager, boost_pct (already a 0-1 fraction). Returns a
    flat list of combo plays sorted by guaranteed (worst-case) profit
    descending -- each also has a 'combo': True flag so the caller can render
    it differently from a solo boosted_scan play.

    cash_available: optional {book: dollars_or_None} (see cash_store.py) --
    each leg is capped independently against its OWN book's balance (unlike
    boosted_scan's hedge leg, these two stakes aren't linked by a shared
    formula, so there's no proportional-scaling step needed here -- just cap
    each and skip the pair if either side has $0 to work with).

    restrict_sports: optional set of sport codes (None = no restriction) --
    the mobile page's sport filter chips. No allowed_books param here on
    purpose: unlike a hedge candidate, a combo's two legs are BOTH already-
    loaded boosts, so book filtering happens one level up (the caller drops
    a loaded boost whose own book isn't in the filter before it ever
    reaches this function, same as it does for a solo boosted_scan call).
    """
    plays = []
    if len(boosts) < 2:
        return plays
    cash_available = cash_available or {}

    needed_sports = {b['sport'] for b in boosts if b['sport'] != 'ALL'}
    if not needed_sports:
        needed_sports = {'MLB', 'NFL', 'NCAAF'}
    # Sport filter chips (mobile page) intersected in the same way boosted_scan/
    # free_bet_scan do -- a combo pair whose only shared sport got filtered out
    # simply produces no combos, rather than a combo the filter should've hidden.
    if restrict_sports is not None:
        needed_sports = needed_sports & restrict_sports

    games_by_sport = {}
    labels_by_sport = {}
    for sp in needed_sports:
        if sp not in SPORT_PAGES:
            continue
        url, sections = SPORT_PAGES[sp]
        try:
            html = fetch_html(url)
        except Exception:
            continue
        raw_games = parse_sport_page(html, sections)
        labels_by_sport[sp] = _disambiguate_labels(raw_games)  # see boosted_scan's identical comment
        games = [g for g in raw_games if not g.get('final')]

        # Same second safety net boosted_scan() uses: VI's own start_time can be
        # missing/stale, so also cross-check against Kalshi's close_time for the
        # same team pair (Kalshi drops a market the instant it closes, which is
        # itself unreliable alone -- combining both is what actually caught the
        # live-game bug this session).
        try:
            kalshi_games = kalshi_client.fetch_games(sp)
        except Exception:
            kalshi_games = []
        try:
            kalshi_totals = kalshi_client.fetch_totals(sp)
        except Exception:
            kalshi_totals = []
        kalshi_games = [g for g in kalshi_games if not _game_has_started(g.get('close_time'))]
        kalshi_totals = [g for g in kalshi_totals if not _game_has_started(g.get('close_time'))]
        close_lookup = _build_close_time_lookup(kalshi_games, kalshi_totals)
        games_by_sport[sp] = [
            g for g in games
            if not _game_has_started(g.get('start_time'))
            and not _game_has_started(close_lookup.get(frozenset((g['team_a'], g['team_b']))))
        ]

    for b1, b2 in itertools.combinations(boosts, 2):
        if b1['book'] == b2['book']:
            continue  # can't bet both sides of one market at the same book
        b1_cash, b2_cash = cash_available.get(b1['book']), cash_available.get(b2['book'])
        if b1_cash is not None:
            b1 = dict(b1, max_wager=min(b1['max_wager'], b1_cash))
        if b2_cash is not None:
            b2 = dict(b2, max_wager=min(b2['max_wager'], b2_cash))
        if b1['max_wager'] <= 0 or b2['max_wager'] <= 0:
            continue  # no cash left at one (or both) of this pair's books
        pair_sports = {b['sport'] for b in (b1, b2) if b['sport'] != 'ALL'}
        if len(pair_sports) == 2:
            continue  # scoped to two different specific sports -- can't both apply to one game
        candidate_sports = pair_sports if pair_sports else needed_sports

        for sp in candidate_sports:
            games = games_by_sport.get(sp, [])
            labels = labels_by_sport.get(sp, {})
            gf1, gf2 = b1['game'].lower(), b2['game'].lower()
            for g in games:
                label = labels[g['game_id']]
                ll = label.lower()
                if gf1 and gf1 not in ll:
                    continue
                if gf2 and gf2 not in ll:
                    continue
                if (_game_after_expiration(b1.get('expires'), g.get('start_time'))
                        or _game_after_expiration(b2.get('expires'), g.get('start_time'))):
                    continue

                if g['market'] == 'moneyline':
                    _, pa1 = book_lines(g['a'], b1['book'])
                    _, pb2 = book_lines(g['b'], b2['book'])
                    combo = _combo_from_sides(b1, pa1, f"{g['team_a']} to win",
                                               b2, pb2, f"{g['team_b']} to win",
                                               sp, 'moneyline', label)
                    if combo:
                        plays.append(combo)
                    _, pb1 = book_lines(g['b'], b1['book'])
                    _, pa2 = book_lines(g['a'], b2['book'])
                    combo_flip = _combo_from_sides(b1, pb1, f"{g['team_b']} to win",
                                                    b2, pa2, f"{g['team_a']} to win",
                                                    sp, 'moneyline', label)
                    if combo_flip:
                        plays.append(combo_flip)

                elif g['market'] == 'total':
                    line_a1, pa1 = book_lines(g['a'], b1['book'])
                    line_b2, pb2 = book_lines(g['b'], b2['book'])
                    if line_a1 is not None and line_b2 is not None and abs(line_a1 - line_b2) < 1e-6:
                        combo = _combo_from_sides(b1, pa1, f'Over {line_a1}',
                                                   b2, pb2, f'Under {line_b2}',
                                                   sp, 'total', label)
                        if combo:
                            plays.append(combo)
                    line_b1, pb1 = book_lines(g['b'], b1['book'])
                    line_a2, pa2 = book_lines(g['a'], b2['book'])
                    if line_b1 is not None and line_a2 is not None and abs(line_b1 - line_a2) < 1e-6:
                        combo_flip = _combo_from_sides(b1, pb1, f'Under {line_b1}',
                                                        b2, pa2, f'Over {line_a2}',
                                                        sp, 'total', label)
                        if combo_flip:
                            plays.append(combo_flip)

    plays.sort(key=lambda p: p['guaranteed_profit'], reverse=True)
    plays = plays[:limit]
    for i, p in enumerate(plays):
        p['id'] = i
    return plays


def scan(sports=('MLB', 'NFL', 'NCAAF')):
    opportunities = []
    for sport in sports:
        url, sections = SPORT_PAGES[sport]
        try:
            html = fetch_html(url)
        except Exception as e:
            continue
        games = [g for g in parse_sport_page(html, sections) if not g.get('final')]

        try:
            kalshi_games = kalshi_client.fetch_games(sport)
        except Exception:
            kalshi_games = []
        try:
            kalshi_totals = kalshi_client.fetch_totals(sport)
        except Exception:
            kalshi_totals = []

        # Drop any game whose scheduled start (Kalshi's close_time) has
        # already passed -- prevents alerting on a stale pre-game price for
        # a game that's actually live/in-progress (real bug: DK showed +120
        # after kickoff when the live line had moved to -560). Applies to
        # Kalshi's own lists directly, and to the VI-scraped games via a
        # team-pair lookup since those have no close_time of their own.
        kalshi_games = [g for g in kalshi_games if not _game_has_started(g.get('close_time'))]
        kalshi_totals = [g for g in kalshi_totals if not _game_has_started(g.get('close_time'))]
        close_time_lookup = _build_close_time_lookup(kalshi_games, kalshi_totals)
        games = [
            g for g in games
            if not _game_has_started(g.get('start_time'))
            and not _game_has_started(close_time_lookup.get(frozenset((g['team_a'], g['team_b']))))
        ]
        ml_games = [g for g in games if g['market'] == 'moneyline']
        total_games = [g for g in games if g['market'] == 'total']

        opportunities += dk_fd_moneyline_and_lines(sport, games)

        if kalshi_games:
            opportunities += dk_fd_vs_kalshi(sport, ml_games, kalshi_games)

        if kalshi_totals and total_games:
            opportunities += dk_fd_totals_vs_kalshi(sport, total_games, kalshi_totals)

        opportunities += kalshi_internal_arb(sport, kalshi_games, kalshi_totals)
        opportunities += kalshi_player_prop_arb(sport)

    # Guaranteed wins/pushes only -- middles have real downside, excluded per request.
    opportunities = [o for o in opportunities if o['type'] != 'middle']
    opportunities.sort(key=lambda o: o.get('edge_pct', o.get('best_case', 0)), reverse=True)
    for i, o in enumerate(opportunities):
        o['id'] = i
    return {'generated_at': time.time(), 'opportunities': opportunities}


if __name__ == '__main__':
    import json
    result = scan()
    print(f"{len(result['opportunities'])} opportunities found")
    print(json.dumps(result['opportunities'][:5], indent=2))
