# Automated Options Strategy Lab

Thirteen independent $100,000 paper portfolios compare cash-secured put entry/exit rules and Opportunity Index entry weights. A–J keep their existing exit rules and 30% return / 70% protection score. A40, A50 and A60 match A (hold to expiry, minimum 75) except for 40/60, 50/50 and 60/40 entry weighting. There is no ten-strategy cap. No broker integration or real orders.

## Schedule and persistence

The dashboard shows the next expected market-hours scan and an updating countdown, with New York, Edmonton or UTC display times. It also shows the next after-close settlement check. The schedule runs on GitHub independently of the dashboard or your computer. Runs are serialized, never overlapping; slow requests can defer a later run. The 15-minute cadence uses quarter-hour idempotency slots and preserves all existing portfolios. Only the evaluation frequency changes; exit thresholds remain unchanged.

`.github/workflows/paper.yml` requests runs every 15 minutes at :07, :22, :37 and :52, 13:00–21:59 UTC on weekdays. The NYSE calendar gates trading to regular sessions and handles daylight saving, holidays and early closes. Entries stop 15 minutes before close. After-close runs settle expiry positions only when their expiry-session close is available. GitHub schedules are best effort, not exact-time guarantees: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule

The single serialized runner persists `state.json` on `paper-results`; this avoids Streamlit restart data loss and main-branch redeployment on every scan. Never delete/reset that branch during an experiment. Dashboard reads are read-only. Corrupt or inaccessible existing state causes a failed run, never a fresh portfolio. GitHub Actions logs show failed/late runs; the dashboard displays last saved time and stale results.

## Common entry rules

Current S&P 500 and Nasdaq-100 union, normalized and deduplicated, refreshed daily from constituent sources. Index-source failure pauses new entries, including cached entries; no small fallback is substituted. Existing holdings are still managed.

1. **Stage 1:** inspect every underlying for price, affordability, 20-day average volume, 30-observation annualized historical volatility, and 5/20-day returns. Every name appears in diagnostics, including unavailable data. The default volume minimum remains 1m shares. A high share price alone cannot exclude an affordable deep OTM put. History without the current session's daily bar is marked stale.
2. **Stage 2:** default 180 ATM checks, configurable 150–200. Use 140 high underlying-proxy names and 40 rotating names by default, interleaved to avoid starving rotation under a deadline. Fetch one representative expiry at 30–45 DTE, nearest 37.5 days. Rank by `ATM IV / HV30 × (1 − ATM spread/ask)`, not raw IV. Show IV, richness, spread, open interest, volume and earnings timing. Earnings are never a Stage 2 exclusion. Missing representative expiries or IV/spread data remain unavailable.
3. **Stage 3:** full scans of up to the top 100 ATM-ranked names (configurable 75–100), plus 20 rotating names drawn from all other Stage 1-eligible stocks, including those outside Stage 2. Interleave the cohorts. Inspect all OTM puts across the configured DTE/collateral range, record observed contract rejections, and compute the existing absolute Opportunity Index.
4. **Rolling selection:** persist all scored contracts and each ticker's best eligible contract for each weight, with components and observation timestamps. Rank the entire fresh cache independently for each strategy, not just the current batch or baseline winner. Default freshness is 30 minutes. Recheck current configuration, universe eligibility, session and freshness before entry. A failed/partial refresh blocks that ticker's cached entries; old values are retained as stale diagnostics. Requote exact contracts before simulated fills. Adverse quote changes defer entry until a new scan rather than inventing a score.
5. **Missed-opportunity audit:** record rotating discoveries that rank in the top ten eligible scanned tickers for a weight or beat the best prescreened ticker by at least five Index points (configurable). Compare best contracts per ticker, so many strikes cannot crowd the top ten. This measures only observed opportunities; it cannot prove there are no misses among unscanned stocks.

Edit `scan_config.json` or pass `--config /path/to/config.json`. The file configures counts, rotation, freshness, DTE, collateral, liquidity and runtime budgets; each run saves its effective settings. Defaults reserve 45% of the 600-second scan budget for Stage 2, the rest for full scans, and allow entry verification until 780 seconds. Requests already in flight may overrun these soft deadlines. The dashboard distinguishes planned, attempted, successful, partial, failed and deferred scans. Yahoo latency/throttling may prevent the target counts. Rotation advances by attempts, not merely planned names. There is no promise of full-universe fresh option coverage every 15 minutes.

Defaults: 21–60 DTE, OTM puts, $3,000–$20,000 collateral, positive bid, spread <=25% of ask, open interest >=100, last trade today, known upcoming earnings outside the option window. Maximum five different stocks, 20% of initial capital per stock, 10% cash reserve. One contract per stock. Missing earnings or quotes means skip, not invented data. Entry candidates are re-quoted by exact contract; adverse changes defer entry rather than using an obsolete score. Verification shares an expiry response for up to 45 seconds across exact contract matches to avoid repeated downloads.

| Strategy | Minimum Index | Exit |
|---|---:|---|
| A | 75 | Hold to expiry |
| B | 75 | 25% premium capture |
| C | 75 | 50% premium capture |
| D | 75 | 75% premium capture |
| E | 75 | 50% capture or 7 DTE |
| F | 75 | 50% capture or ask >=3x entry credit |
| G | 75 | Positive capture >=35 percentage points ahead of theoretical decay |
| H | 100 | 50% capture |
| I | 125 | 50% capture |
| J | 100 | 25% capture, 14 DTE, or ask >=2x credit |

G uses constant entry spot/IV, 4% rate, Black–Scholes put decay normalized to entry theoretical price. This is a heuristic baseline, not promised income. Exit rules and cash-equivalent settlement are unchanged in this revision.

## State compatibility and audit trail

Version 2 migrates version 1 additively: retain all A–J cash, positions, closed trades, events, fees and history; initialize only A40/A50/A60; add cache and migration metadata. Migration is idempotent. Unknown versions, missing original portfolios and unknown portfolio IDs fail without resetting state. The dashboard accepts version 1 while waiting for the runner to migrate. The original manual lab state is untouched.

New weight portfolios have later start dates and independent capital paths. Compare overlapping periods; the broader entry pipeline also changes A–J's future selection, so their pre/post-migration returns are not a controlled weighting-only comparison. The migration timestamp records that boundary. The three new variants differ from A only in the entry weighting definition.

Every run writes an immutable compressed JSON audit under `audits/` on `paper-results`: universe → Stage 1 → ATM IV → full scans → rolling rankings → per-strategy portfolio constraints → selected contracts. Earnings-rejected puts retain their scores. Spread, open interest, stale/failed data, collateral, concentration, minimum scores, deferred verification and closed entry windows are explicit reasons. Complete tables remain in compressed audit files to avoid duplicating large tables in `state.json`; the dashboard loads the chosen audit and offers a JSON download. State retains the latest metadata, 200 run summaries, and 2,000 recent missed-opportunity rows; all historical full audits remain on the results branch. Nothing resets portfolios or deletes historical audits.

## Accounting and limitations

Sell at bid, buy back at ask, $1 fee per contract per side. Short option liabilities are included in NAV. Missing quotes retain last mark and are labeled stale. Periodic loss checks do not guarantee an exit at the limit. Expiry uses cash-equivalent intrinsic settlement at unadjusted expiry-session stock close; no expiry fee. This deliberately excludes physical assignment/covered calls, dividends, interest, early exercise and market impact. It is an option-management experiment, not a full wheel simulation. Yahoo data may be stale or throttled; a same-day last trade is not proof of a fresh bid/ask. Results are forward paper results, not backtests or evidence of profitability.

Baseline puts exclude earnings in the holding window and unknown earnings timing. The separate legacy covered-call scanner **allows earnings** and labels that policy and timing on its candidates; this revision does not add covered-call execution to the automated portfolios. Index mirrors may lag membership changes and public earnings dates may be missing or revised. Yahoo 401/429, empty chains and other failures produce warnings and stale/unavailable status, never synthetic options or zero-liability valuations.

`legacy_app.py` preserves the prior manual app. Its local `data/paper_state.json` is not read or reset by this new experiment.

## Tests and local run

Install `requirements.txt`, then `python -m unittest discover -p 'test_*.py'`. Tests use synthetic fixtures and mocked providers, covering existing exits/accounting, scoring weights, independent contract winners, rolling freshness, rejected candidates, rotation, Yahoo failures, migration, runner audit persistence and old/new Streamlit results. Run `python auto_runner.py --state /path/to/state.json --config scan_config.json` for a market-gated evaluation. Run `streamlit run app.py` for the read-only dashboard. Production updates through GitHub Actions; never upload test fixtures as production results.
