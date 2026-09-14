from __future__ import annotations

from io import StringIO
from math import log10
from typing import Iterable

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

SP500_SOURCE = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
SP500_FALLBACK_SOURCE = "https://raw.githubusercontent.com/chinobing/historical_sp500_constituents/main/sp500_constituents.csv"

# Small fallback only if both live constituent sources fail. This is deliberately
# not presented as the full S&P 500.
FALLBACK_LIQUID = (
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","AVGO","AMD",
    "INTC","NFLX","JPM","BAC","XOM","CVX","WMT","COST","HD","DIS","CRM",
    "ORCL","QCOM","MU","AMAT","LRCX","PLTR","UBER","HOOD","COIN","SMCI",
    "NKE","LULU","PYPL","F","GM","PFE","MRK","JNJ","ABBV","LLY","UNH",
    "T","VZ","KO","PEP","MCD","SBUX","BA","CAT","GE","RTX","QQQ",
)


def normalize_yahoo_symbol(symbol: str) -> str:
    """Convert S&P-style class tickers to Yahoo's hyphen convention."""
    s = str(symbol).strip().upper()
    return s.replace(".", "-")


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def load_sp500_constituents() -> tuple[pd.DataFrame, str, str]:
    """Return current S&P 500 constituent table, source label, and warning."""
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"}
    errors: list[str] = []
    for url, label in ((SP500_SOURCE, "datasets current constituents mirror"), (SP500_FALLBACK_SOURCE, "Wikipedia mirror")):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text))
            cols = {str(c).strip().lower(): c for c in df.columns}
            symbol_col = cols.get("symbol")
            if symbol_col is None:
                raise ValueError("No symbol column")
            out = pd.DataFrame({"Symbol": df[symbol_col].astype(str).map(normalize_yahoo_symbol)})
            sec_col = cols.get("security")
            sector_col = cols.get("gics sector")
            if sec_col is not None:
                out["Security"] = df[sec_col].astype(str)
            if sector_col is not None:
                out["Sector"] = df[sector_col].astype(str)
            out = out.dropna(subset=["Symbol"]).drop_duplicates("Symbol").reset_index(drop=True)
            if not 490 <= len(out) <= 510:
                raise ValueError(f"Only {len(out)} constituents returned")
            return out, label, ""
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    out = pd.DataFrame({"Symbol": list(FALLBACK_LIQUID)})
    return out, "small built-in fallback", "Could not load the full live S&P 500 list: " + " | ".join(errors)


def _extract_symbol_frame(raw: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        lvl0 = list(map(str, raw.columns.get_level_values(0)))
        lvl1 = list(map(str, raw.columns.get_level_values(1)))
        if symbol in lvl0:
            try:
                return raw[symbol].copy()
            except Exception:
                pass
        if symbol in lvl1:
            try:
                return raw.xs(symbol, axis=1, level=1).copy()
            except Exception:
                pass
        return pd.DataFrame()  # Never use another ticker's data for a missing symbol.
    return raw.copy()


@st.cache_data(ttl=15 * 60, show_spinner=False)
def prescreen_underlyings(
    symbols: tuple[str, ...],
    min_cash: float,
    max_cash: float,
    max_otm_prescreen_pct: float = 50.0,
    min_avg_volume: float = 0.0,
    batch_size: int = 60,
    include_rejected: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """
    Cheap first pass over every underlying. This is not an option score.

    The lower affordability gate rules out prices with no OTM strike at the
    minimum collateral. Expensive stocks remain eligible: a deep OTM put may
    fit. max_otm_prescreen_pct only labels that diagnostic, never rejects it.
    """
    warnings: list[str] = []
    rows: list[dict] = []
    if not symbols:
        return pd.DataFrame(), warnings
    max_otm = min(max(float(max_otm_prescreen_pct) / 100.0, 0.0), 0.80)
    min_spot = max(float(min_cash) / 100.0, 0.01)
    max_spot = float(max_cash) / (100.0 * max(1.0 - max_otm, 0.05))

    for start in range(0, len(symbols), int(batch_size)):
        chunk = tuple(symbols[start:start + int(batch_size)])
        try:
            raw = yf.download(
                list(chunk), period="3mo", interval="1d", group_by="ticker",
                auto_adjust=False, actions=False, threads=True, progress=False,
                timeout=20,
            )
        except Exception as exc:
            warnings.append(f"Underlying batch {start + 1}-{start + len(chunk)} failed: {exc}")
            raw = pd.DataFrame()

        for symbol in chunk:
            frame = _extract_symbol_frame(raw, symbol)
            if frame.empty or "Close" not in frame.columns:
                # Fallback to a single-symbol history call; this keeps a transient
                # batch failure from silently excluding the stock.
                try:
                    frame = yf.Ticker(symbol).history(period="3mo", interval="1d", auto_adjust=False)
                except Exception as exc:
                    warnings.append(f"{symbol}: no underlying history ({exc})")
                    rows.append({'Ticker': symbol, 'Eligible': False, 'Rejection Reasons': 'underlying data unavailable'})
                    continue
            if frame.empty or 'Close' not in frame.columns:
                warnings.append(f'{symbol}: no usable underlying history')
                rows.append({'Ticker': symbol, 'Eligible': False, 'Rejection Reasons': 'underlying data unavailable'})
                continue
            close = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
            if close.empty:
                warnings.append(f"{symbol}: no usable close")
                rows.append({'Ticker': symbol, 'Eligible': False, 'Rejection Reasons': 'underlying data unavailable'})
                continue
            spot = float(close.iloc[-1])
            reasons = []
            # No upper spot gate: a deeper OTM strike can still be affordable.
            if not np.isfinite(spot) or spot <= min_spot:
                reasons.append('affordability: no OTM strike above minimum collateral')
            vol_series = pd.to_numeric(frame.get("Volume"), errors="coerce").dropna() if "Volume" in frame.columns else pd.Series(dtype=float)
            avg_vol = float(vol_series.tail(20).mean()) if not vol_series.empty else np.nan
            if np.isfinite(avg_vol) and avg_vol < float(min_avg_volume):
                reasons.append('underlying volume below minimum')
            if not np.isfinite(avg_vol):
                reasons.append('underlying volume unavailable')
            lr = np.log(close / close.shift(1)).dropna()
            hv30 = float(lr.tail(30).std(ddof=1) * np.sqrt(252.0)) if len(lr) >= 30 else np.nan
            if not np.isfinite(hv30) or hv30 <= 0:
                reasons.append('insufficient HV30 data')
            r20 = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 else np.nan
            r5 = float(close.iloc[-1] / close.iloc[-6] - 1.0) if len(close) >= 6 else np.nan
            # Only used to order a user-requested fast scan. It is NOT part of
            # the Opportunity Index and is not used in Full Eligible mode.
            vol_term = hv30 if np.isfinite(hv30) else 0.0
            downside_term = max(-r20, 0.0) if np.isfinite(r20) else 0.0
            liq_term = max(log10(max(avg_vol, 1.0)) - 5.0, 0.0) / 10.0 if np.isfinite(avg_vol) else 0.0
            proxy = vol_term + 0.5 * downside_term + 0.05 * liq_term
            rows.append({
                "Ticker": symbol,
                "Eligible": not reasons,
                "Rejection Reasons": '; '.join(reasons),
                "Price Date": str(close.index[-1]),
                "Near Spot Collateral": spot * 100,
                "Deep OTM Required": spot > max_spot,
                "Stock Price": spot,
                "HV30": hv30,
                "20D Return": r20,
                "5D Return": r5,
                "Avg Volume 20D": avg_vol,
                "Prefilter Proxy": proxy,
            })

    out = pd.DataFrame(rows)
    if out.empty:
        return out, warnings
    if not include_rejected:
        out = out[out['Eligible']].copy()
    if out.empty or 'Prefilter Proxy' not in out:
        return out, warnings
    out = out.sort_values(["Prefilter Proxy", "Avg Volume 20D"], ascending=[False, False]).reset_index(drop=True)
    return out, warnings


def choose_option_scan_symbols(
    prescreen: pd.DataFrame,
    mode: str,
    fast_limit: int,
    extras: Iterable[str] = (),
) -> tuple[str, ...]:
    if prescreen.empty:
        base: list[str] = []
    elif mode == "Full eligible S&P 500":
        base = prescreen["Ticker"].astype(str).tolist()
    else:
        base = prescreen.head(max(int(fast_limit), 1))["Ticker"].astype(str).tolist()
    for x in extras:
        s = normalize_yahoo_symbol(x)
        if s and s not in base:
            base.append(s)
    return tuple(dict.fromkeys(base))
