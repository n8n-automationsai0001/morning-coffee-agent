#!/usr/bin/env python3
"""Fetch PSE portfolio status and send to Telegram."""

import os
import re
import sys
import json
import datetime
import urllib.request
import urllib.error
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ── Optional: yfinance for analyst data ───────────────────────────────────────

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

# ── Optional: numpy for technical analysis forecasts ──────────────────────────

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR    = Path(__file__).parent       # stock-portfolio/
PROJECT_ROOT  = SCRIPT_DIR.parent           # repo root
HOLDINGS_FILE   = SCRIPT_DIR / 'holdings.json'
ENV_FILE        = PROJECT_ROOT / '.env'
PSE_ID_CACHE_FILE = SCRIPT_DIR / 'pse_edge_ids.json'

PSE_API          = "https://phisix-api3.appspot.com/stocks/{symbol}.json"  # kept for back-compat
PSE_API_ENDPOINTS = [
    "https://phisix-api3.appspot.com/stocks/{symbol}.json",
    "https://phisix-api4.appspot.com/stocks/{symbol}.json",
    "https://phisix-api.appspot.com/stocks/{symbol}.json",
    "https://phisix-api2.appspot.com/stocks/{symbol}.json",
]
PSE_EDGE_SEARCH  = "https://edge.pse.com.ph/autoComplete/searchCompanyNameSymbol.ax?term={symbol}"
PSE_EDGE_CHART   = "https://edge.pse.com.ph/common/DisclosureCht.ax"
PSE_EDGE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://edge.pse.com.ph/',
    'X-Requested-With': 'XMLHttpRequest',
    'Accept': 'application/json',
}

# Load persisted PSE Edge ID cache (ticker → [cmpy_id, security_id])
def _load_id_cache() -> dict:
    if PSE_ID_CACHE_FILE.exists():
        try:
            return json.loads(PSE_ID_CACHE_FILE.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}

def _save_id_cache(cache: dict):
    try:
        PSE_ID_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding='utf-8')
    except Exception:
        pass

_pse_edge_id_cache = _load_id_cache()  # ticker → [cmpy_id, security_id]

# Analyst recommendation key → display label
REC_MAP = {
    'strong_buy':   'Strong Buy',
    'buy':          'Buy',
    'hold':         'Neutral',
    'neutral':      'Neutral',
    'outperform':   'Buy',
    'underperform': 'Sell',
    'sell':         'Sell',
    'strong_sell':  'Strong Sell',
}


# ── Load .env ─────────────────────────────────────────────────────────────────

def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key   = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# ── Load holdings ─────────────────────────────────────────────────────────────

def load_holdings():
    with open(HOLDINGS_FILE, encoding='utf-8') as f:
        data = json.load(f)
    return data['holdings']


# ── Fetch prices from phisix-api (PSE live/last-close prices in PHP) ──────────

def fetch_price(ticker: str):
    """Returns (price_php, day_change_pct) or (None, None) on failure.

    Primary: phisix mirrors (live intraday). Falls back to PSE Edge's
    chart data (last close + previous close) when every phisix mirror
    fails — phisix is frequently flaky but PSE Edge is the official
    PSE source and stays reliable from cloud IPs.
    """
    price, pct = _fetch_price_phisix(ticker)
    if price is not None:
        return price, pct
    return _fetch_price_pse_edge(ticker)


def _fetch_price_phisix(ticker: str):
    last_err = None
    for url_template in PSE_API_ENDPOINTS:
        url = url_template.format(symbol=ticker)
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            stock = data['stocks'][0]
            price = float(stock['price']['amount'])
            pct   = float(stock.get('percentChange', 0))
            return price, pct
        except Exception as e:
            last_err = e
            continue
    print(f"  [Warning] phisix unavailable for {ticker} ({last_err}); trying PSE Edge")
    return None, None


def _fetch_price_pse_edge(ticker: str):
    """Last close from PSE Edge chart data. Day % = (last - prev) / prev * 100.
    Reuses the cached company/security IDs from _get_pse_edge_ids.
    """
    closes = fetch_pse_history(ticker)
    if not closes or len(closes) < 2:
        print(f"  [Warning] PSE Edge also unavailable for {ticker}")
        return None, None
    last = closes[-1]
    prev = closes[-2]
    pct = ((last - prev) / prev) * 100 if prev else 0.0
    return last, pct


# ── Fetch analyst data from Yahoo Finance ─────────────────────────────────────

def fetch_analyst_data(ticker: str):
    """Returns (target_mean_price, signal_label) from Yahoo Finance consensus.
    Falls back to (None, None) if yfinance not installed or no data found.
    """
    if not YFINANCE_AVAILABLE:
        return None, None
    try:
        info = yf.Ticker(f"{ticker}.PS").info
        target = info.get('targetMeanPrice')
        rec_key = info.get('recommendationKey', '').lower().replace(' ', '_')
        signal = REC_MAP.get(rec_key)
        return target, signal
    except Exception:
        return None, None


# ── PSE Edge historical data ──────────────────────────────────────────────────

def _get_pse_edge_ids(ticker: str):
    """Returns (cmpy_id, security_id) for a PSE ticker via PSE Edge.
    Caches results to avoid redundant HTTP calls.
    """
    if ticker in _pse_edge_id_cache:
        return _pse_edge_id_cache[ticker]
    try:
        # Step 1: autocomplete → cmpy_id
        url = PSE_EDGE_SEARCH.format(symbol=ticker)
        req = urllib.request.Request(url, headers=PSE_EDGE_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            results = json.loads(r.read())
        match = next((x for x in results if x.get('symbol') == ticker), None)
        if not match:
            return None, None
        cmpy_id = match['cmpyId']

        # Step 2: company page HTML → security_id (first occurrence)
        page_url = f"https://edge.pse.com.ph/companyPage/stockData.do?cmpy_id={cmpy_id}"
        req2 = urllib.request.Request(page_url, headers=PSE_EDGE_HEADERS)
        with urllib.request.urlopen(req2, timeout=10) as r2:
            html = r2.read().decode('utf-8', errors='replace')
        sec_match = re.search(r'security_id[^0-9]{0,15}(\d+)', html)
        if not sec_match:
            return None, None
        security_id = sec_match.group(1)

        _pse_edge_id_cache[ticker] = [cmpy_id, security_id]
        _save_id_cache(_pse_edge_id_cache)
        return cmpy_id, security_id
    except Exception:
        return None, None


_pse_history_cache = {}  # ticker -> list[float] | None; avoids duplicate PSE Edge calls in one run


def fetch_pse_history(ticker: str):
    """Returns a list of closing prices (float) from PSE Edge.
    Returns None on failure. Per-process cache so price + forecast paths
    don't double-fetch. Retries on transient 5xx/timeout.

    Threshold for "enough data" is decided by the caller:
      - fetch_price path needs just 2 points (last + prev close)
      - fetch_technical_forecasts path needs >= 60 for meaningful regression
    """
    import time as _time

    if ticker in _pse_history_cache:
        return _pse_history_cache[ticker]
    cmpy_id, security_id = _get_pse_edge_ids(ticker)
    if not cmpy_id or not security_id:
        _pse_history_cache[ticker] = None
        return None

    end   = datetime.datetime.now().strftime('%m/%d/%Y')
    start = (datetime.datetime.now() - datetime.timedelta(days=730)).strftime('%m/%d/%Y')
    payload = json.dumps({
        'cmpy_id': cmpy_id,
        'security_id': security_id,
        'startDate': start,
        'endDate': end,
    }).encode()
    headers = {**PSE_EDGE_HEADERS,
               'Content-Type': 'application/json',
               'Referer': f'https://edge.pse.com.ph/companyPage/stockData.do?cmpy_id={cmpy_id}'}

    delay = 1
    for attempt in range(3):
        try:
            req = urllib.request.Request(PSE_EDGE_CHART, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            chart = data.get('chartData', [])
            if not chart:
                raise RuntimeError('empty chartData')
            closes = [float(row['CLOSE']) for row in chart]
            _pse_history_cache[ticker] = closes
            return closes
        except Exception as e:
            if attempt < 2:
                _time.sleep(delay)
                delay *= 2
            else:
                print(f"  [PSE Edge] history fetch failed for {ticker} after 3 attempts: {e}")

    _pse_history_cache[ticker] = None
    return None


# ── Fetch historical prices and compute technical forecasts ───────────────────

def fetch_technical_forecasts(ticker: str):
    """Returns (regression_target, regression_low, regression_high, fib_1272, fib_1618, fib_2000)
    using 2 years of historical daily close prices from PSE Edge.
    All values in PHP. Returns (None,)*6 on failure.
    """
    if not NUMPY_AVAILABLE:
        return None, None, None, None, None, None
    try:
        closes_list = fetch_pse_history(ticker)
        if not closes_list or len(closes_list) < 60:
            # Regression + Fibonacci need enough history to be meaningful
            return None, None, None, None, None, None

        closes = np.array(closes_list, dtype=float)
        n = len(closes)

        # ── Linear Regression (log-linear) ────────────────────────────────────
        x = np.arange(n)
        log_prices = np.log(closes)
        slope, intercept = np.polyfit(x, log_prices, 1)

        # Project 252 trading days forward
        future_x = n + 252
        reg_target = np.exp(intercept + slope * future_x)

        # Channel: ±1 std dev of residuals
        residuals = log_prices - (intercept + slope * x)
        std = np.std(residuals)
        reg_low  = np.exp(intercept + slope * future_x - std)
        reg_high = np.exp(intercept + slope * future_x + std)

        # ── Fibonacci Extensions (based on 1-year swing) ──────────────────────
        recent = closes[-252:] if n >= 252 else closes
        swing_low  = float(np.min(recent))
        swing_high = float(np.max(recent))
        diff = swing_high - swing_low

        fib_1272 = swing_low + diff * 1.272
        fib_1618 = swing_low + diff * 1.618
        fib_2000 = swing_low + diff * 2.000

        return reg_target, reg_low, reg_high, fib_1272, fib_1618, fib_2000

    except Exception:
        return None, None, None, None, None, None


# ── Format output ─────────────────────────────────────────────────────────────

def sign(n):
    return '+' if n >= 0 else ''


def build_report(holdings: list) -> str:
    today = datetime.datetime.now().strftime('%b %d, %Y')
    lines = [
        f"PORTFOLIO STATUS — {today}",
        "=" * 44,
    ]

    total_cost  = 0.0
    total_value = 0.0
    total_value_valid = True
    analyst_summary = []

    for h in holdings:
        ticker    = h['ticker']
        avg_price = h['avg_price']
        shares    = h['shares']

        price, day_pct = fetch_price(ticker)
        target_price, signal = fetch_analyst_data(ticker)
        reg_target, reg_low, reg_high, fib_1272, fib_1618, fib_2000 = fetch_technical_forecasts(ticker)

        cost = avg_price * shares
        total_cost += cost

        lines.append("")
        lines.append(f"{ticker}  ({shares:,} shares)")

        if price is None:
            total_value_valid = False
            lines.append(f"  Avg:   PHP {avg_price:,.2f}")
            lines.append(f"  Now:   N/A")
            lines.append(f"  Cost:  PHP {cost:,.2f}")
        else:
            value    = price * shares
            gain_php = value - cost
            gain_pct = (gain_php / cost) * 100
            total_value += value

            day_str = f"  ({sign(day_pct)}{day_pct:.2f}% today)"
            lines.append(f"  Avg:   PHP {avg_price:,.2f}  |  Now: PHP {price:,.2f}{day_str}")
            lines.append(f"  Cost:  PHP {cost:,.2f}  |  Value: PHP {value:,.2f}")
            lines.append(f"  P&L:   {sign(gain_php)}PHP {gain_php:,.2f} ({sign(gain_pct)}{gain_pct:.2f}%)")

        # Forecast lines
        if price:
            # Analyst consensus
            if target_price:
                upside = (target_price - price) / price * 100
                sig_str = signal or 'N/A'
                lines.append(f"  Analyst:    PHP {target_price:,.2f} ({sign(upside)}{upside:.1f}%)  |  Rating: {sig_str}")
                analyst_summary.append((ticker, sig_str, upside))
            elif signal:
                lines.append(f"  Rating: {signal}")
                analyst_summary.append((ticker, signal, None))

            # Linear regression forecast
            if reg_target:
                reg_upside = (reg_target - price) / price * 100
                lines.append(f"  Regression: PHP {reg_target:,.2f} ({sign(reg_upside)}{reg_upside:.1f}%)  |  Range: PHP {reg_low:,.2f}–{reg_high:,.2f}")

            # Fibonacci extension forecast
            if fib_1618:
                fib_upside_1272 = (fib_1272 - price) / price * 100
                fib_upside_1618 = (fib_1618 - price) / price * 100
                fib_upside_2000 = (fib_2000 - price) / price * 100
                lines.append(f"  Fib 1.272:  PHP {fib_1272:,.2f} ({sign(fib_upside_1272)}{fib_upside_1272:.1f}%)")
                lines.append(f"  Fib 1.618:  PHP {fib_1618:,.2f} ({sign(fib_upside_1618)}{fib_upside_1618:.1f}%)  <-- primary target")
                lines.append(f"  Fib 2.000:  PHP {fib_2000:,.2f} ({sign(fib_upside_2000)}{fib_upside_2000:.1f}%)")

        elif not YFINANCE_AVAILABLE:
            lines.append(f"  Forecasts: install yfinance + numpy for technical analysis")

    # Overall
    lines.append("")
    lines.append("=" * 44)
    lines.append("OVERALL SUMMARY")
    lines.append(f"  Total Cost:   PHP {total_cost:,.2f}")

    if total_value_valid and total_value > 0:
        total_gain     = total_value - total_cost
        total_gain_pct = (total_gain / total_cost * 100) if total_cost else 0
        lines.append(f"  Market Value: PHP {total_value:,.2f}")
        lines.append(f"  Total P&L:    {sign(total_gain)}PHP {total_gain:,.2f} ({sign(total_gain_pct)}{total_gain_pct:.2f}%)")
    else:
        lines.append("  Market Value: N/A (some prices unavailable)")

    # Analyst summary table
    if analyst_summary:
        lines.append("")
        lines.append("ANALYST RATINGS SUMMARY")
        lines.append("-" * 44)
        for ticker, sig, upside in analyst_summary:
            upside_str = f"  ({sign(upside)}{upside:.1f}% upside)" if upside is not None else ""
            lines.append(f"  {ticker:<8} {sig}{upside_str}")

    return "\n".join(lines)


def build_telegram_table(holdings: list) -> str:
    """Compact monospace table for Telegram — one row per stock."""
    today = datetime.datetime.now().strftime('%b %d, %Y')

    # Collect all row data first
    rows = []
    total_cost  = 0.0
    total_value = 0.0

    for h in holdings:
        ticker    = h['ticker']
        avg_price = h['avg_price']
        shares    = h['shares']

        price, day_pct = fetch_price(ticker)
        _, _ = fetch_analyst_data(ticker)  # still fetched but not shown in table
        reg_target, _, _, _, fib_1618, _ = fetch_technical_forecasts(ticker)

        cost = avg_price * shares
        total_cost += cost

        if price is not None:
            value    = price * shares
            gain_pct = (value - cost) / cost * 100
            total_value += value
            day_str  = f"{sign(day_pct)}{day_pct:.1f}%"
            pnl_str  = f"{sign(gain_pct)}{gain_pct:.1f}%"
            now_str  = f"{price:,.2f}"
        else:
            day_str  = "N/A"
            pnl_str  = "N/A"
            now_str  = "N/A"

        reg_str = f"{reg_target:,.2f}" if reg_target else "N/A"
        fib_str = f"{fib_1618:,.2f}"  if fib_1618  else "N/A"

        # Buy/Sell: if REG target > avg_price → Buy, else Sell
        if reg_target is not None:
            signal_str = "Buy" if reg_target > avg_price else "Sell"
        else:
            signal_str = "N/A"

        rows.append((ticker, now_str, day_str, pnl_str, reg_str, fib_str, signal_str))

    # Fixed column widths
    C = [6, 8, 7, 7, 9, 9, 5]  # STOCK, NOW, DAY, P&L%, REG, FIB1.618, SIG
    sep = "  ".join("-" * w for w in C)
    hdr = "  ".join(h.ljust(w) for h, w in zip(
        ["STOCK", "NOW", "DAY", "P&L%", "REG", "FIB1.618", "SIG"], C))

    table_lines = [
        f"PORTFOLIO — {today}",
        "",
        hdr,
        sep,
    ]
    for ticker, now_str, day_str, pnl_str, reg_str, fib_str, signal_str in rows:
        cols = [ticker, now_str, day_str, pnl_str, reg_str, fib_str, signal_str]
        table_lines.append("  ".join(v.ljust(w) for v, w in zip(cols, C)))

    table_lines.append(sep)

    # Summary row
    if total_value > 0:
        total_gain     = total_value - total_cost
        total_gain_pct = (total_gain / total_cost * 100) if total_cost else 0
        summary = f"MKT: {total_value:,.0f}  P&L: {sign(total_gain)}{total_gain:,.0f} ({sign(total_gain_pct)}{total_gain_pct:.1f}%)"
    else:
        summary = "Market value unavailable"
    table_lines.append(summary)

    table_text = "\n".join(table_lines)
    return f"<pre>{table_text}</pre>"


# ── Telegram ──────────────────────────────────────────────────────────────────

def get_telegram_chat_id(bot_token: str):
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        for update in reversed(data.get('result', [])):
            msg = update.get('message') or update.get('channel_post')
            if msg:
                return str(msg['chat']['id'])
    except Exception as e:
        print(f"  Could not auto-detect Telegram chat ID: {e}")
    return None


def send_telegram(message: str, parse_mode: str = None):
    bot_token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not bot_token:
        print("  [Telegram] Skipped — TELEGRAM_BOT_TOKEN not set in .env")
        return

    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not chat_id:
        print("  [Telegram] TELEGRAM_CHAT_ID not set — attempting auto-detect...")
        chat_id = get_telegram_chat_id(bot_token)
        if chat_id:
            print(f"  [Telegram] Detected chat ID: {chat_id}. Add to .env to skip next time.")
        else:
            print("  [Telegram] Could not detect chat ID. Send a message to your bot first.")
            return

    msg_data = {"chat_id": chat_id, "text": message}
    if parse_mode:
        msg_data["parse_mode"] = parse_mode
    payload = json.dumps(msg_data).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        if result.get('ok'):
            print("  [Telegram] Sent.")
        else:
            print(f"  [Telegram] Error: {result.get('description')}")
    except urllib.error.HTTPError as e:
        print(f"  [Telegram] Failed: {e} — {e.read().decode('utf-8', errors='replace')}")
    except Exception as e:
        print(f"  [Telegram] Failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    load_env()
    holdings = load_holdings()

    print("Fetching current prices from PSE...")
    if NUMPY_AVAILABLE:
        print("Fetching technical forecasts (regression + Fibonacci) from PSE Edge...")
    else:
        print("  [Info] numpy not installed — run: pip install numpy  (for technical forecasts)")
    if YFINANCE_AVAILABLE:
        print("Fetching analyst consensus from Yahoo Finance...")
    else:
        print("  [Info] yfinance not installed — run: pip install yfinance  (for analyst ratings)")

    report   = build_report(holdings)
    table    = build_telegram_table(holdings)

    print("\n" + report)
    print("\nSending to Telegram...")
    send_telegram(table, parse_mode="HTML")


if __name__ == '__main__':
    main()
