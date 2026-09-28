"""
One-shot boost calculation, run by the GitHub Actions workflow
(.github/workflows/boost-scan.yml) on behalf of the mobile page in docs/ --
NOT part of the always-on app.py/WorkSpace pipeline, and shares no state
with it (no boosts_store.json, no cash_store.json -- those live only on the
WorkSpace/sandbox and are gitignored). This is a stateless calculator: one
boost in, one result out, correlated by a client-generated run_id.

Reads its input from env vars (set by the workflow from workflow_dispatch
inputs), calls the SAME boosted_scan() the live dashboard uses, and writes
the result to docs/results/<run_id>.json -- which GitHub Pages then serves
publicly at https://<user>.github.io/<repo>/results/<run_id>.json for the
page to poll. Also best-effort emails a digest (non-fatal if email isn't
configured -- e.g. secrets not set yet).
"""
import json
import os
import time
import traceback

from arb_engine import boosted_scan

RUN_ID = os.environ["RUN_ID"]  # required -- no sane fallback filename
BOOK = os.environ.get("BOOST_BOOK", "").strip().lower()
SPORT = os.environ.get("BOOST_SPORT", "ALL").strip() or "ALL"
GAME = os.environ.get("BOOST_GAME", "").strip()
EXPIRES = os.environ.get("BOOST_EXPIRES", "").strip()

RESULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "results", f"{RUN_ID}.json")
DISPLAY_LIMIT = 5  # top N plays shown on the mobile page


def _to_float(name, default=0.0):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _to_int(name, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def build_result():
    boost_pct_whole = _to_float("BOOST_PCT", 0)  # e.g. 50 meaning 50%
    max_wager = _to_float("BOOST_MAX_WAGER", 0)
    min_odds = _to_int("BOOST_MIN_ODDS", -100000)

    request_echo = {
        "book": BOOK, "sport": SPORT, "boost_pct": boost_pct_whole,
        "max_wager": max_wager, "min_odds": min_odds, "game": GAME, "expires": EXPIRES,
    }

    if BOOK not in ("draftkings", "fanduel", "fanatics"):
        return {"run_id": RUN_ID, "request": request_echo, "error": f"Invalid book: {BOOK!r}", "plays": []}
    if boost_pct_whole <= 0 or max_wager <= 0:
        return {"run_id": RUN_ID, "request": request_echo,
                "error": "Boost % and Max Wager must both be greater than 0.", "plays": []}

    try:
        plays = boosted_scan(
            book=BOOK, boost_pct=boost_pct_whole / 100.0, max_wager=max_wager,
            min_odds=min_odds, sport=SPORT, game_filter=GAME, expires=EXPIRES,
            limit=DISPLAY_LIMIT,
        )
    except Exception as e:
        return {"run_id": RUN_ID, "request": request_echo,
                "error": f"Scan failed: {type(e).__name__}: {e}", "plays": [],
                "traceback": traceback.format_exc()}

    return {
        "run_id": RUN_ID,
        "generated_at": time.time(),
        "request": request_echo,
        "error": None,
        "plays": plays,
    }


def maybe_send_email(result):
    """Best-effort -- a missing/misconfigured secret must never fail the
    whole run (the mobile page result is what actually matters)."""
    try:
        import email_sender
    except Exception as e:
        print(f"[email] skipped -- import failed: {e}")
        return
    try:
        req = result["request"]
        if result.get("error"):
            subject = f"[Boost Scan] Error -- {req.get('book')} {req.get('boost_pct')}%"
            body = f"<p>Request: {req}</p><p style='color:#dc2626'>{result['error']}</p>"
        elif result["plays"]:
            top = result["plays"][0]
            subject = (f"[Boost Scan] {req.get('book')} {req.get('boost_pct')}% -- "
                       f"top play {top['guaranteed_profit']:.2f} guaranteed profit")
            rows = "".join(
                f"<li>{p['boosted_leg']['book']} {p['boosted_leg']['side']} @ {p['boosted_leg']['price']} "
                f"(stake ${p['boosted_leg']['stake']:.2f}) vs hedge {p['hedge_leg']['book']} "
                f"{p['hedge_leg']['side']} @ {p['hedge_leg']['price']} (stake ${p['hedge_leg']['stake']:.2f}) "
                f"&mdash; guaranteed profit ${p['guaranteed_profit']:.2f} ({p['edge_pct']}%)</li>"
                for p in result["plays"]
            )
            body = f"<p>Request: {req}</p><ul>{rows}</ul>"
        else:
            subject = f"[Boost Scan] No qualifying play -- {req.get('book')} {req.get('boost_pct')}%"
            body = f"<p>Request: {req}</p><p>No qualifying play found right now.</p>"
        email_sender.send_alert_email(subject, body)
    except Exception as e:
        print(f"[email] skipped -- send failed: {type(e).__name__}: {e}")


def main():
    result = build_result()
    os.makedirs(os.path.dirname(RESULT_PATH), exist_ok=True)
    with open(RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {RESULT_PATH}")
    print(json.dumps(result, indent=2)[:2000])
    maybe_send_email(result)


if __name__ == "__main__":
    main()
