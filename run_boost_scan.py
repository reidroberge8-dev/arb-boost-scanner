"""
One-shot multi-boost calculation + cash-available refresh, run by the
GitHub Actions workflow (.github/workflows/boost-scan.yml) on behalf of the
mobile page in docs/. NOT part of the always-on app.py/WorkSpace pipeline,
and shares no PERSISTENT state with it -- the boost list itself lives only
in the mobile page's own browser storage (never committed to this public
repo); this script receives that list fresh on every invocation via
BOOSTS_JSON and never writes it anywhere durable. Deliberate: this repo is
public, so nothing that's meant to stay private (your actual boost list,
account balances) should ever land in a permanently-fetchable file here --
only in a single run's own randomly-named result, same model as v1.

Two modes (env var MODE):
  cash_only -- just fetch the bankroll sheet, return live cash/P&L. Skips
               the odds scan entirely (fast path for a standalone "Refresh
               Cash" button).
  scan (default) -- runs the SAME multi-boost selection logic the live
               WorkSpace dashboard's "Calculate All" uses (ported from
               boost_trigger_poller.py: one distinct play claimed per
               boost, Fanatics exempted from the dedup check since it
               allows duplicate boosted bets), plus the 2-boost combo scan,
               optionally cash-capped, plus a fresh cash-available fetch
               alongside it (so the page's cash display never gets stale
               just because someone forgot to hit Refresh Cash first).
"""
import json
import os
import time
import traceback
from datetime import datetime, timezone

from arb_engine import (
    boosted_scan, dual_boost_combo_scan, apply_cash_pool, _game_has_started, free_bet_scan,
    dk_fd_vs_kalshi, dk_fd_totals_vs_kalshi, kalshi_internal_arb, _build_close_time_lookup,
    _to_eastern_date, _game_after_expiration, SPORTSBOOKS, kalshi_multiplier,
)
import kalshi_client
from odds_scraper import SPORT_PAGES, fetch_html, parse_sport_page, find_true_arb, find_middles, book_lines

# The only 4 books Reid actually holds accounts at -- a middle he can't bet
# both legs of is just noise, so scan_market_wide() restricts MIDDLES (not
# true-arb, which stays market-wide) to pairs where both books are in here.
MY_BOOKS = {'draftkings', 'fanduel', 'fanatics', 'kalshi'}


def decimal_odds(price):
    """American odds -> total return multiple per $1 staked (e.g. -110 -> 1.909)."""
    return 1 + price / 100.0 if price > 0 else 1 + 100.0 / abs(price)


def _middle_stakes_from_decimals(hit, dec_a, dec_b):
    """Shared unit-sizing math behind add_middle_stakes: leg A = 1 unit, leg B
    sized so the profit is IDENTICAL whichever single leg wins alone (there's
    no double-loss outcome in a middle by construction -- the two lines
    always overlap the full range of possible results, so exactly one leg
    always cashes even when the window itself is missed). This is the
    standard "size a middle" formula -- it does NOT guarantee that equalized
    worst case is >= 0. Some middles carry a small guaranteed cost for a big
    payout if the window hits; others are truly free. worst_case_per_unit
    tells you which kind this particular one is, so don't just assume zero
    loss.

    Takes each leg's own decimal multiplier (total return per $1 staked)
    directly rather than deriving both from American-odds prices the way
    add_middle_stakes below does -- a Kalshi leg's own 'price' is an ask
    (0-1 cost), not American odds, so dk_fd_totals_middle_vs_kalshi computes
    its decimal multiplier via kalshi_multiplier() first and calls this
    directly instead of add_middle_stakes."""
    units_a = 1.0
    units_b = round(dec_a / dec_b, 3)
    total = units_a + units_b
    hit['units_a'] = units_a
    hit['units_b'] = units_b
    hit['worst_case_per_unit'] = round(units_a * dec_a - total, 3)  # == units_b*dec_b - total
    hit['best_case_per_unit'] = round(units_a * dec_a + units_b * dec_b - total, 3)
    return hit


def add_middle_stakes(hit):
    """find_middles' own hits (odds_scraper.py) have BOTH legs priced as
    American odds -- decimal_odds() applies directly to each. See
    _middle_stakes_from_decimals for the actual sizing math and the Kalshi-
    leg case this was split out to also support."""
    return _middle_stakes_from_decimals(hit, decimal_odds(hit['price_a']), decimal_odds(hit['price_b']))


RUN_ID = os.environ["RUN_ID"]
MODE = os.environ.get("MODE", "scan").strip().lower()
USE_CASH = os.environ.get("USE_CASH", "true").strip().lower() == "true"
RESULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "results", f"{RUN_ID}.json")


def _strip_internal(plays):
    """boosted_scan/free_bet_scan stamp underscore-prefixed working fields
    (_primary_amount_full, _target_payout_full, _profitable_candidates) on
    each play so apply_cash_pool can re-run its hedge blend against a
    smaller remaining cash pool later -- see arb_engine.apply_cash_pool's
    docstring. Purely internal plumbing, not meant for the mobile page's
    JSON; stripped here right before the final result is assembled."""
    return [{k: v for k, v in p.items() if not k.startswith("_")} for p in plays]


def wager_key(p):
    # Free-bet plays use free_bet_leg instead of boosted_leg -- otherwise
    # identical, so boosts and free bets can share ONE claimed-wagers set
    # (see pick_top_plays/pick_top_freebets) and never both get recommended
    # on the same book/game/market/side.
    leg = p.get("free_bet_leg") or p["boosted_leg"]
    return (leg["book"], p["game"], p["market"], leg["side"])


def boost_desc(b):
    game = f" / {b['game']}" if b.get("game") else ""
    exp = f", expires {b['expires']}" if b.get("expires") else ""
    return f"{b['book'].capitalize()} {b['sport']}{game} ({b['boost_pct']*100:.0f}% boost, ${b['max_wager']:.0f} max{exp})"


def freebet_desc(fb):
    game = f" / {fb['game']}" if fb.get("game") else ""
    exp = f", expires {fb['expires']}" if fb.get("expires") else ""
    return f"{fb['book'].capitalize()} {fb['sport']}{game} (${fb['free_bet_amount']:.0f} free bet{exp})"


def _is_expired(expires):
    """True if expires (a boost/free-bet's optional 'YYYY-MM-DD' expiration
    date) is strictly before today's US-Eastern calendar date -- i.e. the
    offer itself is dead already, independent of whatever the scan did or
    didn't find. Checked directly against today's date rather than any
    particular game's start_time, so pick_top_plays/pick_top_freebets can
    tell 'this boost is expired' apart from 'the market genuinely has
    nothing right now' (real gap reported 9/28: both cases produced the
    identical 'no qualifying play found right now' message, giving no clue
    which one it actually was). '' / None means never expires -> always
    False, same convention as _game_after_expiration in arb_engine.py."""
    if not expires:
        return False
    try:
        exp_date = datetime.strptime(expires, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return False
    return _to_eastern_date(datetime.now(timezone.utc)) > exp_date


def dk_fd_totals_middle_vs_kalshi(sport, total_games, kalshi_totals):
    """Cross-market MIDDLE between a sportsbook's Over/Under total line and
    ANY Kalshi ladder strike -- unlike dk_fd_totals_vs_kalshi (true arb,
    exact-strike-match only), this deliberately looks at EVERY strike that
    does NOT match the sportsbook's own line, since a middle's whole point
    is a window between two DIFFERENT numbers where both bets win.

    Two shapes, same idea as odds_scraper.find_middles' own cross-sportsbook
    totals middle, just with one leg on Kalshi instead of two sportsbooks:
      1. Sportsbook Over(over_line) + Kalshi 'No' on a HIGHER strike (i.e.
         Under(strike), strike > over_line): if the actual total lands in
         (over_line, strike], BOTH legs win.
      2. Kalshi 'Yes' on a LOWER strike (i.e. Over(strike), strike <
         under_line) + sportsbook Under(under_line): if the actual total
         lands in (strike, under_line), BOTH legs win.

    A strike exactly matching the sportsbook's own line needs no special
    exclusion here -- its gap is 0, which already fails the > 0.4 threshold
    below, so it's naturally left to dk_fd_totals_vs_kalshi (true arb)
    instead of double-counted here as a middle too.

    Returns only the NEAREST qualifying strike per (game, book, shape), not
    every strike on the ladder -- this is NOT an arbitrary "pick one" (the
    way exact-match-only would be for arb): moving to a strike farther from
    the sportsbook's own line makes that Kalshi leg cheaper/safer (higher
    implied probability), which strictly WORSENS worst_case_per_unit as the
    gap widens (confirmed monotonic against live MLB data). The nearest
    qualifying strike is provably the best (or tied-best) one for a given
    book+line -- a wider, uglier-looking gap from the same ladder is never
    actually a better opportunity, it's a worse one wearing a bigger number.

    Same sanity bound find_middles uses (a line with abs() > 60 is treated
    as a scraper artifact, skipped) and the same gap > 0.4 threshold to
    ignore trivial half-point noise. Hit shape matches find_middles' own
    (market/team_a/team_b/book_a/line_a/price_a/book_b/line_b/price_b/gap)
    so scan_market_wide's caller can push these into middle_hits with zero
    extra per-item code on the frontend -- the mobile page's book coloring/
    naming (bookColorClass/properBookName) and middleSideText's Over/Under
    labeling already handle 'kalshi' as a book name correctly (it's already
    used that way in hedge legs and the Kalshi Arb section)."""
    hits = []
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

        for book in SPORTSBOOKS:
            over_line, over_price = book_lines(game['a'], book)
            under_line, under_price = book_lines(game['b'], book)

            # Shape 1: sportsbook Over(over_line) + Kalshi 'No' on a higher
            # strike (= Under(strike)) -- window (over_line, strike]. Only
            # the NEAREST qualifying strike -- see this function's own
            # docstring for why that's provably the best one here, not an
            # arbitrary pick.
            if over_line is not None and over_price is not None and abs(over_line) <= 60:
                candidates = [s for s in kev['strikes']
                              if s['floor_strike'] - over_line > 0.4 and s.get('no_ask')]
                if candidates:
                    strike = min(candidates, key=lambda s: s['floor_strike'])
                    dec_b = kalshi_multiplier(strike['no_ask'])
                    if dec_b:
                        dec_a = decimal_odds(over_price)
                        gap = strike['floor_strike'] - over_line
                        hit = {
                            'market': 'total', 'team_a': game['team_a'], 'team_b': game['team_b'],
                            'book_a': book, 'line_a': over_line, 'price_a': over_price,
                            'book_b': 'kalshi', 'line_b': strike['floor_strike'],
                            'price_b': f"{dec_b:.2f}x",
                            'gap': round(gap, 1),
                        }
                        _middle_stakes_from_decimals(hit, dec_a, dec_b)
                        hits.append(hit)

            # Shape 2: Kalshi 'Yes' on a lower strike (= Over(strike)) +
            # sportsbook Under(under_line) -- window (strike, under_line).
            # Only the NEAREST qualifying strike, same reasoning as shape 1.
            if under_line is not None and under_price is not None and abs(under_line) <= 60:
                candidates = [s for s in kev['strikes']
                              if under_line - s['floor_strike'] > 0.4 and s.get('yes_ask')]
                if candidates:
                    strike = max(candidates, key=lambda s: s['floor_strike'])
                    dec_a = kalshi_multiplier(strike['yes_ask'])
                    if dec_a:
                        dec_b = decimal_odds(under_price)
                        gap = under_line - strike['floor_strike']
                        hit = {
                            'market': 'total', 'team_a': game['team_a'], 'team_b': game['team_b'],
                            'book_a': 'kalshi', 'line_a': strike['floor_strike'],
                            'price_a': f"{dec_a:.2f}x",
                            'book_b': book, 'line_b': under_line, 'price_b': under_price,
                            'gap': round(gap, 1),
                        }
                        _middle_stakes_from_decimals(hit, dec_a, dec_b)
                        hits.append(hit)
    return hits


def run_scans(boosts_frac, cash_available, allowed_books=None, restrict_sports=None, before_date=None):
    """boosts_frac: boost dicts with boost_pct as a FRACTION (0.5, not 50).
    Ported from boost_trigger_poller.py's run_scans() -- identical logic.

    allowed_books/restrict_sports: the mobile page's own book/sport filter
    chips (None = no restriction), threaded straight through to every scan
    call so a filtered-out hedge book or sport genuinely gets RE-MAXIMIZED
    around at scan time -- see build_scan_result()'s docstring for the full
    picture (this alone doesn't drop a boost whose own book/sport got
    filtered out entirely; that happens one level up, before this is
    ever called).

    before_date: optional 'YYYY-MM-DD' global date-cutoff from the mobile
    page's own date filter (None/'' = no restriction) -- see
    boosted_scan's docstring for the exact semantics; threaded through
    to both boosted_scan and dual_boost_combo_scan below.

    Each per-boost try/except used to print the crash to the GitHub Actions
    log and otherwise fall through as if raw were just empty -- pick_top_plays
    then reported the exact same 'no qualifying play found right now' message
    for a genuine empty market AND for a scan that outright crashed, with no
    way to tell them apart on the mobile page itself (audit finding, 9/29 --
    scan_market_wide already got this right via its own market_errors list,
    this just brings run_scans/dual_boost_combo_scan up to the same standard).
    The 3rd tuple element (scan_error) carries the crash message through to
    pick_top_plays so it can surface as a distinct, unmissable error instead."""
    plays_by_boost = []
    for b in boosts_frac:
        scan_error = None
        try:
            raw = boosted_scan(book=b["book"], boost_pct=b["boost_pct"],
                                max_wager=b["max_wager"], min_odds=b["min_odds"],
                                sport=b["sport"], game_filter=b.get("game", ""),
                                expires=b.get("expires", ""), cash_available=cash_available,
                                allowed_books=allowed_books, restrict_sports=restrict_sports,
                                before_date=before_date)
        except Exception as e:
            scan_error = f"{type(e).__name__}: {e}"
            print(f"  boosted_scan failed for {b}: {scan_error}")
            raw = []
        for p in raw:
            p["boost_pct"] = round(b["boost_pct"] * 100)
        plays_by_boost.append((b, raw, scan_error))

    combo_plays, combo_errors = [], []
    if len(boosts_frac) >= 2:
        try:
            combo_plays = dual_boost_combo_scan(boosts_frac, cash_available=cash_available, restrict_sports=restrict_sports, before_date=before_date)
        except Exception as e:
            msg = f"Combo play scan failed: {type(e).__name__}: {e}"
            print(f"  dual_boost_combo_scan failed: {type(e).__name__}: {e}")
            combo_errors.append(msg)
    return plays_by_boost, combo_plays, combo_errors


def run_freebet_scans(freebets, cash_available, allowed_books=None, restrict_sports=None, before_date=None):
    """Free-bet analog of run_scans(). No combo scan here -- a 'dual free-
    bet combo' (two free bets on opposite sides of the same market, which
    would need NO external hedge at all and would be automatically risk-
    free) is a clean possible extension but wasn't asked for; this only
    gives free bets the same solo-hedge-scan treatment boosts get.

    allowed_books/restrict_sports/before_date: see run_scans()'s docstring
    -- identical meaning here. Same scan_error 3rd-tuple-element treatment
    as run_scans (audit finding, 9/29) -- see its docstring for why."""
    plays_by_freebet = []
    for fb in freebets:
        scan_error = None
        try:
            raw = free_bet_scan(book=fb["book"], free_bet_amount=fb["free_bet_amount"],
                                 min_odds=fb["min_odds"], sport=fb["sport"],
                                 game_filter=fb.get("game", ""), expires=fb.get("expires", ""),
                                 cash_available=cash_available, splitable=fb.get("splitable", True),
                                 allowed_books=allowed_books, restrict_sports=restrict_sports,
                                 before_date=before_date)
        except Exception as e:
            scan_error = f"{type(e).__name__}: {e}"
            print(f"  free_bet_scan failed for {fb}: {scan_error}")
            raw = []
        plays_by_freebet.append((fb, raw, scan_error))
    return plays_by_freebet


def pick_top_plays(plays_by_boost, claimed=None):
    """No filter params here -- the mobile page's book/sport filter chips
    are already applied upstream, inside run_scans()/boosted_scan() itself
    (as of the filter-recalculation fix), so `raw` here only ever contains
    candidates that already satisfy them; this just claims/dedupes among
    whatever survived. Ported from boost_trigger_poller.py.

    claimed: pass in a shared set so boosts and free bets (see
    pick_top_freebets) never both get recommended on the identical wager --
    build_scan_result() calls this one first, so boosts claim first
    (arbitrary ordering; flip it if Reid wants free bets prioritized)."""
    claimed = set() if claimed is None else claimed
    top_plays, errors = [], []
    for b, raw, scan_error in plays_by_boost:
        if scan_error:
            errors.append(f"{boost_desc(b)}: SCAN ERROR ({scan_error}) -- this is a bug, "
                          f"not an empty market. Check the GitHub Actions run log.")
            continue
        if not raw:
            if _is_expired(b.get("expires")):
                errors.append(f"{boost_desc(b)}: this boost's expiration date has already "
                              f"passed -- remove it or update the date.")
            else:
                errors.append(f"{boost_desc(b)}: no qualifying play found right now.")
            continue
        if b["book"] == "fanatics":
            distinct = raw[0]
        else:
            distinct = next((p for p in raw if wager_key(p) not in claimed), None)
        if distinct:
            if b["book"] != "fanatics":
                claimed.add(wager_key(distinct))
            distinct["boost_id"] = b.get("_id")
            top_plays.append(distinct)
        else:
            errors.append(f"{boost_desc(b)}: every play {b['book']} offers right now is already "
                          f"claimed by another loaded {b['book']} boost.")
    return top_plays, errors


def pick_top_freebets(plays_by_freebet, claimed=None):
    """Free-bet analog of pick_top_plays -- shares the SAME claimed set (pass
    the same set object build_scan_result passed to pick_top_plays) so a
    boost and a free bet never both get recommended on the identical wager.
    Fanatics gets the same dedup exemption boosts have (multiple boosted/
    free bets allowed on the same game there) by extension -- not
    independently confirmed for free bets specifically, flag if wrong."""
    claimed = set() if claimed is None else claimed
    top_freebets, errors = [], []
    for fb, raw, scan_error in plays_by_freebet:
        if scan_error:
            errors.append(f"{freebet_desc(fb)}: SCAN ERROR ({scan_error}) -- this is a bug, "
                          f"not an empty market. Check the GitHub Actions run log.")
            continue
        if not raw:
            if _is_expired(fb.get("expires")):
                errors.append(f"{freebet_desc(fb)}: this free bet's expiration date has "
                              f"already passed -- remove it or update the date.")
            else:
                errors.append(f"{freebet_desc(fb)}: no qualifying play found right now.")
            continue
        if fb["book"] == "fanatics":
            distinct = raw[0]
        else:
            distinct = next((p for p in raw if wager_key(p) not in claimed), None)
        if distinct:
            if fb["book"] != "fanatics":
                claimed.add(wager_key(distinct))
            distinct["free_bet_id"] = fb.get("_id")
            top_freebets.append(distinct)
        else:
            errors.append(f"{freebet_desc(fb)}: every play {fb['book']} offers right now is already "
                          f"claimed by another loaded boost or free bet.")
    return top_freebets, errors


def scan_market_wide(allowed_books=None, restrict_sports=None, before_date=None):
    """Cross-book true-arbitrage + middle detection across ALL traditional
    sportsbooks VegasInsider lists (odds_scraper.BOOKS), independent of any
    loaded boost -- these are opportunities on their own numbers, not tied to
    a promo. Runs every 'Scan Now' click across every sport in SPORT_PAGES; a single
    sport's fetch failure is noted but doesn't take down the others or the
    boost scan alongside it.

    ALSO runs the 3 Kalshi-specific arb checks (arb_engine.dk_fd_vs_kalshi,
    dk_fd_totals_vs_kalshi, kalshi_internal_arb) alongside true-arb/middles --
    these were fully built but never actually wired into any scan pipeline
    until now (confirmed 9/28: Kalshi's own market data fetch was always
    healthy, it just only ever got used as a hedge CANDIDATE inside a
    loaded boost/free-bet's own scan, never as its own independent
    opportunity source). Returned as a 4th list, kalshi_arb -- a different
    shape (a 'legs' list, since these aren't always exactly a book_a/book_b
    pair the same way true-arb/middles are) so it gets its own section on
    the mobile page rather than being force-fit into the true_arb shape.

    ALSO runs dk_fd_totals_middle_vs_kalshi (Reid's ask, 10/5: "why are you
    not looking at Kalshi for middles?") -- a genuine middle (not arb)
    between a sportsbook's total line and any non-matching Kalshi strike.
    Unlike the 3 arb checks above, these feed straight into the SAME
    middle_hits list find_middles' own sportsbook-only hits do, since the
    hit shape (and the mobile page's rendering of it) is identical either
    way -- it's just another middle, one leg happens to be Kalshi.

    allowed_books/restrict_sports: the mobile page's own book/sport filter
    chips (None = no restriction) -- applied uniformly across true-arb,
    middles, AND all 3 Kalshi checks, same as boosted_scan/free_bet_scan.
    A sport not in restrict_sports is skipped before ever fetching it
    (saves a fetch, not just a display filter); Kalshi's own 2 fetches
    (games/totals) are skipped per-sport too if 'kalshi' isn't in
    allowed_books, since every opportunity here needs at least one Kalshi
    leg.
    before_date: optional 'YYYY-MM-DD' global date-cutoff (None/'' = no
    restriction) -- see boosted_scan's docstring for the exact semantics;
    applied to the single shared `games` list below, so it covers true-arb
    and middles together the same way the start_time/final checks already
    do."""
    arb_hits, middle_hits, kalshi_arb_hits, errors = [], [], [], []
    effective_books = allowed_books if allowed_books is not None else MY_BOOKS
    sports_to_scan = [sp for sp in SPORT_PAGES if restrict_sports is None or sp in restrict_sports]
    for sport in sports_to_scan:
        url, sections = SPORT_PAGES[sport]
        try:
            html = fetch_html(url)
            # Drop finished games -- VegasInsider keeps showing a completed
            # game's last-known closing odds in the same table, which can
            # look like a live arb/middle on numbers that are dead (real bug
            # hit 9/27: a finished Cubs/Red Sox game kept surfacing here even
            # after its doubleheader nightcap, game_id 14, had moved on).
            games = [g for g in parse_sport_page(html, sections) if not g.get('final')]
            # Same start_time gate boosted_scan() already applies -- VI keeps a
            # game's last pre-game odds visible in the same table even after
            # kickoff, and 'final' alone only catches it once the game is OVER,
            # not once it's merely started/live. Both true-arb and middles come
            # from this same `games` list, so one filter here covers both.
            games = [
                g for g in games
                if not _game_has_started(g.get('start_time'))
                and not _game_after_expiration(before_date, g.get('start_time'))
            ]
        except Exception as e:
            errors.append(f"{sport} market-wide odds fetch failed: {type(e).__name__}: {e}")
            continue
        for hit in find_true_arb(games):
            # Same MY_BOOKS restriction middles already had -- Reid only has
            # accounts at these 4 (now filter-narrowed, if a filter's active),
            # a true-arb hit needing e.g. Hardrock or Bet365 isn't a bet he
            # can actually place.
            if hit["book_a"] not in effective_books or hit["book_b"] not in effective_books:
                continue
            hit["sport"] = sport
            hit["book_a"] = hit["book_a"].capitalize()
            hit["book_b"] = hit["book_b"].capitalize()
            arb_hits.append(hit)
        for hit in find_middles(games):
            if hit["book_a"] not in effective_books or hit["book_b"] not in effective_books:
                continue
            hit["sport"] = sport
            add_middle_stakes(hit)
            hit["book_a"] = hit["book_a"].capitalize()
            hit["book_b"] = hit["book_b"].capitalize()
            middle_hits.append(hit)

        if "kalshi" not in effective_books:
            continue  # every kalshi_arb opportunity needs a Kalshi leg
        try:
            kalshi_games = kalshi_client.fetch_games(sport)
        except Exception:
            kalshi_games = []
        try:
            kalshi_totals = kalshi_client.fetch_totals(sport)
        except Exception:
            kalshi_totals = []
        kalshi_games = [g for g in kalshi_games if not _game_has_started(g.get('close_time'))]
        kalshi_totals = [g for g in kalshi_totals if not _game_has_started(g.get('close_time'))]
        # Same double-check boosted_scan/free_bet_scan use -- VI's start_time
        # (already applied to `games` above) can be missing/stale on its
        # own, Kalshi's close_time for the same team pair catches it too.
        close_lookup = _build_close_time_lookup(kalshi_games, kalshi_totals)
        kalshi_games_ok = [g for g in games if not _game_has_started(
            close_lookup.get(frozenset((g['team_a'], g['team_b']))))]
        ml_games = [g for g in kalshi_games_ok if g['market'] == 'moneyline']
        total_games = [g for g in kalshi_games_ok if g['market'] == 'total']

        kalshi_opps = []
        if kalshi_games and ml_games:
            kalshi_opps += dk_fd_vs_kalshi(sport, ml_games, kalshi_games)
        if kalshi_totals and total_games:
            kalshi_opps += dk_fd_totals_vs_kalshi(sport, total_games, kalshi_totals)
        kalshi_opps += kalshi_internal_arb(sport, kalshi_games, kalshi_totals)
        for opp in kalshi_opps:
            if any(leg["book"].lower() not in effective_books for leg in opp["legs"]):
                continue
            kalshi_arb_hits.append(opp)

        # Genuine MIDDLES (not just arb) with one leg on Kalshi (Reid's ask,
        # 10/5) -- same effective_books/sport-stamp/capitalize treatment
        # find_middles' own hits get above; sourced separately since this
        # needs Kalshi's totals ladder, which isn't fetched until here.
        if kalshi_totals and total_games:
            for hit in dk_fd_totals_middle_vs_kalshi(sport, total_games, kalshi_totals):
                if hit["book_a"] not in effective_books or hit["book_b"] not in effective_books:
                    continue
                hit["sport"] = sport
                hit["book_a"] = hit["book_a"].capitalize()
                hit["book_b"] = hit["book_b"].capitalize()
                middle_hits.append(hit)

    arb_hits.sort(key=lambda h: -h["edge_pct"])
    middle_hits.sort(key=lambda h: -h["gap"])
    kalshi_arb_hits.sort(key=lambda h: -h["edge_pct"])
    return arb_hits[:15], middle_hits[:15], kalshi_arb_hits[:15], errors


def fetch_cash():
    """Returns (cash_dict_or_None, pl_dict_or_None, error_str_or_None).
    Never raises -- a sheet fetch failure must not take down the whole
    calculate run, it just means cash capping/display sits out this round."""
    try:
        import bankroll_sheet
        data = bankroll_sheet.fetch_bankroll()
        return data["cash"], data["pl"], None
    except Exception as e:
        return None, None, f"Cash fetch failed: {type(e).__name__}: {e}"


def _filter_loaded_items(items, filter_books, filter_sports, desc_fn):
    """Drops a loaded boost/free-bet whose OWN book or sport setting has NO
    overlap at all with the mobile page's filter chips, with a clear skip
    reason recorded (distinct from the existing 'no qualifying play found'
    message, which means something scanned and came up empty, not that it
    got excluded outright). An item whose sport is 'ALL' still survives here
    even with a sport filter active -- its per-sport scan gets narrowed via
    restrict_sports instead (see run_scans/run_freebet_scans), not dropped,
    since 'ALL' by definition already includes whatever's selected."""
    kept, skip_errors = [], []
    for item in items:
        if filter_books and item["book"].lower() not in filter_books:
            skip_errors.append(f"{desc_fn(item)}: skipped -- {item['book']} isn't in the selected book filter.")
            continue
        if filter_sports and item["sport"] != "ALL" and item["sport"] not in filter_sports:
            skip_errors.append(f"{desc_fn(item)}: skipped -- {item['sport']} isn't in the selected sport filter.")
            continue
        kept.append(item)
    return kept, skip_errors


def _apply_pending(cash, pending):
    """Subtracts money already committed to open Play Tracker entries (the
    mobile page's own bookkeeping, sent fresh every scan as PENDING_JSON --
    never stored server-side, never written back to the bankroll sheet)
    from the freshly-fetched sheet balance, BEFORE it's used to cap stake
    sizing below. Without this, a scan would keep recommending stakes
    against money that's already sitting in a placed-but-not-yet-settled
    bet, since the sheet itself only reflects Reid's own manual updates and
    lags real placements. Never mutates `cash` itself -- that stays the
    true, unadjusted sheet value for display; only the derived
    cash_available (used for capping) is adjusted. A book with no sheet
    data (None) stays None, never coerced to 0 by a pending amount."""
    if not cash:
        return cash
    return {book: (max(0.0, amt - (pending.get(book) or 0)) if amt is not None else None)
            for book, amt in cash.items()}


def build_scan_result():
    try:
        boosts_whole = json.loads(os.environ.get("BOOSTS_JSON", "[]"))
        freebets_whole = json.loads(os.environ.get("FREEBETS_JSON", "[]"))
        filter_books = {b.lower() for b in json.loads(os.environ.get("FILTER_BOOKS_JSON", "[]"))}
        filter_sports = set(json.loads(os.environ.get("FILTER_SPORTS_JSON", "[]")))
        before_date = os.environ.get("FILTER_BEFORE_DATE", "").strip()
    except json.JSONDecodeError as e:
        return {"run_id": RUN_ID, "mode": "scan", "error": f"Invalid BOOSTS_JSON/FREEBETS_JSON: {e}",
                "top_plays": [], "combo_plays": [], "top_freebets": [], "errors": [], "cash": None, "pl": None,
                "true_arb": [], "middles": [], "kalshi_arb": [], "market_errors": []}

    # No early-return when boosts_whole/freebets_whole are empty -- Scan Now
    # works with zero of either loaded, it just skips straight to the
    # market-wide true-arb/middles scan below (run_scans/run_freebet_scans/
    # pick_top_plays/pick_top_freebets are all no-ops on an empty list).
    #
    # Cash is fetched EVERY scan regardless of USE_CASH -- Reid's ask:
    # "every time I press scan now, also refresh cash available." USE_CASH
    # still gates whether that fresh cash actually CAPS stake sizing below
    # (cash_available stays None when the toggle is off, same as before);
    # it just no longer also controls whether the cash DISPLAY refreshes,
    # which is a separate concern from whether stakes get capped by it.
    cash, pl, cash_error = fetch_cash()
    try:
        pending = json.loads(os.environ.get("PENDING_JSON", "{}"))
    except json.JSONDecodeError:
        pending = {}
    cash_available = _apply_pending(cash, pending) if USE_CASH else None

    # Filter chips RE-MAXIMIZE within the filter, they don't just hide an
    # already-picked play after the fact (real bug reported 9/28: an NFL
    # filter left a loaded free bet showing nothing at all, because its one
    # already-chosen play happened to be an NCAAF game -- filtering was
    # display-only, so there was never a chance to pick a DIFFERENT, NFL-
    # compliant play instead). allowed_books/restrict_sports get threaded
    # into every scan call below; a loaded item whose OWN book/sport has no
    # overlap with the filter at all gets dropped up front, with a distinct
    # skip message (not "no qualifying play", which would wrongly imply the
    # scan came up empty rather than that it was never attempted).
    allowed_books = filter_books or None
    restrict_sports = filter_sports or None
    boosts_whole_kept, boost_skip_errors = _filter_loaded_items(boosts_whole, filter_books, filter_sports, boost_desc)
    freebets_whole_kept, freebet_skip_errors = _filter_loaded_items(freebets_whole, filter_books, filter_sports, freebet_desc)

    boosts_frac = [dict(b, boost_pct=float(b["boost_pct"]) / 100.0) for b in boosts_whole_kept]
    plays_by_boost, combo_plays_raw, combo_errors = run_scans(boosts_frac, cash_available, allowed_books=allowed_books, restrict_sports=restrict_sports, before_date=before_date)
    # Shared claimed set: boosts claim first (arbitrary ordering), then free
    # bets pick from what's left -- neither ever recommends the identical
    # wager (same book/game/market/side) the other already claimed.
    claimed = set()
    top_plays, errors = pick_top_plays(plays_by_boost, claimed=claimed)
    errors = errors + boost_skip_errors + combo_errors

    plays_by_freebet = run_freebet_scans(freebets_whole_kept, cash_available, allowed_books=allowed_books, restrict_sports=restrict_sports, before_date=before_date)
    top_freebets, freebet_errors = pick_top_freebets(plays_by_freebet, claimed=claimed)
    errors = errors + freebet_errors + freebet_skip_errors

    true_arb, middles, kalshi_arb, market_errors = scan_market_wide(allowed_books=allowed_books, restrict_sports=restrict_sports, before_date=before_date)

    if cash_available and (top_plays or combo_plays_raw or top_freebets):
        pooled = apply_cash_pool(top_plays + combo_plays_raw + top_freebets, cash_available)
        top_plays = sorted([p for p in pooled if not p.get("combo") and not p.get("free_bet")],
                            key=lambda p: p.get("guaranteed_profit", 0), reverse=True)
        combo_plays = sorted([p for p in pooled if p.get("combo")],
                              key=lambda p: p.get("guaranteed_profit", 0), reverse=True)
        top_freebets = sorted([p for p in pooled if p.get("free_bet")],
                               key=lambda p: p.get("guaranteed_profit", 0), reverse=True)
    else:
        combo_plays = combo_plays_raw

    top_plays = _strip_internal(top_plays)
    top_freebets = _strip_internal(top_freebets)

    return {
        "run_id": RUN_ID, "mode": "scan", "generated_at": time.time(),
        "error": None, "cash_error": cash_error,
        "cash": cash, "pl": pl,
        "top_plays": top_plays, "combo_plays": combo_plays, "top_freebets": top_freebets, "errors": errors,
        "true_arb": true_arb, "middles": middles, "kalshi_arb": kalshi_arb, "market_errors": market_errors,
        "n_boosts": len(boosts_whole), "n_freebets": len(freebets_whole),
    }


def build_cash_only_result():
    cash, pl, cash_error = fetch_cash()
    return {
        "run_id": RUN_ID, "mode": "cash_only", "generated_at": time.time(),
        "cash": cash, "pl": pl, "error": cash_error,
    }


def main():
    result = build_cash_only_result() if MODE == "cash_only" else build_scan_result()
    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {RESULT_PATH}")
    print(json.dumps(result, indent=2)[:2000])


if __name__ == "__main__":
    main()
