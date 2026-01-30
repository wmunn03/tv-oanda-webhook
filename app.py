import os
import json
import time
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# -----------------------------
# ENV VARS (set in Render)
# -----------------------------
OANDA_TOKEN = os.environ.get("OANDA_TOKEN", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
OANDA_ENV = os.environ.get("OANDA_ENV", "practice").lower()  # "practice" or "live"
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_TOKEN", "")

# Default execution sizing
DEFAULT_QTY = int(os.environ.get("DEFAULT_QTY", "1000"))

# Default (fallback) SL/TP in pips
DEFAULT_SL_PIPS = float(os.environ.get("DEFAULT_SL_PIPS", "5"))
DEFAULT_TP_PIPS = float(os.environ.get("DEFAULT_TP_PIPS", "10"))

# Execution safety guards
MAX_SLIPPAGE_PIPS = float(os.environ.get("MAX_SLIPPAGE_PIPS", "1.5"))   # reject if price drifts too far
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "60"))        # per-instrument cooldown

# AND-stacking settings
AND_WINDOW_SECONDS = int(os.environ.get("AND_WINDOW_SECONDS", "420"))   # 7 min default
ATR_SL_MULT = float(os.environ.get("ATR_SL_MULT", "1.0"))               # stopPips = max(atrPips*mult, DEFAULT_SL_PIPS)
FIXED_TP_PIPS = float(os.environ.get("FIXED_TP_PIPS", str(DEFAULT_TP_PIPS)))
REVERSE_ON_FLIP = os.environ.get("REVERSE_ON_FLIP", "true").lower() == "true"

# Break-even manager
BE_ENABLED = os.environ.get("BE_ENABLED", "true").lower() == "true"
BE_TRIGGER_PIPS = float(os.environ.get("BE_TRIGGER_PIPS", "4"))
BE_OFFSET_PIPS = float(os.environ.get("BE_OFFSET_PIPS", "0.5"))
BE_POLL_SECONDS = int(os.environ.get("BE_POLL_SECONDS", "5"))

INSTRUMENT_MAP = {
    "EURUSD": "EUR_USD",
    "EUR_USD": "EUR_USD",
}

# In-memory state (resets on restart)
_last_trade_ts = {}    # instrument -> epoch seconds
_signal_cache = {}     # instrument -> event_type -> {"ts":..., "close":..., "atrPips":...}
_be_thread_started = False


# -----------------------------
# OANDA HELPERS
# -----------------------------
def oanda_base_url():
    return "https://api-fxtrade.oanda.com" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com"


def oanda_headers():
    return {
        "Authorization": "Bearer " + OANDA_TOKEN,
        "Content-Type": "application/json",
    }


def pip_size_for(instrument: str) -> float:
    # EUR_USD pip = 0.0001, JPY pairs usually 0.01
    return 0.01 if instrument.endswith("JPY") else 0.0001


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


def pips_between(price_a: float, price_b: float, pip: float) -> float:
    return abs(price_a - price_b) / pip


def get_position(instrument: str):
    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/positions/{instrument}"
    r = requests.get(url, headers=oanda_headers(), timeout=10)
    r.raise_for_status()
    return r.json().get("position", {})


def close_position_side(instrument: str, side: str):
    """
    side: "long" or "short"
    """
    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/positions/{instrument}/close"
    payload = {f"{side}Units": "ALL"}
    r = requests.put(url, headers=oanda_headers(), data=json.dumps(payload), timeout=10)
    print(f"[OANDA_CLOSE] side={side} status={r.status_code} resp={r.text[:250]}")
    r.raise_for_status()
    return r.json()


def modify_trade_sl_tp(trade_id: str, sl_price: float = None, tp_price: float = None):
    """
    Update Stop Loss / Take Profit on an existing trade.
    OANDA: PUT /trades/{tradeID}/orders
    """
    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/trades/{trade_id}/orders"

    payload = {}
    if sl_price is not None:
        payload["stopLoss"] = {"price": f"{sl_price:.5f}"}
    if tp_price is not None:
        payload["takeProfit"] = {"price": f"{tp_price:.5f}"}

    if not payload:
        return None

    r = requests.put(url, headers=oanda_headers(), data=json.dumps(payload), timeout=10)
    print(f"[OANDA_MODIFY] trade={trade_id} status={r.status_code} resp={r.text[:250]}")
    r.raise_for_status()
    return r.json()


def cooldown_ok(instrument: str) -> bool:
    now = int(time.time())
    last = _last_trade_ts.get(instrument, 0)
    return (now - last) >= COOLDOWN_SECONDS


def mark_trade(instrument: str):
    _last_trade_ts[instrument] = int(time.time())


def is_soft_reject(msg: str) -> bool:
    msg = (msg or "").lower()
    return ("cooldown active" in msg) or ("price drift too large" in msg)


def place_market_order(instrument: str, units: int, sl_pips: float, tp_pips: float, alert_price=None):
    """
    units > 0 => buy/long, units < 0 => sell/short
    alert_price: optional TradingView close price captured at signal time (for drift guard)
    """
    mid, bid, ask = get_mid_bid_ask(instrument)
    pip = pip_size_for(instrument)

    # Drift guard (if TradingView provides close)
    if alert_price is not None:
        drift = pips_between(mid, float(alert_price), pip)
        print(
            f"[DRIFT] instrument={instrument} side={'BUY' if units>0 else 'SELL'} "
            f"tv_close={float(alert_price):.5f} mid={mid:.5f} bid={bid:.5f} ask={ask:.5f} drift_pips={drift:.2f}"
        )
        if drift > MAX_SLIPPAGE_PIPS:
            raise RuntimeError(
                f"Price drift too large ({drift:.2f} pips > {MAX_SLIPPAGE_PIPS:.2f} pips). Rejecting."
            )
    else:
        print(f"[PRICE] instrument={instrument} side={'BUY' if units>0 else 'SELL'} mid={mid:.5f} bid={bid:.5f} ask={ask:.5f} (no alert_price)")

    # Build SL/TP using CURRENT mid
    if units > 0:
        sl_price = mid - (sl_pips * pip)
        tp_price = mid + (tp_pips * pip)
        price_bound = ask + (MAX_SLIPPAGE_PIPS * pip)
    else:
        sl_price = mid + (sl_pips * pip)
        tp_price = mid - (tp_pips * pip)
        price_bound = bid - (MAX_SLIPPAGE_PIPS * pip)

    order_payload = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "REDUCE_FIRST",
            "priceBound": f"{price_bound:.5f}",
            "stopLossOnFill": {"price": f"{sl_price:.5f}"},
            "takeProfitOnFill": {"price": f"{tp_price:.5f}"},
        }
    }

    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=oanda_headers(), data=json.dumps(order_payload), timeout=10)
    print(f"[OANDA_ORDER] status={r.status_code} resp={r.text[:250]}")
    r.raise_for_status()
    return r.json()


# -----------------------------
# SIGNAL STACKING HELPERS
# -----------------------------
def remember_signal(instrument: str, event_type: str, payload: dict):
    now = int(time.time())
    cache = _signal_cache.setdefault(instrument, {})
    rec = {"ts": now}

    # TradingView sends close as string sometimes
    if payload.get("close") is not None:
        try:
            rec["close"] = float(payload.get("close"))
        except Exception:
            rec["close"] = None

    if payload.get("atrPips") is not None:
        try:
            rec["atrPips"] = float(payload.get("atrPips"))
        except Exception:
            rec["atrPips"] = None

    cache[event_type] = rec


def signal_fresh(instrument: str, event_type: str) -> bool:
    rec = _signal_cache.get(instrument, {}).get(event_type)
    if not rec:
        return False
    return (int(time.time()) - rec["ts"]) <= AND_WINDOW_SECONDS


def maybe_entry_from_signals(instrument: str):
    # LONG requires: ATR_OK + SO_BULL + OSC_BULL
    long_ready = signal_fresh(instrument, "ATR_OK") and signal_fresh(instrument, "SO_BULL") and signal_fresh(instrument, "OSC_BULL")
    # SHORT requires: ATR_OK + SO_BEAR + OSC_BEAR
    short_ready = signal_fresh(instrument, "ATR_OK") and signal_fresh(instrument, "SO_BEAR") and signal_fresh(instrument, "OSC_BEAR")

    if not (long_ready or short_ready):
        return None

    atr_rec = _signal_cache.get(instrument, {}).get("ATR_OK", {})
    atrPips = atr_rec.get("atrPips")
    if atrPips is None:
        return None

    sl_pips = max(atrPips * ATR_SL_MULT, DEFAULT_SL_PIPS)
    tp_pips = FIXED_TP_PIPS
    alert_price = atr_rec.get("close")  # used for drift guard

    if long_ready:
        return {"action": "buy", "sl_pips": sl_pips, "tp_pips": tp_pips, "alert_price": alert_price}
    else:
        return {"action": "sell", "sl_pips": sl_pips, "tp_pips": tp_pips, "alert_price": alert_price}


# -----------------------------
# BREAK-EVEN WORKER
# -----------------------------
def break_even_worker():
    if not BE_ENABLED:
        print("[BE] Disabled")
        return

    print("[BE] Worker started")
    while True:
        try:
            # Only manage instruments we map (currently EUR_USD)
            for _, instrument in INSTRUMENT_MAP.items():
                pos = get_position(instrument)

                long_units = float(pos.get("long", {}).get("units", "0"))
                short_units = float(pos.get("short", {}).get("units", "0"))

                if long_units == 0 and short_units == 0:
                    continue

                mid, bid, ask = get_mid_bid_ask(instrument)
                pip = pip_size_for(instrument)

                # LONG BE
                if long_units > 0:
                    avg = float(pos["long"].get("averagePrice", "0"))
                    profit_pips = (mid - avg) / pip

                    if profit_pips >= BE_TRIGGER_PIPS:
                        new_sl = avg + (BE_OFFSET_PIPS * pip)
                        trade_ids = pos["long"].get("tradeIDs", [])
                        for tid in trade_ids:
                            try:
                                modify_trade_sl_tp(tid, sl_price=new_sl)
                            except Exception as e:
                                print(f"[BE] modify long trade {tid} failed: {e}")

                # SHORT BE
                if short_units < 0:
                    avg = float(pos["short"].get("averagePrice", "0"))
                    profit_pips = (avg - mid) / pip

                    if profit_pips >= BE_TRIGGER_PIPS:
                        new_sl = avg - (BE_OFFSET_PIPS * pip)
                        trade_ids = pos["short"].get("tradeIDs", [])
                        for tid in trade_ids:
                            try:
                                modify_trade_sl_tp(tid, sl_price=new_sl)
                            except Exception as e:
                                print(f"[BE] modify short trade {tid} failed: {e}")

        except Exception as e:
            print(f"[BE] Worker error: {e}")

        time.sleep(BE_POLL_SECONDS)


def start_workers_once():
    global _be_thread_started
    if _be_thread_started:
        return
    t = threading.Thread(target=break_even_worker, daemon=True)
    t.start()
    _be_thread_started = True


# -----------------------------
# ROUTES
# -----------------------------
@app.get("/")
def health():
    start_workers_once()
    return "ok"


@app.post("/webhook")
def webhook():
    start_workers_once()

    # Auth via query param token
    token = request.args.get("token", "")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    # Validate required env vars
    if not OANDA_TOKEN or not OANDA_ACCOUNT_ID:
        return jsonify({"ok": False, "error": "Missing OANDA env vars"}), 500

    data = request.get_json(silent=True) or {}

    # -----------------------------
    # MODE A: Event stacking from LuxAlgo + ATR Gate
    # -----------------------------
    event_type = str(data.get("type", "")).upper().strip()
    if event_type in ("SO_BULL", "SO_BEAR", "OSC_BULL", "OSC_BEAR", "ATR_OK"):
        symbol = str(data.get("symbol", "")).upper()
        if symbol not in INSTRUMENT_MAP:
            return jsonify({"ok": False, "error": "Unsupported symbol: " + symbol}), 400

        instrument = INSTRUMENT_MAP[symbol]

        # Store the event
        remember_signal(instrument, event_type, data)

        # Current position state
        pos = get_position(instrument)
        long_units = float(pos.get("long", {}).get("units", "0"))
        short_units = float(pos.get("short", {}).get("units", "0"))
        in_long = long_units > 0
        in_short = short_units < 0

        flip_to_long = event_type in ("SO_BULL", "OSC_BULL")
        flip_to_short = event_type in ("SO_BEAR", "OSC_BEAR")

        # Early exit / reverse logic (flip on opposite signals)
        # If in long and bearish flip arrives: close long
        if in_long and flip_to_short:
            close_position_side(instrument, "long")
            # if not reversing, stop here
            if not REVERSE_ON_FLIP:
                return jsonify({"ok": True, "note": f"Closed LONG on {event_type}"}), 200

        # If in short and bullish flip arrives: close short
        if in_short and flip_to_long:
            close_position_side(instrument, "short")
            if not REVERSE_ON_FLIP:
                return jsonify({"ok": True, "note": f"Closed SHORT on {event_type}"}), 200

        # Entry trigger only when AND conditions satisfied
        trade = maybe_entry_from_signals(instrument)
        if not trade:
            return jsonify({"ok": True, "note": f"Stored {event_type}, waiting for other signals"}), 200

        # Cooldown: enforce for fresh entries, but allow reversals (we already closed above if needed)
        if not cooldown_ok(instrument):
            msg = f"Cooldown active ({COOLDOWN_SECONDS}s). Skipping."
            print(f"[SOFT_REJECT] {msg} instrument={instrument}")
            return jsonify({"ok": False, "error": msg, "soft": True}), 200

        qty = int(data.get("quantity", 0)) or DEFAULT_QTY
        action = trade["action"]
        units = qty if action == "buy" else -qty

        try:
            resp = place_market_order(
                instrument,
                units,
                float(trade["sl_pips"]),
                float(trade["tp_pips"]),
                alert_price=trade["alert_price"]
            )
            mark_trade(instrument)
            return jsonify({
                "ok": True,
                "mode": "event_stack",
                "fired": action,
                "instrument": instrument,
                "units": units,
                "sl_pips": trade["sl_pips"],
                "tp_pips": trade["tp_pips"],
                "oanda": resp
            }), 200

        except Exception as e:
            msg = str(e)
            if is_soft_reject(msg):
                print(f"[SOFT_REJECT] {msg}")
                return jsonify({"ok": False, "error": msg, "soft": True}), 200
            print(f"[ERROR] {msg}")
            return jsonify({"ok": False, "error": msg}), 500

    # -----------------------------
    # MODE B: Direct trade webhooks (backward compatible)
    # Expected: {"symbol":"EURUSD","action":"buy|sell","quantity":1000,"alert_price":1.2345,"sl_pips":5,"tp_pips":10}
    # -----------------------------
    symbol = str(data.get("symbol", "")).upper()
    action = str(data.get("action", "")).lower()
    qty = int(data.get("quantity", 0))
    alert_price = data.get("alert_price", None)

    if symbol not in INSTRUMENT_MAP:
        return jsonify({"ok": False, "error": "Unsupported symbol: " + symbol}), 400
    if action not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "Invalid action: " + action}), 400
    if qty <= 0:
        return jsonify({"ok": False, "error": "quantity must be > 0"}), 400

    instrument = INSTRUMENT_MAP[symbol]

    if not cooldown_ok(instrument):
        msg = f"Cooldown active ({COOLDOWN_SECONDS}s). Skipping."
        print(f"[SOFT_REJECT] {msg} instrument={instrument}")
        return jsonify({"ok": False, "error": msg, "soft": True}), 200

    sl_pips = float(data.get("sl_pips", DEFAULT_SL_PIPS))
    tp_pips = float(data.get("tp_pips", DEFAULT_TP_PIPS))
    units = qty if action == "buy" else -qty

    try:
        resp = place_market_order(instrument, units, sl_pips, tp_pips, alert_price=alert_price)
        mark_trade(instrument)
        return jsonify({
            "ok": True,
            "mode": "direct",
            "instrument": instrument,
            "action": action,
            "units": units,
            "sl_pips": sl_pips,
            "tp_pips": tp_pips,
            "alert_price": alert_price,
            "oanda": resp
        }), 200

    except Exception as e:
        msg = str(e)
        if is_soft_reject(msg):
            print(f"[SOFT_REJECT] {msg}")
            return jsonify({"ok": False, "error": msg, "soft": True}), 200
        print(f"[ERROR] {msg}")
        return jsonify({"ok": False, "error": msg}), 500
