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

from arb_engine import boosted_scan, dual_boost_combo_scan, apply_cash_pool, _game_has_started
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


def boost_play_legs(p):
    return [p["leg_a"]["book"], p["leg_b"]["book"]] if p.get("combo") \
        else [p["boosted_leg"]["book"], p["hedge_leg"]["book"]]


def wager_key(p):
    return (p["boosted_leg"]["book"], p["game"], p["market"], p["boosted_leg"]["side"])


def boost_desc(b):
    game = f" / {b['game']}" if b.get("game") else ""
    exp = f", expires {b['expires']}" if b.get("expires") else ""
    return f"{b['book'].capitalize()} {b['sport']}{game} ({b['boost_pct']*100:.0f}% boost, ${b['max_wager']:.0f} max{exp})"


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


def pick_top_plays(plays_by_boost):
    """No sport/book filters here (unlike the WorkSpace version) -- the
    mobile page doesn't expose global filters, each boost's own `sport`
    field already scopes its own scan. Ported from boost_trigger_poller.py."""
    claimed = set()
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
    except json.JSONDecodeError as e:
        return {"run_id": RUN_ID, "mode": "scan", "error": f"Invalid BOOSTS_JSON: {e}",
                "top_plays": [], "combo_plays": [], "errors": [], "cash": None, "pl": None,
                "true_arb": [], "middles": [], "market_errors": []}

    # No early-return when boosts_whole is empty -- Scan Now works with zero
    # boosts loaded, it just skips straight to the market-wide true-arb/
    # middles scan below (run_scans/pick_top_plays are no-ops on an empty list).
    cash, pl, cash_error = fetch_cash() if USE_CASH else (None, None, None)
    cash_available = cash if USE_CASH else None

    boosts_frac = [dict(b, boost_pct=float(b["boost_pct"]) / 100.0) for b in boosts_whole]
    plays_by_boost, combo_plays_raw = run_scans(boosts_frac, cash_available)
    top_plays, errors = pick_top_plays(plays_by_boost)
    true_arb, middles, market_errors = scan_market_wide()

    if cash_available and (top_plays or combo_plays_raw):
        pooled = apply_cash_pool(top_plays + combo_plays_raw, cash_available)
        top_plays = sorted([p for p in pooled if not p.get("combo")],
                            key=lambda p: p.get("guaranteed_profit", 0), reverse=True)
        combo_plays = sorted([p for p in pooled if p.get("combo")],
                              key=lambda p: p.get("guaranteed_profit", 0), reverse=True)
    else:
        combo_plays = combo_plays_raw

    return {
        "run_id": RUN_ID, "mode": "scan", "generated_at": time.time(),
        "error": None, "cash_error": cash_error,
        "cash": cash, "pl": pl,
        "top_plays": top_plays, "combo_plays": combo_plays, "errors": errors,
        "true_arb": true_arb, "middles": middles, "market_errors": market_errors,
        "n_boosts": len(boosts_whole),
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
