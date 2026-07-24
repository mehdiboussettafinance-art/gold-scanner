"""
STRATEGY 3 — M15 Trend-Pullback Scanner (XAUUSD)
Mirrors the TradingView Pine Script "XAUUSD Trend-Pullback Scanner PRO"
exactly, but running fully on the 15-minute timeframe (trend + entry both
on M15, matching what you see as triangles on the chart).

Event-driven only: sends a Telegram alert the moment a signal fires,
same as the Pine Script alertcondition() would — no periodic chatter.
"""

import os
import json
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TWELVE_DATA_KEY   = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL, INTERVAL, OUTPUTSIZE = "XAU/USD", "15min", 300
STATE_FILE = "state_m15.json"

EMA_FAST, EMA_TREND, EMA_MACRO = 20, 50, 200
RSI_LEN, RSI_LOW, RSI_HIGH = 14, 35, 55   # matches the updated Pine Script settings
ATR_LEN, PULLBACK_ATR_MULT, ATR_STOP_MULT = 14, 0.6, 1.5
RR1, RR2 = 1.0, 2.0

US_DATA_WINDOW = ("08:20", "09:10")
FOMC_WINDOW    = ("13:55", "14:50")


def fetch_candles():
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": SYMBOL, "interval": INTERVAL, "outputsize": OUTPUTSIZE,
              "apikey": TWELVE_DATA_KEY, "order": "ASC"}
    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df.sort_values("datetime").reset_index(drop=True)


def add_indicators(df):
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_trend"] = df["close"].ewm(span=EMA_TREND, adjust=False).mean()
    df["ema_macro"] = df["close"].ewm(span=EMA_MACRO, adjust=False).mean()
    delta = df["close"].diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / RSI_LEN, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + avg_gain / avg_loss))
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / ATR_LEN, adjust=False).mean()
    return df


def check_signal(df):
    last, prev = df.iloc[-1], df.iloc[-2]
    uptrend = last["close"] > last["ema_trend"] > last["ema_macro"]
    downtrend = last["close"] < last["ema_trend"] < last["ema_macro"]
    dist = abs(last["close"] - last["ema_fast"])
    near_pullback = dist <= (last["atr"] * PULLBACK_ATR_MULT)
    rsi_long_zone = (RSI_LOW <= last["rsi"] <= RSI_HIGH) and (last["rsi"] > prev["rsi"])
    rsi_short_zone = ((100 - RSI_HIGH) <= last["rsi"] <= (100 - RSI_LOW)) and (last["rsi"] < prev["rsi"])
    body = abs(last["close"] - last["open"])
    lw = min(last["open"], last["close"]) - last["low"]
    uw = last["high"] - max(last["open"], last["close"])
    bullish = (lw >= body * 1.5 and last["close"] > last["open"]) or \
              (last["close"] > last["open"] and last["open"] <= prev["close"] and last["close"] >= prev["open"] and prev["close"] < prev["open"])
    bearish = (uw >= body * 1.5 and last["close"] < last["open"]) or \
              (last["close"] < last["open"] and last["open"] >= prev["close"] and last["close"] <= prev["open"] and prev["close"] > prev["open"])
    long_signal = uptrend and near_pullback and rsi_long_zone and bullish
    short_signal = downtrend and near_pullback and rsi_short_zone and bearish
    return long_signal, short_signal, last


def in_blackout_now():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    hm = now_et.strftime("%H:%M")
    return US_DATA_WINDOW[0] <= hm <= US_DATA_WINDOW[1] or FOMC_WINDOW[0] <= hm <= FOMC_WINDOW[1]


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    full_msg = "[STRATEGY 3 - M15 Trend-Pullback] ⚡\n" + message
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": full_msg}, timeout=15)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_alert_time": None}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def main():
    df = add_indicators(fetch_candles())
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
        print("No signal on latest closed M15 candle.")
        return

    if in_blackout_now():
        print("Signal found but inside news blackout window. Suppressed.")
        return

    atr = last["atr"]
    stop_dist = atr * ATR_STOP_MULT
    price = last["close"]

    if long_signal:
        sl, tp1, tp2 = price - stop_dist, price + stop_dist * RR1, price + stop_dist * RR2
        msg = (f"🟢🚀 GOLD BUY SIGNAL (XAUUSD, M15)\nEntry: {price:.2f}\nStop Loss: {sl:.2f} 🛑\n"
               f"TP1 (1:{RR1}): {tp1:.2f} 🎯\nTP2 (1:{RR2}): {tp2:.2f} 🎯🎯\n"
               "Risk 0.5-1% of account. Confirm chart before entering ✅")
    else:
        sl, tp1, tp2 = price + stop_dist, price - stop_dist * RR1, price - stop_dist * RR2
        msg = (f"🔴🛑 GOLD SELL SIGNAL (XAUUSD, M15)\nEntry: {price:.2f}\nStop Loss: {sl:.2f} 🛑\n"
               f"TP1 (1:{RR1}): {tp1:.2f} 🎯\nTP2 (1:{RR2}): {tp2:.2f} 🎯🎯\n"
               "Risk 0.5-1% of account. Confirm chart before entering ✅")

    send_telegram(msg)
    print("Alert sent:", msg)

    state["last_alert_time"] = candle_time
    save_state(state)


if __name__ == "__main__":
    main()
