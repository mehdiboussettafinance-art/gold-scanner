"""
STRATEGY 2 — Asian Range Liquidity Sweep + MSS + FVG (XAUUSD)

Logic (per the provided methodology):
1. Build the Asian session range (00:00-06:00 UTC) using 15m candles.
2. During the London window (07:00-10:00 UTC), watch for a sweep of that
   range's high or low.
3. After a sweep, drop to 1m candles and look for a Market Structure Shift
   (MSS) + Fair Value Gap (FVG) within 15 minutes.
4. If found, compute a limit entry at the FVG's proximal edge, with SL beyond
   the sweep extreme and TP at 1:3 R:R. Track whether price fills that entry
   within the next 10 minutes; if not, the setup expires.
5. Max ONE trade per day. No new entries after 11:00 UTC.

IMPORTANT LIMITATIONS (please read):
- "GMT" is treated as UTC for scheduling simplicity — off by ~1 hour during
  UK daylight saving time (late March-late October). Adjust SESSION hours
  below manually during that period if you want exact GMT alignment.
- Spread cannot be verified precisely — Twelve Data's free tier provides
  OHLC candles, not your broker's live bid/ask. The spread filter is
  therefore SKIPPED here; verify spread manually at entry time.
- Swing/MSS detection uses closed 1m candles only (no repainting), which
  adds a small amount of latency versus a live tick-based tool.
"""

import os
import json
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================================================
# CONFIG
# ============================================================
TWELVE_DATA_KEY  = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
STATE_FILE = "sweep_state.json"

ASIAN_START_H, ASIAN_END_H = 0, 6      # UTC
LONDON_START_H, LONDON_END_H = 7, 10   # UTC
LATE_CUTOFF_H = 11                     # UTC, no new entries after this

MIN_SWEEP_DIST = 0.30     # $ distance beyond range to count as a real sweep ("3 pips")
SL_BUFFER      = 0.50     # $ buffer beyond the sweep extreme ("5 pips")
RISK_REWARD    = 3.0
BE_TRIGGER_RR  = 1.5      # move SL to breakeven once 1:1.5 is reached (informational only)
FVG_MAX_MINUTES = 15      # must find MSS+FVG within this many minutes of the sweep
ENTRY_EXPIRY_MINUTES = 10 # cancel pending limit order if not filled in this time

# News blackout (same as Strategy 1, US Eastern Time)
US_DATA_WINDOW = ("08:20", "09:10")
FOMC_WINDOW    = ("13:55", "14:50")


# ============================================================
# DATA FETCH
# ============================================================
def fetch_candles(interval, outputsize):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_KEY,
        "order": "ASC",
    }
    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize("UTC")
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df.sort_values("datetime").reset_index(drop=True)


# ============================================================
# NEWS BLACKOUT (same logic as Strategy 1)
# ============================================================
def in_news_blackout():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    hm = now_et.strftime("%H:%M")

    def in_window(t, window):
        return window[0] <= t <= window[1]

    return in_window(hm, US_DATA_WINDOW) or in_window(hm, FOMC_WINDOW)


# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    full_msg = "[STRATEGY 2 - Liquidity Sweep]\n" + message
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": full_msg}, timeout=15)


# ============================================================
# STATE
# ============================================================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, default=str)


def fresh_daily_state(today_str):
    return {
        "date": today_str,
        "asian_high": None,
        "asian_low": None,
        "sweep": None,          # {"type": "sell"/"buy", "time": iso, "extreme": price}
        "pending_signal": None, # {"direction","entry","sl","tp","created","expires","status"}
        "trade_taken_today": False,
    }


# ============================================================
# SWING / MSS / FVG DETECTION (1m candles, closed only)
# ============================================================
def find_last_swing(df1m, direction):
    """
    direction = 'high' looks for the most recent local swing high
    direction = 'low'  looks for the most recent local swing low
    Uses a simple 2-bar fractal on already-closed candles.
    """
    n = len(df1m)
    for i in range(n - 3, 1, -1):
        if direction == "high":
            if (df1m["high"][i] > df1m["high"][i - 1] and df1m["high"][i] > df1m["high"][i - 2]
                    and df1m["high"][i] > df1m["high"][i + 1] and df1m["high"][i] > df1m["high"][i + 2]):
                return df1m["high"][i]
        else:
            if (df1m["low"][i] < df1m["low"][i - 1] and df1m["low"][i] < df1m["low"][i - 2]
                    and df1m["low"][i] < df1m["low"][i + 1] and df1m["low"][i] < df1m["low"][i + 2]):
                return df1m["low"][i]
    return None


def check_mss_and_fvg(df1m, sweep_type):
    """
    sweep_type 'sell' -> looking for bearish MSS (close below last swing low) + bearish FVG
    sweep_type 'buy'  -> looking for bullish MSS (close above last swing high) + bullish FVG
    Returns (found: bool, entry, sl_extra_ref)
    """
    if len(df1m) < 6:
        return False, None

    last = df1m.iloc[-1]

    if sweep_type == "sell":
        swing_low = find_last_swing(df1m.iloc[:-1].reset_index(drop=True), "low")
        if swing_low is None or last["close"] >= swing_low:
            return False, None
        # MSS confirmed. Check FVG on the last 3 candles (C1, C2, C3=last)
        c1, c3 = df1m.iloc[-3], df1m.iloc[-1]
        if c1["low"] > c3["high"]:  # bearish gap
            entry = c1["low"]  # proximal edge (nearest to current falling price)
            return True, entry
        return False, None

    else:  # buy
        swing_high = find_last_swing(df1m.iloc[:-1].reset_index(drop=True), "high")
        if swing_high is None or last["close"] <= swing_high:
            return False, None
        c1, c3 = df1m.iloc[-3], df1m.iloc[-1]
        if c1["high"] < c3["low"]:  # bullish gap
            entry = c1["high"]  # proximal edge
            return True, entry
        return False, None


# ============================================================
# MAIN
# ============================================================
def main():
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    weekday = now_utc.weekday()  # 5=Sat, 6=Sun

    state = load_state()
    if state.get("date") != today_str:
        state = fresh_daily_state(today_str)
        save_state(state)

    if weekday >= 5:
        print("Weekend — market closed, skipping.")
        return

    hour = now_utc.hour

    # ------------------------------------------------------
    # Phase 1: build Asian range
    # ------------------------------------------------------
    if ASIAN_START_H <= hour < ASIAN_END_H or hour == ASIAN_END_H:
        df15 = fetch_candles("15min", 60)
        today_asia = df15[
            (df15["datetime"].dt.date.astype(str) == today_str)
            & (df15["datetime"].dt.hour >= ASIAN_START_H)
            & (df15["datetime"].dt.hour < ASIAN_END_H)
        ]
        if len(today_asia) > 0:
            state["asian_high"] = float(today_asia["high"].max())
            state["asian_low"] = float(today_asia["low"].min())
            save_state(state)
            print(f"Asian range updated: high={state['asian_high']}, low={state['asian_low']}")
        return  # nothing else to do during Asian session

    # ------------------------------------------------------
    # Stop entirely outside the trading window or after 1 trade
    # ------------------------------------------------------
    if hour >= LATE_CUTOFF_H or hour < LONDON_START_H:
        print("Outside London entry window. No action.")
        return

    if state.get("trade_taken_today"):
        print("Trade already taken today. No further action.")
        return

    if state.get("asian_high") is None:
        print("Asian range not available yet today.")
        return

    # ------------------------------------------------------
    # Phase 2: check for a pending signal that needs monitoring
    # ------------------------------------------------------
    pending = state.get("pending_signal")
    if pending and pending.get("status") == "pending":
        expires = datetime.fromisoformat(pending["expires"])
        df1m = fetch_candles("1min", 15)
        last_price = float(df1m.iloc[-1]["close"])
        touched = (
            (pending["direction"] == "sell" and df1m["high"].max() >= pending["entry"])
            or (pending["direction"] == "buy" and df1m["low"].min() <= pending["entry"])
        )

        if touched:
            pending["status"] = "filled"
            state["trade_taken_today"] = True
            save_state(state)
            send_telegram(
                f"ENTRY TRIGGERED — {pending['direction'].upper()}\n"
                f"Entry: {pending['entry']:.2f}\n"
                f"Stop Loss: {pending['sl']:.2f}\n"
                f"Take Profit: {pending['tp']:.2f}\n"
                f"Move SL to breakeven once price reaches 1:{BE_TRIGGER_RR} R:R.\n"
                "Reminder: verify live spread manually before confirming fill."
            )
            print("Signal filled.")
            return

        if now_utc >= expires:
            pending["status"] = "expired"
            state["pending_signal"] = pending
            save_state(state)
            send_telegram(
                f"Setup expired unfilled ({pending['direction'].upper()} @ {pending['entry']:.2f}). "
                "No trade taken for this attempt."
            )
            print("Signal expired.")
            return

        print("Pending signal still active, not yet touched or expired.")
        return

    # ------------------------------------------------------
    # Phase 3: look for a fresh sweep
    # ------------------------------------------------------
    sweep = state.get("sweep")
    if not sweep:
        df15 = fetch_candles("15min", 10)
        last15 = df15.iloc[-1]
        if last15["high"] >= state["asian_high"] + MIN_SWEEP_DIST:
            sweep = {"type": "sell", "time": now_utc.isoformat(), "extreme": float(last15["high"])}
        elif last15["low"] <= state["asian_low"] - MIN_SWEEP_DIST:
            sweep = {"type": "buy", "time": now_utc.isoformat(), "extreme": float(last15["low"])}

        if sweep:
            state["sweep"] = sweep
            save_state(state)
            send_telegram(
                f"LIQUIDITY SWEEP DETECTED — possible {sweep['type'].upper()} setup forming.\n"
                f"Watching for MSS + FVG confirmation on 1m over the next {FVG_MAX_MINUTES} minutes."
            )
            print("Sweep detected.")
        else:
            print("No sweep yet.")
        return

    # ------------------------------------------------------
    # Phase 4: sweep already flagged, look for MSS + FVG
    # ------------------------------------------------------
    sweep_time = datetime.fromisoformat(sweep["time"])
    if now_utc - sweep_time > timedelta(minutes=FVG_MAX_MINUTES):
        print("Sweep window expired without MSS+FVG confirmation. Resetting sweep.")
        state["sweep"] = None
        save_state(state)
        return

    if in_news_blackout():
        print("Inside news blackout window. Holding off MSS/FVG check.")
        return

    df1m = fetch_candles("1min", 15)
    found, entry = check_mss_and_fvg(df1m, sweep["type"])

    if found:
        if sweep["type"] == "sell":
            sl = sweep["extreme"] + SL_BUFFER
            risk = sl - entry
            tp = entry - risk * RISK_REWARD
        else:
            sl = sweep["extreme"] - SL_BUFFER
            risk = entry - sl
            tp = entry + risk * RISK_REWARD

        pending_signal = {
            "direction": sweep["type"],
            "entry": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "created": now_utc.isoformat(),
            "expires": (now_utc + timedelta(minutes=ENTRY_EXPIRY_MINUTES)).isoformat(),
            "status": "pending",
        }
        state["pending_signal"] = pending_signal
        save_state(state)

        send_telegram(
            f"MSS + FVG CONFIRMED — {sweep['type'].upper()} setup ready.\n"
            f"Pending Limit Entry: {entry:.2f}\n"
            f"Stop Loss: {sl:.2f}\n"
            f"Take Profit (1:{RISK_REWARD:g}): {tp:.2f}\n"
            f"Order expires if unfilled within {ENTRY_EXPIRY_MINUTES} minutes.\n"
            "Reminder: verify spread manually — not measured from this data source."
        )
        print("MSS+FVG confirmed, pending signal created.")
    else:
        print("Sweep active, waiting for MSS+FVG confirmation.")


if __name__ == "__main__":
    main()
