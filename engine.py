from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import date, datetime
from types import SimpleNamespace
from typing import Iterable

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

ABS_RETURN_TARGET_PER_DAY = 0.001  # 0.10%/day
ABS_PROTECTION_TARGET = 1.0
HV_WINDOW = 30
EARNINGS_TIE_THRESHOLD = 1.0

_YF_OPTIONS_URLS = (
    "https://query1.finance.yahoo.com/v7/finance/options/{symbol}",
    "https://query2.finance.yahoo.com/v7/finance/options/{symbol}",
)
_YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def safe_float(value, default=np.nan) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        return out if np.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def normalize_dates(values: Iterable) -> list[date]:
    out: list[date] = []
    for value in values:
        try:
            ts = pd.Timestamp(value)
            if ts.tzinfo is not None:
                ts = ts.tz_convert(None)
            out.append(ts.date())
        except Exception:
            continue
    return sorted(set(out))


def get_stock_price(ticker: yf.Ticker) -> float:
    try:
        fi = ticker.fast_info
        for key in ("last_price", "lastPrice", "regular_market_price", "regularMarketPrice"):
            try:
                value = safe_float(fi.get(key))
            except Exception:
                try:
                    value = safe_float(fi[key])
                except Exception:
                    value = np.nan
            if np.isfinite(value) and value > 0:
                return value
    except Exception:
        pass
    hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
    if hist.empty:
        raise ValueError("No stock price available")
    close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
    if close.empty:
        raise ValueError("No stock price available")
    return float(close.iloc[-1])


def get_historical_volatility(ticker: yf.Ticker, window: int = HV_WINDOW) -> float:
    try:
        hist = ticker.history(period="3mo", interval="1d", auto_adjust=True)
        if hist.empty or "Close" not in hist.columns:
            return np.nan
        close = pd.to_numeric(hist["Close"], errors="coerce").dropna()
        log_returns = np.log(close / close.shift(1)).dropna()
        if len(log_returns) < 10:
            return np.nan
        sample = log_returns.tail(min(window, len(log_returns)))
        vol = float(sample.std(ddof=1) * np.sqrt(252.0))
        return vol if np.isfinite(vol) and vol > 0 else np.nan
    except Exception:
        return np.nan


def get_earnings_dates(ticker: yf.Ticker) -> list[date]:
    dates: list[date] = []
    try:
        cal = ticker.calendar
        if isinstance(cal, dict):
            for key, value in cal.items():
                if "earn" not in str(key).lower():
                    continue
                vals = value if isinstance(value, (list, tuple, set, pd.Series, np.ndarray)) else [value]
                dates.extend(normalize_dates(vals))
    except Exception:
        pass
    try:
        ed = ticker.get_earnings_dates(limit=12)
        if isinstance(ed, pd.DataFrame) and not ed.empty:
            dates.extend(normalize_dates(ed.index))
    except Exception:
        pass
    return sorted(set(dates))


def next_earnings_date(earnings_dates: list[date], today: date | None = None) -> str:
    today = today or date.today()
    future = [d for d in earnings_dates if d >= today]
    return future[0].isoformat() if future else "Unknown"


def earnings_for_window(earnings_dates: list[date], expiry: date, today: date) -> tuple[bool, str]:
    upcoming = [d for d in earnings_dates if today <= d <= expiry]
    if upcoming:
        return True, upcoming[0].isoformat()
    return False, next_earnings_date(earnings_dates, today)


def _option_chain_from_yahoo_json(payload: dict) -> tuple[list[str], SimpleNamespace, str]:
    result = (((payload or {}).get("optionChain") or {}).get("result") or [])
    if not result:
        err = (((payload or {}).get("optionChain") or {}).get("error") or {})
        return [], SimpleNamespace(calls=pd.DataFrame(), puts=pd.DataFrame()), str(err or "empty Yahoo option-chain result")
    root = result[0] or {}
    expirations = [
        datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
        for ts in (root.get("expirationDates") or [])
        if str(ts).isdigit()
    ]
    options = root.get("options") or []
    block = options[0] if options else {}
    calls = pd.DataFrame(block.get("calls") or [])
    puts = pd.DataFrame(block.get("puts") or [])
    return expirations, SimpleNamespace(calls=calls, puts=puts), ""


@st.cache_data(ttl=45, show_spinner=False)
def yahoo_option_chain_direct(symbol: str, expiry: str | None = None):
    symbol = str(symbol).upper().strip()
    params = {}
    if expiry:
        try:
            params["date"] = int(pd.Timestamp(expiry, tz="UTC").timestamp())
        except Exception:
            pass
    errors = []
    for endpoint in _YF_OPTIONS_URLS:
        try:
            response = requests.get(endpoint.format(symbol=symbol), params=params, headers=_YF_HEADERS, timeout=15)
            if response.status_code != 200:
                errors.append(f"{endpoint.split('//',1)[1].split('/',1)[0]} HTTP {response.status_code}")
                continue
            payload = response.json()
            expirations, chain, error = _option_chain_from_yahoo_json(payload)
            if expirations or not chain.calls.empty or not chain.puts.empty:
                return expirations, chain.calls, chain.puts, error
            errors.append(error or "empty option-chain result")
        except Exception as exc:
            errors.append(str(exc))
    return [], pd.DataFrame(), pd.DataFrame(), "Yahoo direct fallback failed: " + " | ".join(errors)


def get_option_expirations(ticker: yf.Ticker, symbol: str) -> tuple[list[str], str]:
    try:
        expirations = list(ticker.options)
        if expirations:
            return expirations, ""
        yf_error = "yfinance returned no expirations"
    except Exception as exc:
        yf_error = str(exc)
    expirations, _, _, error = yahoo_option_chain_direct(symbol)
    if expirations:
        return expirations, ""
    return [], error or yf_error


def get_option_chain(ticker: yf.Ticker, symbol: str, expiry: str):
    try:
        chain = ticker.option_chain(expiry)
        calls = getattr(chain, "calls", None)
        puts = getattr(chain, "puts", None)
        if isinstance(puts, pd.DataFrame) and not puts.empty:
            return chain, ""
        if isinstance(calls, pd.DataFrame) and not calls.empty:
            return chain, ""
        yf_error = "yfinance returned an empty option chain"
    except Exception as exc:
        yf_error = str(exc)
    _, calls, puts, error = yahoo_option_chain_direct(symbol, expiry)
    if not calls.empty or not puts.empty:
        return SimpleNamespace(calls=calls, puts=puts), ""
    return None, error or yf_error


def get_atm_iv(chain, stock_price: float) -> float:
    values: list[float] = []
    for frame in (getattr(chain, "calls", None), getattr(chain, "puts", None)):
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        if "strike" not in frame.columns or "impliedVolatility" not in frame.columns:
            continue
        temp = frame[["strike", "impliedVolatility"]].copy()
        temp["strike"] = pd.to_numeric(temp["strike"], errors="coerce")
        temp["impliedVolatility"] = pd.to_numeric(temp["impliedVolatility"], errors="coerce")
        temp = temp.dropna()
        temp = temp[temp["impliedVolatility"] > 0]
        if temp.empty:
            continue
        temp["distance"] = (temp["strike"] - stock_price).abs()
        for iv in temp.nsmallest(2, "distance")["impliedVolatility"].tolist():
            iv = safe_float(iv)
            if np.isfinite(iv) and iv > 0:
                values.append(iv)
    return float(np.median(values)) if values else np.nan


def expected_move_for_expiry(hv30: float, atm_iv: float, dte: int):
    scale = np.sqrt(float(dte) / 365.0) if dte > 0 else np.nan
    hv_move = hv30 * scale if np.isfinite(hv30) and hv30 > 0 else np.nan
    iv_move = atm_iv * scale if np.isfinite(atm_iv) and atm_iv > 0 else np.nan
    candidates = []
    if np.isfinite(hv_move) and hv_move > 0:
        candidates.append((hv_move, "Historical"))
    if np.isfinite(iv_move) and iv_move > 0:
        candidates.append((iv_move, "Implied"))
    if not candidates:
        return hv_move, iv_move, np.nan, "Unavailable"
    move, source = max(candidates, key=lambda x: x[0])
    return hv_move, iv_move, float(move), source


def weighted_harmonic_mean(a, b, weight_a: float) -> np.ndarray:
    w_a = float(weight_a)
    w_b = 1.0 - w_a
    av = np.asarray(a, dtype=float)
    bv = np.asarray(b, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = (w_a / av) + (w_b / bv)
        return np.where((av > 0) & (bv > 0) & (denom > 0), 1.0 / denom, 0.0)


def opportunity_band(score: float) -> str:
    score = safe_float(score, 0.0)
    if score >= 150:
        return "Exceptional"
    if score >= 125:
        return "Very strong"
    if score >= 100:
        return "Strong"
    if score >= 75:
        return "Good"
    if score >= 50:
        return "Moderate"
    return "Low"


def apply_put_scores(df: pd.DataFrame, return_weight_pct: int) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    r = (pd.to_numeric(out["Return / Day"], errors="coerce") / ABS_RETURN_TARGET_PER_DAY * 100.0).clip(lower=0).fillna(0.0)
    p = (pd.to_numeric(out["Protection Ratio"], errors="coerce") / ABS_PROTECTION_TARGET * 100.0).clip(lower=0).fillna(0.0)
    w = float(return_weight_pct) / 100.0
    score = weighted_harmonic_mean(r, p, w)
    out["Return Score"] = r.round(1)
    out["Protection Score"] = p.round(1)
    out["Opportunity Index"] = np.round(score, 1)
    out["Opportunity Level"] = out["Opportunity Index"].map(opportunity_band)
    out["Balance Gap"] = (r - p).abs().round(1)
    return out


def scan_put_ticker(symbol: str, min_dte: int, max_dte: int, min_cash: float, max_cash: float,
                    min_cushion_pct: float, return_weight_pct: int = 30, otm_only: bool = True,
                    audit_rows: list | None = None, asof: date | None = None,
                    deadline: float | None = None):
    warnings: list[str] = []
    today = asof or date.today()
    ticker = yf.Ticker(symbol)
    stock_price = get_stock_price(ticker)
    earnings_dates = get_earnings_dates(ticker)
    hv30 = get_historical_volatility(ticker, HV_WINDOW)
    expirations, expiration_error = get_option_expirations(ticker, symbol)
    if not expirations:
        raise ValueError(f"Could not load option expirations: {expiration_error}")
    rows = []
    for expiry_str in expirations:
        if deadline is not None and time.monotonic() >= deadline:
            warnings.append(f'{symbol}: full scan incomplete; time budget reached')
            break
        try:
            expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        except Exception:
            continue
        dte = (expiry - today).days
        if dte < min_dte or dte > max_dte:
            continue
        chain, chain_error = get_option_chain(ticker, symbol, expiry_str)
        observed = time.time()
        if chain is None:
            warnings.append(f"{symbol} {expiry_str}: unavailable ({chain_error})")
            continue
        puts = chain.puts.copy()
        if puts.empty:
            warnings.append(f"{symbol} {expiry_str}: no puts")
            continue
        atm_iv = get_atm_iv(chain, stock_price)
        _, _, expected_move, expected_source = expected_move_for_expiry(hv30, atm_iv, dte)
        if not np.isfinite(expected_move) or expected_move <= 0:
            warnings.append(f'{symbol} {expiry_str}: expected move unavailable')
            continue
        for _, option in puts.iterrows():
            strike = safe_float(option.get("strike"))
            bid = safe_float(option.get("bid"), 0.0)
            ask = safe_float(option.get("ask"), 0.0)
            last = safe_float(option.get("lastPrice"), np.nan)
            contract_iv = safe_float(option.get("impliedVolatility"), np.nan)
            reasons = []
            if not np.isfinite(strike) or strike <= 0 or bid <= 0:
                reasons.append('invalid strike or bid')
            if otm_only and strike >= stock_price:
                reasons.append('not OTM')
            cash_required = strike * 100.0
            if not (min_cash <= cash_required <= max_cash):
                reasons.append('collateral outside configured range')
            if reasons:
                if audit_rows is not None:
                    audit_rows.append(dict(ticker=symbol, contract=str(option.get('contractSymbol', '')),
                        expiry=expiry_str, strike=strike, bid=bid, ask=ask, rejections=reasons))
                continue
            premium_received = bid * 100.0
            ret = premium_received / cash_required
            rpd = ret / dte if dte > 0 else 0.0
            breakeven = strike - bid
            cushion = (stock_price - breakeven) / stock_price
            if cushion * 100.0 < min_cushion_pct:
                if audit_rows is not None:
                    audit_rows.append(dict(ticker=symbol, contract=str(option.get('contractSymbol', '')),
                        expiry=expiry_str, strike=strike, rejections=['cushion below minimum']))
                continue
            protection_ratio = cushion / expected_move if expected_move > 0 else 0.0
            earnings_inside, earnings_date = earnings_for_window(earnings_dates, expiry, today)
            rows.append({
                "Ticker": symbol,
                "Observed At": observed,
                "Stock Price": stock_price,
                "Strike": strike,
                "Put": f"${strike:g}P",
                "Expiry": expiry_str,
                "DTE": dte,
                "Contract": str(option.get("contractSymbol", "")),
                "Bid": bid,
                "Ask": ask,
                "Last": last,
                "Contract IV": contract_iv,
                "ATM IV": atm_iv,
                "HV30": hv30,
                "Cash Required": cash_required,
                "Premium Received": premium_received,
                "Return": ret,
                "Return / Day": rpd,
                "$ / Day": premium_received / dte if dte > 0 else 0.0,
                "Breakeven": breakeven,
                "Cushion": cushion,
                "Expected Move": expected_move,
                "Expected Move Source": expected_source,
                "Protection Ratio": protection_ratio,
                "Annualized Return": ret * 365.0 / dte if dte > 0 else np.nan,
                "Earnings in Period": f"YES · {earnings_date}" if earnings_inside else "No",
                "Has Earnings": bool(earnings_inside),
                "Earnings Known": any(d >= today for d in earnings_dates),
                "Open Interest": safe_float(option.get("openInterest"), 0.0),
                "Last Trade Date": option.get("lastTradeDate"),
            })
    df = apply_put_scores(pd.DataFrame(rows), return_weight_pct)
    if df.empty:
        return df, warnings
    return df.sort_values(["Opportunity Index", "Return / Day", "Protection Ratio"], ascending=[False, False, False]).reset_index(drop=True), warnings


@st.cache_data(ttl=45, show_spinner=False)
def scan_universe(symbols: tuple[str, ...], min_dte: int, max_dte: int, min_cash: float, max_cash: float,
                  min_cushion_pct: float, return_weight_pct: int):
    frames = []
    warnings = []
    for symbol in symbols:
        try:
            df, w = scan_put_ticker(symbol, min_dte, max_dte, min_cash, max_cash, min_cushion_pct, return_weight_pct, True)
            if not df.empty:
                frames.append(df)
            warnings.extend(w)
        except Exception as exc:
            warnings.append(f"{symbol}: {exc}")
    if not frames:
        return pd.DataFrame(), warnings
    all_df = pd.concat(frames, ignore_index=True)
    return all_df.sort_values(["Opportunity Index", "Return / Day", "Protection Ratio"], ascending=[False, False, False]).reset_index(drop=True), warnings


def best_per_ticker(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    winners = []
    for symbol, group in df.groupby("Ticker", sort=False):
        group = group.sort_values(["Opportunity Index", "Balance Gap", "Return / Day"], ascending=[False, True, False])
        top_score = float(group.iloc[0]["Opportunity Index"])
        near = group[group["Opportunity Index"] >= top_score - EARNINGS_TIE_THRESHOLD]
        no_earn = near[~near["Has Earnings"]]
        winner = no_earn.sort_values(["Opportunity Index", "Balance Gap", "Return / Day"], ascending=[False, True, False]).iloc[0] if not no_earn.empty else group.iloc[0]
        winners.append(winner)
    return pd.DataFrame(winners).sort_values(["Opportunity Index", "Return / Day"], ascending=[False, False]).reset_index(drop=True)


def quote_option(symbol: str, expiry: str, contract: str, strike: float, option_type: str = "put") -> dict:
    ticker = yf.Ticker(symbol)
    stock_price = get_stock_price(ticker)
    chain, error = get_option_chain(ticker, symbol, expiry)
    if chain is None:
        return {"ok": False, "error": error, "stock_price": stock_price}
    frame = chain.puts if option_type.lower() == "put" else chain.calls
    if frame is None or frame.empty:
        return {"ok": False, "error": "empty option chain", "stock_price": stock_price}
    row = None
    if contract and "contractSymbol" in frame.columns:
        matches = frame[frame["contractSymbol"].astype(str) == str(contract)]
        if not matches.empty:
            row = matches.iloc[0]
    if row is None:
        temp = frame.copy()
        temp["strike"] = pd.to_numeric(temp["strike"], errors="coerce")
        temp = temp.dropna(subset=["strike"])
        if not temp.empty:
            idx = (temp["strike"] - float(strike)).abs().idxmin()
            row = temp.loc[idx]
    if row is None:
        return {"ok": False, "error": "contract not found", "stock_price": stock_price}
    bid = safe_float(row.get("bid"), np.nan)
    ask = safe_float(row.get("ask"), np.nan)
    last = safe_float(row.get("lastPrice"), np.nan)
    iv = safe_float(row.get("impliedVolatility"), np.nan)
    if np.isfinite(ask) and ask > 0:
        close_mark = ask
    elif np.isfinite(bid) and bid > 0 and np.isfinite(last) and last > 0:
        close_mark = max(bid, last)
    elif np.isfinite(last) and last > 0:
        close_mark = last
    else:
        close_mark = bid if np.isfinite(bid) else np.nan
    return {
        "ok": True, "stock_price": stock_price, "bid": bid, "ask": ask, "last": last,
        "close_mark": close_mark, "iv": iv,
        "contract": str(row.get("contractSymbol", contract)), "strike": safe_float(row.get("strike"), strike),
    }


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put_price(spot: float, strike: float, t_years: float, rate: float, sigma: float) -> float:
    if t_years <= 0:
        return max(strike - spot, 0.0)
    if sigma <= 0 or spot <= 0 or strike <= 0:
        return max(strike * math.exp(-rate * t_years) - spot, 0.0)
    vol_sqrt = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / vol_sqrt
    d2 = d1 - vol_sqrt
    return strike * math.exp(-rate * t_years) * norm_cdf(-d2) - spot * norm_cdf(-d1)


def expected_capture_fraction(position: dict, asof: date | None = None, rate: float = 0.04) -> float:
    """Constant-spot/constant-IV Black-Scholes baseline, normalized to entry theoretical value."""
    asof = asof or date.today()
    try:
        expiry = pd.Timestamp(position["expiry"]).date()
        entry_dte = int(position["dte_entry"])
        remaining = max((expiry - asof).days, 0)
        spot = float(position["stock_entry"])
        strike = float(position["strike"])
        sigma = safe_float(position.get("contract_iv_entry"), np.nan)
        if not np.isfinite(sigma) or sigma <= 0:
            sigma = safe_float(position.get("atm_iv_entry"), np.nan)
        if not np.isfinite(sigma) or sigma <= 0:
            return np.nan
        p0 = bs_put_price(spot, strike, max(entry_dte, 1) / 365.0, rate, sigma)
        pt = bs_put_price(spot, strike, remaining / 365.0, rate, sigma)
        if p0 <= 0:
            return np.nan
        return float(np.clip(1.0 - pt / p0, -5.0, 1.0))
    except Exception:
        return np.nan


def scan_calls_for_basis(symbol: str, adjusted_basis: float, min_dte: int = 14, max_dte: int = 45) -> pd.DataFrame:
    ticker = yf.Ticker(symbol)
    price = get_stock_price(ticker)
    expirations, error = get_option_expirations(ticker, symbol)
    if not expirations:
        raise ValueError(error or "No expirations")
    rows = []
    today = date.today()
    earnings_dates = get_earnings_dates(ticker)
    for expiry_str in expirations:
        try:
            expiry = pd.Timestamp(expiry_str).date()
        except Exception:
            continue
        dte = (expiry - today).days
        if dte < min_dte or dte > max_dte:
            continue
        chain, _ = get_option_chain(ticker, symbol, expiry_str)
        if chain is None or chain.calls is None or chain.calls.empty:
            continue
        for _, option in chain.calls.iterrows():
            strike = safe_float(option.get("strike"))
            bid = safe_float(option.get("bid"), 0.0)
            ask = safe_float(option.get("ask"), np.nan)
            if not np.isfinite(strike) or bid <= 0:
                continue
            if strike <= price or strike < adjusted_basis:
                continue
            premium = bid * 100.0
            basis_capital = adjusted_basis * 100.0
            return_per_day = (premium / basis_capital) / dte if basis_capital > 0 and dte > 0 else 0.0
            upside = (strike - price) / price if price > 0 else 0.0
            rows.append({
                "Ticker": symbol, "Stock Price": price, "Strike": strike, "Call": f"${strike:g}C",
                "Expiry": expiry_str, "DTE": dte, "Bid": bid, "Ask": ask,
                "Contract": str(option.get("contractSymbol", "")), "Premium Received": premium,
                "Return / Day": return_per_day, "Upside": upside,
                "Earnings in Period": earnings_for_window(earnings_dates, expiry, today)[0],
                "Earnings Known": any(d >= today for d in earnings_dates),
                "Next Earnings": next_earnings_date(earnings_dates, today),
                "Earnings Policy": "Allowed; covered calls are not earnings-filtered",
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    max_ret = max(float(df["Return / Day"].max()), 1e-12)
    max_up = max(float(df["Upside"].max()), 1e-12)
    df["Return Score"] = df["Return / Day"] / max_ret * 100.0
    df["Upside Score"] = df["Upside"] / max_up * 100.0
    df["Score"] = weighted_harmonic_mean(df["Return Score"], df["Upside Score"], 0.5)
    return df.sort_values(["Score", "Return / Day"], ascending=[False, False]).reset_index(drop=True)
