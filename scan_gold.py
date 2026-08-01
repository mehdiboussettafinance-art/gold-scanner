"""
XAUUSD Trend-Pullback Scanner — Strategy 1 (v2: higher frequency)
Changes from v1:
- Trend bias computed on H4 (unchanged, strict filter)
- Entry conditions now checked on H1 candles (was H4) -> checked every hour
  instead of every 4 hours = up to 4x more opportunities
- RSI pullback zone widened from 40-50 to 35-55
- Added a second entry pattern: range-compression breakout in the direction
  of the H4 trend, as an alternative to the classic pullback
Still talks to you every single hour.
"""

import os
import json
import random
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TWELVE_DATA_KEY   = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
STATE_FILE = "state.json"

TREND_INTERVAL, TREND_OUTPUTSIZE = "4h", 300
ENTRY_INTERVAL, ENTRY_OUTPUTSIZE = "1h", 300

EMA_TREND, EMA_MACRO = 50, 200      # computed on H4 (trend bias)
EMA_FAST = 20                        # computed on H1 (pullback zone)
RSI_LEN, RSI_LOW, RSI_HIGH = 14, 35, 55   # widened zone
ATR_LEN, PULLBACK_ATR_MULT, ATR_STOP_MULT = 14, 0.6, 1.5
RR1, RR2 = 1.0, 2.0

CONSOLIDATION_LOOKBACK = 10
CONSOLIDATION_ATR_RATIO = 0.7   # current ATR must be below 70% of its 20-period average to count as "tight"

US_DATA_WINDOW = ("08:20", "09:10")
FOMC_WINDOW    = ("13:55", "14:50")
OVERLAP_START_H, OVERLAP_END_H = 13, 16

OPENERS = [
    "👋 أنا لسا هنا، خلني أحدثك عن آخر وضع.",
    "🕓 مرت ساعة، وهذا اللي شفته بالسوق.",
    "📌 تحديث سريع قبل لا تسأل وش صاير.",
    "🧠 خلاصة اللي راقبته للتو:",
    "🔔 ما نسيتك، هذا آخر شي عندي.",
    "📋 تقرير الساعة جاهز:",
]
TREND_TALK = {
    "uptrend": ["الاتجاه العام (H4) لسا صاعد بقوة 📈", "الترند صاعد وواضح 🟢 على H4", "الذهب متمسك بالصعود 📈 على الفريم الكبير"],
    "downtrend": ["الاتجاه هابط بوضوح 📉 على H4", "الترند نازل 🔴 على الفريم الكبير", "السوق مستمر بالهبوط 📉 على H4"],
    "ranging": ["الوضع متذبذب على H4 حاليًا 🌊", "السوق عرضي هالفترة على الفريم الكبير 😐", "حركة جانبية على H4 بدون قرار 🤏"],
}
CLOSERS_IDLE = [
    "😴 بس هذا وضعنا الحين، ولا فيه إشارة جاهزة على H1.",
    "🤷 لسا نراقب H1، ما توفرت كل الشروط سوا.",
    "⏳ صبر شوي، أفحص H1 كل ساعة، أول فرصة أخبرك.",
    "🔍 نكمل المراقبة على الفريم الأصغر، صبرك يستاهل.",
]
BLACKOUT_ON  = ["🔇 فيه حظر أخبار شغال حاليًا، حتى لو صارت إشارة بنؤجلها."]
BLACKOUT_OFF = ["🔊 ما فيه حظر أخبار حاليًا، الطريق مفتوح لو صارت إشارة."]


def rsi_note(rsi_val, direction_hint):
    if direction_hint == "uptrend":
        if RSI_LOW <= rsi_val <= RSI_HIGH:
            return "بمنطقة الشراء المقبولة 👍"
        return "برا منطقتنا المفضلة حاليًا."
    elif direction_hint == "downtrend":
        if (100 - RSI_HIGH) <= rsi_val <= (100 - RSI_LOW):
            return "بمنطقة البيع المقبولة 👍"
        return "برا منطقتنا المفضلة حاليًا."
    return "السوق عرضي، RSI ما يعطي إشارة قوية."


def fetch_candles(interval, outputsize):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": SYMBOL, "interval": interval, "outputsize": outputsize,
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


def add_ema(df, span, col):
    df[col] = df["close"].ewm(span=span, adjust=False).mean()
    return df


def add_rsi_atr(df):
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
    df["atr_avg20"] = df["atr"].rolling(20).mean()
    return df


def get_trend_bias(df4h):
    df4h = add_ema(df4h, EMA_TREND, "ema_trend")
    df4h = add_ema(df4h, EMA_MACRO, "ema_macro")
    last = df4h.iloc[-1]
    if last["close"] > last["ema_trend"] > last["ema_macro"]:
        return "uptrend"
    if last["close"] < last["ema_trend"] < last["ema_macro"]:
        return "downtrend"
    return "ranging"


def check_entry(df1h, bias):
    df1h = add_ema(df1h, EMA_FAST, "ema_fast")
    df1h = add_rsi_atr(df1h)
    last, prev = df1h.iloc[-1], df1h.iloc[-2]

    # --- Pattern A: classic pullback + rejection candle ---
    dist = abs(last["close"] - last["ema_fast"])
    near_pullback = dist <= (last["atr"] * PULLBACK_ATR_MULT)
    rsi_long_zone = (RSI_LOW <= last["rsi"] <= RSI_HIGH) and (last["rsi"] > prev["rsi"])
    rsi_short_zone = ((100 - RSI_HIGH) <= last["rsi"] <= (100 - RSI_LOW)) and (last["rsi"] < prev["rsi"])
    body = abs(last["close"] - last["open"])
    lw = min(last["open"], last["close"]) - last["low"]
    uw = last["high"] - max(last["open"], last["close"])
    bullish_reject = (lw >= body * 1.5 and last["close"] > last["open"]) or \
                      (last["close"] > last["open"] and last["open"] <= prev["close"] and last["close"] >= prev["open"] and prev["close"] < prev["open"])
    bearish_reject = (uw >= body * 1.5 and last["close"] < last["open"]) or \
                      (last["close"] < last["open"] and last["open"] >= prev["close"] and last["close"] <= prev["open"] and prev["close"] > prev["open"])

    pullback_long = bias == "uptrend" and near_pullback and rsi_long_zone and bullish_reject
    pullback_short = bias == "downtrend" and near_pullback and rsi_short_zone and bearish_reject

    # --- Pattern B: range-compression breakout in trend direction ---
    recent = df1h.iloc[-(CONSOLIDATION_LOOKBACK + 1):-1]
    range_high, range_low = recent["high"].max(), recent["low"].min()
    is_tight = pd.notna(last["atr_avg20"]) and last["atr"] < CONSOLIDATION_ATR_RATIO * last["atr_avg20"]
    breakout_long = bias == "uptrend" and is_tight and last["close"] > range_high
    breakout_short = bias == "downtrend" and is_tight and last["close"] < range_low

    long_signal = pullback_long or breakout_long
    short_signal = pullback_short or breakout_short
    pattern = "pullback" if (pullback_long or pullback_short) else ("breakout" if (breakout_long or breakout_short) else None)

    return long_signal, short_signal, last, near_pullback, pattern


def in_blackout_now():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    hm = now_et.strftime("%H:%M")
    return US_DATA_WINDOW[0] <= hm <= US_DATA_WINDOW[1] or FOMC_WINDOW[0] <= hm <= FOMC_WINDOW[1]


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    full_message = "📊 Secondary Strategy\n" + message
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": full_message}, timeout=15)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def main():
    now_utc = datetime.now(timezone.utc)
    if now_utc.weekday() >= 5:
        print("Weekend — market closed, skipping.")
        return
    today_str = now_utc.strftime("%Y-%m-%d")
    hour = now_utc.hour

    state = load_state()
    if state.get("date") != today_str:
        state = {"date": today_str, "last_alert_time": state.get("last_alert_time"),
                  "overlap_start_sent": False, "overlap_end_warned": False,
                  "last_status_hour": None}

    hour_key = f"{today_str}-{hour}"
    if state.get("last_status_hour") == hour_key:
        print("Already sent the hourly status for this hour (duplicate trigger). Skipping status, still checking signal silently.")
        already_sent_status_this_hour = True
    else:
        already_sent_status_this_hour = False

    if hour == OVERLAP_START_H and not state.get("overlap_start_sent"):
        send_telegram("🚀 بدأت نافذة أفضل سيولة اليوم (لندن-نيويورك)! خليني أراقب بتركيز أكبر 👀")
        state["overlap_start_sent"] = True
        save_state(state)

    if hour == OVERLAP_END_H - 1 and not state.get("overlap_end_warned"):
        send_telegram("⏳ باقي أقل من ساعة على انتهاء أفضل نافذة سيولة اليوم.")
        state["overlap_end_warned"] = True
        save_state(state)

    df4h = fetch_candles(TREND_INTERVAL, TREND_OUTPUTSIZE)
    if len(df4h) < EMA_MACRO + 5:
        print("Not enough H4 data yet.")
        save_state(state)
        return
    bias = get_trend_bias(df4h)

    df1h = fetch_candles(ENTRY_INTERVAL, ENTRY_OUTPUTSIZE)
    if len(df1h) < 30:
        print("Not enough H1 data yet.")
        save_state(state)
        return
    long_signal, short_signal, last, near_pullback, pattern = check_entry(df1h, bias)
    candle_time = str(last["datetime"])
    blackout = in_blackout_now()

    parts = [random.choice(OPENERS)]
    parts.append(f"💰 السعر الحالي: {last['close']:.2f}")
    parts.append(random.choice(TREND_TALK[bias]))
    parts.append(f"📊 RSI (H1): {last['rsi']:.1f} — {rsi_note(last['rsi'], bias)}")
    parts.append(random.choice(BLACKOUT_ON if blackout else BLACKOUT_OFF))

    already_alerted_this_candle = state.get("last_alert_time") == candle_time
    if not (long_signal or short_signal) or already_alerted_this_candle:
        parts.append(random.choice(CLOSERS_IDLE))

    if not already_sent_status_this_hour:
        send_telegram("\n".join(parts))
        state["last_status_hour"] = hour_key
        save_state(state)

    if already_alerted_this_candle:
        print("Already alerted for this candle.")
        return
    if not (long_signal or short_signal):
        print("No trade signal this hour.")
        return
    if blackout:
        print("Signal found but blackout active. Suppressed.")
        return

    atr = last["atr"]
    stop_dist = atr * ATR_STOP_MULT
    price = last["close"]
    pattern_label = "ارتداد كلاسيكي 🔁" if pattern == "pullback" else "اختراق نطاق ضيق 💥"

    if long_signal:
        sl, tp1, tp2 = price - stop_dist, price + stop_dist * RR1, price + stop_dist * RR2
        msg = (f"🟢🚀 GOLD BUY SIGNAL (XAUUSD)\nنمط الدخول: {pattern_label}\nEntry: {price:.2f}\n"
               f"Stop Loss: {sl:.2f} 🛑\nTP1 (1:{RR1}): {tp1:.2f} 🎯\nTP2 (1:{RR2}): {tp2:.2f} 🎯🎯\n"
               "Risk 0.5-1% of account. Confirm chart before entering ✅")
    else:
        sl, tp1, tp2 = price + stop_dist, price - stop_dist * RR1, price - stop_dist * RR2
        msg = (f"🔴🛑 GOLD SELL SIGNAL (XAUUSD)\nنمط الدخول: {pattern_label}\nEntry: {price:.2f}\n"
               f"Stop Loss: {sl:.2f} 🛑\nTP1 (1:{RR1}): {tp1:.2f} 🎯\nTP2 (1:{RR2}): {tp2:.2f} 🎯🎯\n"
               "Risk 0.5-1% of account. Confirm chart before entering ✅")

    send_telegram(msg)
    state["last_alert_time"] = candle_time
    save_state(state)


if __name__ == "__main__":
    main()
