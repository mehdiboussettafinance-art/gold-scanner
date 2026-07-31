
"""
XAUUSD Trading Bot - إصدار محدّث
- شراء / بيع مع الاتجاه وعكسه تصحيحياً
- ملاحظة: تنبيهات الأخبار الحين بملف منفصل (news_monitor.py) يشتغل أكثر تكرارًا
  بدون ما يستهلك حصة Twelve Data
"""

import os
import json
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

# ---------- مفاتيح البيئة ----------
TWELVE_DATA_KEY = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
STATE_FILE = "state.json"

# ---------- إعدادات الإطارات ----------
MACRO_INTERVAL, MACRO_OUTPUTSIZE = "4h", 300
TREND_INTERVAL, TREND_OUTPUTSIZE = "1h", 300
ENTRY_INTERVAL, ENTRY_OUTPUTSIZE = "15min", 500
SWING_LOOKBACK = 20

EMA_MACRO = 200
EMA_TREND_H1 = 50
EMA_MACRO_H1 = 200
EMA_FAST = 20
EMA_MID = 50
RSI_LEN = 14
ATR_LEN = 14

PULLBACK_ATR_MULT = 1.2
REJECTION_WICK_RATIO = 1.2
RSI_BUY_LOW, RSI_BUY_HIGH = 35, 55
RSI_SELL_LOW, RSI_SELL_HIGH = 45, 65
ATR_STOP_MULT = 1.5
RR1, RR2 = 1.0, 2.0
MAX_SIGNALS_PER_DAY = 2

# نافذة الحظر حول أي خبر عالي التأثير (دقائق قبل/بعد وقته الفعلي)
NEWS_BLACKOUT_BEFORE_MIN = 30
NEWS_BLACKOUT_AFTER_MIN = 30
NEWS_STATE_FILE = "news_state.json"  # يكتبه news_monitor.py، هذا الملف يقرأه بس


def in_news_blackout_now():
    """
    يتحقق من الحظر بقراءة نفس الملف اللي يحدّثه news_monitor.py،
    بدون ما يسوي أي طلب لـ FMP بنفسه (يوفر الحصة المجانية).
    """
    if not os.path.exists(NEWS_STATE_FILE):
        return False, None
    try:
        with open(NEWS_STATE_FILE) as f:
            news_state = json.load(f)
    except Exception:
        return False, None

    now_utc = datetime.now(timezone.utc)
    for e in news_state.get("todays_events", []):
        event_time = datetime.fromisoformat(e["time_utc"])
        window_start = event_time - timedelta(minutes=NEWS_BLACKOUT_BEFORE_MIN)
        window_end = event_time + timedelta(minutes=NEWS_BLACKOUT_AFTER_MIN)
        if window_start <= now_utc <= window_end:
            return True, e["event"]
    return False, None


# ============================================================
# البيانات والمؤشرات
# ============================================================
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
    return df


def get_macro_filter(df4h):
    df4h = add_ema(df4h, EMA_MACRO, "ema_macro")
    last = df4h.iloc[-1]
    if last["close"] > last["ema_macro"]:
        return "uptrend"
    elif last["close"] < last["ema_macro"]:
        return "downtrend"
    return "neutral"


def get_trend_bias_h1(df1h):
    df1h = add_ema(df1h, EMA_TREND_H1, "ema_trend")
    df1h = add_ema(df1h, EMA_MACRO_H1, "ema_macro")
    last = df1h.iloc[-1]
    if last["close"] > last["ema_trend"] > last["ema_macro"]:
        return "uptrend"
    if last["close"] < last["ema_trend"] < last["ema_macro"]:
        return "downtrend"
    return "ranging"


def find_swing_high_low(df, lookback=SWING_LOOKBACK):
    if len(df) < lookback:
        return None, None
    recent = df.iloc[-lookback:-1]
    return recent["high"].max(), recent["low"].min()


def check_entry(df, bias, macro_filter):
    if len(df) < 30:
        return False, False, None, None, None

    df = add_ema(df, EMA_FAST, "ema_fast")
    df = add_ema(df, EMA_MID, "ema_mid")
    df = add_rsi_atr(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    swing_high, swing_low = find_swing_high_low(df)
    if swing_high is None or swing_low is None:
        return False, False, None, None, None

    body = abs(last["close"] - last["open"])
    lw = min(last["open"], last["close"]) - last["low"]
    uw = last["high"] - max(last["open"], last["close"])
    bullish_reject = (last["close"] > last["open"]) and (lw >= body * REJECTION_WICK_RATIO)
    bearish_reject = (last["close"] < last["open"]) and (uw >= body * REJECTION_WICK_RATIO)

    near_ema = abs(last["close"] - last["ema_fast"]) <= (last["atr"] * PULLBACK_ATR_MULT)
    rsi_buy_ok = RSI_BUY_LOW <= last["rsi"] <= RSI_BUY_HIGH
    rsi_sell_ok = RSI_SELL_LOW <= last["rsi"] <= RSI_SELL_HIGH

    buy_signal = False
    sell_signal = False
    pattern = None

    if bias == "uptrend" and macro_filter != "downtrend":
        at_support = last["low"] <= swing_low * 1.005
        micro_trend_up = last["ema_fast"] > last["ema_mid"]
        if at_support and near_ema and micro_trend_up and rsi_buy_ok and bullish_reject:
            buy_signal = True
            pattern = "🟢 شراء من الدعم"

        broke_high = prev["close"] > swing_high or last["close"] > swing_high
        retesting = abs(last["close"] - swing_high) <= (last["atr"] * 0.7) or near_ema
        if not buy_signal and broke_high and retesting and rsi_sell_ok and bearish_reject:
            sell_signal = True
            pattern = "🔴 بيع تصحيحي بعد كسر قمة"

    elif bias == "downtrend" and macro_filter != "uptrend":
        at_resistance = last["high"] >= swing_high * 0.995
        micro_trend_down = last["ema_fast"] < last["ema_mid"]
        if at_resistance and near_ema and micro_trend_down and rsi_sell_ok and bearish_reject:
            sell_signal = True
            pattern = "🔴 بيع من المقاومة"

        broke_low = prev["close"] < swing_low or last["close"] < swing_low
        retesting_low = abs(last["close"] - swing_low) <= (last["atr"] * 0.7) or near_ema
        if not sell_signal and broke_low and retesting_low and rsi_buy_ok and bullish_reject:
            buy_signal = True
            pattern = "🟢 شراء تصحيحي بعد كسر قاع"

    return buy_signal, sell_signal, last, pattern, (swing_high, swing_low)


# ============================================================
# تيليجرام / الحالة
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    except Exception as e:
        print(f"Telegram error: {e}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, default=str)


# ============================================================
# MAIN
# ============================================================
def main():
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")

    state = load_state()
    if state.get("date") != today_str:
        state = {"date": today_str, "last_alert_time": None, "signals_today": 0}

    for key in ["signals_today"]:
        if key not in state:
            state[key] = 0

    save_state(state)

    try:
        df4h = fetch_candles(MACRO_INTERVAL, MACRO_OUTPUTSIZE)
        df1h = fetch_candles(TREND_INTERVAL, TREND_OUTPUTSIZE)
        df_m15 = fetch_candles(ENTRY_INTERVAL, ENTRY_OUTPUTSIZE)
        df_m30 = fetch_candles("30min", ENTRY_OUTPUTSIZE)
    except Exception as e:
        print(f"خطأ في جلب البيانات: {e}")
        return

    if len(df4h) < 50 or len(df1h) < 50 or len(df_m15) < 30:
        print("بيانات غير كافية.")
        save_state(state)
        return

    macro_filter = get_macro_filter(df4h)
    bias_h1 = get_trend_bias_h1(df1h)

    buy_m15, sell_m15, last_m15, pattern_m15, swings_m15 = check_entry(df_m15, bias_h1, macro_filter)
    buy_m30, sell_m30, last_m30, pattern_m30, swings_m30 = check_entry(df_m30, bias_h1, macro_filter)

    if buy_m15 or sell_m15:
        buy_signal, sell_signal = buy_m15, sell_m15
        last, pattern = last_m15, pattern_m15
        tf_used = "M15"
    elif buy_m30 or sell_m30:
        buy_signal, sell_signal = buy_m30, sell_m30
        last, pattern = last_m30, pattern_m30
        tf_used = "M30"
    else:
        buy_signal = sell_signal = False
        last = df_m15.iloc[-1]
        pattern = None
        tf_used = "—"

    candle_time = str(last["datetime"])
    blackout, blackout_event = in_news_blackout_now()

    already_alerted = (state.get("last_alert_time") == candle_time)
    signal_allowed = (buy_signal or sell_signal) and not already_alerted and not blackout
    if signal_allowed and state["signals_today"] >= MAX_SIGNALS_PER_DAY:
        signal_allowed = False

    if not signal_allowed:
        if blackout and (buy_signal or sell_signal):
            print(f"إشارة موجودة لكن ممنوعة بسبب حظر خبر: {blackout_event}")
        else:
            print("لا توجد إشارة جديدة.")
        save_state(state)
        return

    atr = last["atr"]
    stop_dist = atr * ATR_STOP_MULT
    price = last["close"]

    if buy_signal:
        if macro_filter == "downtrend":
            save_state(state)
            return
        sl = price - stop_dist
        tp1 = price + stop_dist * RR1
        tp2 = price + stop_dist * RR2
        msg = (f"🟢🚀 GOLD BUY\n📌 {pattern}\n⏱️ الإطار: {tf_used}\n📍 الدخول: {price:.2f}\n"
               f"🛑 وقف الخسارة: {sl:.2f}\n🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f}\n"
               "⚠️ المخاطرة 0.5-1% من الحساب.")
    else:
        if macro_filter == "uptrend":
            save_state(state)
            return
        sl = price + stop_dist
        tp1 = price - stop_dist * RR1
        tp2 = price - stop_dist * RR2
        msg = (f"🔴🛑 GOLD SELL\n📌 {pattern}\n⏱️ الإطار: {tf_used}\n📍 الدخول: {price:.2f}\n"
               f"🛑 وقف الخسارة: {sl:.2f}\n🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f}\n"
               "⚠️ المخاطرة 0.5-1% من الحساب.")

    send_telegram(msg)
    state["last_alert_time"] = candle_time
    state["signals_today"] += 1
    save_state(state)
    print(f"✅ إشارة أُرسلت: {pattern}")


if __name__ == "__main__":
    main()
