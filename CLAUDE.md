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
| `run_boost_scan.py` | Entry point run by the GitHub Actions job. Reads inputs from env vars, calls into `arb_engine`, fetches bankroll, writes the result JSON. |
| `arb_engine.py` | The actual scan logic: boosted bets, free bets, true arbs, middles, Kalshi arbs/ladders, cross-book hedge-blending, cash-pool capping. Biggest file (~1700 lines), heavily commented on *why*. |
| `odds_scraper.py` | Scrapes/loads sportsbook odds data. |
| `kalshi_client.py` | Kalshi market data client. |
| `bankroll_sheet.py` | Reads Reid's manually-maintained bankroll from a Google Sheet (CSV export URL, label-matched parsing — the sheet's row layout has changed before, so it doesn't assume fixed row positions). URL comes from the `ARB_BANKROLL_CSV_URL` repo secret, no hardcoded fallback (repo is public). |
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
   `use_cash`, `pending_json`.
2. GitHub spins up an `ubuntu-latest` runner (Python 3.13, 5-min timeout), installs
   `requirements.txt`, and runs `run_boost_scan.py` with those inputs as env vars plus the
   `ARB_BANKROLL_CSV_URL` secret.
3. The script writes its output to `docs/results/<run_id>.json`. The workflow's last step
   commits that file straight to `main` (`git add` → `commit` → `pull --rebase` →
   `push`), using GitHub's own auto-issued token — no manual secret needed for this part.
4. The frontend polls for `docs/results/<run_id>.json` to appear and renders it when found.

**No boost/free-bet list is ever stored server-side.** It lives only in the browser's
localStorage and is sent fresh as JSON on every dispatch — deliberate, since the repo is
public and anyone could otherwise read Reid's bet list from the repo itself.

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

## Critical gotcha: CRLF line endings

`arb_engine.py`, `run_boost_scan.py`, `docs/index.html`, and
`.github/workflows/boost-scan.yml` are all CRLF (`\r\n`). A plain-text editor or naive
string write can silently flip them to LF, producing a huge noisy diff or a file with
mixed line endings. Safe edit recipe:

1. Read the file as bytes, decode as UTF-8.
2. `.replace('\r\n', '\n')` to normalize to LF for editing.
3. Do your exact-match string replacement (assert the match count first — don't blind-replace).
4. `.replace('\n', '\r\n')` to restore CRLF.
5. Encode UTF-8, write bytes back.

New files you create (like this one) don't have this constraint — plain LF is fine.

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

## Known open items

None outstanding as of November 17, 2026 (repo verified clean, `HEAD` matches
`origin/main`). The one previously-noted soft gap — no feedback on a first-ever scan
when a game filter will never match anything — is already mostly covered by the
no-match-warning feature above.

## When in doubt

The code is deliberately commented on *why*, not just *what*, specifically to support
handoffs like this one. Read the surrounding comments and recent `git log` messages
before guessing at intent.
