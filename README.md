# Options Strategy Lab v0.2

A separate Streamlit paper-trading research lab. It uses the same Opportunity Index logic as the wheel dashboard, but can scan a broad market universe and run competing $100,000 virtual management models side-by-side.

## Broad universe

- **S&P 500 + custom** loads the current S&P 500 constituent list from an auto-updated public mirror and adds custom tickers such as SHOP or SPCX.
- A cheap underlying pre-screen evaluates every index constituent first.
- **Full eligible S&P 500** requests option chains for every constituent that can plausibly fit the collateral rules.
- **Fast broad scan** requests option chains for the top N pre-screen candidates to reduce Yahoo throttling. The speed proxy does not change the Opportunity Index.
- **Custom watchlist only** behaves like the original small-universe lab.

## Paper models

A: hold puts to expiration/assignment.

B: 50% winner.

C: 50% winner + DTE risk exit.

D10 / D25 / D50: opportunity-redeployment models with different required forward-return/day improvements.

E: D25 plus a wide loss-control experiment.

## Important

Yahoo/yfinance is a free unofficial data source and can throttle or return 401 errors. Full S&P option-chain scans are therefore slower and less reliable than a paid bulk options API. The app never fabricates missing quotes; failures are shown in Data Warnings.

Streamlit Cloud local disk is ephemeral. Download the paper-state backup periodically.
