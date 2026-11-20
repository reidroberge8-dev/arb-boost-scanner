# CLAUDE.md — Arb Boost Scanner

This file is auto-read by Claude Code when this project is opened. It exists so a new
device/assistant can pick up maintenance of this project with no prior context.

## What this is

A personal sports-betting arbitrage / boost-tracking tool for Reid Roberge. It's a static
single-page frontend (`docs/index.html`) backed by a Python scan engine that runs on
GitHub Actions (not a server Reid hosts himself).

- **Repo**: `reidroberge8-dev/arb-boost-scanner` on GitHub (public).
- **Live site**: https://reidroberge8-dev.github.io/arb-boost-scanner/
- Reid calls it "Arby Sniper" informally. It's his only actively-used betting tool
  (a related project, `arb-tracker`, is frozen/retired — ignore it).

**Important**: the repo and live site don't move when you set up a new device. Only the
*development* work (editing code, pushing commits) shifts here. Reid can already use the
live site from any browser today — that's a separate, simpler thing, see "Live-site auth"
below.

## Architecture

| File | Purpose |
|---|---|
| `docs/index.html` | The entire frontend: UI, state, localStorage, dispatches the GitHub Actions workflow, polls for results. One big file, no build step. |
| `run_boost_scan.py` | Entry point run by the GitHub Actions job. Reads inputs from env vars (including the balances the page sends as `cash_json`), calls into `arb_engine`, writes the result JSON. |
| `arb_engine.py` | The actual scan logic: boosted bets, free bets, true arbs, middles, Kalshi arbs/ladders, cross-book hedge-blending, cash-pool capping. Biggest file (~1700 lines), heavily commented on *why*. |
| `odds_scraper.py` | Scrapes/loads sportsbook odds data. |
| `kalshi_client.py` | Kalshi market data client. |
| `team_map.py` | Team name/abbreviation normalization. |
| `.github/workflows/boost-scan.yml` | The Actions workflow definition — see request flow below. |
| `docs/results/*.json` | Accumulated scan result files (one per run). See "Known quirk" below — this grows forever, nothing prunes it. |
| `requirements.txt` | Just `beautifulsoup4==4.12.3`. |
| `email_config.py` / `email_sender.py` references | Dead code — not used by anything. `email_config.py` doesn't even exist in the repo (gitignored). Don't bother wiring these up. |

### Request flow (how a scan actually runs)

1. User clicks a scan button in `docs/index.html`. The page generates a `run_id`
   (timestamp + random suffix) and POSTs a `workflow_dispatch` to the GitHub Actions API
   with inputs: `run_id`, `mode` (scan/cash_only/games_only), `boosts_json`,
   `freebets_json`, `filter_books_json`, `filter_sports_json`, `filter_before_date`,
   `use_cash`, `pending_json`, `cash_json`. That's 10 inputs — check GitHub's current
   workflow_dispatch input limit before adding another (pack into an existing JSON input
   if needed).
2. GitHub spins up an `ubuntu-latest` runner (Python 3.13, 5-min timeout), installs
   `requirements.txt`, and runs `run_boost_scan.py` with those inputs as env vars.
3. The script writes its output to `docs/results/<run_id>.json`. The workflow's last step
   commits that file straight to `main` (`git add` → `commit` → `pull --rebase` →
   `push`), using GitHub's own auto-issued token — no manual secret needed for this part.
4. The frontend polls for `docs/results/<run_id>.json` to appear and renders it when found.

**No boost/free-bet list is ever stored in the repo.** It lives in the browser's
localStorage (mirrored to Reid's private Firebase, see below) and is sent fresh as JSON
on every dispatch — deliberate, since the repo is public and anyone could otherwise read
Reid's bet list from the repo itself.

### Bankroll (in-app since Nov 17 2026)

The Bankroll tab is the only place balances live. `arb_bankroll` = `{starting, withdrawals,
balances, updated_at}` per book (draftkings/fanduel/fanatics/kalshi); `arb_bankroll_log` =
"Record Bankroll" history `[{_id, date, balances, starting_total, withdrawals_total}]`.
P/L = balance + withdrawals − starting. `loadCash()` derives the
old `{cash, pl, fetched_at}` shape from it, so the Scanner tab's Bankroll summary, Est.
available, and Closed Bets' Realized Profit are unchanged consumers. Scan Now sends the
balances as `cash_json`; `run_boost_scan.fetch_cash()` reads `CASH_JSON` (no sheet, no
network). Reid's numbers live only in his browser + Firebase — never put them in the repo
(it's public). Reid wants no Google Sheet anywhere in the app (11/17) — the old sheet
import box was removed after his history was brought in; don't reintroduce sheet links or
imports. Log entries may carry an `other` amount (older weeks that included a Novig
balance) that's added to that entry's total/profit. `cash_only` mode is legacy and unused.

### Matchups

Loaded automatically once per page load (`autoLoadMatchups`, a quiet `games_only` run that
doesn't block Scan Now); the top Refresh button reloads the page, which re-triggers it.
There is no Load Matchups button any more.

### Cross-device sync (Firebase, added Nov 17 2026)

Hosting stays on GitHub Pages; Firebase project `arby-sniper` (Spark/free plan, Reid's
Google account) is only the data store. The sync code is the last `<script>` block in
`docs/index.html` (Firebase compat SDK 10.12.2 from gstatic, loaded after the main script
so the app still works local-only if it fails to load).

- localStorage remains the app's only read/write path. The sync block hooks
  `Storage.prototype.setItem/removeItem` for the keys in `SYNC_KEYS` and mirrors them to
  Firestore `users/{uid}/kv/{key}` = `{ v: <raw string or null>, t: serverTimestamp }`.
  A live `onSnapshot` writes other devices' changes back and re-renders via `showScreen()`.
  **If you add a new localStorage key that should follow Reid across devices, add it to
  `SYNC_KEYS`.** Not synced on purpose: `arb_active_page`, the games cache,
  `arb_filter_sport_universe`.
- Conflicts: `LIST_KEYS` (boosts, free bets, tracker, placed legs) merge **item by item
  by `_id`**. Each device keeps `arb_sync_base` (last value it knew the cloud had); pushes
  run in a Firestore transaction that 3-way merges base / local / cloud — adds and edits
  on either side survive, a delete wins only if the other side didn't edit that item.
  Other keys are last-write-wins. Unacked local changes (`arb_sync_dirty`) are retried on
  reconnect, on `online`, when the tab becomes visible, and every 30s.
- The first merge waits for a real server snapshot (`!metadata.fromCache`); an offline
  start otherwise sees an empty cache and would push stale data. Any code that reads a
  synced key, `await`s, then saves it must re-read after the await (see `renderTracker`'s
  auto-close) or it can drop changes that arrived from another device in the meantime.
- First sign-in on a device (`arb_sync_uid` unset): empty cloud → upload local; otherwise
  cloud wins and local is kept in `arb_presync_backup` (written once, never overwritten).
- Auth is **email/password** (Google pop-up sign-in is unreliable from an iOS home-screen
  web app). Reid's user is created in the Firebase console; there is no sign-up form.
- Firestore rule: `match /users/{userId}/{document=**} { allow read, write: if
  request.auth != null && request.auth.uid == userId; }`.
- The web config (apiKey etc.) in the page is public by design — it identifies the
  project, it isn't a credential.
- Testing: gstatic is not reachable from the cloud sandbox, so tests intercept the SDK
  URLs in Playwright and serve a fake SDK; simulate devices as separate browser contexts
  sharing one fake cloud. Don't call `page.evaluate` on another page from inside an
  `expose_function` callback (deadlocks) — queue and flush between steps.

## New-device setup

```
git clone https://github.com/reidroberge8-dev/arb-boost-scanner.git
cd arb-boost-scanner
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

**Git push auth** (classic PAT, scope `repo`):
```
git config --global credential.helper store
# first push will prompt for username (reidroberge8-dev) and password — paste the PAT as the password
```
This writes `~/.git-credentials` and is host-keyed, so it covers any other repo under this
account too. Remote URLs stay plain `https://github.com/...` (no token embedded in the URL).
If a push ever 403s, the PAT expired — mint a fresh one at
`https://github.com/settings/tokens/new`.

**`gh` CLI auth** — do **not** run `gh auth login` interactively. In a sandboxed/headless
assistant terminal (no real TTY), the interactive device-code/browser flow hangs
(`GetConsoleMode failed`-style failure) — this is a generic limitation of automated
terminal sessions, not specific to this repo. Instead:
```
export GH_TOKEN="<your PAT>"
gh run list --workflow=boost-scan.yml -L 5     # now works without any login step
```
`gh repo create` / `gh api .../pages` were only needed for the original one-time repo/Pages
setup — not needed again, the repo and Pages are already live.

**Never commit a real token value anywhere in this repo** (it's public). Only commit
instructions for generating one.

## Live-site auth (separate, end-user concern — not a dev setup step)

The live site itself needs a GitHub personal access token pasted into its own in-page
prompt ("🔑 Access Token Needed") before it can dispatch scans. That token should be
scoped **only to this repo**, with **Actions: Read and write** permission and nothing
else — create it at `github.com/settings/personal-access-tokens/new`. It's stored in that
browser's localStorage only, never sent anywhere else and never committed. It does not
sync across browsers/devices — Reid (or anyone using the site) pastes it once per
browser. This is unrelated to the git/gh setup above and doesn't require Claude or a dev
environment at all — just opening the live URL in a normal browser.

## Line endings: check, don't assume

Files are stored in the repo with **LF** line endings (verified Nov 17 2026: 0 CR bytes
in `arb_engine.py`, `run_boost_scan.py`, `docs/index.html`, `boost-scan.yml`). The
original Windows dev machine showed them as CRLF only because of git's `autocrlf`
checkout conversion. So in a Linux/cloud clone, do NOT convert to CRLF — that rewrites
every line. Before editing, check with `grep -c $'\r' <file>` and preserve whatever is
there; after editing, `git diff --stat` should show only the lines you meant to change.
Still do exact-match replacements with an asserted match count, never blind replaces.

## Shipping discipline

- **Self-test before every commit.**
  - Frontend (`docs/index.html`): strip out any truly risky inline `<script>` tags if
    needed, build a JSDOM instance *without* `runScripts: 'dangerously'` (that option
    crashes the whole harness on any thrown error inside the page's own scripts — too
    fragile to use). Extract the script body via regex, then
    `vm.runInContext(scriptText + yourTestCode, vm.createContext(window))`. Remember:
    top-level `let`/`const` in the page's script are **not** `window.X` properties — your
    test code must reference them as bare names inside the same `vm` context, not
    `window.X`.
  - Backend: import `arb_engine` / `run_boost_scan` directly and use
    `unittest.mock.patch` to fake network calls — don't hit real sportsbook/Kalshi
    endpoints or burn a real Actions run just to test logic.
- **Independent review** for any non-trivial logic change: have a second
  agent/session re-derive and re-test from scratch rather than trusting your own
  self-check. Small, purely cosmetic/UI tweaks can be self-verified only.
- **Commit messages**: title, then the actual ask/root-cause quoted, a bulleted
  breakdown of what changed, and a "Verified:" paragraph stating what you actually
  tested (with real counts, not "looks good").
- **Always `git fetch && git rebase origin/main` before pushing.** The automated Boost
  Scan workflow commits a new `docs/results/*.json` file to `main` constantly — if you
  don't rebase first, your push will conflict.
- **Confirm the Pages deploy** after pushing: `gh run list` / `gh run watch
  --exit-status`. Note: a deploy run can show "cancelled" because a newer one raced in
  and superseded it — that's benign; check that the *latest* run on `main` succeeded,
  not the specific run you started watching.
- **For any UI-visible change, do a real interactive browser check**, not just the jsdom
  harness. jsdom cannot catch real CSS layout bugs — this gap has caught at least two
  real production bugs in this project's history that the automated tests missed. When
  checking a just-shipped change in a browser, force a real reload (cache-busting query
  string like `?_cb=<timestamp>`) since a tab can otherwise serve a cached pre-deploy
  version.

## Misc environment gotchas (generic, not specific to this repo)

- `ripgrep`'s `\|` is a literal pipe character, not regex alternation — use bare `|`.
- A browser tab left open across a deploy can silently keep serving the old version;
  always force-reload with a cache-busting query param when verifying a fresh deploy.

## Known architecture quirk / maintenance item

`docs/results/*.json` accumulates forever — 414 files, ~4.5MB as of November 2026, and
nothing prunes it. Worth addressing eventually (e.g. a prune-by-age cleanup step, or
switching to GitHub Actions artifacts instead of committing results into the repo), but
it isn't urgent and shouldn't be changed unprompted — ask first.

## Feature history (condensed)

Full blow-by-blow detail for anything before this file existed lived only in the prior
assistant's own memory and isn't transferable — treat `git log` and the repo's own commit
messages as the deepest available source of truth for history older than this doc.

- **Pre-Oct 5 2026**: initial engine — boosted bets, free bets, true arbs, middles,
  Kalshi arbs and Kalshi ladders, with cross-book hedge-blending and cash-pool capping.
- **Oct 5**: NBA disable toggle, scan checkboxes, combo-aware "Run All Plays Together",
  date-cutoff filter, Kalshi middles.
- **Oct 16** (large batch): Free Bet Type dropdown + Select All + Open/Closed Bets fixes;
  screenshot styling; clear-on-focus + Game autocomplete; "Load Upcoming Games" button;
  bold leg stake; screenshot share-text removal + 3x mobile scale; **multi-game filter
  with OR semantics** (backward compatible with single-game filter); no-match warning
  follow-up; tiny-input/huge-X-button CSS bugfix; Select-All button label spelled out
  fully; "Load Upcoming Games" renamed to "Load Matchups" and recolored to the theme
  green; loaded matchups now persist across a page refresh via a new localStorage cache.
- **Nov 17**: development moved to Reid's Claude account (cloud sessions; git push and
  `gh` work through the session's GitHub connection — no PAT needed there). Cross-device
  sync via Firebase (see above), with a ☁ sync button next to Refresh and a "Sign in to
  sync" option on the token screen. Open Bets: reliable auto-close (per-game-day ESPN
  fetches, college matching, 12h start-time window), live games on top with a LIVE banner,
  app-wide 20s auto-refresh. In-app Bankroll tab (no Google Sheet anywhere); matchups auto-load
  and the Load Matchups / Refresh Cash buttons are gone.

- **Nov 18**: True Arb and Kalshi Arb cards show units per leg ("— bet N units",
  `arbUnits()` in index.html: stake ∝ 1/decimal odds, smallest leg = 1 unit; also on
  screenshots). Stale-price guard in `odds_scraper.book_lines()`: a book's price is
  dropped if its implied prob is >4.0 pts better than the median of ≥3 other books on
  the same side/number (`STALE_GAP_PTS`, `STALE_MIN_PEERS`). Cause: VI froze Fanatics
  Pelicans ML at +200 (real +155) → fake 1.63% arb. Note VI also lags a few cents
  (FD showed -186 vs real -176); the guard can't catch small lag.

- **Nov 20**: DraftKings over-max boosts (Reid: DK lets you bet past a boost's max; the
  part over the max pays the plain price, `OVER_MAX_BOOST_BOOKS`), and combos now sized
  for the most GUARANTEED profit (Reid said yes to giving up best-case upside). Exact
  sizing in `_size_combo`: both outcomes pay the same R; each leg buys R at boosted odds
  up to its max, then plain DK odds; profit(R) is concave so it's max at a kink or the
  cash limit (per-book or shared same-book balance). That covers a DK top-up on the
  lower-paying leg AND betting under the max on the higher-paying leg, whichever is
  better. R is capped at the larger fully-boosted payout (past that it's a plain true
  arb). `apply_cash_pool` re-sizes combos with `_resize_combo(p, remaining)`. Verified
  against scipy linprog. Combo legs carry `boost_max` / `unboosted_stake`; the page's
  `boostedPayout(..., boostMax)` boosts only the first `boost_max` dollars and
  `overMaxNoteHtml` explains the split. Solo boost + hedge is deliberately unchanged:
  extra plain dollars there earn d*(1-c)-1, positive only when DK's plain price already
  arbs.

## Known open items

- **10-day true-arb scan (Reid's ask, 11/18)**: scan all matchups in the next 10 days
  for true arbs, not just what VI's main odds page lists (NBA ≈ today/tomorrow). VI has
  a day picker on its odds pages, but the per-date URL format was NOT verified — the
  11/18 session couldn't reach vegasinsider.com. Reid has since added vegasinsider.com
  to his allowed domains, so a new session can fetch it: find the per-date URL, confirm
  it returns that day's board (not today's again), fetch each day in
  `_fetch_sport_games`, de-dupe games, keep the stale-price guard, test on live pages.
  Don't guess the URL — a param VI ignores would duplicate every game.
- Optional: Kalshi Ladder cards put 1u on the larger leg (small leg shows e.g. 0.067u);
  offered to switch to smallest-leg = 1u like the arb cards. Not requested yet.

Otherwise clean as of November 17, 2026 (repo verified clean, `HEAD` matches
`origin/main`). The one previously-noted soft gap — no feedback on a first-ever scan
when a game filter will never match anything — is already mostly covered by the
no-match-warning feature above.

## When in doubt

The code is deliberately commented on *why*, not just *what*, specifically to support
handoffs like this one. Read the surrounding comments and recent `git log` messages
before guessing at intent.
