import os, json, time, requests
from flask import Flask, request, jsonify
from datetime import datetime, timezone
import threading

app = Flask(__name__)

# -----------------------------
# ENV VARS (set in Render)
# -----------------------------
OANDA_TOKEN      = os.environ.get("OANDA_TOKEN", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
OANDA_ENV        = os.environ.get("OANDA_ENV", "practice").lower()
WEBHOOK_TOKEN    = os.environ.get("WEBHOOK_TOKEN", "")

# Risk management
RISK_PERCENT     = float(os.environ.get("RISK_PERCENT", "2.0"))  # Percent of account to risk per trade
MIN_UNITS        = int(os.environ.get("MIN_UNITS", "1000"))      # Minimum position size
MAX_UNITS        = int(os.environ.get("MAX_UNITS", "50000"))     # Maximum position size

DEFAULT_SL_PIPS  = float(os.environ.get("DEFAULT_SL_PIPS", "25"))
DEFAULT_TP_PIPS  = float(os.environ.get("DEFAULT_TP_PIPS", "50"))

# Time window (UTC, format "HH:MM")
TRADE_WINDOW_START = os.environ.get("TRADE_WINDOW_START", "08:00")
TRADE_WINDOW_END   = os.environ.get("TRADE_WINDOW_END", "15:00")
CLOSE_ALL_BY       = os.environ.get("CLOSE_ALL_BY", "17:00")

# Risk model
USE_ATR_STOPS = os.environ.get("USE_ATR_STOPS", "true").lower() in ("1", "true", "yes")
ATR_SL_MULT   = float(os.environ.get("ATR_SL_MULT", "1.5"))
ATR_TP_MULT   = float(os.environ.get("ATR_TP_MULT", "2.0"))
ATR_PERIOD    = int(os.environ.get("ATR_PERIOD", "14"))

# Execution guards
MAX_SLIPPAGE_PIPS = float(os.environ.get("MAX_SLIPPAGE_PIPS", "3.0"))
COOLDOWN_SECONDS  = int(os.environ.get("COOLDOWN_SECONDS", "300"))

INSTRUMENT_MAP = {
    "EURUSD": "EUR_USD",
    "EUR_USD": "EUR_USD",
    "OANDA:EURUSD": "EUR_USD",
}

_last_trade_ts = {}

# -----------------------------
# OANDA HELPERS
# -----------------------------
def oanda_base_url():
    return "https://api-fxtrade.oanda.com" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com"

def oanda_headers():
    return {"Authorization": f"Bearer {OANDA_TOKEN}", "Content-Type": "application/json"}

def pip_size_for(instrument: str) -> float:
    return 0.01 if instrument.endswith("JPY") else 0.0001

# -----------------------------
# TIME WINDOW HELPERS
# -----------------------------
def parse_time(t_str: str):
    """Parse HH:MM string to (hour, minute) tuple"""
    parts = t_str.strip().split(":")
    return int(parts[0]), int(parts[1])

def is_within_trade_window() -> bool:
    """Check if current UTC time is within TRADE_WINDOW_START and TRADE_WINDOW_END"""
    now = datetime.now(timezone.utc)
    
    # Skip weekends
    if now.weekday() >= 5:
        return False
    
    start_h, start_m = parse_time(TRADE_WINDOW_START)
    end_h, end_m = parse_time(TRADE_WINDOW_END)
    
    start_mins = start_h * 60 + start_m
    end_mins = end_h * 60 + end_m
    now_mins = now.hour * 60 + now.minute
    
    if start_mins <= end_mins:
        return start_mins <= now_mins <= end_mins
    else:
        return now_mins >= start_mins or now_mins <= end_mins

def is_past_close_time() -> bool:
    """Check if current UTC time is past CLOSE_ALL_BY"""
    now = datetime.now(timezone.utc)
    close_h, close_m = parse_time(CLOSE_ALL_BY)
    close_mins = close_h * 60 + close_m
    now_mins = now.hour * 60 + now.minute
    return now_mins >= close_mins

# -----------------------------
# OANDA DATA FUNCTIONS
# -----------------------------
def get_account_balance() -> float:
    """Fetch current account balance from OANDA"""
    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/summary"
    r = requests.get(url, headers=oanda_headers(), timeout=10)
    r.raise_for_status()
    balance = float(r.json()["account"]["balance"])
    return balance

def get_mid_bid_ask(instrument: str):
    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    r = requests.get(url, headers=oanda_headers(), params={"instruments": instrument}, timeout=10)
    r.raise_for_status()
    prices = r.json().get("prices", [])
    if not prices:
        raise RuntimeError("No pricing data returned from OANDA.")
    bid = float(prices[0]["bids"][0]["price"])
    ask = float(prices[0]["asks"][0]["price"])
    mid = (bid + ask) / 2.0
    return mid, bid, ask

def fetch_atr(instrument: str, granularity: str = "H1", period: int = 14) -> float:
    """Fetch candles from OANDA and calculate ATR"""
    url = f"{oanda_base_url()}/v3/instruments/{instrument}/candles"
    params = {
        "granularity": granularity,
        "count": period + 1,
        "price": "M"
    }
    r = requests.get(url, headers=oanda_headers(), params=params, timeout=10)
    r.raise_for_status()
    candles = r.json().get("candles", [])
    
    if len(candles) < period + 1:
        return None
    
    true_ranges = []
    for i in range(1, len(candles)):
        curr = candles[i]["mid"]
        prev = candles[i-1]["mid"]
        
        high = float(curr["h"])
        low = float(curr["l"])
        prev_close = float(prev["c"])
        
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    
    atr = sum(true_ranges[-period:]) / period
    return atr

def get_open_trades(instrument: str = None):
    """Get open trades, optionally filtered by instrument"""
    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=oanda_headers(), timeout=10)
    r.raise_for_status()
    trades = r.json().get("trades", [])
    
    if instrument:
        trades = [t for t in trades if t["instrument"] == instrument]
    
    return trades

def close_trade(trade_id: str):
    """Close a specific trade"""
    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/close"
    r = requests.put(url, headers=oanda_headers(), timeout=10)
    r.raise_for_status()
    return r.json()

def close_all_positions():
    """Close all open trades"""
    trades = get_open_trades()
    results = []
    for t in trades:
        try:
            result = close_trade(t["id"])
            results.append({"trade_id": t["id"], "instrument": t["instrument"], "result": "closed"})
            print(f"[AUTO_CLOSE] Closed trade {t['id']} on {t['instrument']}")
        except Exception as e:
            print(f"[AUTO_CLOSE_ERROR] Failed to close {t['id']}: {e}")
            results.append({"trade_id": t["id"], "error": str(e)})
    return results

# -----------------------------
# POSITION SIZING
# -----------------------------
def calculate_position_size(sl_pips: float, instrument: str) -> int:
    """
    Calculate position size based on account balance and risk percentage.
    
    Formula: Units = (Balance × Risk%) / (SL_Pips × Pip_Value_Per_Unit)
    
    For EUR/USD (quote = USD), pip value per unit = pip size (0.0001)
    """
    try:
        balance = get_account_balance()
        pip_size = pip_size_for(instrument)
        
        # Risk amount in dollars
        risk_amount = balance * (RISK_PERCENT / 100.0)
        
        # Pip value per unit (for USD-quoted pairs, this equals pip size)
        # For JPY pairs or non-USD quotes, this would need conversion
        pip_value_per_unit = pip_size
        
        # Calculate units
        units = risk_amount / (sl_pips * pip_value_per_unit)
        
        # Round down to nearest 100
        units = int(units // 100) * 100
        
        # Apply min/max limits
        units = max(MIN_UNITS, min(MAX_UNITS, units))
        
        print(f"[POSITION_SIZE] balance=${balance:.2f} risk={RISK_PERCENT}% (${risk_amount:.2f}) sl={sl_pips:.1f}pips -> {units} units")
        
        return units
        
    except Exception as e:
        print(f"[POSITION_SIZE_ERROR] {e}. Using MIN_UNITS={MIN_UNITS}")
        return MIN_UNITS

# -----------------------------
# TRADE HELPERS
# -----------------------------
def pips_between(a: float, b: float, pip: float) -> float:
    return abs(a - b) / pip

def cooldown_ok(instrument: str) -> bool:
    now = int(time.time())
    last = _last_trade_ts.get(instrument, 0)
    return (now - last) >= COOLDOWN_SECONDS

def mark_trade(instrument: str):
    _last_trade_ts[instrument] = int(time.time())

def safe_json():
    data = request.get_json(silent=True)
    if data is not None:
        return data, None

    raw = request.data.decode("utf-8", errors="replace").strip()
    if not raw:
        return None, "empty body"
    try:
        return json.loads(raw), None
    except Exception as e:
        return None, f"invalid JSON: {str(e)}"

def place_market_order(instrument: str, units: int, sl_pips: float, tp_pips: float, alert_price=None):
    mid, bid, ask = get_mid_bid_ask(instrument)
    pip = pip_size_for(instrument)

    if alert_price is not None:
        drift = pips_between(mid, float(alert_price), pip)
        print(f"[DRIFT] instrument={instrument} side={'BUY' if units>0 else 'SELL'} alert={float(alert_price):.5f} mid={mid:.5f} drift={drift:.2f}")
        if drift > MAX_SLIPPAGE_PIPS:
            raise RuntimeError(f"Price drift too large ({drift:.2f} > {MAX_SLIPPAGE_PIPS} pips)")

    if units > 0:
        sl_price = mid - (sl_pips * pip)
        tp_price = mid + (tp_pips * pip)
        price_bound = ask + (MAX_SLIPPAGE_PIPS * pip)
    else:
        sl_price = mid + (sl_pips * pip)
        tp_price = mid - (tp_pips * pip)
        price_bound = bid - (MAX_SLIPPAGE_PIPS * pip)

    payload = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "priceBound": f"{price_bound:.5f}",
            "stopLossOnFill": {"price": f"{sl_price:.5f}"},
            "takeProfitOnFill": {"price": f"{tp_price:.5f}"},
        }
    }

    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=oanda_headers(), data=json.dumps(payload), timeout=10)
    print(f"[OANDA_ORDER] status={r.status_code} resp={r.text[:300]}")
    r.raise_for_status()
    return r.json()

# -----------------------------
# BACKGROUND AUTO-CLOSE THREAD
# -----------------------------
def auto_close_worker():
    """Background thread that closes positions at CLOSE_ALL_BY time"""
    closed_today = False
    last_date = None
    
    while True:
        time.sleep(60)
        
        try:
            now = datetime.now(timezone.utc)
            today = now.date()
            
            if last_date != today:
                closed_today = False
                last_date = today
            
            # Skip weekends
            if now.weekday() >= 5:
                continue
            
            if is_past_close_time() and not closed_today:
                trades = get_open_trades()
                if trades:
                    print(f"[AUTO_CLOSE] Closing {len(trades)} position(s) at {now.strftime('%H:%M')} UTC")
                    close_all_positions()
                closed_today = True
        except Exception as e:
            print(f"[AUTO_CLOSE_ERROR] {e}")

auto_close_thread = threading.Thread(target=auto_close_worker, daemon=True)
auto_close_thread.start()

# -----------------------------
# ROUTES
# -----------------------------
@app.get("/")
def health():
    return "ok"

@app.get("/debug")
def debug():
    now_utc = datetime.now(timezone.utc)
    
    balance = None
    open_trades = []
    try:
        balance = get_account_balance()
        open_trades = get_open_trades()
    except:
        pass
    
    return jsonify({
        "ok": True,
        "current_time_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "day_of_week": now_utc.strftime("%A"),
        "trade_window": f"{TRADE_WINDOW_START} - {TRADE_WINDOW_END} UTC",
        "close_all_by": f"{CLOSE_ALL_BY} UTC",
        "within_trade_window": is_within_trade_window(),
        "past_close_time": is_past_close_time(),
        "account": {
            "balance": balance,
            "risk_percent": RISK_PERCENT,
            "risk_per_trade": round(balance * RISK_PERCENT / 100, 2) if balance else None,
        },
        "config": {
            "ATR_SL_MULT": ATR_SL_MULT,
            "ATR_TP_MULT": ATR_TP_MULT,
            "ATR_PERIOD": ATR_PERIOD,
            "COOLDOWN_SECONDS": COOLDOWN_SECONDS,
            "MAX_SLIPPAGE_PIPS": MAX_SLIPPAGE_PIPS,
            "MIN_UNITS": MIN_UNITS,
            "MAX_UNITS": MAX_UNITS,
        },
        "last_trade_ts": _last_trade_ts,
        "open_trades": open_trades
    })

@app.get("/close-all")
def close_all_endpoint():
    """Manual endpoint to close all positions"""
    token = request.args.get("token", "")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    
    results = close_all_positions()
    return jsonify({"ok": True, "closed": results})

@app.post("/webhook")
def webhook():
    token = request.args.get("token", "")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    if not OANDA_TOKEN or not OANDA_ACCOUNT_ID:
        return jsonify({"ok": False, "error": "Missing OANDA env vars"}), 500

    data, err = safe_json()
    if err:
        print(f"[BAD_JSON] {err}")
        return jsonify({"ok": False, "soft": True, "error": err}), 200

    # Normalize symbol
    symbol_raw = str(data.get("symbol", data.get("ticker", ""))).strip()
    symbol = symbol_raw.upper()
    if symbol not in INSTRUMENT_MAP:
        msg = f"Unsupported symbol: {symbol_raw}"
        print(f"[BAD_SYMBOL] {msg}")
        return jsonify({"ok": False, "soft": True, "error": msg}), 200

    instrument = INSTRUMENT_MAP[symbol]

    # Check trade window
    if not is_within_trade_window():
        now_utc = datetime.now(timezone.utc).strftime("%H:%M")
        msg = f"Outside trade window. Current: {now_utc} UTC, Window: {TRADE_WINDOW_START}-{TRADE_WINDOW_END} UTC"
        print(f"[WINDOW_REJECT] {msg}")
        return jsonify({"ok": False, "soft": True, "error": msg}), 200

    # Check if past close time
    if is_past_close_time():
        msg = f"Past daily close time ({CLOSE_ALL_BY} UTC). No new trades."
        print(f"[CLOSE_REJECT] {msg}")
        return jsonify({"ok": False, "soft": True, "error": msg}), 200

    # Cooldown check
    if not cooldown_ok(instrument):
        remaining = COOLDOWN_SECONDS - (int(time.time()) - _last_trade_ts.get(instrument, 0))
        msg = f"Cooldown active. {remaining}s remaining."
        print(f"[COOLDOWN] {msg}")
        return jsonify({"ok": False, "soft": True, "error": msg}), 200

    # Parse event type
    event_type = str(data.get("type", "")).upper()
    close_val = data.get("close", None)

    try:
        close_price = float(close_val) if close_val is not None else None
    except:
        close_price = None

    # Only process SO signals for intraday system
    if event_type not in ("SO_BULL", "SO_BEAR"):
        msg = f"Ignoring event: {event_type}. Only SO_BULL/SO_BEAR accepted."
        print(f"[IGNORE] {msg}")
        return jsonify({"ok": True, "note": msg}), 200

    direction = "BULL" if event_type == "SO_BULL" else "BEAR"
    action = "buy" if direction == "BULL" else "sell"
    
    print(f"[SIGNAL] {event_type} received for {instrument}")

    # Calculate SL/TP using ATR from OANDA
    if USE_ATR_STOPS:
        try:
            atr = fetch_atr(instrument, "H1", ATR_PERIOD)
            if atr:
                pip = pip_size_for(instrument)
                atr_pips = atr / pip
                sl_pips = max(10.0, atr_pips * ATR_SL_MULT)
                tp_pips = max(20.0, atr_pips * ATR_TP_MULT)
                print(f"[ATR] H1 ATR={atr:.5f} ({atr_pips:.1f} pips) -> SL={sl_pips:.1f} TP={tp_pips:.1f}")
            else:
                sl_pips, tp_pips = DEFAULT_SL_PIPS, DEFAULT_TP_PIPS
                print(f"[ATR] Could not calculate, using defaults: SL={sl_pips} TP={tp_pips}")
        except Exception as e:
            print(f"[ATR_ERROR] {e}. Using defaults.")
            sl_pips, tp_pips = DEFAULT_SL_PIPS, DEFAULT_TP_PIPS
    else:
        sl_pips, tp_pips = DEFAULT_SL_PIPS, DEFAULT_TP_PIPS

    # Calculate position size based on risk
    units = calculate_position_size(sl_pips, instrument)
    if action == "sell":
        units = -units

    # Place trade
    try:
        resp = place_market_order(instrument, units, sl_pips, tp_pips, alert_price=close_price)
        mark_trade(instrument)
        print(f"[TRADE] {action.upper()} {instrument} units={units} SL={sl_pips:.1f} TP={tp_pips:.1f}")
        return jsonify({
            "ok": True,
            "action": action,
            "instrument": instrument,
            "units": abs(units),
            "sl_pips": round(sl_pips, 1),
            "tp_pips": round(tp_pips, 1),
            "oanda": resp
        }), 200
    except Exception as e:
        msg = str(e)
        print(f"[ERROR] {msg}")
        return jsonify({"ok": False, "error": msg}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
