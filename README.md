# Automated Options Strategy Lab

Ten independent $100,000 paper portfolios compare cash-secured put entry/exit rules using the existing 30% return / 70% protection Opportunity Index. No broker integration or real orders.

## Schedule and persistence

`.github/workflows/paper.yml` requests runs at :07 and :37, 13:00–21:59 UTC on weekdays. The NYSE calendar gates trading to regular sessions and handles daylight saving, holidays and early closes. Entries stop 15 minutes before close. After-close runs settle expiry positions only when their expiry-session close is available. GitHub schedules are best effort, not exact-time guarantees: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule

The single serialized runner persists `state.json` on `paper-results`; this avoids Streamlit restart data loss and main-branch redeployment on every scan. Never delete/reset that branch during an experiment. Dashboard reads are read-only. Corrupt or inaccessible existing state causes a failed run, never a fresh portfolio. GitHub Actions logs show failed/late runs; the dashboard displays last saved time and stale results.

## Common entry rules

Current S&P 500 and Nasdaq-100 union, refreshed daily. Screen all underlyings for affordable collateral and 1m average daily volume; fetch option chains for top 20 plus 20 rotating candidates per cycle. Runtime budget may defer remaining stocks. This is not a full option-chain scan of all constituents each half hour. Index-source failure pauses new entries. Existing holdings are still managed.

21–60 DTE, OTM puts, $3,000–$20,000 collateral, positive bid, spread <=25% of ask, open interest >=100, last trade today, known upcoming earnings outside the option window. Maximum five different stocks, 20% of initial capital per stock, 10% cash reserve. One contract per stock. Missing earnings or quotes means skip, not invented data. Entry candidates are re-quoted by exact contract; adverse changes defer entry rather than using an obsolete score.

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

G uses constant entry spot/IV, 4% rate, Black–Scholes put decay normalized to entry theoretical price. This is a heuristic baseline, not promised income. Rules stay fixed for version 1. Changing them mid-experiment invalidates comparisons; use a new version and separate state for a new experiment.

## Accounting and limitations

Sell at bid, buy back at ask, $1 fee per contract per side. Short option liabilities are included in NAV. Missing quotes retain last mark and are labeled stale. Periodic loss checks do not guarantee an exit at the limit. Expiry uses cash-equivalent intrinsic settlement at unadjusted expiry-session stock close; no expiry fee. This deliberately excludes physical assignment/covered calls, dividends, interest, early exercise and market impact. It is an option-management experiment, not a full wheel simulation. Yahoo data may be stale or throttled; a same-day last trade is not proof of a fresh bid/ask. Results are forward paper results, not backtests or evidence of profitability.

`legacy_app.py` preserves the prior manual app. Its local `data/paper_state.json` is not read or reset by this new experiment.

## Tests and local run

Install `requirements.txt`, then `python -m unittest test_paper.py`. Run `python auto_runner.py --state /path/to/state.json` for a market-gated evaluation. Run `streamlit run app.py` for the read-only dashboard. Initialize production through GitHub Actions **Run workflow**; never upload test fixtures as production results.
