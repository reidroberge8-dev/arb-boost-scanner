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

from arb_engine import boosted_scan, dual_boost_combo_scan, apply_cash_pool, _game_has_started, free_bet_scan
from odds_scraper import SPORT_PAGES, fetch_html, parse_sport_page, find_true_arb, find_middles

# The only 4 books Reid actually holds accounts at -- a middle he can't bet
# both legs of is just noise, so scan_market_wide() restricts MIDDLES (not
# true-arb, which stays market-wide) to pairs where both books are in here.
MY_BOOKS = {'draftkings', 'fanduel', 'fanatics', 'kalshi'}


def decimal_odds(price):
    """American odds -> total return multiple per $1 staked (e.g. -110 -> 1.909)."""
    return 1 + price / 100.0 if price > 0 else 1 + 100.0 / abs(price)


def add_middle_stakes(hit):
    """Unit sizing for a middle: leg A = 1 unit, leg B sized so the profit is
    IDENTICAL whichever single leg wins alone (there's no double-loss outcome
    in a middle by construction -- the two lines always overlap the full
    range of possible results, so exactly one leg always cashes even when
    the window itself is missed). This is the standard "size a middle"
    formula -- it does NOT guarantee that equalized worst case is >= 0.
    Some middles carry a small guaranteed cost for a big payout if the
    window hits; others are truly free. worst_case_per_unit tells you which
    kind this particular one is, so don't just assume zero loss."""
    dec_a = decimal_odds(hit['price_a'])
    dec_b = decimal_odds(hit['price_b'])
    units_a = 1.0
    units_b = round(dec_a / dec_b, 3)
    total = units_a + units_b
    hit['units_a'] = units_a
    hit['units_b'] = units_b
    hit['worst_case_per_unit'] = round(units_a * dec_a - total, 3)  # == units_b*dec_b - total
    hit['best_case_per_unit'] = round(units_a * dec_a + units_b * dec_b - total, 3)
    return hit

RUN_ID = os.environ["RUN_ID"]
MODE = os.environ.get("MODE", "scan").strip().lower()
USE_CASH = os.environ.get("USE_CASH", "true").strip().lower() == "true"
RESULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "results", f"{RUN_ID}.json")


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


def run_scans(boosts_frac, cash_available):
    """boosts_frac: boost dicts with boost_pct as a FRACTION (0.5, not 50).
    Ported from boost_trigger_poller.py's run_scans() -- identical logic."""
    plays_by_boost = []
    for b in boosts_frac:
        try:
            raw = boosted_scan(book=b["book"], boost_pct=b["boost_pct"],
                                max_wager=b["max_wager"], min_odds=b["min_odds"],
                                sport=b["sport"], game_filter=b.get("game", ""),
                                expires=b.get("expires", ""), cash_available=cash_available)
        except Exception as e:
            print(f"  boosted_scan failed for {b}: {type(e).__name__}: {e}")
            raw = []
        for p in raw:
            p["boost_pct"] = round(b["boost_pct"] * 100)
        plays_by_boost.append((b, raw))

    combo_plays = []
    if len(boosts_frac) >= 2:
        try:
            combo_plays = dual_boost_combo_scan(boosts_frac, cash_available=cash_available)
        except Exception as e:
            print(f"  dual_boost_combo_scan failed: {type(e).__name__}: {e}")
    return plays_by_boost, combo_plays


def run_freebet_scans(freebets, cash_available):
    """Free-bet analog of run_scans(). No combo scan here -- a 'dual free-
    bet combo' (two free bets on opposite sides of the same market, which
    would need NO external hedge at all and would be automatically risk-
    free) is a clean possible extension but wasn't asked for; this only
    gives free bets the same solo-hedge-scan treatment boosts get."""
    plays_by_freebet = []
    for fb in freebets:
        try:
            raw = free_bet_scan(book=fb["book"], free_bet_amount=fb["free_bet_amount"],
                                 min_odds=fb["min_odds"], sport=fb["sport"],
                                 game_filter=fb.get("game", ""), expires=fb.get("expires", ""),
                                 cash_available=cash_available, splitable=fb.get("splitable", True))
        except Exception as e:
            print(f"  free_bet_scan failed for {fb}: {type(e).__name__}: {e}")
            raw = []
        plays_by_freebet.append((fb, raw))
    return plays_by_freebet


def pick_top_plays(plays_by_boost, claimed=None):
    """No sport/book filters here (unlike the WorkSpace version) -- the
    mobile page doesn't expose global filters, each boost's own `sport`
    field already scopes its own scan. Ported from boost_trigger_poller.py.

    claimed: pass in a shared set so boosts and free bets (see
    pick_top_freebets) never both get recommended on the identical wager --
    build_scan_result() calls this one first, so boosts claim first
    (arbitrary ordering; flip it if Reid wants free bets prioritized)."""
    claimed = set() if claimed is None else claimed
    top_plays, errors = [], []
    for b, raw in plays_by_boost:
        if not raw:
            errors.append(f"{boost_desc(b)}: no qualifying play found right now.")
            continue
        if b["book"] == "fanatics":
            distinct = raw[0]
        else:
            distinct = next((p for p in raw if wager_key(p) not in claimed), None)
        if distinct:
            if b["book"] != "fanatics":
                claimed.add(wager_key(distinct))
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
    for fb, raw in plays_by_freebet:
        if not raw:
            errors.append(f"{freebet_desc(fb)}: no qualifying play found right now.")
            continue
        if fb["book"] == "fanatics":
            distinct = raw[0]
        else:
            distinct = next((p for p in raw if wager_key(p) not in claimed), None)
        if distinct:
            if fb["book"] != "fanatics":
                claimed.add(wager_key(distinct))
            top_freebets.append(distinct)
        else:
            errors.append(f"{freebet_desc(fb)}: every play {fb['book']} offers right now is already "
                          f"claimed by another loaded boost or free bet.")
    return top_freebets, errors


def scan_market_wide():
    """Cross-book true-arbitrage + middle detection across ALL traditional
    sportsbooks VegasInsider lists (odds_scraper.BOOKS), independent of any
    loaded boost -- these are opportunities on their own numbers, not tied to
    a promo. Runs every 'Scan Now' click across all 3 sports; a single
    sport's fetch failure is noted but doesn't take down the others or the
    boost scan alongside it."""
    arb_hits, middle_hits, errors = [], [], []
    for sport, (url, sections) in SPORT_PAGES.items():
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
            games = [g for g in games if not _game_has_started(g.get('start_time'))]
        except Exception as e:
            errors.append(f"{sport} market-wide odds fetch failed: {type(e).__name__}: {e}")
            continue
        for hit in find_true_arb(games):
            # Same MY_BOOKS restriction middles already had -- Reid only has
            # accounts at these 4, a true-arb hit needing e.g. Hardrock or
            # Bet365 isn't a bet he can actually place.
            if hit["book_a"] not in MY_BOOKS or hit["book_b"] not in MY_BOOKS:
                continue
            hit["sport"] = sport
            hit["book_a"] = hit["book_a"].capitalize()
            hit["book_b"] = hit["book_b"].capitalize()
            arb_hits.append(hit)
        for hit in find_middles(games):
            if hit["book_a"] not in MY_BOOKS or hit["book_b"] not in MY_BOOKS:
                continue
            hit["sport"] = sport
            add_middle_stakes(hit)
            hit["book_a"] = hit["book_a"].capitalize()
            hit["book_b"] = hit["book_b"].capitalize()
            middle_hits.append(hit)
    arb_hits.sort(key=lambda h: -h["edge_pct"])
    middle_hits.sort(key=lambda h: -h["gap"])
    return arb_hits[:15], middle_hits[:15], errors


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


def build_scan_result():
    try:
        boosts_whole = json.loads(os.environ.get("BOOSTS_JSON", "[]"))
        freebets_whole = json.loads(os.environ.get("FREEBETS_JSON", "[]"))
    except json.JSONDecodeError as e:
        return {"run_id": RUN_ID, "mode": "scan", "error": f"Invalid BOOSTS_JSON/FREEBETS_JSON: {e}",
                "top_plays": [], "combo_plays": [], "top_freebets": [], "errors": [], "cash": None, "pl": None,
                "true_arb": [], "middles": [], "market_errors": []}

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
    cash_available = cash if USE_CASH else None

    boosts_frac = [dict(b, boost_pct=float(b["boost_pct"]) / 100.0) for b in boosts_whole]
    plays_by_boost, combo_plays_raw = run_scans(boosts_frac, cash_available)
    # Shared claimed set: boosts claim first (arbitrary ordering), then free
    # bets pick from what's left -- neither ever recommends the identical
    # wager (same book/game/market/side) the other already claimed.
    claimed = set()
    top_plays, errors = pick_top_plays(plays_by_boost, claimed=claimed)

    plays_by_freebet = run_freebet_scans(freebets_whole, cash_available)
    top_freebets, freebet_errors = pick_top_freebets(plays_by_freebet, claimed=claimed)
    errors = errors + freebet_errors

    true_arb, middles, market_errors = scan_market_wide()

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

    return {
        "run_id": RUN_ID, "mode": "scan", "generated_at": time.time(),
        "error": None, "cash_error": cash_error,
        "cash": cash, "pl": pl,
        "top_plays": top_plays, "combo_plays": combo_plays, "top_freebets": top_freebets, "errors": errors,
        "true_arb": true_arb, "middles": middles, "market_errors": market_errors,
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
