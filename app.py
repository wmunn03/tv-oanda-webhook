import os
import json
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

INSTRUMENT_MAP = {
    "EURUSD": "EUR_USD",
    "EUR_USD": "EUR_USD",
}

def oanda_base_url():
    if OANDA_ENV == "live":
        return "https://api-fxtrade.oanda.com"
    return "https://api-fxpractice.oanda.com"

def oanda_headers():
    return {
        "Authorization": "Bearer " + OANDA_TOKEN,
        "Content-Type": "application/json",
    }

def pip_size_for(instrument):
    # EUR_USD pip = 0.0001, JPY pairs usually 0.01
    if instrument.endswith("JPY"):
        return 0.01
    return 0.0001

def close_opposite_position(instrument, desired_side):
    """
    desired_side:
      "buy"  -> close any shorts
      "sell" -> close any longs
    """
    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/positions/{instrument}/close"
    payload = {"shortUnits": "ALL"} if desired_side == "buy" else {"longUnits": "ALL"}

    try:
        r = requests.put(url, headers=oanda_headers(), data=json.dumps(payload), timeout=10)
        print("close_opposite_position:", r.status_code, r.text[:200])
    except Exception as e:
        print("close_opposite_position error:", str(e))

def get_mid_price(instrument):
    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/pricing"
    r = requests.get(url, headers=oanda_headers(), params={"instruments": instrument}, timeout=10)
    r.raise_for_status()
    prices = r.json().get("prices", [])
    if not prices:
        raise RuntimeError("No pricing data returned from OANDA.")

    bid = float(prices[0]["bids"][0]["price"])
    ask = float(prices[0]["asks"][0]["price"])
    return (bid + ask) / 2.0

def place_market_order(instrument, units, sl_pips, tp_pips):
    mid = get_mid_price(instrument)
    pip = pip_size_for(instrument)

    # units > 0 => buy/long, units < 0 => sell/short
    if units > 0:
        sl_price = mid - (sl_pips * pip)
        tp_price = mid + (tp_pips * pip)
    else:
        sl_price = mid + (sl_pips * pip)
        tp_price = mid - (tp_pips * pip)

    order_payload = {
        "order": {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": f"{sl_price:.5f}"},
            "takeProfitOnFill": {"price": f"{tp_price:.5f}"},
        }
    }

    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=oanda_headers(), data=json.dumps(order_payload), timeout=10)
    r.raise_for_status()
    return r.json()

@app.get("/")
def health():
    return "ok"

@app.post("/webhook")
def webhook():
    token = request.args.get("token", "")
    if WEBHOOK_TOKEN and token != WEBHOOK_TOKEN:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401

    if not OANDA_TOKEN or not OANDA_ACCOUNT_ID:
        return jsonify({"ok": False, "error": "Missing OANDA env vars"}), 500

    data = request.get_json(silent=True) or {}

    symbol = str(data.get("symbol", "")).upper()
    action = str(data.get("action", "")).lower()
    qty = int(data.get("quantity", 0))

    if symbol not in INSTRUMENT_MAP:
        return jsonify({"ok": False, "error": "Unsupported symbol: " + symbol}), 400
    if action not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "Invalid action: " + action}), 400
    if qty <= 0:
        return jsonify({"ok": False, "error": "quantity must be > 0"}), 400

    sl_pips = float(data.get("sl_pips", DEFAULT_SL_PIPS))
    tp_pips = float(data.get("tp_pips", DEFAULT_TP_PIPS))

    instrument = INSTRUMENT_MAP[symbol]

    # Close opposite side to avoid accidental hedging
    close_opposite_position(instrument, action)

    units = qty if action == "buy" else -qty

    try:
        resp = place_market_order(instrument, units, sl_pips, tp_pips)
        return jsonify({"ok": True, "instrument": instrument, "action": action, "units": units, "oanda": resp})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

