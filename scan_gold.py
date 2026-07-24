"""
XAUUSD Trend-Pullback Scanner — Strategy v3 (1-2 signals/day target)
Changes from v2:
- Macro filter: uses H4 200 EMA only (price must be on correct side)
- Trend bias computed on H1 (was H4) → checked every hour, faster cycle
- Relaxed pullback: ATR distance 1.0x, no RSI direction requirement, simpler rejection candle
- Max 2 signals per day (cooldown counter in state)
- News pre-warning 1 hour before each blackout window
Hourly status messages + Arabic conversations preserved.
"""

import os
import json
import random
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TWELVE_DATA_KEY   = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
STATE_FILE = "state.json"

TREND_INTERVAL, TREND_OUTPUTSIZE = "4h", 300   # for macro filter (200 EMA)
ENTRY_INTERVAL, ENTRY_OUTPUTSIZE = "1h", 300   # for trend bias + entry

EMA_MACRO = 200                      # on H4, decides directional allowance
EMA_TREND_H1, EMA_MACRO_H1 = 50, 200 # on H1 for trend bias
EMA_FAST = 20                        # on H1 pullback zone
RSI_LEN, RSI_LOW, RSI_HIGH = 14, 35, 55   # widened zone
ATR_LEN, PULLBACK_ATR_MULT, ATR_STOP_MULT = 14, 1.0, 1.5  # relaxed pullback distance
RR1, RR2 = 1.0, 2.0

CONSOLIDATION_LOOKBACK = 10
CONSOLIDATION_ATR_RATIO = 0.7   # current ATR must be below 70% of its 20-period average

US_DATA_WINDOW = ("08:20", "09:10")   # ET
FOMC_WINDOW    = ("13:55", "14:50")   # ET
OVERLAP_START_H, OVERLAP_END_H = 13, 16   # UTC hours (8-11 ET) – approximate overlap

MAX_SIGNALS_PER_DAY = 2

OPENERS = [
    "👋 أنا لسا هنا، خلني أحدثك عن آخر وضع.",
    "🕓 مرت ساعة، وهذا اللي شفته بالسوق.",
    "📌 تحديث سريع قبل لا تسأل وش صاير.",
    "🧠 خلاصة اللي راقبته للتو:",
    "🔔 ما نسيتك، هذا آخر شي عندي.",
    "📋 تقرير الساعة جاهز:",
]
TREND_TALK = {
    "uptrend": ["الاتجاه العام (H1) لسا صاعد بقوة 📈", "الترند صاعد وواضح 🟢 على H1", "الذهب متمسك بالصعود 📈 على الساعة"],
    "downtrend": ["الاتجاه هابط بوضوح 📉 على H1", "الترند نازل 🔴 على الساعة", "السوق مستمر بالهبوط 📉 على H1"],
    "ranging": ["الوضع متذبذب على H1 حاليًا 🌊", "السوق عرضي هالفترة على الساعة 😐", "حركة جانبية على H1 بدون قرار 🤏"],
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


def get_macro_filter(df4h):
    """Use 200 EMA on H4 to determine allowed direction."""
    df4h = add_ema(df4h, EMA_MACRO, "ema_macro")
    last = df4h.iloc[-1]
    if last["close"] > last["ema_macro"]:
        return "uptrend"   # only longs allowed
    elif last["close"] < last["ema_macro"]:
        return "downtrend" # only shorts allowed
    else:
        return "neutral"   # allow both (very rare)


def get_trend_bias_h1(df1h):
    """Compute EMA50/200 on H1 and decide trend."""
    df1h = add_ema(df1h, EMA_TREND_H1, "ema_trend")
    df1h = add_ema(df1h, EMA_MACRO_H1, "ema_macro")
    last = df1h.iloc[-1]
    if last["close"] > last["ema_trend"] > last["ema_macro"]:
        return "uptrend"
    if last["close"] < last["ema_trend"] < last["ema_macro"]:
        return "downtrend"
    return "ranging"


def check_entry(df1h, bias):
    df1h = add_ema(df1h, EMA_FAST, "ema_fast")
    df1h = add_rsi_atr(df1h)
    last, prev = df1h.iloc[-1], df1h.iloc[-2]

    # --- Pattern A: classic pullback (simplified) ---
    dist = abs(last["close"] - last["ema_fast"])
    near_pullback = dist <= (last["atr"] * PULLBACK_ATR_MULT)

    # RSI zone only, no directional requirement
    rsi_in_long_zone = RSI_LOW <= last["rsi"] <= RSI_HIGH
    rsi_in_short_zone = (100 - RSI_HIGH) <= last["rsi"] <= (100 - RSI_LOW)

    # Rejection candle: just long wick in the direction of the trade
    body = abs(last["close"] - last["open"])
    lw = min(last["open"], last["close"]) - last["low"]
    uw = last["high"] - max(last["open"], last["close"])
    bullish_reject = (last["close"] > last["open"]) and (lw >= body * 1.2)
    bearish_reject = (last["close"] < last["open"]) and (uw >= body * 1.2)

    pullback_long = bias == "uptrend" and near_pullback and rsi_in_long_zone and bullish_reject
    pullback_short = bias == "downtrend" and near_pullback and rsi_in_short_zone and bearish_reject

    # --- Pattern B: range-compression breakout (unchanged) ---
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
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)


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
    today_str = now_utc.strftime("%Y-%m-%d")
    hour = now_utc.hour

    state = load_state()
    if state.get("date") != today_str:
        state = {"date": today_str, "last_alert_time": state.get("last_alert_time"),
                  "overlap_start_sent": False, "overlap_end_warned": False,
                  "last_status_hour": None,
                  "signals_today": 0,   # reset daily counter
                  "warned_us_data": False, "warned_fomc": False}

    # Ensure daily signal counter
    if "signals_today" not in state:
        state["signals_today"] = 0

    hour_key = f"{today_str}-{hour}"
    already_sent_status_this_hour = state.get("last_status_hour") == hour_key

    # ----- News pre-warnings (1 hour before blackout) -----
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() < 5:   # weekdays only
        hm = now_et.strftime("%H:%M")
        # US data warning at 07:20 ET (1h before 08:20)
        if hm == "07:20" and not state.get("warned_us_data"):
            send_telegram("⚠️ تنبيه: بعد ساعة يبدأ حظر أخبار البيانات الأمريكية (08:20 - 09:10 ET). يفضّل عدم الدخول.")
            state["warned_us_data"] = True
            save_state(state)
        # FOMC warning at 12:55 ET (1h before 13:55)
        if hm == "12:55" and not state.get("warned_fomc"):
            send_telegram("⚠️ تنبيه: بعد ساعة يبدأ حظر أخبار الفيدرالي (13:55 - 14:50 ET). يفضّل عدم الدخول.")
            state["warned_fomc"] = True
            save_state(state)
        # Reset warnings next day automatically via date check

    # Hourly overlap notifications (keep as is)
    if hour == OVERLAP_START_H and not state.get("overlap_start_sent"):
        send_telegram("🚀 بدأت نافذة أفضل سيولة اليوم (لندن-نيويورك)! خليني أراقب بتركيز أكبر 👀")
        state["overlap_start_sent"] = True
        save_state(state)

    if hour == OVERLAP_END_H - 1 and not state.get("overlap_end_warned"):
        send_telegram("⏳ باقي أقل من ساعة على انتهاء أفضل نافذة سيولة اليوم.")
        state["overlap_end_warned"] = True
        save_state(state)

    # ----- Fetch data -----
    df4h = fetch_candles(TREND_INTERVAL, TREND_OUTPUTSIZE)
    if len(df4h) < EMA_MACRO + 5:
        print("Not enough H4 data yet.")
        save_state(state)
        return
    macro_filter = get_macro_filter(df4h)

    df1h = fetch_candles(ENTRY_INTERVAL, ENTRY_OUTPUTSIZE)
    if len(df1h) < 50:
        print("Not enough H1 data yet.")
        save_state(state)
        return

    bias_h1 = get_trend_bias_h1(df1h)
    long_signal, short_signal, last, near_pullback, pattern = check_entry(df1h, bias_h1)
    candle_time = str(last["datetime"])
    blackout = in_blackout_now()

    # ----- Build hourly status message -----
    parts = [random.choice(OPENERS)]
    parts.append(f"💰 السعر الحالي: {last['close']:.2f}")
    parts.append(random.choice(TREND_TALK[bias_h1]))
    parts.append(f"📊 RSI (H1): {last['rsi']:.1f} — {rsi_note(last['rsi'], bias_h1)}")
    parts.append(random.choice(BLACKOUT_ON if blackout else BLACKOUT_OFF))

    # Suppress signal if daily limit reached or already alerted this candle
    already_alerted_this_candle = state.get("last_alert_time") == candle_time
    signal_allowed = (long_signal or short_signal) and not already_alerted_this_candle and not blackout
    if signal_allowed and state.get("signals_today", 0) >= MAX_SIGNALS_PER_DAY:
        signal_allowed = False  # daily limit hit
        # optionally inform
    if not signal_allowed:
        parts.append(random.choice(CLOSERS_IDLE))

    # Send hourly status if not already done this hour
    if not already_sent_status_this_hour:
        send_telegram("\n".join(parts))
        state["last_status_hour"] = hour_key
        save_state(state)

    # ----- Handle trade signal -----
    if not signal_allowed:
        if long_signal or short_signal:
            reason = "daily limit reached" if state.get("signals_today", 0) >= MAX_SIGNALS_PER_DAY else "duplicate/blackout"
            print(f"Signal suppressed: {reason}")
        else:
            print("No trade signal this hour.")
        return

    # Proceed with signal
    atr = last["atr"]
    stop_dist = atr * ATR_STOP_MULT
    price = last["close"]
    pattern_label = "ارتداد كلاسيكي 🔁" if pattern == "pullback" else "اختراق نطاق ضيق 💥"

    if long_signal:
        # Check macro filter: only allow if macro is uptrend (price > 200 EMA)
        if macro_filter == "downtrend":
            print("Long signal suppressed by macro filter (H4 200 EMA).")
            return
        sl, tp1, tp2 = price - stop_dist, price + stop_dist * RR1, price + stop_dist * RR2
        msg = (f"🟢🚀 GOLD BUY SIGNAL (XAUUSD)\nنمط الدخول: {pattern_label}\nEntry: {price:.2f}\n"
               f"Stop Loss: {sl:.2f} 🛑\nTP1 (1:{RR1}): {tp1:.2f} 🎯\nTP2 (1:{RR2}): {tp2:.2f} 🎯🎯\n"
               "Risk 0.5-1% of account. Confirm chart before entering ✅")
    else:
        if macro_filter == "uptrend":
            print("Short signal suppressed by macro filter (H4 200 EMA).")
            return
        sl, tp1, tp2 = price + stop_dist, price - stop_dist * RR1, price - stop_dist * RR2
        msg = (f"🔴🛑 GOLD SELL SIGNAL (XAUUSD)\nنمط الدخول: {pattern_label}\nEntry: {price:.2f}\n"
               f"Stop Loss: {sl:.2f} 🛑\nTP1 (1:{RR1}): {tp1:.2f} 🎯\nTP2 (1:{RR2}): {tp2:.2f} 🎯🎯\n"
               "Risk 0.5-1% of account. Confirm chart before entering ✅")

    send_telegram(msg)
    state["last_alert_time"] = candle_time
    state["signals_today"] = state.get("signals_today", 0) + 1
    save_state(state)


if __name__ == "__main__":
    main()
