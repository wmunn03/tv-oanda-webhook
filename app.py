import os
import json
import time
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

DEFAULT_SL_PIPS = float(os.environ.get("DEFAULT_SL_PIPS", "5"))
DEFAULT_TP_PIPS = float(os.environ.get("DEFAULT_TP_PIPS", "10"))

# Execution safety guards
MAX_SLIPPAGE_PIPS = float(os.environ.get("MAX_SLIPPAGE_PIPS", "1.5"))   # reject if price drifts too far
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "60"))        # per-instrument cooldown

INSTRUMENT_MAP = {
    "EURUSD": "EUR_USD",
    "EUR_USD": "EUR_USD",
}

# In-memory cooldown state (note: resets on deploy/restart)
_last_trade_ts = {}  # instrument -> epoch seconds


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
    alert_price: (optional) TradingView close price captured at signal time
    """
    mid, bid, ask = get_mid_bid_ask(instrument)
    pip = pip_size_for(instrument)

    # Drift guard: if Pine sends alert_price, reject if moved too far
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
        # BUY: worst fill is higher; cap near ask
        price_bound = ask + (MAX_SLIPPAGE_PIPS * pip)
    else:
        sl_price = mid + (sl_pips * pip)
        tp_price = mid - (tp_pips * pip)
        # SELL: worst fill is lower; cap near bid
        price_bound = bid - (MAX_SLIPPAGE_PIPS * pip)

    order_payload = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            # Key change: lets OANDA reduce/flip without a separate close call
            "positionFill": "REDUCE_FIRST",
            # Key change: prevent terrible fills
            "priceBound": f"{price_bound:.5f}",
            "stopLossOnFill": {"price": f"{sl_price:.5f}"},
            "takeProfitOnFill": {"price": f"{tp_price:.5f}"},
        }
    }

    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=oanda_headers(), data=json.dumps(order_payload), timeout=10)
    print(f"[OANDA] status={r.status_code} resp={r.text[:250]}")
    r.raise_for_status()
    return r.json()


@app.get("/")
def health():
    return "ok"


@app.post("/webhook")
def webhook():
    # Auth via query param token
    token = request.args.get("token", "")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    # Validate required env vars
    if not OANDA_TOKEN or not OANDA_ACCOUNT_ID:
        return jsonify({"ok": False, "error": "Missing OANDA env vars"}), 500

    data = request.get_json(silent=True) or {}

    symbol = str(data.get("symbol", "")).upper()
    action = str(data.get("action", "")).lower()
    qty = int(data.get("quantity", 0))

    # Optional: Pine can send the signal bar close price for drift guarding
    alert_price = data.get("alert_price", None)

    if symbol not in INSTRUMENT_MAP:
        return jsonify({"ok": False, "error": "Unsupported symbol: " + symbol}), 400
    if action not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "Invalid action: " + action}), 400
    if qty <= 0:
        return jsonify({"ok": False, "error": "quantity must be > 0"}), 400

    instrument = INSTRUMENT_MAP[symbol]

    # Step 1: server-side cooldown
    if not cooldown_ok(instrument):
        msg = f"Cooldown active ({COOLDOWN_SECONDS}s). Skipping."
        print(f"[SOFT_REJECT] {msg} instrument={instrument}")
        return jsonify({"ok": False, "error": msg}), 200

    sl_pips = float(data.get("sl_pips", DEFAULT_SL_PIPS))
    tp_pips = float(data.get("tp_pips", DEFAULT_TP_PIPS))

    units = qty if action == "buy" else -qty

    try:
        resp = place_market_order(instrument, units, sl_pips, tp_pips, alert_price=alert_price)
        mark_trade(instrument)
        return jsonify({
            "ok": True,
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

        # Soft rejects (intentional skips) return 200 so TV doesn’t retry/spam
        if is_soft_reject(msg):
            print(f"[SOFT_REJECT] {msg}")
            return jsonify({"ok": False, "error": msg}), 200

        # Real failures return 500 so you notice immediately
        print(f"[ERROR] {msg}")
        return jsonify({"ok": False, "error": msg}), 500
