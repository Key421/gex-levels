"""
Daily GEX Levels — local web page
---------------------------------
Pulls Cboe's delayed options chain (≈15 min delay) — or, if Cboe is down/blank, Yahoo Finance's
chain with gamma computed from its IV — computes gamma exposure by strike,
finds zero gamma from a full gamma profile, and shows a copy-ready string for the
"Daily GEX Levels" Pine indicator.

String format:  @YYYY-MM-DDTHHMM,price:Label:netGEX($mn),price:Label:netGEX,...
  - the @date/time lets the indicator warn you when levels are stale
  - the netGEX number lets the indicator scale line thickness by size

Easiest:  double-click start_gex.bat (installs packages, starts the page, opens your browser)
Manual:   py -m pip install -r requirements.txt   then   py gex_page.py

Auto-refresh: leave the page open. It re-pulls levels at the scheduled times
(default 8:30 pre-market, 9:46 open, 12:30 midday ET), copies the string, and notifies you.
"""
import json
import re
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

VERSION = "v9 · Yahoo backup feed"
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
SYMBOLS = {"SPX": "_SPX", "NDX": "_NDX", "SPY": "SPY", "QQQ": "QQQ"}
NY = ZoneInfo("America/New_York")
RISK_FREE = 0.04          # annual rate used in the gamma profile
CACHE_SECONDS = 60
_cache: dict[str, tuple[float, dict]] = {}
# Cboe's CDN rejects bare "Mozilla/5.0" requests with 403; send normal browser headers.
CBOE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.cboe.com",
    "Referer": "https://www.cboe.com/",
}
MAX_GAMMA_CENTER_PCT = 4.0   # gamma-weighted strike centre this far from spot = Cboe greeks mid-refresh (junk)

app = FastAPI(title="GEX Levels")


def fetch_chain(sym: str) -> dict:
    now = time.time()
    if sym in _cache and now - _cache[sym][0] < CACHE_SECONDS:
        return _cache[sym][1]
    r = requests.get(CBOE_URL.format(sym=SYMBOLS[sym]), timeout=30, headers=CBOE_HEADERS)
    r.raise_for_status()
    j = r.json()
    data = j["data"]
    # Cboe's top-level "timestamp" is UTC (e.g. 14:53 at 9:53 ET) — convert to ET wall-clock for parse_cboe_ts().
    ts_utc = parse_cboe_ts(j.get("timestamp") or data.get("timestamp"))
    data["_root_ts"] = (ts_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(NY).strftime("%Y-%m-%d %H:%M:%S")
                        if ts_utc else None)
    data["_source"] = "Cboe"
    _cache[sym] = (now, data)
    return data


# ---------------- Yahoo Finance backup chain ----------------
# Yahoo gives open interest, volume and IV per contract but no greeks, so gamma is computed here
# with the same Black-Scholes code the zero-gamma profile uses. One expiration per request.
YAHOO_OPT_URL = "https://query2.finance.yahoo.com/v7/finance/options/{t}"
YAHOO_SYMBOLS = {"SPX": "^SPX", "NDX": "^NDX", "SPY": "SPY", "QQQ": "QQQ"}
YAHOO_ALL_DTE_CAP = 60       # "all expirations" on Yahoo = next 60 days (one request per expiry)
_yahoo = {"session": None, "crumb": None}
_ycache: dict[tuple, tuple[float, dict]] = {}


def _yahoo_session(refresh: bool = False):
    if refresh or _yahoo["session"] is None:
        s = requests.Session()
        s.headers.update({"User-Agent": CBOE_HEADERS["User-Agent"], "Accept-Language": "en-US,en;q=0.9"})
        try:
            s.get("https://fc.yahoo.com", timeout=20)      # sets the cookie the crumb is tied to (404 is normal)
        except requests.RequestException:
            pass
        crumb = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=20).text.strip()
        if not crumb or "<" in crumb or " " in crumb:
            raise RuntimeError("Yahoo didn't hand out a session token (crumb)")
        _yahoo.update(session=s, crumb=crumb)
    return _yahoo["session"], _yahoo["crumb"]


def _yahoo_get(t: str, params: dict) -> dict:
    for attempt in (0, 1):
        s, crumb = _yahoo_session(refresh=attempt == 1)
        r = s.get(YAHOO_OPT_URL.format(t=t), params={**params, "crumb": crumb}, timeout=20)
        if r.status_code in (401, 403) and attempt == 0:
            continue                                         # stale crumb — get a new one and retry once
        r.raise_for_status()
        res = (r.json().get("optionChain") or {}).get("result") or []
        if not res:
            raise RuntimeError(f"Yahoo returned no option chain for {t}")
        return res[0]
    raise RuntimeError("Yahoo rejected the request")


def fetch_chain_yahoo(sym: str, max_dte: int | None) -> dict:
    """Build a Cboe-shaped chain from Yahoo so compute_levels() can use it unchanged."""
    horizon = YAHOO_ALL_DTE_CAP if max_dte is None else max_dte
    key = (sym, horizon)
    if key in _ycache and time.time() - _ycache[key][0] < CACHE_SECONDS:
        return _ycache[key][1]
    t = YAHOO_SYMBOLS[sym]
    first = _yahoo_get(t, {})
    q = first.get("quote") or {}
    spot = q.get("regularMarketPrice") or 0
    now = datetime.now(NY)
    exps = [e for e in first.get("expirationDates") or []
            if 0 <= (datetime.fromtimestamp(e, ZoneInfo("UTC")).date() - now.date()).days <= horizon]
    chains = {first["options"][0]["expirationDate"]: first["options"][0]} if first.get("options") else {}
    todo = [e for e in exps if e not in chains]
    with ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(lambda e: _yahoo_get(t, {"date": e}), todo):
            for o in res.get("options") or []:
                chains[o["expirationDate"]] = o

    options = []
    for e, ch in chains.items():
        if e not in exps:
            continue
        exp_dt = datetime.fromtimestamp(e, ZoneInfo("UTC")).date()
        exp_close = datetime(exp_dt.year, exp_dt.month, exp_dt.day, 16, tzinfo=NY)
        T = max((exp_close - now).total_seconds(), 1800) / (365 * 24 * 3600)
        for c in (ch.get("calls") or []) + (ch.get("puts") or []):
            iv = c.get("impliedVolatility") or 0
            ok = spot > 0 and 0.01 < iv < 5          # Yahoo reports placeholder IVs like 0.00001 on illiquid strikes
            gamma = float(bs_gamma(spot, c["strike"], T, iv)) if ok else 0.0
            options.append({"option": c.get("contractSymbol", ""), "open_interest": c.get("openInterest") or 0,
                            "volume": c.get("volume") or 0, "iv": iv if ok else 0, "gamma": gamma})
    ts = q.get("regularMarketTime")
    data = {"current_price": spot, "options": options, "_source": "Yahoo",
            "_root_ts": datetime.fromtimestamp(ts, NY).strftime("%Y-%m-%d %H:%M:%S") if ts else None,
            "_note": f"Yahoo backup: {len(exps)} expirations" + (f" (all = next {YAHOO_ALL_DTE_CAP} days)" if max_dte is None else "")}
    _ycache[key] = (time.time(), data)
    return data


def parse_cboe_ts(ts) -> datetime | None:
    """Cboe timestamps are ET wall-clock strings; try the common formats."""
    if not ts:
        return None
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(str(ts)[:26], f).replace(tzinfo=NY)
        except ValueError:
            pass
    return None


YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{t}"
CHART_TICKERS = {"NDX": ("^NDX", "NQ=F"), "SPX": ("^GSPC", "ES=F"), "SPY": ("SPY", None), "QQQ": ("QQQ", None)}
RANGES = {"1d": "5m", "5d": "15m", "1mo": "60m"}
_hcache: dict[tuple, tuple[float, list]] = {}


def yahoo_bars(ticker: str, rng: str, interval: str | None = None) -> list[dict]:
    interval = interval or RANGES[rng]
    key = (ticker, rng, interval)
    if key in _hcache and time.time() - _hcache[key][0] < CACHE_SECONDS:
        return _hcache[key][1]
    r = requests.get(YF_URL.format(t=ticker), params={"interval": interval, "range": rng},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars: dict[int, dict] = {}
    for i, t in enumerate(res.get("timestamp") or []):
        o, h, l, c = (q[f][i] for f in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        bars[t] = {"utc": t, "open": o, "high": h, "low": l, "close": c}
    out = [bars[t] for t in sorted(bars)]
    _hcache[key] = (time.time(), out)
    return out


def calc_basis(idx: list[dict], fut: list[dict]) -> float | None:
    """Futures minus index at the latest bar where both traded."""
    idx_by = {b["utc"]: b["close"] for b in idx}
    for b in reversed(fut):
        if b["utc"] in idx_by:
            return round(b["close"] - idx_by[b["utc"]], 2)
    return None


def to_chart(bars: list[dict]) -> list[dict]:
    """Shift UTC to New York wall-clock time so the chart axis reads in ET."""
    out = []
    for b in bars:
        off = int(datetime.fromtimestamp(b["utc"], NY).utcoffset().total_seconds())
        out.append({"time": b["utc"] + off, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"]})
    return out


def fmt(k: float) -> str:
    return str(int(k)) if float(k).is_integer() else f"{k:g}"


def bs_gamma(S, K, T, iv, r=RISK_FREE):
    """Black-Scholes gamma, vectorised. S can be a grid (shape [n,1]) against option arrays."""
    sqrtT = np.sqrt(T)
    d1 = (np.log(S / K) + (r + 0.5 * iv**2) * T) / (iv * sqrtT)
    return np.exp(-0.5 * d1**2) / (np.sqrt(2 * np.pi) * S * iv * sqrtT)


def zero_gamma_profile(spot, K, T, iv, oi, sign, pct_range):
    """Re-price gamma for every option across a grid of spot prices and find where total GEX = 0."""
    grid = np.linspace(spot * (1 - pct_range / 100), spot * (1 + pct_range / 100), 401)
    S = grid[:, None]
    g = bs_gamma(S, K[None, :], T[None, :], iv[None, :])
    total = (g * oi[None, :] * sign[None, :]).sum(axis=1) * 100 * grid**2 * 0.01
    flips = []
    for i in range(len(grid) - 1):
        a, b = total[i], total[i + 1]
        if a == 0 or (a < 0) != (b < 0):
            x = grid[i] if a == b else grid[i] + (grid[i + 1] - grid[i]) * (-a / (b - a))
            flips.append(float(x))
    if not flips:
        return None
    return min(flips, key=lambda x: abs(x - spot))


WEIGHTS = {"oi": "Open interest (last close)", "oi_vol": "OI + today's volume", "vol": "Today's volume only"}


SOURCES = {"auto": "Auto", "cboe": "Cboe", "yahoo": "Yahoo"}


class FeedNotReady(ValueError):
    """A feed served a chain that's blank or half-refreshed — try the backup / retry shortly, never log it."""


def compute_levels(data: dict, pct_range: float, max_dte: int | None, top_n: int, weight: str = "oi") -> dict:
    src = data.get("_source", "Cboe")
    spot = float(data.get("current_price") or 0)
    if spot <= 0:
        raise FeedNotReady(f"{src} returned no spot price — the feed may be down. Try again in a minute.")
    now = datetime.now(NY)
    calls: dict[float, float] = {}
    puts: dict[float, float] = {}
    abs_all = abs_0dte = 0.0
    K_l, T_l, iv_l, oi_l, sgn_l = [], [], [], [], []
    n_chain = len(data.get("options") or [])
    n_match = n_used = n_iv = n_gamma = 0
    oi_sum = vol_sum = 0.0
    k_abs_sum = 0.0          # Σ strike·|gex| — for the gamma-centre sanity check

    for o in data["options"]:
        m = re.search(r"(\d{6})([CP])(\d{8})$", o.get("option", ""))
        if not m:
            continue
        exp_s, cp, k_s = m.groups()
        k = int(k_s) / 1000
        if abs(k - spot) / spot > pct_range / 100:
            continue
        exp_dt = datetime.strptime(exp_s, "%y%m%d").replace(hour=16, tzinfo=NY)
        if exp_dt <= now:
            continue
        if max_dte is not None and (exp_dt.date() - now.date()).days > max_dte:
            continue
        n_match += 1
        oi = o.get("open_interest") or 0
        vol = o.get("volume") or 0
        oi_sum += oi
        vol_sum += vol
        w = oi if weight == "oi" else oi + vol if weight == "oi_vol" else vol
        if w <= 0:
            continue
        n_used += 1
        gamma = o.get("gamma") or 0
        if gamma <= 0:           # blank greek — counting it would put fake zero-GEX strikes in the books
            continue
        n_gamma += 1
        sign = 1 if cp == "C" else -1
        g = gamma * w * 100 * spot**2 * 0.01
        book = calls if cp == "C" else puts
        book[k] = book.get(k, 0) + g
        abs_all += abs(g)
        k_abs_sum += k * abs(g)
        if exp_dt.date() == now.date():
            abs_0dte += abs(g)

        iv = o.get("iv") or 0
        if iv > 0:
            n_iv += 1
            K_l.append(k)
            T_l.append(max((exp_dt - now).total_seconds(), 1800) / (365 * 24 * 3600))
            iv_l.append(iv)
            oi_l.append(w)
            sgn_l.append(sign)

    # Cboe periodically serves the chain with greeks zeroed (often right around the open).
    # Without these checks the walls land on arbitrary strikes at the edge of the range.
    if n_used and n_gamma < max(20, 0.25 * n_used):
        raise FeedNotReady(f"{src}'s greeks are blank right now (gamma on only {n_gamma} of {n_used} options) — "
                           "the feed is mid-refresh. Levels were NOT saved. Try again in a couple of minutes.")
    if abs_all and abs(k_abs_sum / abs_all / spot - 1) * 100 > MAX_GAMMA_CENTER_PCT:
        raise FeedNotReady(f"{src}'s greeks look half-refreshed (gamma is centred {abs(k_abs_sum / abs_all / spot - 1) * 100:.1f}% "
                           "away from spot). Levels were NOT saved. Try again in a couple of minutes.")
    if not calls or not puts:
        if max_dte == 0:
            raise ValueError("No 0DTE options left today — they expire at 4:00 PM ET. Click 'Back to 7 DTE' or try again next session.")
        raise ValueError("No options matched the filters — widen the range or DTE.")

    strikes = sorted(set(calls) | set(puts))
    net = {k: calls.get(k, 0) - puts.get(k, 0) for k in strikes}
    call_wall = max(calls, key=calls.get)
    put_wall = max(puts, key=puts.get)

    # Zero gamma: full profile when IVs are available, strike-based estimate as fallback
    flip, method = None, "profile"
    if len(K_l) >= 20:
        flip = zero_gamma_profile(spot, np.array(K_l), np.array(T_l), np.array(iv_l),
                                  np.array(oi_l, dtype=float), np.array(sgn_l, dtype=float), pct_range)
    if flip is None:
        method = "strike estimate"
        flips, cum = [], 0.0
        for k in strikes:
            prev, cum = cum, cum + net[k]
            if (prev < 0 <= cum) or (prev > 0 >= cum):
                flips.append(k)
        flip = min(flips, key=lambda k: abs(k - spot)) if flips else None
    if flip is not None:
        flip = round(flip, 1)

    used = {call_wall, put_wall}
    top = [k for k in sorted(strikes, key=lambda k: abs(net[k]), reverse=True) if k not in used][:top_n]

    # (price, label, size in $mn per 1% move)
    levels = [(call_wall, "Call Wall", calls[call_wall] / 1e6), (put_wall, "Put Wall", -puts[put_wall] / 1e6)]
    if flip is not None:
        levels.append((flip, "Zero Gamma", 0.0))
    levels += [(k, "GEX+" if net[k] > 0 else "GEX-", net[k] / 1e6) for k in top]

    # ---- data health ----
    warnings = []
    iv_pct = round(n_iv / n_gamma * 100) if n_gamma else 0
    gamma_pct = round(n_gamma / n_used * 100) if n_used else 0
    cboe_dt = parse_cboe_ts(data.get("_root_ts"))
    age_min = round((now - cboe_dt).total_seconds() / 60) if cboe_dt else None
    mkt_open = now.weekday() < 5 and 930 <= now.hour * 100 + now.minute < 1600
    if n_gamma < 100:
        warnings.append(f"Only {n_gamma} options were used — levels may be unreliable. Try a wider strike range or more DTE.")
    if gamma_pct < 60:
        warnings.append(f"{src} had usable gamma on only {gamma_pct}% of options — the feed may be partly refreshed. Re-check in a few minutes.")
    if iv_pct < 50:
        warnings.append(f"Only {iv_pct}% of options had implied volatility — zero gamma may fall back to the rough strike estimate.")
    if method != "profile":
        warnings.append("Zero gamma uses the rough strike estimate (not enough IV data for the full profile).")
    if mkt_open and age_min is not None and age_min > 30:
        warnings.append(f"{src} data is {age_min} minutes old — the feed may be lagging.")
    if weight != "oi" and vol_sum == 0:
        warnings.append("No options volume yet today — volume weighting has nothing to add.")
    health = {
        "status": "ok" if not warnings else "warn",
        "chain_options": n_chain, "matched": n_match, "used": n_gamma, "iv_pct": iv_pct, "gamma_pct": gamma_pct,
        "vol_oi_ratio": round(vol_sum / oi_sum, 2) if oi_sum else None,
        "source": src, "note": data.get("_note"),
        "data_time": cboe_dt.strftime("%H:%M ET") if cboe_dt else None, "age_min": age_min,
        "warnings": warnings,
    }

    hm = now.hour * 100 + now.minute
    run = "Pre-market" if hm < 930 else "Open" if hm < 1200 else "Midday" if hm < 1600 else "After hours"
    pine = f"@{now:%Y-%m-%dT%H%M}," + ",".join(f"{fmt(k)}:{lbl}:{v:.0f}" for k, lbl, v in levels)

    return {
        "spot": spot,
        "run": run,
        "zero_gamma_method": method,
        "zero_dte_pct": round(abs_0dte / abs_all * 100, 1) if abs_all else 0.0,
        "max_dte": max_dte,
        "weight": weight,
        "weight_label": WEIGHTS[weight],
        "source": src,
        "health": health,
        "call_wall": call_wall, "put_wall": put_wall, "zero_gamma": flip,
        "total_net_gex_bn": round(sum(net.values()) / 1e9, 2),
        "levels": [{"price": k, "label": lbl, "gex_mn": round(v, 1)} for k, lbl, v in levels],
        "by_strike": [[k, round(net[k] / 1e6, 1)] for k in strikes],
        "pine_string": pine,
        "as_of": now.strftime("%Y-%m-%d %H:%M ET"),
    }


DB_PATH = Path(__file__).with_name("gex_log.db")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS levels (
        date TEXT, run TEXT, symbol TEXT, max_dte TEXT, weight TEXT, time TEXT,
        spot REAL, call_wall REAL, put_wall REAL, zero_gamma REAL, levels_json TEXT,
        source TEXT,
        PRIMARY KEY (date, run, symbol, max_dte, weight))""")
    if "source" not in [c[1] for c in con.execute("PRAGMA table_info(levels)")]:
        con.execute("ALTER TABLE levels ADD COLUMN source TEXT")      # logs made before the Yahoo backup
    return con


def log_levels(sym: str, res: dict) -> None:
    now = datetime.now(NY)
    if now.weekday() >= 5:
        return
    with db() as con:
        con.execute("""INSERT OR REPLACE INTO levels (date, run, symbol, max_dte, weight, time, spot, call_wall,
                       put_wall, zero_gamma, levels_json, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            now.strftime("%Y-%m-%d"), res["run"], sym, str(res["max_dte"]), res["weight"], now.strftime("%H:%M"),
            res["spot"], res["call_wall"], res["put_wall"], res["zero_gamma"], json.dumps(res["levels"]),
            res.get("source")))


@app.get("/api/levels")
def api_levels(
    sym: str = Query("NDX"),
    pct_range: float = Query(8.0, ge=0.5, le=50),
    max_dte: int | None = Query(None, ge=0),    # omitted = all expirations (the page's "blank = all")
    top_n: int = Query(3, ge=0, le=10),
    weight: str = Query("oi"),
    log: int = Query(0),
    source: str = Query("auto"),
):
    sym = sym.upper()
    if sym not in SYMBOLS:
        raise HTTPException(400, f"Unknown symbol. Use one of {list(SYMBOLS)}")
    if weight not in WEIGHTS:
        raise HTTPException(400, f"Unknown weight. Use one of {list(WEIGHTS)}")
    if source not in SOURCES:
        raise HTTPException(400, f"Unknown source. Use one of {list(SOURCES)}")

    order = ["Cboe", "Yahoo"] if source == "auto" else [SOURCES[source]]
    failures: list[str] = []
    res = None
    for src in order:
        try:
            data = fetch_chain(sym) if src == "Cboe" else fetch_chain_yahoo(sym, max_dte)
        except Exception as e:          # network / blocked / format change
            failures.append(f"Couldn't fetch {src} data: {e}")
            continue
        try:
            res = {"symbol": sym, **compute_levels(data, pct_range, max_dte, top_n, weight)}
            break
        except FeedNotReady as e:       # blank snapshot — drop it from the cache so a retry refetches
            if src == "Cboe":
                _cache.pop(sym, None)
            else:
                _ycache.pop((sym, YAHOO_ALL_DTE_CAP if max_dte is None else max_dte), None)
            failures.append(str(e))
        except ValueError as e:         # a settings problem (no 0DTE left, filters too tight) — another feed won't help
            raise HTTPException(422, str(e))
    if res is None:
        raise HTTPException(503, " · ".join(f.replace(" Levels were NOT saved.", "") for f in failures)
                            + " — Levels were NOT saved.")
    if failures:                        # the backup was used — say why
        res["health"]["warnings"].insert(0, f"Cboe unavailable, so these levels come from the Yahoo Finance backup "
                                            f"(gamma computed from Yahoo's IV). Cboe said: {failures[0]}")
        res["health"]["status"] = "warn"
    if log:
        try:
            log_levels(sym, res)
            res["logged"] = True
        except Exception as e:          # logging must never break the page
            res["logged"] = False
            res["health"]["warnings"].append(f"Couldn't save this run to the log: {e}")
    return res


# ---------------- Track record ----------------
RUN_PRIORITY = {"Open": 0, "Midday": 1, "Pre-market": 2}
INDEX_TICKER = {"NDX": "^NDX", "SPX": "^GSPC", "SPY": "SPY", "QQQ": "QQQ"}
HOLD_BARS = 12          # 12 × 5-min bars = 60 minutes after first touch


def score_day(bars: list[dict], cw, pw, zg, tol: float) -> dict:
    highs = [b["high"] for b in bars]; lows = [b["low"] for b in bars]; closes = [b["close"] for b in bars]
    out = {"range": round(max(highs) - min(lows), 2), "open": bars[0]["open"], "close": closes[-1]}
    out["regime"] = None if zg is None else ("pos" if bars[0]["open"] >= zg else "neg")

    def wall(level, side):
        if level is None:
            return {"touched": False, "held": None, "reaction": None}
        idx = next((i for i, b in enumerate(bars) if (b["high"] >= level - tol if side == "call" else b["low"] <= level + tol)), None)
        if idx is None:
            return {"touched": False, "held": None, "reaction": None}
        if idx == 0 and (bars[0]["open"] > level + tol if side == "call" else bars[0]["open"] < level - tol):
            # Session started on the far side of the wall — it was never tested from the right side.
            return {"touched": False, "held": None, "reaction": None, "beyond": True}
        win = bars[idx: idx + HOLD_BARS + 1]
        if side == "call":
            held = not any(b["close"] > level + tol for b in win)
            reaction = level - min(b["low"] for b in win)
        else:
            held = not any(b["close"] < level - tol for b in win)
            reaction = max(b["high"] for b in win) - level
        return {"touched": True, "held": held, "reaction": round(reaction, 2)}

    out["call"] = wall(cw, "call")
    out["put"] = wall(pw, "put")
    out["inside"] = (cw is None or max(closes) <= cw + tol) and (pw is None or min(closes) >= pw - tol)
    if zg is None:
        out["zg_crosses"] = None
    else:
        sides = [c >= zg for c in closes]
        out["zg_crosses"] = sum(1 for a, b in zip(sides, sides[1:]) if a != b)
    return out


def looks_junk(spot, cw, pw, zg) -> bool:
    """Runs saved before the blank-greeks check: no zero gamma and both walls far from spot."""
    far = lambda lv: lv is None or abs(lv / spot - 1) * 100 > MAX_GAMMA_CENTER_PCT
    return bool(spot) and zg is None and far(cw) and far(pw)


@app.get("/api/report")
def api_report(sym: str = Query("NDX"), max_dte: str = Query("7"), weight: str = Query("oi")):
    sym = sym.upper()
    if sym not in INDEX_TICKER:
        raise HTTPException(400, "Bad symbol")
    with db() as con:
        rows = con.execute("""SELECT date, run, time, spot, call_wall, put_wall, zero_gamma FROM levels
                              WHERE symbol=? AND max_dte=? AND weight=? AND run IN ('Open','Midday','Pre-market')""",
                           (sym, str(max_dte), weight)).fetchall()
        total_runs = con.execute("SELECT COUNT(*) FROM levels").fetchone()[0]
    best: dict[str, tuple] = {}
    junk = 0
    for r in rows:
        if looks_junk(r[3], r[4], r[5], r[6]):
            junk += 1
            continue
        if r[0] not in best or RUN_PRIORITY[r[1]] < RUN_PRIORITY[best[r[0]][1]]:
            best[r[0]] = r
    if not best:
        return {"days": [], "summary": None, "logged_runs": total_runs, "db_path": str(DB_PATH),
                "skipped": [f"{junk} run(s) ignored — saved while Cboe's greeks were blank"] if junk else []}

    try:
        bars = yahoo_bars(INDEX_TICKER[sym], "60d", "5m")
    except Exception as e:
        raise HTTPException(502, f"Couldn't fetch price history for scoring: {e}")
    by_day: dict[str, list[dict]] = {}
    for b in bars:
        dt = datetime.fromtimestamp(b["utc"], NY)
        b2 = {**b, "hm": dt.hour * 100 + dt.minute}
        by_day.setdefault(dt.strftime("%Y-%m-%d"), []).append(b2)

    today = datetime.now(NY).strftime("%Y-%m-%d")
    days, skipped = [], []
    if junk:
        skipped.append(f"{junk} run(s) ignored — saved while Cboe's greeks were blank")
    for date in sorted(best):
        _, run, t, spot, cw, pw, zg = best[date]
        start = max(int(t.replace(":", "")), 930)
        session = [b for b in by_day.get(date, []) if start <= b["hm"] < 1600]
        if date == today and datetime.now(NY).hour < 16:
            skipped.append(f"{date}: session still in progress")
            continue
        if len(session) < 6:
            skipped.append(f"{date}: no price data (older than 60 days or a holiday)")
            continue
        sc = score_day(session, cw, pw, zg, tol=spot * 0.001)
        days.append({"date": date, "run": run, "time": t, "call_wall": cw, "put_wall": pw, "zero_gamma": zg, **sc})

    def pct(a, b):
        return round(a / b * 100) if b else None

    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 1) if xs else None

    n = len(days)
    ct = [d for d in days if d["call"]["touched"]]; pt = [d for d in days if d["put"]["touched"]]
    pos = [d for d in days if d["regime"] == "pos"]; neg = [d for d in days if d["regime"] == "neg"]
    summary = None if not n else {
        "days": n,
        "call_touch_pct": pct(len(ct), n), "call_hold_pct": pct(sum(d["call"]["held"] for d in ct), len(ct)),
        "call_reaction": avg([d["call"]["reaction"] for d in ct]),
        "put_touch_pct": pct(len(pt), n), "put_hold_pct": pct(sum(d["put"]["held"] for d in pt), len(pt)),
        "put_reaction": avg([d["put"]["reaction"] for d in pt]),
        "inside_pct": pct(sum(d["inside"] for d in days), n),
        "pos_days": len(pos), "neg_days": len(neg),
        "pos_range": avg([d["range"] for d in pos]), "neg_range": avg([d["range"] for d in neg]),
        "avg_zg_crosses": avg([d["zg_crosses"] for d in days]),
    }
    return {"days": days[::-1], "summary": summary, "skipped": skipped, "logged_runs": total_runs,
            "db_path": str(DB_PATH), "hold_minutes": HOLD_BARS * 5}


@app.get("/api/history")
def api_history(sym: str = Query("NDX"), rng: str = Query("1d")):
    sym = sym.upper()
    if sym not in CHART_TICKERS or rng not in RANGES:
        raise HTTPException(400, "Bad symbol or range")
    idx_t, fut_t = CHART_TICKERS[sym]
    try:
        idx = yahoo_bars(idx_t, rng)
    except Exception as e:
        raise HTTPException(502, f"Couldn't fetch price history: {e}")
    fut, basis = [], None
    if fut_t:
        try:
            fut = yahoo_bars(fut_t, rng)
            basis = calc_basis(idx, fut)
            if basis is None and rng == "1d":          # e.g. pre-market: no overlap yet today
                basis = calc_basis(yahoo_bars(idx_t, "5d"), yahoo_bars(fut_t, "5d"))
        except Exception:
            fut = []
    return {"symbol": sym, "index_ticker": idx_t, "futures_ticker": fut_t,
            "index": to_chart(idx), "futures": to_chart(fut), "basis": basis}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GEX Levels</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#14171c;--muted:#5d6673;--border:#dde1e7;--accent:#2563eb;
--green:#15803d;--red:#b91c1c;--orange:#c2410c;--code:#eef1f5;--hl:#fff7e0;
--grid:#eceff3;--up:#94a3b8;--down:#475569;
--lv-call:#16a34a;--lv-put:#dc2626;--lv-zero:#d97706;--lv-pos:#0891b2;--lv-neg:#c026d3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--text:#e8ebef;--muted:#9aa4b1;
--border:#2a3038;--accent:#60a5fa;--green:#4ade80;--red:#f87171;--orange:#fb923c;--code:#0b0e12;--hl:#2b2412;
--grid:#1f252d;--up:#64748b;--down:#cbd5e1;
--lv-call:#22c55e;--lv-put:#f43f5e;--lv-zero:#f59e0b;--lv-pos:#22d3ee;--lv-neg:#e879f9}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:760px;margin:0 auto;padding:28px 16px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:0 0 10px}
.sub{color:var(--muted);margin:0 0 20px;font-size:14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:16px}
.card.flash{background:var(--hl);transition:background .3s}
.controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;align-items:end}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}
select,input{width:100%;padding:8px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font:inherit}
input[type=checkbox]{width:auto;margin:0}
button{padding:9px 14px;border:0;border-radius:6px;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--border)}
button:disabled{opacity:.6;cursor:wait}
.pine{display:flex;gap:8px;align-items:stretch}
.pine code{flex:1;background:var(--code);border:1px solid var(--border);border-radius:6px;padding:10px;
font:13px ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all;user-select:all}
.stats{display:flex;gap:24px;flex-wrap:wrap;margin:0 0 12px;font-size:14px}
.stats b{display:block;font-size:18px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--border)}
th{font-size:12px;color:var(--muted);font-weight:600}
td.num,th.num{text-align:right}
.Call{color:var(--green)}.Put{color:var(--red)}.Zero{color:var(--orange)}
.err{color:var(--red)}.muted{color:var(--muted);font-size:13px}
.runs{display:grid;gap:8px;margin-bottom:12px}
.run{display:grid;grid-template-columns:auto 1fr 110px;gap:10px;align-items:center}
.run input[type=time]{padding:6px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.chart-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.chart-head h2{margin:0}
.chart-opts{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px}
.chart-opts select{width:auto;padding:5px 8px}
.chart-opts label{display:flex;gap:6px;align-items:center;margin:0;font-size:13px;color:var(--text)}
#pc{height:440px;position:relative}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:10px 0 0}
.legend span{display:flex;align-items:center;gap:6px}
.legend i{display:inline-block;width:18px;height:0;border-top:2px solid}
.legend i.dash{border-top-style:dashed}.legend i.dot{border-top-style:dotted}
#gexwrap{position:relative;margin-top:18px}
.live-head{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.live-head h2{margin:0}
.live-top{display:flex;gap:16px;flex-wrap:wrap;align-items:stretch;margin-bottom:8px}
.regime{flex:1 1 260px;border-radius:8px;padding:12px 14px;border:1px solid var(--border)}
.regime b{display:block;font-size:20px;letter-spacing:.02em}
.regime.pos{background:color-mix(in srgb,var(--lv-call) 12%,transparent);border-color:color-mix(in srgb,var(--lv-call) 45%,transparent)}
.regime.neg{background:color-mix(in srgb,var(--lv-put) 12%,transparent);border-color:color-mix(in srgb,var(--lv-put) 45%,transparent)}
.regime.pos b{color:var(--green)}.regime.neg b{color:var(--red)}
.px{flex:0 1 200px;padding:12px 14px;border:1px solid var(--border);border-radius:8px}
.px b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
#gauge{display:block;width:100%;height:78px;margin:4px 0 8px}
.dists{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.chip{display:flex;align-items:center;gap:6px;border:1px solid var(--border);border-radius:999px;padding:4px 10px;font-size:13px;font-variant-numeric:tabular-nums}
.chip i{width:8px;height:8px;border-radius:50%;display:inline-block}
.chip.near{border-width:2px;font-weight:600}
.play{border-left:3px solid var(--accent);padding:8px 12px;background:var(--code);border-radius:0 6px 6px 0;margin-bottom:10px}
.play b{display:block;margin-bottom:2px}
.cautions{list-style:none;padding:0;margin:0 0 10px;display:grid;gap:6px}
.cautions li{font-size:13px;padding:6px 10px;border-radius:6px;background:color-mix(in srgb,var(--lv-zero) 14%,transparent)}
.health{font-size:13px;margin:10px 0 0;padding:8px 10px;border-radius:6px;border:1px solid var(--border)}
.health.warn{background:color-mix(in srgb,var(--lv-zero) 12%,transparent);border-color:color-mix(in srgb,var(--lv-zero) 40%,transparent)}
.health ul{margin:4px 0 0;padding-left:18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:10px 0}
.tile{border:1px solid var(--border);border-radius:8px;padding:10px 12px}
.tile .k{font-size:12px;color:var(--muted)}
.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .s{font-size:12px;color:var(--muted)}
.scroll{overflow-x:auto}
#rtable td,#rtable th{white-space:nowrap;font-size:13px}
.cautions li.ok{background:transparent;color:var(--muted);padding-left:0}
.live-opts{font-size:13px;margin-bottom:6px}
.live-opts label{display:flex;gap:6px;align-items:center;margin:0;font-size:13px;color:var(--text)}
.live-opts input[type=number]{width:72px;padding:4px 6px}
details summary{cursor:pointer;font-size:13px;color:var(--muted)}
#events{margin:6px 0 0;padding-left:18px;font-size:13px;font-variant-numeric:tabular-nums}
#gexsvg{display:block;width:100%;height:220px}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--border);border-radius:6px;
padding:6px 8px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.15);display:none;white-space:nowrap;font-variant-numeric:tabular-nums}
</style></head>
<body><main>
<h1>Daily GEX Levels <span class="muted" style="font-weight:400">__VERSION__</span></h1>
<p class="sub">Cboe delayed chain (≈15 min), with Yahoo Finance as an automatic backup. Copy the string into the "Daily GEX Levels" indicator.</p>

<div class="card controls">
  <div><label for="sym">Underlying</label>
    <select id="sym"><option selected>NDX</option><option>SPX</option><option>QQQ</option><option>SPY</option></select></div>
  <div><label for="rng">Strike range ± %</label><input id="rng" type="number" value="8" min="0.5" step="0.5"></div>
  <div><label for="dte">Max DTE (blank = all)</label><input id="dte" type="number" min="0" value="7" placeholder="all"></div>
  <div><label for="wt">Positioning</label>
    <select id="wt"><option value="oi">Open interest (last close)</option><option value="oi_vol">OI + today's volume</option><option value="vol">Today's volume only</option></select></div>
  <div><label for="top">Extra GEX strikes</label><input id="top" type="number" value="3" min="0" max="10"></div>
  <div><label for="src">Data source</label>
    <select id="src"><option value="auto">Auto (Cboe → Yahoo)</option><option value="cboe">Cboe only</option><option value="yahoo">Yahoo only</option></select></div>
  <div class="row" style="gap:8px"><button id="go">Get levels</button><button class="ghost" id="zdte" title="Toggle 0DTE-only levels (Max DTE = 0)">0DTE only</button></div>
</div>

<div class="card" id="live">
  <div class="live-head"><h2>Live read</h2><span class="muted" id="liveupd">Waiting for levels…</span></div>
  <div class="live-top">
    <div class="regime" id="regime"><b>—</b><span class="muted"></span></div>
    <div class="px"><span class="muted" id="pxname">Price</span><b id="livepx">—</b><span class="muted" id="pxsub"></span></div>
  </div>
  <svg id="gauge" role="img" aria-label="Price position between the put wall and call wall"></svg>
  <div class="dists" id="dists"></div>
  <div class="play" id="play"></div>
  <ul class="cautions" id="cautions"></ul>
  <div class="row live-opts">
    <label><input type="checkbox" id="evtday"> News / event day (CPI, FOMC, NFP…)</label>
    <label><input type="checkbox" id="livealerts" checked> Alert on regime flips &amp; wall approaches</label>
    <label>“Near” = within <input type="number" id="proxpct" value="0.15" min="0.02" step="0.05">%</label>
  </div>
  <details><summary>Today's events</summary><ul id="events"></ul></details>
  <p class="muted" style="margin:8px 0 0">Context, not a trade signal — confirm with your own price action. Updates about once a minute.</p>
</div>

<div id="out"></div>

<div class="card" id="chartcard">
  <div class="chart-head">
    <h2 id="charttitle">NDX with GEX levels</h2>
    <div class="chart-opts">
      <select id="view" aria-label="Chart symbol"><option value="index">Index</option><option value="futures">Futures</option></select>
      <select id="range" aria-label="Chart range"><option value="1d">Today · 5m</option><option value="5d">5 days · 15m</option><option value="1mo">1 month · 1h</option></select>
      <label><input type="checkbox" id="fit" checked> Fit all levels</label>
    </div>
  </div>
  <div id="pc"></div>
  <div class="legend" id="legend"></div>
  <p class="muted" id="chartmsg" style="margin:6px 0 0"></p>
  <div id="gexwrap">
    <h2 style="margin:0 0 6px">Net GEX by strike <span class="muted">($mn per 1% move)</span></h2>
    <svg id="gexsvg" role="img" aria-label="Net gamma exposure by strike"></svg>
    <div class="tip" id="tip"></div>
  </div>
</div>

<div class="card" id="trackcard">
  <div class="chart-head"><h2>Track record</h2><button class="ghost" id="rep">Build report</button></div>
  <p class="muted" style="margin:0">Scores each logged day (the Open run, else Midday, else Pre-market) against how the index actually traded from then to the close.
  A wall counts as <b>held</b> if price touched it and didn't close beyond it during the next hour. Uses the current Underlying, Max DTE and Positioning settings.</p>
  <div id="repout"><p class="muted">Every “Get levels” and auto-refresh is saved automatically. Come back after a couple of weeks of runs.</p></div>
</div>

<div class="card">
  <h2>Auto-refresh schedule (ET, weekdays)</h2>
  <div class="runs" id="runs"></div>
  <div class="row">
    <button class="ghost" id="notif">Enable notifications</button>
    <span class="muted" id="next"></span>
  </div>
  <p class="muted">Keep this tab open. At each time the page pulls fresh levels, copies the string, beeps and shows a notification.
  Pre-market uses the prior close (open interest is already final); midday catches same-day gamma shifts.</p>
</div>
</main>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const $ = id => document.getElementById(id);
const cls = l => l.startsWith("Call") ? "Call" : l.startsWith("Put") ? "Put" : l.startsWith("Zero") ? "Zero" : "";
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const store = {
  get(k, d){ try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
  set(k, v){ try { localStorage.setItem(k, JSON.stringify(v)); } catch {} }
};

// ---------- settings persistence ----------
const KEY = id => "gex_" + (id === "dte" ? "dte7" : id);
for (const id of ["sym","rng","dte","top","wt","src"]) {
  const v = store.get(KEY(id), null); if (v !== null) $(id).value = v;
  $(id).addEventListener("change", () => store.set(KEY(id), $(id).value));
}

let current = null;

async function copyText(txt){
  try { await navigator.clipboard.writeText(txt); return true; }
  catch {
    try { const s = getSelection(), rg = document.createRange(); rg.selectNodeContents($("pine")); s.removeAllRanges(); s.addRange(rg); return document.execCommand("copy"); }
    catch { return false; }
  }
}

async function load(){
  const btn = $("go"); btn.disabled = true; btn.textContent = "Loading…";
  const p = new URLSearchParams({sym:$("sym").value, pct_range:$("rng").value || 8, top_n:$("top").value || 0, weight:$("wt").value, source:$("src").value, log:1});
  if ($("dte").value !== "") p.set("max_dte", $("dte").value);
  try{
    const r = await fetch("/api/levels?" + p);
    const d = await r.json();
    if(!r.ok) throw new Error(d.detail || r.statusText);
    current = d;
    $("out").innerHTML = `
      <div class="card" id="pinecard">
        <label>Pine input string · ${esc(d.run)} run</label>
        <div class="pine"><code id="pine">${esc(d.pine_string)}</code><button id="copy">Copy</button></div>
        <p class="muted" id="copymsg" style="margin:8px 0 0"></p>
      </div>
      <div class="card">
        <div class="stats">
          <div>Spot<b>${d.spot.toLocaleString()}</b></div>
          <div>Total net GEX<b>${d.total_net_gex_bn >= 0 ? "+" : ""}${d.total_net_gex_bn} bn</b></div>
          <div>Zero gamma<b style="font-size:14px">${esc(d.zero_gamma_method)}</b></div>
          <div>From 0DTE<b>${d.max_dte === 0 ? "100% (0DTE only)" : d.zero_dte_pct + "%"}</b></div>
        </div>
        <table><thead><tr><th>Level</th><th class="num">Strike</th><th class="num">GEX ($mn / 1%)</th></tr></thead>
        <tbody>${d.levels.map(l => `<tr><td class="${cls(l.label)}">${esc(l.label)}</td>
          <td class="num">${l.price.toLocaleString()}</td><td class="num">${l.label.startsWith("Zero") ? "—" : l.gex_mn.toLocaleString()}</td></tr>`).join("")}</tbody></table>
        <p class="muted">As of ${esc(d.as_of)} · ${esc(d.symbol)} · <b>${esc(d.source)}</b> data · ${esc(d.weight_label)}${d.max_dte != null ? " · ≤" + d.max_dte + " DTE" : " · all expirations"}${d.logged ? " · saved to log" : ""}</p>
        ${healthHtml(d.health)}
      </div>`;
    drawGexBars();
    loadHistory();
    liveState = null;
    liveTick();
    $("copy").onclick = async () => {
      const ok = await copyText(d.pine_string);
      $("copy").textContent = ok ? "Copied" : "Select & copy"; setTimeout(() => $("copy").textContent = "Copy", 1500);
    };
    return d;
  }catch(e){
    $("out").innerHTML = `<div class="card err">${esc(e.message)}</div>`;
    return null;
  }finally{ btn.disabled = false; btn.textContent = "Get levels"; }
}
$("go").onclick = load;
function healthHtml(h){
  if (!h) return "";
  const facts = [`${h.used.toLocaleString()} of ${h.chain_options.toLocaleString()} options used`, `IV on ${h.iv_pct}%`];
  if (h.vol_oi_ratio != null) facts.push(`today's volume = ${h.vol_oi_ratio}× OI`);
  if (h.data_time) facts.push(`${h.source} data ${h.data_time}${h.age_min != null ? ` (${h.age_min} min old)` : ""}`);
  if (h.note) facts.push(h.note);
  return `<div class="health ${h.status}"><b>${h.status === "ok" ? "✓ Data looks healthy" : "⚠ Check the data"}</b> · ${facts.map(esc).join(" · ")}
    ${h.warnings.length ? `<ul>${h.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}</div>`;
}
function syncZdte(){ const on = $("dte").value === "0"; $("zdte").textContent = on ? "Back to 7 DTE" : "0DTE only"; }
$("zdte").onclick = () => { $("dte").value = $("dte").value === "0" ? "7" : "0"; store.set(KEY("dte"), $("dte").value); syncZdte(); load(); };
$("dte").addEventListener("change", syncZdte); syncZdte();

// ---------- schedule ----------
const DEFAULT_RUNS = [
  {name:"Pre-market", t:"08:30", on:true},
  {name:"Open",       t:"09:46", on:true},
  {name:"Midday",     t:"12:30", on:true},
];
let runs = store.get("gex_runs", DEFAULT_RUNS);
let fired = store.get("gex_fired", {});

function renderRuns(){
  $("runs").innerHTML = runs.map((r, i) => `
    <div class="run">
      <input type="checkbox" id="on${i}" ${r.on ? "checked" : ""} aria-label="${esc(r.name)} run on">
      <label for="on${i}" style="margin:0;font-size:14px;color:var(--text)">${esc(r.name)}</label>
      <input type="time" id="t${i}" value="${esc(r.t)}" aria-label="${esc(r.name)} time">
    </div>`).join("");
  runs.forEach((r, i) => {
    $("on" + i).onchange = e => { runs[i].on = e.target.checked; store.set("gex_runs", runs); tick(); };
    $("t" + i).onchange  = e => { runs[i].t = e.target.value; store.set("gex_runs", runs); tick(); };
  });
}

function etNow(){
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {timeZone:"America/New_York", hourCycle:"h23",
    weekday:"short", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"})
    .formatToParts(new Date()).map(p => [p.type, p.value]));
  return { date:`${parts.year}-${parts.month}-${parts.day}`, wd:parts.weekday, mins:(+parts.hour % 24) * 60 + +parts.minute };
}
const toMins = t => { const [h, m] = t.split(":").map(Number); return h * 60 + m; };

function beep(){
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.25].forEach(off => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.15, ctx.currentTime + off); g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + off + 0.2);
      o.start(ctx.currentTime + off); o.stop(ctx.currentTime + off + 0.2);
    });
  } catch {}
}

let flashTimer = null;
function flashTitle(msg){
  clearInterval(flashTimer); let on = false;
  flashTimer = setInterval(() => { document.title = (on = !on) ? msg : "GEX Levels"; }, 1000);
  window.addEventListener("focus", () => { clearInterval(flashTimer); document.title = "GEX Levels"; }, {once:true});
}

async function autoRun(run, attempt = 1){
  const d = await load();
  if (!d) {
    // Both feeds can hiccup around the open — retry a few times before giving up.
    if (attempt < 5) {
      $("out").insertAdjacentHTML("beforeend", `<p class="muted">${esc(run.name)} auto-refresh: retrying in 2 minutes (attempt ${attempt + 1} of 5)…</p>`);
      setTimeout(() => autoRun(run, attempt + 1), 120000);
    } else flashTitle("⚠ GEX refresh failed");
    return;
  }
  const copied = await copyText(d.pine_string);
  const msg = copied ? "Copied to clipboard — paste into TradingView." : "Click Copy, then paste into TradingView.";
  if ($("copymsg")) $("copymsg").textContent = `${run.name} auto-refresh at ${run.t} ET. ${msg}`;
  if ($("pinecard")) { $("pinecard").classList.add("flash"); setTimeout(() => $("pinecard").classList.remove("flash"), 4000); }
  beep(); flashTitle("● New GEX levels");
  try {
    if ("Notification" in window && Notification.permission === "granted")
      new Notification(`GEX levels updated (${run.name})`, {body: msg + "\n" + d.pine_string.slice(0, 120)});
  } catch {}
}

function tick(){
  const now = etNow(), weekday = !["Sat","Sun"].includes(now.wd);
  // fire any run whose time passed in the last 10 minutes and hasn't fired today
  if (weekday) for (const r of runs) {
    if (!r.on || !r.t) continue;
    const key = now.date + " " + r.t, m = toMins(r.t);
    if (now.mins >= m && now.mins < m + 10 && !fired[key]) {
      fired[key] = true;
      for (const k of Object.keys(fired)) if (!k.startsWith(now.date)) delete fired[k];
      store.set("gex_fired", fired);
      autoRun(r);
    }
  }
  // next run label
  const upcoming = runs.filter(r => r.on && r.t).map(r => ({...r, m: toMins(r.t)})).sort((a, b) => a.m - b.m);
  const nxt = weekday ? upcoming.find(r => r.m > now.mins) : null;
  if (nxt) { const left = nxt.m - now.mins; $("next").textContent = `Next: ${nxt.name} at ${nxt.t} ET (in ${Math.floor(left / 60)}h ${left % 60}m)`; }
  else $("next").textContent = upcoming.length ? `Next: ${upcoming[0].name} at ${upcoming[0].t} ET (next weekday)` : "No runs enabled";
}

function syncNotifBtn(){
  if (!("Notification" in window)) { $("notif").style.display = "none"; return; }
  if (Notification.permission === "granted") { $("notif").textContent = "Notifications on"; $("notif").disabled = true; }
}
$("notif").onclick = async () => { try { await Notification.requestPermission(); } catch {} syncNotifBtn(); beep(); };

// ---------- price chart ----------
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const LEVEL_STYLE = {
  Call: {c:"--lv-call", w:2, s:"Solid",  name:"Call Wall"},
  Put:  {c:"--lv-put",  w:2, s:"Solid",  name:"Put Wall"},
  Zero: {c:"--lv-zero", w:2, s:"Dashed", name:"Zero Gamma"},
  Pos:  {c:"--lv-pos",  w:1, s:"Dotted", name:"+GEX"},
  Neg:  {c:"--lv-neg",  w:1, s:"Dotted", name:"−GEX"},
};
const lvType = l => l.startsWith("Call") ? "Call" : l.startsWith("Put") ? "Put" : l.startsWith("Zero") ? "Zero" : l.includes("-") ? "Neg" : "Pos";
let chart = null, candles = null, priceLines = [], shown = [], hist = null;

function chartTheme(){
  return {
    layout: {background: {type: "solid", color: css("--card")}, textColor: css("--muted"), fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif"},
    grid: {vertLines: {color: css("--grid")}, horzLines: {color: css("--grid")}},
    rightPriceScale: {borderColor: css("--border")},
    timeScale: {borderColor: css("--border"), timeVisible: true, secondsVisible: false},
  };
}

function ensureChart(){
  if (chart) return true;
  if (!window.LightweightCharts) { $("chartmsg").textContent = "Chart library didn't load (needs internet for unpkg.com). Levels above still work."; return false; }
  chart = LightweightCharts.createChart($("pc"), {...chartTheme(), height: 440, crosshair: {mode: LightweightCharts.CrosshairMode.Normal}});
  candles = chart.addCandlestickSeries({
    upColor: css("--up"), downColor: css("--down"), wickUpColor: css("--up"), wickDownColor: css("--down"), borderVisible: false,
    autoscaleInfoProvider: orig => {
      const r = orig();
      if (!r || !$("fit").checked || !shown.length) return r;
      return {priceRange: {minValue: Math.min(r.priceRange.minValue, ...shown), maxValue: Math.max(r.priceRange.maxValue, ...shown)}, margins: r.margins};
    },
  });
  new ResizeObserver(() => chart.applyOptions({width: $("pc").clientWidth})).observe($("pc"));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    chart.applyOptions(chartTheme());
    candles.applyOptions({upColor: css("--up"), downColor: css("--down"), wickUpColor: css("--up"), wickDownColor: css("--down")});
    drawLevels(); drawGexBars();
  });
  return true;
}

function drawLevels(){
  if (!candles || !current) return;
  priceLines.forEach(pl => candles.removePriceLine(pl)); priceLines = []; shown = [];
  const fut = $("view").value === "futures" && hist && hist.futures.length;
  const basis = fut ? (hist.basis || 0) : 0;
  for (const l of current.levels) {
    const st = LEVEL_STYLE[lvType(l.label)], price = l.price + basis;
    shown.push(price);
    priceLines.push(candles.createPriceLine({
      price, color: css(st.c), lineWidth: st.w, lineStyle: LightweightCharts.LineStyle[st.s], axisLabelVisible: true,
      title: st.name + (fut ? " " + l.price.toLocaleString() : ""),
    }));
  }
  const used = [...new Set(current.levels.map(l => lvType(l.label)))];
  $("legend").innerHTML = used.map(t => { const st = LEVEL_STYLE[t];
    return `<span><i class="${st.s === "Dashed" ? "dash" : st.s === "Dotted" ? "dot" : ""}" style="border-top-color:${css(st.c)}"></i>${st.name}</span>`; }).join("");
  chart.priceScale("right").applyOptions({autoScale: true});   // re-run autoscale with the new levels
}

async function loadHistory(){
  if (!current || !ensureChart()) return;
  const sym = current.symbol;
  $("view").querySelector('[value=futures]').disabled = !["NDX", "SPX"].includes(sym);
  if ($("view").value === "futures" && !["NDX", "SPX"].includes(sym)) $("view").value = "index";
  try {
    const r = await fetch(`/api/history?sym=${sym}&rng=${$("range").value}`);
    const h = await r.json();
    if (!r.ok) throw new Error(h.detail || r.statusText);
    hist = h;
    const fut = $("view").value === "futures" && h.futures.length;
    candles.setData(fut ? h.futures : h.index);
    $("charttitle").textContent = `${fut ? h.futures_ticker.replace("=F", " futures") : sym} with GEX levels`;
    $("chartmsg").textContent = fut
      ? (h.basis != null ? `Levels shifted ${h.basis >= 0 ? "+" : ""}${h.basis} pts (futures − index). Axis tags show the converted price; titles keep the index strike.` : "Couldn't line up futures with the index for a basis — levels shown unshifted.")
      : `Yahoo Finance prices (may be delayed) · times in ET.`;
    drawLevels();
    chart.timeScale().fitContent();
  } catch (e) {
    $("chartmsg").textContent = "Price chart unavailable: " + e.message;
  }
}
for (const id of ["view", "range"]) {
  const v = store.get("gex_" + id, null); if (v !== null) $(id).value = v;
  $(id).addEventListener("change", () => { store.set("gex_" + id, $(id).value); loadHistory(); });
}
$("fit").onchange = () => { if (chart) chart.priceScale("right").applyOptions({autoScale: true}); };
setInterval(() => { if (current && $("range").value === "1d") loadHistory(); }, 120000);

// ---------- net GEX by strike ----------
function drawGexBars(){
  const svg = $("gexsvg"), tip = $("tip");
  if (!current || !current.by_strike || !current.by_strike.length) { svg.innerHTML = ""; return; }
  const data = current.by_strike, W = svg.clientWidth || 700, H = 220, m = {l: 56, r: 12, t: 14, b: 26};
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const kMin = data[0][0], kMax = data[data.length - 1][0];
  const maxAbs = Math.max(...data.map(d => Math.abs(d[1])), 1);
  const x = k => m.l + (kMax === kMin ? pw / 2 : (k - kMin) / (kMax - kMin) * pw);
  const y = v => m.t + ph / 2 - v / maxAbs * (ph / 2);
  const step = pw / data.length, bw = Math.max(1, Math.min(14, step - 2));
  const fmtMn = v => (v >= 0 ? "+" : "−") + (Math.abs(v) >= 1000 ? (Math.abs(v) / 1000).toFixed(2) + "B" : Math.round(Math.abs(v)) + "M");
  const ink = css("--muted"), grid = css("--grid"), pos = css("--lv-call"), neg = css("--lv-put"), zc = css("--lv-zero"), txt = css("--text");
  let g = `<line x1="${m.l}" x2="${W - m.r}" y1="${y(0)}" y2="${y(0)}" stroke="${css("--border")}"/>`;
  for (const v of [maxAbs, -maxAbs]) g += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="${grid}"/>
    <text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="${ink}">${fmtMn(v)}</text>`;
  g += `<text x="${m.l - 6}" y="${y(0) + 4}" text-anchor="end" font-size="11" fill="${ink}">0</text>`;
  for (const [k, v] of data) {
    const top = Math.min(y(v), y(0)), h = Math.max(1, Math.abs(y(v) - y(0)));
    g += `<rect x="${x(k) - bw / 2}" y="${top}" width="${bw}" height="${h}" rx="${Math.min(2, bw / 2)}" fill="${v >= 0 ? pos : neg}"/>`;
  }
  // Vertical markers: label on the side away from the other marker so they never collide
  const zg = current.levels.find(l => l.label.startsWith("Zero"));
  const zk = zg ? zg.price : null;
  const marker = (k, label, color, other, row) => {
    if (k == null) return "";
    const right = other == null ? true : k >= other;          // put label on the outer side
    const tx = x(k) + (right ? 5 : -5);
    return `<line x1="${x(k)}" x2="${x(k)}" y1="${m.t}" y2="${H - m.b}" stroke="${color}" stroke-width="1.5" stroke-dasharray="4 3"/>
      <text x="${tx}" y="${m.t + 10 + row * 14}" text-anchor="${right ? "start" : "end"}" font-size="11" fill="${txt}">${label}</text>`;
  };
  g += marker(current.spot, "Spot " + Math.round(current.spot).toLocaleString(), txt, zk, 0)
     + marker(zk, "Zero γ " + (zk != null ? Math.round(zk).toLocaleString() : ""), zc, current.spot, 1);
  // Nice round strike ticks
  const raw = (kMax - kMin) / 6, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const tstep = [1, 2, 2.5, 5, 10].map(f => f * mag).find(v => v >= raw) || raw;
  for (let k = Math.ceil(kMin / tstep) * tstep; k <= kMax; k += tstep) {
    const anchor = x(k) < m.l + 24 ? "start" : x(k) > W - m.r - 24 ? "end" : "middle";
    g += `<text x="${x(k)}" y="${H - 8}" text-anchor="${anchor}" font-size="11" fill="${ink}">${k.toLocaleString()}</text>`;
  }
  g += `<rect id="hit" x="${m.l}" y="${m.t}" width="${pw}" height="${ph}" fill="transparent"/>`;
  g += `<line id="hov" y1="${m.t}" y2="${H - m.b}" stroke="${ink}" stroke-width="1" visibility="hidden"/>`;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.innerHTML = g;

  const hit = svg.querySelector("#hit"), hov = svg.querySelector("#hov");
  hit.onmousemove = e => {
    const rect = svg.getBoundingClientRect(), px = (e.clientX - rect.left) * (W / rect.width);
    const k0 = kMin + (px - m.l) / pw * (kMax - kMin);
    const d = data.reduce((a, b) => Math.abs(b[0] - k0) < Math.abs(a[0] - k0) ? b : a);
    hov.setAttribute("x1", x(d[0])); hov.setAttribute("x2", x(d[0])); hov.setAttribute("visibility", "visible");
    tip.innerHTML = `<b>${d[0].toLocaleString()}</b> · ${fmtMn(d[1])} <span style="color:${ink}">(${((d[0] / current.spot - 1) * 100).toFixed(1)}% from spot)</span>`;
    tip.style.display = "block";
    const tx = (x(d[0]) / W) * rect.width;
    tip.style.left = Math.min(tx + 12, rect.width - tip.offsetWidth - 4) + "px"; tip.style.top = "34px";
  };
  hit.onmouseleave = () => { tip.style.display = "none"; hov.setAttribute("visibility", "hidden"); };
}
new ResizeObserver(() => drawGexBars()).observe($("gexwrap"));

// ---------- track record ----------
async function buildReport(){
  const btn = $("rep"); btn.disabled = true; btn.textContent = "Building…";
  const dte = $("dte").value === "" ? "None" : $("dte").value;
  try {
    const r = await fetch(`/api/report?sym=${$("sym").value}&max_dte=${dte}&weight=${$("wt").value}`);
    const d = await r.json(); if (!r.ok) throw new Error(d.detail || r.statusText);
    const S = d.summary;
    if (!S) {
      $("repout").innerHTML = `<p class="muted">No scored days yet for these settings (${d.logged_runs} runs saved in total).
        ${d.skipped && d.skipped.length ? "<br>" + d.skipped.map(esc).join("<br>") : ""}</p>`;
      return;
    }
    const tile = (k, v, sub) => `<div class="tile"><div class="k">${k}</div><div class="v">${v ?? "—"}</div><div class="s">${sub}</div></div>`;
    const pc = v => v == null ? "—" : v + "%";
    const early = S.days < 20 ? `<p class="health warn">⚠ Only ${S.days} day${S.days === 1 ? "" : "s"} scored — treat this as an early read. 20+ days gives a much more trustworthy picture.</p>` : "";
    const yes = b => b == null ? "—" : b ? "✓" : "✗";
    const wallCell = w => w.beyond ? "opened beyond" : w.touched ? `touched · ${w.held ? "held ✓" : "broke ✗"}` : "not reached";
    $("repout").innerHTML = `${early}
      <div class="tiles">
        ${tile("Call wall held", pc(S.call_hold_pct), `touched ${pc(S.call_touch_pct)} of days · avg pullback ${S.call_reaction ?? "—"} pts`)}
        ${tile("Put wall held", pc(S.put_hold_pct), `touched ${pc(S.put_touch_pct)} of days · avg bounce ${S.put_reaction ?? "—"} pts`)}
        ${tile("Stayed inside walls", pc(S.inside_pct), "closes between put & call wall")}
        ${tile("Range · positive γ", S.pos_range ?? "—", `${S.pos_days} days opened above zero γ`)}
        ${tile("Range · negative γ", S.neg_range ?? "—", `${S.neg_days} days opened below zero γ`)}
        ${tile("Zero-γ crosses / day", S.avg_zg_crosses ?? "—", "5-min closes crossing zero γ")}
      </div>
      <p class="muted">If negative-gamma days show a clearly bigger range than positive-gamma days, the regime read is working. Hold rates well above 50% suggest the walls are worth fading.</p>
      <div class="scroll"><table id="rtable"><thead><tr><th>Date</th><th>Run</th><th>Regime</th><th class="num">Range</th>
        <th>Call wall</th><th>Put wall</th><th>Inside</th><th class="num">ZG crosses</th></tr></thead><tbody>
        ${d.days.map(x => `<tr><td>${x.date}</td><td>${esc(x.run)} ${x.time}</td>
          <td class="${x.regime === "pos" ? "Call" : x.regime === "neg" ? "Put" : ""}">${x.regime === "pos" ? "▲ positive" : x.regime === "neg" ? "▼ negative" : "—"}</td>
          <td class="num">${x.range.toLocaleString()}</td>
          <td>${wallCell(x.call)}</td>
          <td>${wallCell(x.put)}</td>
          <td>${yes(x.inside)}</td><td class="num">${x.zg_crosses ?? "—"}</td></tr>`).join("")}
      </tbody></table></div>
      <p class="muted">${d.logged_runs} runs saved in ${esc(d.db_path)}${d.skipped && d.skipped.length ? " · skipped: " + d.skipped.map(esc).join("; ") : ""}</p>`;
  } catch (e) {
    $("repout").innerHTML = `<p class="err">${esc(e.message)}</p>`;
  } finally { btn.disabled = false; btn.textContent = "Build report"; }
}
$("rep").onclick = buildReport;

// ---------- live read ----------
let liveState = null;
let events = store.get("gex_events", {date: "", list: []});
const fmtP = v => v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const sgn = v => (v >= 0 ? "+" : "−") + Math.abs(v).toLocaleString(undefined, {maximumFractionDigits: 2});

function thirdFriday(y, m){ const dow = new Date(Date.UTC(y, m - 1, 1)).getUTCDay(); return 1 + ((5 - dow + 7) % 7) + 14; }

function logEvent(text, notify){
  const now = etNow();
  if (events.date !== now.date) events = {date: now.date, list: []};
  const hh = String(Math.floor(now.mins / 60)).padStart(2, "0"), mm = String(now.mins % 60).padStart(2, "0");
  events.list.unshift(`${hh}:${mm} ET — ${text}`); events.list = events.list.slice(0, 40);
  store.set("gex_events", events); renderEvents();
  if (notify && $("livealerts").checked) {
    beep(); flashTitle("● " + text.slice(0, 40));
    try { if ("Notification" in window && Notification.permission === "granted") new Notification("GEX live read", {body: text}); } catch {}
  }
}
function renderEvents(){
  const now = etNow();
  const list = events.date === now.date ? events.list : [];
  $("events").innerHTML = list.length ? list.map(e => `<li>${esc(e)}</li>`).join("") : `<li class="muted">Nothing yet today.</li>`;
}

function playbook(pos, zone){
  const P = {
    "above-call": ["Above the call wall", "Extension territory. Moves here are often stretched — tighten stops on longs and watch for a failed breakout back under the wall."],
    "near-call":  pos ? ["Call wall overhead · positive gamma", "Rallies tend to stall here. Look for rejection to fade, or take profits on longs."]
                      : ["Call wall overhead · negative gamma", "Squeezes can run through the wall. Respect momentum; don't short just because it's a level."],
    "near-zero":  ["At the zero-gamma pivot", "A cross can flip the day from chop to trend. Wait for price to hold one side before committing."],
    "middle":     pos ? ["Positive gamma · mid-range", "Expect chop and mean reversion. Fade moves toward the walls; avoid chasing breakouts."]
                      : ["Negative gamma · mid-range", "Moves tend to extend. Favor trading with momentum, give stops room, and consider smaller size."],
    "near-put":   pos ? ["Put wall support · positive gamma", "Dips tend to get bought here. Look for a hold to go long, stop just under the wall."]
                      : ["Put wall · negative gamma", "A clean break can accelerate. Watch for breakdown continuation; don't assume support holds."],
    "below-put":  ["Below the put wall", "Downside acceleration territory. A reclaim of the wall is the reversal tell; until then, respect the trend."],
  };
  return P[zone];
}

function drawGauge(price, lv){
  const svg = $("gauge"), W = svg.clientWidth || 700, H = 78, ml = 10, mr = 10;
  const pts = [price, ...lv.map(l => l.p)];
  let lo = Math.min(...pts), hi = Math.max(...pts); const pad = (hi - lo) * 0.06 || 10; lo -= pad; hi += pad;
  const x = v => ml + (v - lo) / (hi - lo) * (W - ml - mr);
  const zg = lv.find(l => l.t === "Zero"), ty = 40, ink = css("--muted"), txt = css("--text");
  let g = "";
  if (zg) {
    g += `<rect x="${ml}" y="${ty - 5}" width="${Math.max(0, x(zg.p) - ml)}" height="10" rx="5" fill="${css("--lv-put")}" opacity=".18"/>`;
    g += `<rect x="${x(zg.p)}" y="${ty - 5}" width="${Math.max(0, W - mr - x(zg.p))}" height="10" rx="5" fill="${css("--lv-call")}" opacity=".18"/>`;
  } else g += `<rect x="${ml}" y="${ty - 5}" width="${W - ml - mr}" height="10" rx="5" fill="${css("--grid")}"/>`;
  // level ticks with staggered labels
  const majors = lv.filter(l => ["Call", "Put", "Zero"].includes(l.t)).sort((a, b) => a.p - b.p);
  lv.filter(l => !["Call", "Put", "Zero"].includes(l.t)).forEach(l =>
    g += `<line x1="${x(l.p)}" x2="${x(l.p)}" y1="${ty - 7}" y2="${ty + 7}" stroke="${css(LEVEL_STYLE[l.t].c)}" stroke-width="1.5"/>`);
  let lastX = -1e9, row = 0;
  for (const l of majors) {
    const xx = x(l.p); row = xx - lastX < 90 ? 1 - row : 0; lastX = xx;
    const anchor = xx < 40 ? "start" : xx > W - 40 ? "end" : "middle";
    g += `<line x1="${xx}" x2="${xx}" y1="${ty - 9}" y2="${ty + 9}" stroke="${css(LEVEL_STYLE[l.t].c)}" stroke-width="3" stroke-linecap="round"/>
      <text x="${xx}" y="${ty + 22 + row * 13}" text-anchor="${anchor}" font-size="11" fill="${ink}">${LEVEL_STYLE[l.t].name}</text>`;
  }
  const px = x(price), pa = px < 50 ? "start" : px > W - 50 ? "end" : "middle";
  g += `<path d="M${px - 6},${ty - 16} L${px + 6},${ty - 16} L${px},${ty - 7} Z" fill="${txt}"/>
    <text x="${px}" y="${ty - 21}" text-anchor="${pa}" font-size="12" font-weight="600" fill="${txt}">${fmtP(price)}</text>`;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.innerHTML = g;
}

async function liveTick(){
  if (!current) return;
  let h;
  try {
    const r = await fetch(`/api/history?sym=${current.symbol}&rng=1d`);
    h = await r.json(); if (!r.ok) throw new Error(h.detail || r.statusText);
  } catch (e) { $("liveupd").textContent = "Live price unavailable: " + e.message; return; }

  const fut = $("view").value === "futures" && h.futures.length;
  const series = fut ? h.futures : h.index;
  if (!series.length) { $("liveupd").textContent = "No price bars yet today."; return; }
  const bar = series[series.length - 1], price = bar.close, basis = fut ? (h.basis || 0) : 0;
  const lv = current.levels.map(l => ({t: lvType(l.label), p: l.price + basis, idx: l.price}));
  const get = t => lv.find(l => l.t === t);
  const cw = get("Call"), pw = get("Put"), zg = get("Zero");
  const prox = price * (parseFloat($("proxpct").value) || 0.15) / 100;
  const pos = zg ? price >= zg.p : null;

  // zone
  let zone = "middle";
  if (cw && price > cw.p + prox) zone = "above-call";
  else if (pw && price < pw.p - prox) zone = "below-put";
  else if (cw && Math.abs(price - cw.p) <= prox) zone = "near-call";
  else if (pw && Math.abs(price - pw.p) <= prox) zone = "near-put";
  else if (zg && Math.abs(price - zg.p) <= prox) zone = "near-zero";

  // header + price
  const barHM = new Date(bar.time * 1000).toISOString().slice(11, 16);
  $("liveupd").textContent = `Last bar ${barHM} ET · checked ${new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`;
  $("pxname").textContent = fut ? h.futures_ticker.replace("=F", " futures") : current.symbol;
  $("livepx").textContent = fmtP(price);
  $("pxsub").textContent = fut ? `basis ${sgn(basis)} vs ${current.symbol}` : "index";

  // regime
  const rg = $("regime");
  if (pos === null) { rg.className = "regime"; rg.innerHTML = `<b>NO ZERO GAMMA</b><span class="muted">Zero gamma wasn't found in these levels.</span>`; }
  else {
    const d = price - zg.p;
    rg.className = "regime " + (pos ? "pos" : "neg");
    rg.innerHTML = `<b>${pos ? "▲ POSITIVE GAMMA" : "▼ NEGATIVE GAMMA"}</b>
      <span>${sgn(d)} pts ${pos ? "above" : "below"} zero gamma (${fmtP(zg.p)}) · ${pos ? "expect chop / mean reversion" : "expect trend / bigger moves"}</span>`;
  }

  gaugeArgs = [price, lv]; drawGauge(price, lv);

  // distance chips (walls + zero first, then others nearest-first)
  const order = {Call: 0, Zero: 1, Put: 2};
  const chips = [...lv].sort((a, b) => (order[a.t] ?? 9) - (order[b.t] ?? 9) || Math.abs(a.p - price) - Math.abs(b.p - price));
  $("dists").innerHTML = chips.map(l => {
    const d = l.p - price, near = Math.abs(d) <= prox, st = LEVEL_STYLE[l.t];
    return `<span class="chip${near ? " near" : ""}" style="${near ? "border-color:" + css(st.c) : ""}"><i style="background:${css(st.c)}"></i>
      ${near ? "Near " : ""}${st.name} ${fmtP(l.p)} · ${sgn(d)} (${(d / price * 100).toFixed(2)}%)</span>`;
  }).join("");

  // playbook
  const pb = playbook(pos !== false, zone);
  $("play").innerHTML = `<b>${esc(pb[0])}</b>${esc(pb[1])}`;

  // cautions
  const now = etNow(), [Y, M, D] = now.date.split("-").map(Number), cautions = [];
  const weekday = !["Sat", "Sun"].includes(now.wd);
  if (weekday && D === thirdFriday(Y, M)) cautions.push("Monthly options expiration today — a lot of gamma rolls off; levels can lose their grip, especially into the afternoon.");
  const evKey = "gex_evt_" + now.date;
  $("evtday").checked = !!store.get(evKey, false);
  if ($("evtday").checked) cautions.push("News / event day — headline flow can overwhelm dealer hedging. Expect levels to break more easily.");
  const fm = /^@(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})/.exec(current.pine_string || "");
  if (fm && weekday) {
    const fMins = +fm[2] * 60 + +fm[3];
    const lastRun = runs.filter(r => r.on && r.t && toMins(r.t) <= now.mins).sort((a, b) => toMins(b.t) - toMins(a.t))[0];
    if (fm[1] !== now.date) cautions.push(`Levels are from ${fm[1]} — click Get levels for today's.`);
    else if (lastRun && fMins < toMins(lastRun.t)) cautions.push(`Levels are from before the ${lastRun.name} run (${lastRun.t} ET) — click Get levels to refresh.`);
  }
  if (weekday && now.mins >= 15 * 60 && now.mins < 16 * 60) cautions.push("Last hour — same-day gamma decays fast into the close; levels carry less weight.");
  const barMins = +barHM.slice(0, 2) * 60 + +barHM.slice(3, 5);
  if (weekday && now.mins >= 9 * 60 + 45 && now.mins < 16 * 60 && now.mins - barMins > 20) cautions.push("Price data looks delayed (last bar is over 20 minutes old).");
  $("cautions").innerHTML = cautions.length ? cautions.map(c => `<li>⚠ ${esc(c)}</li>`).join("") : `<li class="ok">No special cautions right now.</li>`;

  // events: regime flips and entering a near-wall / beyond-wall zone
  const state = {pos, zone};
  if (liveState) {
    if (pos !== null && liveState.pos !== null && pos !== liveState.pos)
      logEvent(`Crossed ${pos ? "ABOVE" : "BELOW"} zero gamma at ${fmtP(price)} → ${pos ? "positive" : "negative"} gamma`, true);
    if (zone !== liveState.zone && zone !== "middle") {
      const names = {"near-call": "Near the call wall", "near-put": "Near the put wall", "near-zero": "At the zero-gamma pivot", "above-call": "Broke above the call wall", "below-put": "Broke below the put wall"};
      logEvent(`${names[zone]} (${fmtP(price)})`, true);
    }
  }
  liveState = state;
}

$("evtday").onchange = () => { store.set("gex_evt_" + etNow().date, $("evtday").checked); liveTick(); };
$("proxpct").value = store.get("gex_prox", "0.15");
$("proxpct").onchange = () => { store.set("gex_prox", $("proxpct").value); liveTick(); };
$("livealerts").checked = store.get("gex_livealerts", true);
$("livealerts").onchange = () => store.set("gex_livealerts", $("livealerts").checked);
$("view").addEventListener("change", () => { liveState = null; liveTick(); });
let gaugeArgs = null, rsT = null;
window.addEventListener("resize", () => { clearTimeout(rsT); rsT = setTimeout(() => { if (gaugeArgs) drawGauge(...gaugeArgs); }, 150); });
renderEvents();
setInterval(liveTick, 30000);

renderRuns(); syncNotifBtn(); tick(); setInterval(tick, 15000);
load();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(PAGE.replace("__VERSION__", VERSION), headers={"Cache-Control": "no-store"})


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    if port_in_use(8000):
        print("\n*** An older GEX page is already running on port 8000, so this version can't start. ***")
        print("Close the other GEX window (or end python.exe in Task Manager), then run start_gex.bat again.\n")
        sys.exit(1)
    print(f"GEX page {VERSION} starting at http://localhost:8000")
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8000")).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)
