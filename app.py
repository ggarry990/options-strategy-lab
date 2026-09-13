from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import streamlit as st

from engine import (
    best_per_ticker,
    expected_capture_fraction,
    opportunity_band,
    quote_option,
    safe_float,
    scan_calls_for_basis,
    scan_universe,
)

from universe import (
    choose_option_scan_symbols,
    load_sp500_constituents,
    prescreen_underlyings,
)

APP_VERSION = "0.2"
DATA_DIR = Path(__file__).resolve().parent / "data"
STATE_PATH = DATA_DIR / "paper_state.json"
SNAPSHOT_PATH = DATA_DIR / "scan_history.csv"
STARTING_CAPITAL = 100_000.0
MODELS = {
    "A · Hold to Expiry": {"kind": "hold"},
    "B · 50% Winner": {"kind": "profit50"},
    "C · 50% + Time Exit": {"kind": "profit50_time"},
    "D10 · Redeploy +10%": {"kind": "redeploy", "edge": 0.10},
    "D25 · Redeploy +25%": {"kind": "redeploy", "edge": 0.25},
    "D50 · Redeploy +50%": {"kind": "redeploy", "edge": 0.50},
    "E · Redeploy + Loss Control": {"kind": "risk", "edge": 0.25},
}

st.set_page_config(page_title="Options Strategy Lab", page_icon="🧪", layout="wide")
st.markdown(
    """
<style>
.block-container {padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1500px;}
[data-testid="stMetricValue"] {font-size: 1.65rem;}
.small-note {font-size: 0.86rem; opacity: .78;}
@media (max-width: 768px) {
  .block-container {padding-left: .7rem; padding-right: .7rem;}
  h1 {font-size: 1.7rem !important;}
  h2 {font-size: 1.35rem !important;}
  [data-testid="stMetricValue"] {font-size: 1.35rem;}
}
</style>
""",
    unsafe_allow_html=True,
)


def new_model_state() -> dict[str, Any]:
    return {
        "starting_capital": STARTING_CAPITAL,
        "cash": STARTING_CAPITAL,
        "positions": [],
        "closed_trades": [],
        "events": [],
        "nav_history": [],
        "fees": 0.0,
        "assignments": 0,
        "last_run_date": None,
    }


def default_state() -> dict[str, Any]:
    return {
        "version": APP_VERSION,
        "created": datetime.now().isoformat(timespec="seconds"),
        "models": {name: new_model_state() for name in MODELS},
    }


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        return default_state()
    state.setdefault("version", APP_VERSION)
    state.setdefault("models", {})
    for name in MODELS:
        state["models"].setdefault(name, new_model_state())
        m = state["models"][name]
        for k, v in new_model_state().items():
            m.setdefault(k, v)
    return state


def load_state() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        return default_state()
    try:
        return normalize_state(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except Exception:
        return default_state()


def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def log_event(model: dict[str, Any], action: str, text: str, **fields) -> None:
    item = {"timestamp": datetime.now().isoformat(timespec="seconds"), "action": action, "detail": text}
    item.update(fields)
    model["events"].append(item)
    model["events"] = model["events"][-1000:]


def commission_for_contracts(contracts: int, per_contract: float) -> float:
    return max(int(contracts), 0) * float(per_contract)


def open_put(model: dict[str, Any], row: pd.Series, fee_per_contract: float, reason: str) -> bool:
    collateral = float(row["Cash Required"])
    premium = float(row["Premium Received"])
    if collateral > model["cash"] + 1e-9:
        return False
    fee = commission_for_contracts(1, fee_per_contract)
    model["cash"] -= collateral
    model["cash"] += premium - fee
    model["fees"] += fee
    pos = {
        "type": "short_put",
        "ticker": str(row["Ticker"]),
        "contract": str(row.get("Contract", "")),
        "strike": float(row["Strike"]),
        "expiry": str(row["Expiry"]),
        "entry_date": date.today().isoformat(),
        "dte_entry": int(row["DTE"]),
        "stock_entry": float(row["Stock Price"]),
        "entry_credit": float(row["Bid"]),
        "premium_received": premium,
        "collateral": collateral,
        "atm_iv_entry": safe_float(row.get("ATM IV"), np.nan),
        "contract_iv_entry": safe_float(row.get("Contract IV"), np.nan),
        "expected_move_entry": safe_float(row.get("Expected Move"), np.nan),
        "protection_entry": safe_float(row.get("Protection Ratio"), np.nan),
        "opp_index_entry": safe_float(row.get("Opportunity Index"), np.nan),
        "return_per_day_entry": safe_float(row.get("Return / Day"), np.nan),
        "earnings": str(row.get("Earnings in Period", "")),
        "time_exit_dte": 21 if int(row["DTE"]) > 21 else 7,
        "entry_fee": fee,
    }
    model["positions"].append(pos)
    log_event(model, "OPEN PUT", reason, ticker=pos["ticker"], strike=pos["strike"], expiry=pos["expiry"], credit=pos["entry_credit"], opportunity_index=pos["opp_index_entry"])
    return True


def open_call(model: dict[str, Any], stock: dict[str, Any], row: pd.Series, fee_per_contract: float, reason: str) -> bool:
    if any(p.get("type") == "short_call" and p.get("stock_id") == stock.get("stock_id") for p in model["positions"]):
        return False
    premium = float(row["Premium Received"])
    fee = commission_for_contracts(1, fee_per_contract)
    model["cash"] += premium - fee
    model["fees"] += fee
    call = {
        "type": "short_call",
        "ticker": stock["ticker"],
        "stock_id": stock["stock_id"],
        "contract": str(row.get("Contract", "")),
        "strike": float(row["Strike"]),
        "expiry": str(row["Expiry"]),
        "entry_date": date.today().isoformat(),
        "dte_entry": int(row["DTE"]),
        "entry_credit": float(row["Bid"]),
        "premium_received": premium,
        "entry_fee": fee,
    }
    model["positions"].append(call)
    log_event(model, "OPEN CALL", reason, ticker=call["ticker"], strike=call["strike"], expiry=call["expiry"], credit=call["entry_credit"])
    return True


def close_put(model: dict[str, Any], pos: dict[str, Any], mark: float, fee_per_contract: float, reason: str) -> None:
    fee = commission_for_contracts(1, fee_per_contract)
    exit_cost = max(float(mark), 0.0) * 100.0
    model["cash"] += float(pos["collateral"]) - exit_cost - fee
    model["fees"] += fee
    pnl = float(pos["premium_received"]) - exit_cost - float(pos.get("entry_fee", 0.0)) - fee
    days_held = max((date.today() - pd.Timestamp(pos["entry_date"]).date()).days, 0)
    closed = dict(pos)
    closed.update({"exit_date": date.today().isoformat(), "exit_mark": mark, "realized_pnl": pnl, "days_held": days_held, "exit_reason": reason})
    model["closed_trades"].append(closed)
    log_event(model, "CLOSE PUT", reason, ticker=pos["ticker"], strike=pos["strike"], expiry=pos["expiry"], exit_mark=mark, pnl=pnl)
    model["positions"].remove(pos)


def expire_or_assign_put(model: dict[str, Any], pos: dict[str, Any], stock_price: float) -> None:
    pnl = float(pos["premium_received"]) - float(pos.get("entry_fee", 0.0))
    days_held = max((date.today() - pd.Timestamp(pos["entry_date"]).date()).days, 0)
    if stock_price >= float(pos["strike"]):
        model["cash"] += float(pos["collateral"])
        closed = dict(pos)
        closed.update({"exit_date": date.today().isoformat(), "exit_mark": 0.0, "realized_pnl": pnl, "days_held": days_held, "exit_reason": "Expired OTM"})
        model["closed_trades"].append(closed)
        log_event(model, "EXPIRE PUT", "Expired OTM", ticker=pos["ticker"], strike=pos["strike"], pnl=pnl)
    else:
        stock_id = f"{pos['ticker']}-{date.today().isoformat()}-{len(model['positions'])}"
        stock = {
            "type": "stock",
            "stock_id": stock_id,
            "ticker": pos["ticker"],
            "shares": 100,
            "cost_basis": float(pos["strike"]),
            "adjusted_basis": float(pos["strike"]) - float(pos["entry_credit"]),
            "assigned_date": date.today().isoformat(),
            "source_put": pos["contract"],
        }
        model["positions"].append(stock)
        model["assignments"] += 1
        closed = dict(pos)
        closed.update({"exit_date": date.today().isoformat(), "exit_mark": max(float(pos["strike"]) - stock_price, 0.0), "realized_pnl": None, "days_held": days_held, "exit_reason": "Assigned"})
        model["closed_trades"].append(closed)
        log_event(model, "ASSIGN", "Put assigned; stock entered at strike and adjusted basis includes put premium", ticker=pos["ticker"], strike=pos["strike"], stock_price=stock_price, adjusted_basis=stock["adjusted_basis"])
    model["positions"].remove(pos)


def expire_call(model: dict[str, Any], call: dict[str, Any], stock_price: float) -> None:
    stock = next((p for p in model["positions"] if p.get("type") == "stock" and p.get("stock_id") == call.get("stock_id")), None)
    pnl_call = float(call["premium_received"]) - float(call.get("entry_fee", 0.0))
    if stock is not None and stock_price > float(call["strike"]):
        proceeds = float(call["strike"]) * int(stock.get("shares", 100))
        model["cash"] += proceeds
        stock_pnl = (float(call["strike"]) - float(stock["cost_basis"])) * int(stock.get("shares", 100))
        log_event(model, "CALLED AWAY", "Covered call assigned; shares sold at call strike", ticker=call["ticker"], strike=call["strike"], stock_pnl=stock_pnl, call_premium=pnl_call)
        model["positions"].remove(stock)
    else:
        log_event(model, "EXPIRE CALL", "Covered call expired OTM", ticker=call["ticker"], strike=call["strike"], premium_kept=pnl_call)
    closed = dict(call)
    closed.update({"exit_date": date.today().isoformat(), "realized_pnl": pnl_call, "exit_reason": "Called away" if stock is None else "Expired/settled"})
    model["closed_trades"].append(closed)
    model["positions"].remove(call)


def candidate_rows_for_cash(scan: pd.DataFrame, model: dict[str, Any], max_positions: int, concentration_limit: float) -> list[pd.Series]:
    if scan.empty:
        return []
    existing_tickers = {p.get("ticker") for p in model["positions"] if p.get("type") in ("short_put", "stock")}
    current_puts = sum(1 for p in model["positions"] if p.get("type") == "short_put")
    capacity = max(max_positions - current_puts, 0)
    if capacity <= 0:
        return []
    rows = []
    seen = set()
    max_collateral = float(model.get("starting_capital", STARTING_CAPITAL)) * float(concentration_limit)
    for _, row in scan.iterrows():
        ticker = str(row["Ticker"])
        if ticker in seen or ticker in existing_tickers:
            continue
        collateral = float(row["Cash Required"])
        if collateral > model["cash"] or collateral > max_collateral:
            continue
        rows.append(row)
        seen.add(ticker)
        if len(rows) >= capacity:
            break
    return rows


def find_current_row(scan: pd.DataFrame, pos: dict[str, Any]) -> pd.Series | None:
    if scan.empty:
        return None
    if pos.get("contract"):
        m = scan[scan["Contract"].astype(str) == str(pos["contract"])]
        if not m.empty:
            return m.iloc[0]
    m = scan[(scan["Ticker"] == pos["ticker"]) & (scan["Expiry"].astype(str) == str(pos["expiry"]))]
    if not m.empty:
        idx = (pd.to_numeric(m["Strike"], errors="coerce") - float(pos["strike"])).abs().idxmin()
        return m.loc[idx]
    return None


def best_replacement(scan: pd.DataFrame, pos: dict[str, Any], model: dict[str, Any]) -> pd.Series | None:
    if scan.empty:
        return None
    # Avoid simply rolling into the exact same contract.
    candidates = scan.copy()
    if "Contract" in candidates.columns:
        candidates = candidates[candidates["Contract"].astype(str) != str(pos.get("contract", ""))]
    if candidates.empty:
        return None
    # Must be fundable after releasing this put's collateral and paying its close mark; exact cash is checked later.
    return candidates.iloc[0]


def calculate_nav(model: dict[str, Any], quote_cache: dict[str, dict]) -> tuple[float, float, float, float]:
    nav = float(model["cash"])
    reserved = 0.0
    stock_value = 0.0
    option_liability = 0.0
    for p in model["positions"]:
        typ = p.get("type")
        if typ == "short_put":
            reserved += float(p.get("collateral", 0.0))
            q = quote_cache.get(p.get("contract", ""), {})
            mark = safe_float(q.get("close_mark"), 0.0)
            option_liability += max(mark, 0.0) * 100.0
        elif typ == "stock":
            q = quote_cache.get(f"stock:{p['ticker']}", {})
            px = safe_float(q.get("stock_price"), safe_float(p.get("cost_basis"), 0.0))
            stock_value += px * int(p.get("shares", 100))
        elif typ == "short_call":
            q = quote_cache.get(p.get("contract", ""), {})
            mark = safe_float(q.get("close_mark"), 0.0)
            option_liability += max(mark, 0.0) * 100.0
    nav += reserved + stock_value - option_liability
    return nav, reserved, stock_value, option_liability


def collect_quotes(state: dict[str, Any]) -> tuple[dict[str, dict], list[str]]:
    quote_cache: dict[str, dict] = {}
    warnings = []
    seen = set()
    for model in state["models"].values():
        for p in model["positions"]:
            typ = p.get("type")
            if typ in ("short_put", "short_call"):
                key = p.get("contract") or f"{p['ticker']}:{p['expiry']}:{p['strike']}:{typ}"
                if key in seen:
                    continue
                seen.add(key)
                try:
                    q = quote_option(p["ticker"], p["expiry"], p.get("contract", ""), p["strike"], "put" if typ == "short_put" else "call")
                    quote_cache[p.get("contract", key)] = q
                    quote_cache[f"stock:{p['ticker']}"] = {"stock_price": q.get("stock_price")}
                    if not q.get("ok"):
                        warnings.append(f"{p['ticker']} {p['expiry']} {p['strike']}: {q.get('error')}")
                except Exception as exc:
                    warnings.append(f"{p['ticker']} quote: {exc}")
            elif typ == "stock":
                stock_key = f"stock:{p['ticker']}"
                if stock_key in quote_cache:
                    continue
                try:
                    # Reuse a tiny option quote helper path if covered call exists, otherwise yfinance via call scanner later.
                    calls = scan_calls_for_basis(p["ticker"], float(p.get("adjusted_basis", p.get("cost_basis", 0.0))), 14, 45)
                    if not calls.empty:
                        quote_cache[stock_key] = {"stock_price": float(calls.iloc[0]["Stock Price"])}
                except Exception as exc:
                    warnings.append(f"{p['ticker']} stock quote: {exc}")
    return quote_cache, warnings


def forward_metrics(pos: dict[str, Any], q: dict, scan: pd.DataFrame) -> dict[str, float]:
    mark = safe_float(q.get("close_mark"), np.nan)
    bid = safe_float(q.get("bid"), np.nan)
    stock = safe_float(q.get("stock_price"), np.nan)
    entry = float(pos["entry_credit"])
    expiry = pd.Timestamp(pos["expiry"]).date()
    dte = max((expiry - date.today()).days, 0)
    captured = (entry - mark) / entry if entry > 0 and np.isfinite(mark) else np.nan
    expected_capture = expected_capture_fraction(pos)
    ahead = captured - expected_capture if np.isfinite(captured) and np.isfinite(expected_capture) else np.nan
    uncaptured = max(mark, 0.0) * 100.0 if np.isfinite(mark) else np.nan
    current_rpd = ((max(mark, 0.0) * 100.0) / float(pos["collateral"]) / dte) if np.isfinite(mark) and dte > 0 else 0.0
    current_row = find_current_row(scan, pos)
    current_opp = safe_float(current_row.get("Opportunity Index"), np.nan) if current_row is not None else np.nan
    current_protection = safe_float(current_row.get("Protection Ratio"), np.nan) if current_row is not None else np.nan
    return {
        "mark": mark, "bid": bid, "stock": stock, "dte": dte, "captured": captured,
        "expected_capture": expected_capture, "ahead": ahead, "uncaptured": uncaptured,
        "remaining_return_per_day": current_rpd, "current_opp": current_opp,
        "current_protection": current_protection,
    }


def evaluate_model(name: str, model: dict[str, Any], scan: pd.DataFrame, quote_cache: dict[str, dict],
                   fee_per_contract: float, max_positions: int, concentration_limit: float, force: bool = False) -> list[str]:
    actions: list[str] = []
    today_iso = date.today().isoformat()
    if model.get("last_run_date") == today_iso and not force:
        return ["Already evaluated today. Enable Force re-run to evaluate again."]

    cfg = MODELS[name]
    kind = cfg["kind"]

    # 1) Settle expirations and manage open options.
    for pos in list(model["positions"]):
        typ = pos.get("type")
        if typ == "short_put":
            q = quote_cache.get(pos.get("contract", ""), {})
            if not q.get("ok"):
                actions.append(f"{pos['ticker']} put: quote unavailable; no action")
                continue
            metrics = forward_metrics(pos, q, scan)
            if metrics["dte"] <= 0:
                expire_or_assign_put(model, pos, float(metrics["stock"]))
                actions.append(f"{pos['ticker']} put settled at expiry")
                continue

            close_reason = None
            if kind == "profit50" and np.isfinite(metrics["captured"]) and metrics["captured"] >= 0.50:
                close_reason = f"50% winner: {metrics['captured']:.1%} captured"
            elif kind == "profit50_time":
                if np.isfinite(metrics["captured"]) and metrics["captured"] >= 0.50:
                    close_reason = f"50% winner: {metrics['captured']:.1%} captured"
                elif metrics["dte"] <= int(pos.get("time_exit_dte", 21)):
                    close_reason = f"Time exit at {metrics['dte']} DTE"
            elif kind in ("redeploy", "risk"):
                if kind == "risk" and np.isfinite(metrics["mark"]) and metrics["mark"] >= 3.0 * float(pos["entry_credit"]):
                    close_reason = f"Loss control: buyback mark {metrics['mark']:.2f} >= 3× entry credit {pos['entry_credit']:.2f}"
                else:
                    replacement = best_replacement(scan, pos, model)
                    captured_ok = np.isfinite(metrics["captured"]) and metrics["captured"] >= 0.25
                    if replacement is not None and captured_ok:
                        alt_rpd = float(replacement["Return / Day"])
                        current_rpd = max(float(metrics["remaining_return_per_day"]), 1e-12)
                        edge = alt_rpd / current_rpd - 1.0
                        alt_protection = safe_float(replacement.get("Protection Ratio"), np.nan)
                        protection_ok = (not np.isfinite(metrics["current_protection"])) or (np.isfinite(alt_protection) and alt_protection >= metrics["current_protection"])
                        required_edge = float(cfg.get("edge", 0.25))
                        if edge >= required_edge and protection_ok:
                            close_reason = (
                                f"Redeploy: {metrics['captured']:.1%} captured; alt return/day advantage {edge:.0%} "
                                f">= {required_edge:.0%}; replacement protection acceptable"
                            )
            if close_reason:
                close_put(model, pos, float(metrics["mark"]), fee_per_contract, close_reason)
                actions.append(f"CLOSE {pos['ticker']} {pos['strike']:g}P — {close_reason}")
            else:
                ahead_txt = f", ahead {metrics['ahead']:.1%}" if np.isfinite(metrics["ahead"]) else ""
                actions.append(f"HOLD {pos['ticker']} {pos['strike']:g}P — captured {metrics['captured']:.1%}{ahead_txt}, {metrics['dte']} DTE")

        elif typ == "short_call":
            q = quote_cache.get(pos.get("contract", ""), {})
            if q.get("ok"):
                dte = max((pd.Timestamp(pos["expiry"]).date() - date.today()).days, 0)
                if dte <= 0:
                    expire_call(model, pos, float(q["stock_price"]))
                    actions.append(f"{pos['ticker']} covered call settled")

    # 2) Open covered calls on assigned stock when possible. Calls are held to expiration in every model.
    for stock in [p for p in list(model["positions"]) if p.get("type") == "stock"]:
        if any(p.get("type") == "short_call" and p.get("stock_id") == stock.get("stock_id") for p in model["positions"]):
            continue
        try:
            calls = scan_calls_for_basis(stock["ticker"], float(stock["adjusted_basis"]), 14, 45)
            if not calls.empty:
                row = calls.iloc[0]
                if open_call(model, stock, row, fee_per_contract, "Highest covered-call score above adjusted basis"):
                    actions.append(f"OPEN CALL {stock['ticker']} {row['Strike']:g}C {row['Expiry']} @ {row['Bid']:.2f}")
            else:
                actions.append(f"{stock['ticker']} stock: no qualifying covered call today")
        except Exception as exc:
            actions.append(f"{stock['ticker']} stock: call scan failed ({exc})")

    # 3) Deploy free collateral into the best current put candidates.
    for row in candidate_rows_for_cash(scan, model, max_positions, concentration_limit):
        reason = f"Highest eligible Opportunity Index {row['Opportunity Index']:.1f} ({opportunity_band(row['Opportunity Index'])})"
        if open_put(model, row, fee_per_contract, reason):
            actions.append(f"OPEN {row['Ticker']} {row['Strike']:g}P {row['Expiry']} @ {row['Bid']:.2f} · OI {row['Opportunity Index']:.1f}")

    model["last_run_date"] = today_iso
    return actions or ["No action"]


def summary_table(state: dict[str, Any], quote_cache: dict[str, dict]) -> pd.DataFrame:
    rows = []
    for name, model in state["models"].items():
        nav, reserved, stock_value, liability = calculate_nav(model, quote_cache)
        start = float(model.get("starting_capital", STARTING_CAPITAL))
        hist = model.get("nav_history", [])
        prior_navs = [safe_float(x.get("nav"), np.nan) for x in hist if isinstance(x, dict)] + [nav]
        prior_navs = [x for x in prior_navs if np.isfinite(x)]
        peak = max(prior_navs) if prior_navs else nav
        dd = nav / peak - 1.0 if peak > 0 else 0.0
        model["nav_history"].append({"date": date.today().isoformat(), "nav": nav})
        # one point per date
        dedup = {}
        for x in model["nav_history"]:
            if isinstance(x, dict) and x.get("date"):
                dedup[x["date"]] = x
        model["nav_history"] = list(dedup.values())[-1000:]
        closed = [x for x in model.get("closed_trades", []) if x.get("type") in ("short_put", "short_call")]
        pnls = [safe_float(x.get("realized_pnl"), np.nan) for x in closed]
        valid_pnls = [x for x in pnls if np.isfinite(x)]
        wins = sum(x > 0 for x in valid_pnls)
        days = [safe_float(x.get("days_held"), np.nan) for x in closed if x.get("type") == "short_put"]
        valid_days = [x for x in days if np.isfinite(x)]
        rows.append({
            "Model": name,
            "NAV": nav,
            "Return": nav / start - 1.0,
            "Cash": float(model["cash"]),
            "Open Collateral": reserved,
            "Stock Value": stock_value,
            "Option Liability": liability,
            "Open Positions": len(model["positions"]),
            "Closed Option Trades": len(valid_pnls),
            "Win Rate": wins / len(valid_pnls) if valid_pnls else np.nan,
            "Assignments": int(model.get("assignments", 0)),
            "Avg Days Held": float(np.mean(valid_days)) if valid_days else np.nan,
            "Fees": float(model.get("fees", 0.0)),
            "Current Drawdown": dd,
        })
    return pd.DataFrame(rows)


def snapshot_scan(winners: pd.DataFrame) -> None:
    if winners.empty:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = winners.copy()
    out.insert(0, "Timestamp", datetime.now().isoformat(timespec="seconds"))
    keep = ["Timestamp", "Ticker", "Stock Price", "Strike", "Expiry", "DTE", "Bid", "Ask", "Cash Required", "Return / Day", "$ / Day", "Cushion", "Expected Move", "Protection Ratio", "Return Score", "Protection Score", "Opportunity Index", "Opportunity Level", "Earnings in Period"]
    out = out[[c for c in keep if c in out.columns]]
    if SNAPSHOT_PATH.exists():
        try:
            old = pd.read_csv(SNAPSHOT_PATH)
            out = pd.concat([old, out], ignore_index=True)
        except Exception:
            pass
    out.tail(10000).to_csv(SNAPSHOT_PATH, index=False)


# ---------------- UI ----------------
if "paper_state" not in st.session_state:
    st.session_state.paper_state = load_state()
if "live_scan" not in st.session_state:
    st.session_state.live_scan = pd.DataFrame()
if "scan_warnings" not in st.session_state:
    st.session_state.scan_warnings = []
if "last_scan_time" not in st.session_state:
    st.session_state.last_scan_time = None
if "prescreen_df" not in st.session_state:
    st.session_state.prescreen_df = pd.DataFrame()
if "universe_info" not in st.session_state:
    st.session_state.universe_info = {}

st.title("Options Strategy Lab")
st.caption("A separate $100,000 paper-trading lab using the same Opportunity Index logic as your wheel dashboard. It can scan the current S&P 500 plus custom names, then let competing paper-management models trade the best scored opportunities.")

with st.sidebar:
    st.header("Live scan settings")
    universe_mode = st.selectbox(
        "Universe",
        ["S&P 500 + custom", "Custom watchlist only"],
        index=0,
        help="S&P 500 mode evaluates the current index universe first, then pulls option chains for eligible names. Custom names are always added.",
    )
    custom_raw = st.text_area(
        "Custom / non-S&P names",
        "SPCX, SHOP",
        height=75,
        help="Useful for names outside the S&P 500. Comma or line separated.",
    )
    custom_symbols = tuple(dict.fromkeys(x.strip().upper().replace('.', '-') for x in custom_raw.replace("\n", ",").split(",") if x.strip()))
    broad_scan_mode = st.radio(
        "S&P option-chain depth",
        ["Fast broad scan", "Full eligible S&P 500"],
        horizontal=False,
        help="Fast mode examines every S&P underlying first, then option-scores the top proxy candidates. Full mode option-scores every constituent that can plausibly fit the collateral rules; it is much slower and Yahoo may throttle it.",
        disabled=universe_mode == "Custom watchlist only",
    )
    fast_limit = st.slider("Fast mode: option-chain candidates", 25, 250, 120, 5, disabled=universe_mode == "Custom watchlist only")
    max_otm_prescreen = st.slider(
        "Affordability pre-screen: allow up to % OTM", 10, 70, 50, 5,
        help="This only prevents impossible/very-deep-OTM names from consuming Yahoo requests. It does not change the Opportunity Index.",
        disabled=universe_mode == "Custom watchlist only",
    )
    min_underlying_volume = st.number_input(
        "Min 20D stock volume (S&P pre-screen)", 0, 20_000_000, 250_000, step=50_000,
        disabled=universe_mode == "Custom watchlist only",
    )
    c1, c2 = st.columns(2)
    min_dte = c1.number_input("Min DTE", 1, 120, 14)
    max_dte = c2.number_input("Max DTE", 1, 180, 60)
    c3, c4 = st.columns(2)
    min_cash = c3.number_input("Min collateral", 0, 100000, 3000, step=500)
    max_cash = c4.number_input("Max collateral", 1000, 100000, 30000, step=500)
    min_cushion = st.number_input("Minimum cushion %", 0.0, 50.0, 0.0, step=0.5)
    return_weight = st.slider("Return weight %", 0, 100, 30, 5, help="Protection weight is the remainder.")
    st.divider()
    st.header("Paper portfolio settings")
    max_positions = st.slider("Max open puts per model", 1, 10, 5)
    concentration_limit = st.slider("Max collateral per underlying", 0.10, 1.00, 0.30, 0.05)
    fee_per_contract = st.number_input("Commission + fee / contract", 0.0, 10.0, 1.00, 0.25)
    force_run = st.checkbox("Force re-run today", False, help="Normally the paper engine only makes one decision set per calendar day.")

    st.divider()
    state_json = json.dumps(st.session_state.paper_state, indent=2, default=str)
    st.download_button("Download paper-state backup", state_json, file_name=f"paper_state_{date.today().isoformat()}.json", mime="application/json", use_container_width=True)
    restore = st.file_uploader("Restore paper-state backup", type=["json"])
    if restore is not None and st.button("Restore uploaded state", use_container_width=True):
        try:
            st.session_state.paper_state = normalize_state(json.load(restore))
            save_state(st.session_state.paper_state)
            st.success("State restored.")
            st.rerun()
        except Exception as exc:
            st.error(f"Restore failed: {exc}")
    if st.button("Reset all paper portfolios to $100,000", type="secondary", use_container_width=True):
        st.session_state.paper_state = default_state()
        save_state(st.session_state.paper_state)
        st.success("Paper portfolios reset.")
        st.rerun()

btn1, btn2 = st.columns([1, 1])
with btn1:
    refresh_clicked = st.button("Refresh Live Market", type="primary", use_container_width=True)
with btn2:
    run_clicked = st.button("Run Today's Paper Decisions", use_container_width=True, disabled=st.session_state.live_scan.empty)

if refresh_clicked:
    st.cache_data.clear()
    warnings = []
    if universe_mode == "Custom watchlist only":
        option_symbols = custom_symbols
        st.session_state.prescreen_df = pd.DataFrame()
        st.session_state.universe_info = {"source": "custom", "constituents": len(option_symbols), "prescreened": len(option_symbols), "option_scanned": len(option_symbols)}
    else:
        with st.spinner("Loading the current S&P 500 universe and doing the cheap underlying pre-screen..."):
            sp500, source_label, source_warning = load_sp500_constituents()
            if source_warning:
                warnings.append(source_warning)
            sp_symbols = tuple(sp500["Symbol"].astype(str).tolist())
            pre, pre_warnings = prescreen_underlyings(
                sp_symbols, float(min_cash), float(max_cash), float(max_otm_prescreen), float(min_underlying_volume)
            )
            warnings.extend(pre_warnings)
            st.session_state.prescreen_df = pre
            option_symbols = choose_option_scan_symbols(pre, broad_scan_mode, int(fast_limit), custom_symbols)
            st.session_state.universe_info = {
                "source": source_label,
                "constituents": len(sp_symbols),
                "prescreened": len(pre),
                "option_scanned": len(option_symbols),
                "mode": broad_scan_mode,
            }
    if not option_symbols:
        st.session_state.live_scan = pd.DataFrame()
        st.session_state.scan_warnings = warnings + ["No symbols survived the universe/pre-screen settings."]
        st.session_state.last_scan_time = datetime.now().isoformat(timespec="seconds")
    else:
        with st.spinner(f"Option-scoring {len(option_symbols)} names across {int(min_dte)}–{int(max_dte)} DTE. Full S&P scans can take several minutes..."):
            scan, scan_warnings = scan_universe(option_symbols, int(min_dte), int(max_dte), float(min_cash), float(max_cash), float(min_cushion), int(return_weight))
            warnings.extend(scan_warnings)
            winners = best_per_ticker(scan)
            st.session_state.live_scan = winners
            st.session_state.scan_warnings = warnings
            st.session_state.last_scan_time = datetime.now().isoformat(timespec="seconds")
            snapshot_scan(winners)

# Quotes for portfolio NAV are refreshed on page load only when there are positions.
quote_cache = {}
quote_warnings = []
if any(m.get("positions") for m in st.session_state.paper_state["models"].values()):
    try:
        quote_cache, quote_warnings = collect_quotes(st.session_state.paper_state)
    except Exception as exc:
        quote_warnings = [str(exc)]

# Run model actions using current scan + current quotes.
action_results = {}
if run_clicked:
    if st.session_state.live_scan.empty:
        st.error("Refresh the live market first.")
    else:
        if not quote_cache:
            quote_cache, quote_warnings = collect_quotes(st.session_state.paper_state)
        for name, model in st.session_state.paper_state["models"].items():
            action_results[name] = evaluate_model(name, model, st.session_state.live_scan, quote_cache, float(fee_per_contract), int(max_positions), float(concentration_limit), bool(force_run))
        save_state(st.session_state.paper_state)
        # Pull quotes again after entries/exits for a cleaner NAV snapshot where possible.
        try:
            quote_cache, quote_warnings = collect_quotes(st.session_state.paper_state)
        except Exception:
            pass

scan_tab, models_tab, actions_tab, history_tab, method_tab = st.tabs(["1 · Today's Opportunities", "2 · Model Scoreboard", "3 · Today's Actions", "4 · History", "5 · Rules"])

with scan_tab:
    st.subheader("Today's best live opportunities")
    if st.session_state.last_scan_time:
        st.caption(f"Last refreshed: {st.session_state.last_scan_time}. Refreshing market data does not place or change paper trades.")
    if st.session_state.universe_info:
        ui = st.session_state.universe_info
        a,b,c,d = st.columns(4)
        a.metric("Universe names", ui.get("constituents", 0))
        b.metric("Passed stock pre-screen", ui.get("prescreened", 0))
        c.metric("Option chains attempted", ui.get("option_scanned", 0))
        d.metric("Scored opportunities", len(st.session_state.live_scan))
        st.caption(f"Universe source: {ui.get('source','')}. Fast mode uses a transparent speed proxy only to choose which chains to request; the Opportunity Index itself is unchanged. Full eligible mode does not use that proxy to exclude candidates.")
    if st.session_state.live_scan.empty:
        st.info("Press Refresh Live Market to scan the current option chains.")
    else:
        show = st.session_state.live_scan.copy()
        show["Return / Day %"] = show["Return / Day"] * 100.0
        show["Cushion %"] = show["Cushion"] * 100.0
        show["Expected Move %"] = show["Expected Move"] * 100.0
        cols = ["Opportunity Index", "Opportunity Level", "Ticker", "Stock Price", "Put", "Expiry", "DTE", "Bid", "Ask", "Cash Required", "Premium Received", "Return / Day %", "$ / Day", "Cushion %", "Expected Move %", "Protection Ratio", "Return Score", "Protection Score", "Earnings in Period"]
        st.dataframe(show[[c for c in cols if c in show.columns]], hide_index=True, use_container_width=True, column_config={
            "Opportunity Index": st.column_config.NumberColumn(format="%.1f"),
            "Stock Price": st.column_config.NumberColumn(format="$%.2f"),
            "Bid": st.column_config.NumberColumn(format="$%.2f"),
            "Ask": st.column_config.NumberColumn(format="$%.2f"),
            "Cash Required": st.column_config.NumberColumn(format="$%.0f"),
            "Premium Received": st.column_config.NumberColumn(format="$%.0f"),
            "Return / Day %": st.column_config.NumberColumn(format="%.3f%%"),
            "$ / Day": st.column_config.NumberColumn(format="$%.2f"),
            "Cushion %": st.column_config.NumberColumn(format="%.2f%%"),
            "Expected Move %": st.column_config.NumberColumn(format="%.2f%%"),
            "Protection Ratio": st.column_config.NumberColumn(format="%.2fx"),
            "Return Score": st.column_config.NumberColumn(format="%.1f"),
            "Protection Score": st.column_config.NumberColumn(format="%.1f"),
        })
        best = show.iloc[0]
        st.success(f"Current #1: {best['Ticker']} {best['Put']} · {best['Expiry']} · Opportunity Index {best['Opportunity Index']:.1f} · {best['Return / Day']*100:.3f}%/day · protection {best['Protection Ratio']:.2f}× expected move")
    if not st.session_state.prescreen_df.empty:
        with st.expander("S&P 500 underlying pre-screen details"):
            pre_show = st.session_state.prescreen_df.copy()
            for c in ("HV30", "20D Return", "5D Return"):
                if c in pre_show.columns:
                    pre_show[c] = pre_show[c] * 100.0
            st.dataframe(pre_show.head(300), hide_index=True, use_container_width=True)
    if st.session_state.scan_warnings:
        with st.expander(f"Data warnings ({len(st.session_state.scan_warnings)})"):
            for w in st.session_state.scan_warnings:
                st.write("•", w)

with models_tab:
    st.subheader("$100,000 model comparison")
    scoreboard = summary_table(st.session_state.paper_state, quote_cache)
    save_state(st.session_state.paper_state)
    if not scoreboard.empty:
        display = scoreboard.copy()
        display["Return %"] = display["Return"] * 100.0
        display["Win Rate %"] = display["Win Rate"] * 100.0
        display["Drawdown %"] = display["Current Drawdown"] * 100.0
        cols = ["Model", "NAV", "Return %", "Cash", "Open Collateral", "Stock Value", "Option Liability", "Open Positions", "Closed Option Trades", "Win Rate %", "Assignments", "Avg Days Held", "Fees", "Drawdown %"]
        st.dataframe(display[cols], hide_index=True, use_container_width=True, column_config={
            "NAV": st.column_config.NumberColumn(format="$%.0f"),
            "Return %": st.column_config.NumberColumn(format="%.2f%%"),
            "Cash": st.column_config.NumberColumn(format="$%.0f"),
            "Open Collateral": st.column_config.NumberColumn(format="$%.0f"),
            "Stock Value": st.column_config.NumberColumn(format="$%.0f"),
            "Option Liability": st.column_config.NumberColumn(format="$%.0f"),
            "Win Rate %": st.column_config.NumberColumn(format="%.1f%%"),
            "Avg Days Held": st.column_config.NumberColumn(format="%.1f"),
            "Fees": st.column_config.NumberColumn(format="$%.0f"),
            "Drawdown %": st.column_config.NumberColumn(format="%.2f%%"),
        })

    st.markdown("#### Open positions")
    pos_rows = []
    for name, model in st.session_state.paper_state["models"].items():
        for p in model["positions"]:
            row = {"Model": name, "Type": p.get("type"), "Ticker": p.get("ticker")}
            if p.get("type") in ("short_put", "short_call"):
                q = quote_cache.get(p.get("contract", ""), {})
                row.update({"Strike": p.get("strike"), "Expiry": p.get("expiry"), "Entry Credit": p.get("entry_credit"), "Current Buyback": q.get("close_mark"), "Entry OI": p.get("opp_index_entry")})
                if p.get("type") == "short_put" and q.get("ok"):
                    met = forward_metrics(p, q, st.session_state.live_scan)
                    row.update({"Captured %": met["captured"] * 100 if np.isfinite(met["captured"]) else np.nan, "Ahead pts": met["ahead"] * 100 if np.isfinite(met["ahead"]) else np.nan, "DTE": met["dte"], "Remaining $/day": met["uncaptured"] / met["dte"] if met["dte"] > 0 and np.isfinite(met["uncaptured"]) else np.nan})
            elif p.get("type") == "stock":
                row.update({"Strike": p.get("adjusted_basis"), "Expiry": "", "Entry Credit": "", "Current Buyback": "", "Entry OI": ""})
            pos_rows.append(row)
    if pos_rows:
        st.dataframe(pd.DataFrame(pos_rows), hide_index=True, use_container_width=True)
    else:
        st.info("No paper positions yet. Refresh the market, then Run Today's Paper Decisions.")

with actions_tab:
    st.subheader("Today's model decisions")
    if action_results:
        for name in MODELS:
            with st.expander(name, expanded=True):
                for a in action_results.get(name, ["No action"]):
                    st.write("•", a)
    else:
        st.info("Run Today's Paper Decisions after refreshing the market to see each model's actual action.")
    if quote_warnings:
        with st.expander(f"Portfolio quote warnings ({len(quote_warnings)})"):
            for w in quote_warnings:
                st.write("•", w)

with history_tab:
    st.subheader("Paper-trade and scan history")
    model_choice = st.selectbox("Model", list(MODELS.keys()))
    m = st.session_state.paper_state["models"][model_choice]
    if m.get("events"):
        st.dataframe(pd.DataFrame(m["events"])[::-1], hide_index=True, use_container_width=True)
    else:
        st.info("No model events yet.")
    if SNAPSHOT_PATH.exists():
        try:
            h = pd.read_csv(SNAPSHOT_PATH)
            st.markdown("#### Live-scan snapshots")
            st.dataframe(h.tail(300).iloc[::-1], hide_index=True, use_container_width=True)
        except Exception:
            pass

with method_tab:
    st.subheader("What each model is testing")
    st.markdown(
        """
**A · Hold to Expiry** — opens the highest eligible Opportunity Index puts and never closes a put early. Expiry either releases collateral or creates assigned stock.

**B · 50% Winner** — closes a short put once at least 50% of the entry premium has been captured, then the freed collateral can be redeployed into the current best candidate.

**C · 50% + Time Exit** — same as B, but also exits at 21 DTE for positions originally opened above 21 DTE; short-dated entries use 7 DTE. This deliberately tests whether avoiding late-expiry risk improves the portfolio.

**D10 / D25 / D50 · Opportunity Redeployment** — a position becomes reviewable after 25% premium capture. It closes only when the current best alternative offers at least 10%, 25%, or 50% more *forward return/day* and has protection at least as good as the current position. The +35-point Ahead-of-Schedule figure is displayed as a diagnostic; it is **not assumed to be optimal**.

**E · Redeploy + Loss Control** — D25 plus a wide loss exit when the buyback mark reaches 3× the entry credit, equivalent to a loss of about 200% of the original credit before fees. This tests whether explicit loss control improves risk-adjusted results.

For assigned stock, every model uses the same covered-call rule: select the highest-scoring OTM call with strike at or above adjusted stock basis and hold that call to expiration. This keeps the wheel treatment consistent across models.

**Execution assumptions:** new short options use the displayed bid; buy-to-close uses ask when available; commissions/fees are included separately. This is paper trading, not a claim of executable fills. Yahoo/yfinance data can be delayed or unavailable, so a failed quote produces no paper action rather than fabricated data.

**Broad-universe scan:** in S&P 500 mode, every constituent is first checked with cheap underlying data. The pre-screen removes names that cannot plausibly fit the collateral range (within the chosen maximum OTM depth) and optionally very low-stock-volume names. **Full eligible S&P 500** then requests options for every surviving constituent. **Fast broad scan** ranks the survivors by a transparent realized-volatility/downside/liquidity proxy and only requests the top N chains to reduce Yahoo throttling; this proxy is not used in the Opportunity Index. Custom names such as SHOP/SPCX are added separately.
        """
    )
    st.warning("Forward paper results need a meaningful sample before they support any real-money decision. A model can look best for weeks simply because market regime favored it.")

st.caption(f"Options Strategy Lab v{APP_VERSION} · Data: yfinance/Yahoo Finance · State is stored locally by the app. On Streamlit Cloud, use the downloadable state backup because container storage is not guaranteed to be permanent.")
