import os, json, time, requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# -----------------------------
# ENV VARS (set in Render)
# -----------------------------
OANDA_TOKEN      = os.environ.get("OANDA_TOKEN", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
OANDA_ENV        = os.environ.get("OANDA_ENV", "practice").lower()  # "practice" or "live"
WEBHOOK_TOKEN    = os.environ.get("WEBHOOK_TOKEN", "")

DEFAULT_QTY      = int(os.environ.get("DEFAULT_QTY", "1000"))
DEFAULT_SL_PIPS  = float(os.environ.get("DEFAULT_SL_PIPS", "5"))
DEFAULT_TP_PIPS  = float(os.environ.get("DEFAULT_TP_PIPS", "10"))

# Event-stack behavior
AND_WINDOW_SECONDS = int(os.environ.get("AND_WINDOW_SECONDS", "600"))  # 10 minutes
REQUIRE_BOTH_CONFIRMATIONS = os.environ.get("REQUIRE_BOTH_CONFIRMATIONS", "true").lower() in ("1","true","yes")
REVERSE_ON_FLIP = os.environ.get("REVERSE_ON_FLIP", "true").lower() in ("1","true","yes")

# Risk model
USE_ATR_STOPS = os.environ.get("USE_ATR_STOPS", "true").lower() in ("1","true","yes")
ATR_SL_MULT   = float(os.environ.get("ATR_SL_MULT", "1.2"))
ATR_TP_MULT   = float(os.environ.get("ATR_TP_MULT", "2.0"))

# Execution guards
MAX_SLIPPAGE_PIPS = float(os.environ.get("MAX_SLIPPAGE_PIPS", "1.5"))
COOLDOWN_SECONDS  = int(os.environ.get("COOLDOWN_SECONDS", "60"))

INSTRUMENT_MAP = {
    "EURUSD": "EUR_USD",
    "EUR_USD": "EUR_USD",
    "OANDA:EURUSD": "EUR_USD",
}

_last_trade_ts = {}     # instrument -> epoch seconds
_state = {}             # instrument -> {atr_ok, atr_pips, so, osc, ts_*}

def oanda_base_url():
    return "https://api-fxtrade.oanda.com" if OANDA_ENV == "live" else "https://api-fxpractice.oanda.com"

def oanda_headers():
    return {"Authorization": f"Bearer {OANDA_TOKEN}", "Content-Type": "application/json"}

def pip_size_for(instrument: str) -> float:
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

def pips_between(a: float, b: float, pip: float) -> float:
    return abs(a - b) / pip

def cooldown_ok(instrument: str) -> bool:
    now = int(time.time())
    last = _last_trade_ts.get(instrument, 0)
    return (now - last) >= COOLDOWN_SECONDS

def mark_trade(instrument: str):
    _last_trade_ts[instrument] = int(time.time())

def safe_json():
    """
    TradingView sometimes sends text/plain; sometimes invalid JSON if alert message is wrong.
    We try hard to parse, and log raw body if not parseable.
    """
    data = request.get_json(silent=True)
    if data is not None:
        return data, None

    raw = request.data.decode("utf-8", errors="replace").strip()
    if not raw:
        return None, "empty body (alert message likely blank / not JSON)"
    try:
        return json.loads(raw), None
    except Exception as e:
        return None, f"invalid JSON body: {str(e)} | raw={raw[:200]}"

def compute_sl_tp_pips(payload: dict):
    # payload can include sl_pips/tp_pips; otherwise use ATR if available; else defaults
    sl_pips = float(payload.get("sl_pips", DEFAULT_SL_PIPS))
    tp_pips = float(payload.get("tp_pips", DEFAULT_TP_PIPS))
    return sl_pips, tp_pips

def place_market_order(instrument: str, units: int, sl_pips: float, tp_pips: float, alert_price=None):
    mid, bid, ask = get_mid_bid_ask(instrument)
    pip = pip_size_for(instrument)

    if alert_price is not None:
        drift = pips_between(mid, float(alert_price), pip)
        print(f"[DRIFT] instrument={instrument} side={'BUY' if units>0 else 'SELL'} tv_close={float(alert_price):.5f} mid={mid:.5f} drift_pips={drift:.2f}")
        if drift > MAX_SLIPPAGE_PIPS:
            raise RuntimeError(f"Price drift too large ({drift:.2f} pips > {MAX_SLIPPAGE_PIPS:.2f} pips). Rejecting.")
    else:
        print(f"[PRICE] instrument={instrument} side={'BUY' if units>0 else 'SELL'} mid={mid:.5f} bid={bid:.5f} ask={ask:.5f} (no alert_price)")

    # Build SL/TP from current mid
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
            "positionFill": "REDUCE_FIRST" if REVERSE_ON_FLIP else "DEFAULT",
            "priceBound": f"{price_bound:.5f}",
            "stopLossOnFill": {"price": f"{sl_price:.5f}"},
            "takeProfitOnFill": {"price": f"{tp_price:.5f}"},
        }
    }

    url = f"{oanda_base_url()}/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    r = requests.post(url, headers=oanda_headers(), data=json.dumps(payload), timeout=10)
    print(f"[OANDA_ORDER] status={r.status_code} resp={r.text[:250]}")
    r.raise_for_status()
    return r.json()

def get_state(instr: str):
    if instr not in _state:
        _state[instr] = {"atr_ok": False, "atr_pips": None, "so": None, "osc": None, "ts_atr": 0, "ts_so": 0, "ts_osc": 0}
    return _state[instr]

def within_window(ts: int) -> bool:
    return (int(time.time()) - int(ts)) <= AND_WINDOW_SECONDS

def maybe_fire_from_stack(instrument: str, symbol: str, close_price: float):
    s = get_state(instrument)

    # Must have recent ATR_OK
    if not (s["atr_ok"] and within_window(s["ts_atr"])):
        return None

    # Confirmation logic
    so  = s["so"]  if within_window(s["ts_so"])  else None
    osc = s["osc"] if within_window(s["ts_osc"]) else None

    # If require both: need both and aligned direction
    if REQUIRE_BOTH_CONFIRMATIONS:
        if not so or not osc or so != osc:
            return None
        direction = so
    else:
        # Looser: trade if either exists; if both exist and conflict, do nothing
        if so and osc and so != osc:
            return None
        direction = so or osc
        if not direction:
            return None

    # Compute SL/TP
    if USE_ATR_STOPS and s["atr_pips"] is not None:
        sl_pips = max(0.1, float(s["atr_pips"]) * ATR_SL_MULT)
        tp_pips = max(0.1, float(s["atr_pips"]) * ATR_TP_MULT)
    else:
        sl_pips, tp_pips = DEFAULT_SL_PIPS, DEFAULT_TP_PIPS

    action = "buy" if direction == "BULL" else "sell"
    qty = DEFAULT_QTY

    return {"symbol": symbol, "action": action, "quantity": qty, "alert_price": close_price, "sl_pips": sl_pips, "tp_pips": tp_pips}

@app.get("/")
def health():
    return "ok"

@app.get("/debug")
def debug():
    return jsonify({
        "ok": True,
        "env": {
            "OANDA_ENV": OANDA_ENV,
            "AND_WINDOW_SECONDS": AND_WINDOW_SECONDS,
            "REQUIRE_BOTH_CONFIRMATIONS": REQUIRE_BOTH_CONFIRMATIONS,
            "REVERSE_ON_FLIP": REVERSE_ON_FLIP,
            "USE_ATR_STOPS": USE_ATR_STOPS,
            "ATR_SL_MULT": ATR_SL_MULT,
            "ATR_TP_MULT": ATR_TP_MULT,
            "COOLDOWN_SECONDS": COOLDOWN_SECONDS,
        },
        "state": _state,
        "last_trade_ts": _last_trade_ts
    })

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
        # return 200 so TV doesn't retry forever
        return jsonify({"ok": False, "soft": True, "error": err}), 200

    # Normalize symbol
    symbol_raw = str(data.get("symbol", data.get("ticker", ""))).strip()
    symbol = symbol_raw.upper()
    if symbol not in INSTRUMENT_MAP:
        msg = f"Unsupported symbol: {symbol_raw}"
        print(f"[BAD_SYMBOL] {msg} payload_keys={list(data.keys())}")
        return jsonify({"ok": False, "soft": True, "error": msg}), 200

    instrument = INSTRUMENT_MAP[symbol]

    # Cooldown (server-side)
    if not cooldown_ok(instrument):
        msg = f"Cooldown active ({COOLDOWN_SECONDS}s). Skipping."
        print(f"[SOFT_REJECT] {msg} instrument={instrument}")
        return jsonify({"ok": False, "soft": True, "error": msg}), 200

    # ---- Mode A: direct order payload (buy/sell) ----
    if "action" in data:
        action = str(data.get("action", "")).lower()
        qty = int(data.get("quantity", 0) or 0)
        alert_price = data.get("alert_price", None)

        if action not in ("buy", "sell") or qty <= 0:
            msg = f"Invalid action/quantity: action={action} qty={qty}"
            print(f"[BAD_DIRECT] {msg} data={data}")
            return jsonify({"ok": False, "soft": True, "error": msg}), 200

        sl_pips, tp_pips = compute_sl_tp_pips(data)
        units = qty if action == "buy" else -qty

        try:
            resp = place_market_order(instrument, units, sl_pips, tp_pips, alert_price=alert_price)
            mark_trade(instrument)
            return jsonify({"ok": True, "mode": "direct", "instrument": instrument, "action": action, "units": units, "sl_pips": sl_pips, "tp_pips": tp_pips, "oanda": resp}), 200
        except Exception as e:
            msg = str(e)
            print(f"[ERROR_DIRECT] {msg}")
            return jsonify({"ok": False, "error": msg}), 500

    # ---- Mode B: LuxAlgo event stack ----
    event_type = str(data.get("type", "")).upper()
    close_val = data.get("close", None)

    if not event_type:
        msg = f"Missing 'type' or 'action' in payload keys={list(data.keys())}"
        print(f"[BAD_EVENT] {msg} data={data}")
        return jsonify({"ok": False, "soft": True, "error": msg}), 200

    # Parse close as float
    try:
        close_price = float(close_val) if close_val is not None else None
    except:
        close_price = None

    s = get_state(instrument)

    # Update state
    now = int(time.time())

    if event_type == "ATR_OK":
        # atrPips can be provided, otherwise we just mark atr_ok
        atr_pips = data.get("atrPips", data.get("atr_pips", None))
        try:
            s["atr_pips"] = float(atr_pips) if atr_pips is not None else s["atr_pips"]
        except:
            pass
        s["atr_ok"] = True
        s["ts_atr"] = now
        print(f"[STACK] ATR_OK instrument={instrument} atr_pips={s['atr_pips']}")
        return jsonify({"ok": True, "note": "Stored ATR_OK"}), 200

    if event_type in ("SO_BULL", "SO_BEAR"):
        s["so"] = "BULL" if event_type.endswith("BULL") else "BEAR"
        s["ts_so"] = now
        print(f"[STACK] SO={s['so']} instrument={instrument}")
    elif event_type in ("OSC_BULL", "OSC_BEAR"):
        s["osc"] = "BULL" if event_type.endswith("BULL") else "BEAR"
        s["ts_osc"] = now
        print(f"[STACK] OSC={s['osc']} instrument={instrument}")
    else:
        msg = f"Unknown type: {event_type}"
        print(f"[BAD_TYPE] {msg}")
        return jsonify({"ok": False, "soft": True, "error": msg}), 200

    # Try to fire
    if close_price is None:
        # no close => can’t drift-guard, but can still trade if we want; we’ll pass None
        close_price = None

    order_payload = maybe_fire_from_stack(instrument, symbol, close_price if close_price is not None else None)
    if not order_payload:
        return jsonify({"ok": True, "note": f"Stored {event_type}, waiting for stack"}), 200

    # Place trade
    action = order_payload["action"]
    qty = int(order_payload["quantity"])
    units = qty if action == "buy" else -qty
    sl_pips = float(order_payload["sl_pips"])
    tp_pips = float(order_payload["tp_pips"])
    alert_price = order_payload.get("alert_price", None)

    try:
        resp = place_market_order(instrument, units, sl_pips, tp_pips, alert_price=alert_price)
        mark_trade(instrument)
        print(f"[FIRED] {action.upper()} instrument={instrument} units={units} sl_pips={sl_pips:.2f} tp_pips={tp_pips:.2f}")
        return jsonify({"ok": True, "fired": action, "instrument": instrument, "units": units, "sl_pips": sl_pips, "tp_pips": tp_pips, "oanda": resp}), 200
    except Exception as e:
        msg = str(e)
        print(f"[ERROR_STACK] {msg}")
        return jsonify({"ok": False, "error": msg}), 500
