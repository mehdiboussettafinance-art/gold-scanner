"""
XAUUSD Trend-Pullback Scanner — Strategy 1
Now includes session start/close heads-up + periodic status updates
so you know the bot is alive and what it's seeing, not just silent
until a trade signal fires.
"""

import os
import json
import random
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ============================================================
# CONFIG
# ============================================================
TWELVE_DATA_KEY   = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL    = "XAU/USD"
INTERVAL  = "4h"
OUTPUTSIZE = 300
STATE_FILE = "state.json"

EMA_FAST, EMA_TREND, EMA_MACRO = 20, 50, 200
RSI_LEN = 14
RSI_LOW, RSI_HIGH = 40, 50
ATR_LEN = 14
PULLBACK_ATR_MULT = 0.6
ATR_STOP_MULT = 1.5
RR1, RR2 = 1.0, 2.0

US_DATA_WINDOW = ("08:20", "09:10")
FOMC_WINDOW    = ("13:55", "14:50")

# Best-liquidity window (London-New York overlap), UTC, approx (ignores DST shifts)
OVERLAP_START_H, OVERLAP_END_H = 13, 16

STATUS_EMOJIS = ["📊", "🔍", "🧐", "⚙️", "🛰️", "🔭"]
IDLE_EMOJIS   = ["😴", "🌙", "☕", "🤷"]
GREEN_EMOJIS  = ["🟢", "✅", "🚀"]
RED_EMOJIS    = ["🔴", "🛑", "⚠️"]


# ============================================================
# DATA FETCH
# ============================================================
def fetch_candles():
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL, "interval": INTERVAL, "outputsize": OUTPUTSIZE,
        "apikey": TWELVE_DATA_KEY, "order": "ASC",
    }
    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df.sort_values("datetime").reset_index(drop=True)


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
# SIGNAL LOGIC
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

    bias = "uptrend" if uptrend else ("downtrend" if downtrend else "ranging")
    return long_signal, short_signal, last, bias


# ============================================================
# NEWS BLACKOUT
# ============================================================
def in_blackout_now():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
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
# STATE
# ============================================================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ============================================================
# MAIN
# ============================================================
def main():
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    hour = now_utc.hour

    state = load_state()
    if state.get("date") != today_str:
        state = {
            "date": today_str,
            "last_alert_time": state.get("last_alert_time"),
            "last_status_time": None,
            "overlap_start_sent": False,
            "overlap_end_warned": False,
        }

    # --- Session start heads-up (London-NY overlap) ---
    if hour == OVERLAP_START_H and not state.get("overlap_start_sent"):
        send_telegram(
            f"{random.choice(GREEN_EMOJIS)} بدأت نافذة أفضل سيولة اليوم (تقاطع لندن-نيويورك)! "
            f"{random.choice(STATUS_EMOJIS)} راقب معي، الفرص الأقوى عادة تطلع بهالفترة 👀"
        )
        state["overlap_start_sent"] = True
        save_state(state)

    # --- Session closing soon heads-up ---
    if hour == OVERLAP_END_H - 1 and not state.get("overlap_end_warned"):
        send_telegram(
            f"{random.choice(RED_EMOJIS)} تنبيه: أفضل نافذة سيولة اليوم توشك تخلص خلال أقل من ساعة ⏳ "
            "لو ما فيه إشارة لين الحين، الأرجح نستنى ليوم ثاني 🌙"
        )
        state["overlap_end_warned"] = True
        save_state(state)

    df = fetch_candles()
    df = add_indicators(df)

    if len(df) < EMA_MACRO + 5:
        print("Not enough data yet for EMA200.")
        save_state(state)
        return

    long_signal, short_signal, last, bias = check_signal(df)
    candle_time = str(last["datetime"])

    # --- Periodic status update, once per new H4 candle ---
    if state.get("last_status_time") != candle_time:
        bias_txt = {"uptrend": "صاعد 📈", "downtrend": "هابط 📉", "ranging": "متذبذب 🌊"}[bias]
        blackout_txt = "🔇 فيه حظر أخبار حاليًا" if in_blackout_now() else "🔊 ما فيه حظر أخبار"
        send_telegram(
            f"{random.choice(STATUS_EMOJIS)} تحديث سريع من الاستراتيجية الأولى:\n"
            f"السعر الحالي: {last['close']:.2f} 💰\n"
            f"الاتجاه العام: {bias_txt}\n"
            f"RSI: {last['rsi']:.1f}\n"
            f"{blackout_txt}\n"
            f"{random.choice(IDLE_EMOJIS)} لسا نراقب، ما فيه إشارة جاهزة هالشمعة."
        )
        state["last_status_time"] = candle_time
        save_state(state)

    if state.get("last_alert_time") == candle_time:
        print("Already alerted for this candle. Skipping trade check.")
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
            f"{random.choice(GREEN_EMOJIS)} GOLD BUY SIGNAL (XAUUSD) 🟡\n"
            f"Entry: {price:.2f}\n"
            f"Stop Loss: {sl:.2f} 🛑\n"
            f"TP1 (1:{RR1}): {tp1:.2f} 🎯\n"
            f"TP2 (1:{RR2}): {tp2:.2f} 🎯🎯\n"
            "Risk 0.5-1% of account. Confirm chart before entering ✅"
        )
    else:
        sl = price + stop_dist
        tp1 = price - stop_dist * RR1
        tp2 = price - stop_dist * RR2
        msg = (
            f"{random.choice(RED_EMOJIS)} GOLD SELL SIGNAL (XAUUSD) 🟡\n"
            f"Entry: {price:.2f}\n"
            f"Stop Loss: {sl:.2f} 🛑\n"
            f"TP1 (1:{RR1}): {tp1:.2f} 🎯\n"
            f"TP2 (1:{RR2}): {tp2:.2f} 🎯🎯\n"
            "Risk 0.5-1% of account. Confirm chart before entering ✅"
        )

    send_telegram(msg)
    print("Alert sent:", msg)

    state["last_alert_time"] = candle_time
    save_state(state)


if __name__ == "__main__":
    main()
