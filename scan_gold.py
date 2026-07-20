"""
XAUUSD Trend-Pullback Scanner
Runs the same strategy logic as the Pine Script version, but pulls data
from Twelve Data (free tier) and sends alerts via Telegram (free, unlimited).

Designed to run on a schedule (e.g. GitHub Actions, every hour).
Keeps a small state file so it never sends the same signal twice.
"""

import os
import json
import requests
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

# ============================================================
# CONFIG (from environment variables / GitHub Secrets)
# ============================================================
TWELVE_DATA_KEY   = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL    = "XAU/USD"
INTERVAL  = "4h"        # match the H4 timeframe used in the strategy
OUTPUTSIZE = 300        # enough bars for EMA200 to be valid

STATE_FILE = "state.json"

# Strategy parameters (same as the Pine Script version)
EMA_FAST, EMA_TREND, EMA_MACRO = 20, 50, 200
RSI_LEN = 14
RSI_LOW, RSI_HIGH = 40, 50
ATR_LEN = 14
PULLBACK_ATR_MULT = 0.6
ATR_STOP_MULT = 1.5
RR1, RR2 = 1.0, 2.0

# News blackout windows (US Eastern Time)
US_DATA_WINDOW = ("08:20", "09:10")   # CPI / NFP / Retail Sales
FOMC_WINDOW    = ("13:55", "14:50")   # FOMC statement / press conf


# ============================================================
# DATA FETCH
# ============================================================
def fetch_candles():
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": OUTPUTSIZE,
        "apikey": TWELVE_DATA_KEY,
        "order": "ASC",
    }
    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")

    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


# ============================================================
# INDICATORS
# ============================================================
def add_indicators(df):
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_trend"] = df["close"].ewm(span=EMA_TREND, adjust=False).mean()
    df["ema_macro"] = df["close"].ewm(span=EMA_MACRO, adjust=False).mean()

    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))

    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / ATR_LEN, adjust=False).mean()

    return df


# ============================================================
# SIGNAL LOGIC (mirrors the Pine Script conditions)
# ============================================================
def check_signal(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]

    uptrend = last["close"] > last["ema_trend"] > last["ema_macro"]
    downtrend = last["close"] < last["ema_trend"] < last["ema_macro"]

    dist_from_fast = abs(last["close"] - last["ema_fast"])
    near_pullback = dist_from_fast <= (last["atr"] * PULLBACK_ATR_MULT)

    rsi_long_zone = (RSI_LOW <= last["rsi"] <= RSI_HIGH) and (last["rsi"] > prev["rsi"])
    rsi_short_zone = ((100 - RSI_HIGH) <= last["rsi"] <= (100 - RSI_LOW)) and (last["rsi"] < prev["rsi"])

    body = abs(last["close"] - last["open"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    upper_wick = last["high"] - max(last["open"], last["close"])

    bullish_pin = lower_wick >= body * 1.5 and last["close"] > last["open"]
    bullish_engulf = (last["close"] > last["open"] and last["open"] <= prev["close"]
                       and last["close"] >= prev["open"] and prev["close"] < prev["open"])
    bullish_reject = bullish_pin or bullish_engulf

    bearish_pin = upper_wick >= body * 1.5 and last["close"] < last["open"]
    bearish_engulf = (last["close"] < last["open"] and last["open"] >= prev["close"]
                       and last["close"] <= prev["open"] and prev["close"] > prev["open"])
    bearish_reject = bearish_pin or bearish_engulf

    long_signal = uptrend and near_pullback and rsi_long_zone and bullish_reject
    short_signal = downtrend and near_pullback and rsi_short_zone and bearish_reject

    return long_signal, short_signal, last


# ============================================================
# NEWS BLACKOUT CHECK
# ============================================================
def in_blackout_now():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:  # weekend, market mostly closed anyway
        return False
    hm = now_et.strftime("%H:%M")

    def in_window(t, window):
        return window[0] <= t <= window[1]

    return in_window(hm, US_DATA_WINDOW) or in_window(hm, FOMC_WINDOW)


# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)


# ============================================================
# STATE (avoid duplicate alerts for the same candle)
# ============================================================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_alert_time": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ============================================================
# MAIN
# ============================================================
def main():
    df = fetch_candles()
    df = add_indicators(df)

    if len(df) < EMA_MACRO + 5:
        print("Not enough data yet for EMA200.")
        return

    long_signal, short_signal, last = check_signal(df)
    candle_time = str(last["datetime"])

    state = load_state()
    if state.get("last_alert_time") == candle_time:
        print("Already alerted for this candle. Skipping.")
        return

    if not (long_signal or short_signal):
        print("No signal on latest closed candle.")
        return

    if in_blackout_now():
        print("Signal found but inside news blackout window. Suppressed.")
        return

    atr = last["atr"]
    stop_dist = atr * ATR_STOP_MULT
    price = last["close"]

    if long_signal:
        sl = price - stop_dist
        tp1 = price + stop_dist * RR1
        tp2 = price + stop_dist * RR2
        msg = (
            "GOLD BUY SIGNAL (XAUUSD)\n"
            f"Entry: {price:.2f}\n"
            f"Stop Loss: {sl:.2f}\n"
            f"TP1 (1:{RR1}): {tp1:.2f}\n"
            f"TP2 (1:{RR2}): {tp2:.2f}\n"
            "Risk 0.5-1% of account. Confirm chart before entering."
        )
    else:
        sl = price + stop_dist
        tp1 = price - stop_dist * RR1
        tp2 = price - stop_dist * RR2
        msg = (
            "GOLD SELL SIGNAL (XAUUSD)\n"
            f"Entry: {price:.2f}\n"
            f"Stop Loss: {sl:.2f}\n"
            f"TP1 (1:{RR1}): {tp1:.2f}\n"
            f"TP2 (1:{RR2}): {tp2:.2f}\n"
            "Risk 0.5-1% of account. Confirm chart before entering."
        )

    send_telegram(msg)
    print("Alert sent:", msg)

    state["last_alert_time"] = candle_time
    save_state(state)


if __name__ == "__main__":
    main()
