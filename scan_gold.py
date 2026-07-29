"""
XAUUSD Trading Bot - إصدار كامل (يعمل على الاتجاهين)
- شراء / بيع مع الاتجاه وعكسه تصحيحياً
- تحذيرات أخبار مع تأثيرها
- رسائل عربية مفصلة ومتنوعة
"""

import os
import json
import random
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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

# ---------- المؤشرات ----------
EMA_MACRO = 200
EMA_TREND_H1 = 50
EMA_MACRO_H1 = 200
EMA_FAST = 20
EMA_MID = 50
RSI_LEN = 14
ATR_LEN = 14

# معاملات الدخول (مرنة قليلاً لزيادة الفرص)
PULLBACK_ATR_MULT = 1.2          # كان 1.0 – مساحة أوسع للارتداد
REJECTION_WICK_RATIO = 1.2       # كان 1.3 – شمعة أسهل قليلاً
RSI_BUY_LOW, RSI_BUY_HIGH = 35, 55
RSI_SELL_LOW, RSI_SELL_HIGH = 45, 65
ATR_STOP_MULT = 1.5
RR1, RR2 = 1.0, 2.0
MAX_SIGNALS_PER_DAY = 2

# نوافذ الأخبار بتوقيت نيويورك
US_DATA_WINDOW = ("08:20", "09:10")    # بيانات يومية
FOMC_WINDOW    = ("13:55", "14:50")    # الفيدرالي

# تأثير الأخبار (يُرسل عند بدء الحظر)
NEWS_IMPACT = {
    "us_data": "📰 بيانات اقتصادية أمريكية (تأثير عالي جداً على الذهب) – يمنع التداول حتى 9:10.",
    "fomc":    "🏛️ اجتماع الفيدرالي وتصريحاته (تأثير عالٍ جداً) – يمنع التداول حتى 14:50.",
    "nfp":     "💼 بيانات التوظيف NFP (تأثير عالٍ جداً – السوق يتحرك بعنف).",
    "cpi":     "📊 بيانات التضخم CPI (تأثير عالٍ جداً على أسعار الفائدة والذهب).",
}

# ---------- النصوص العربية ----------
OPENERS = [
    "👋 أهلًا، تقرير الساعة الجديد جاهز.",
    "🕓 تحديث الساعة، خلينا نشوف اللي صار.",
    "📌 تقرير سريع ومختصر قبل ما تسألني.",
    "🧠 خلاصة تحليلي للساعة الأخيرة:",
    "🔔 معاك واحدث المستجدات.",
    "📋 التقرير الدوري للساعة جاهز:",
]

TREND_TALK = {
    "uptrend": ["🔵 الاتجاه العام (H1) صاعد.", "📈 الذهب في موجة صاعدة."],
    "downtrend": ["🔴 الاتجاه العام (H1) هابط.", "📉 الذهب في موجة هابطة."],
    "ranging": ["🌊 السوق متذبذب على H1.", "⏸️ حركة عرضية بدون اتجاه."],
}

MACRO_TALK = {
    "uptrend": "🟩 الماكرو (4H فوق EMA200): يسمح بشراء فقط.",
    "downtrend": "🟥 الماكرو (4H تحت EMA200): يسمح ببيع فقط.",
    "neutral": "⬜ الماكرو محايد: لا قيود."
}

RSI_COMMENTS = [
    "📊 RSI = {rsi:.1f} ({zone})",
    "🎚️ الـ RSI عند {rsi:.1f}: {zone}",
]

CLOSERS_IDLE = [
    "😴 لا توجد إشارة الآن. أتابع بصمت.",
    "🤷 لم تكتمل الشروط، صبرك جميل.",
    "⏳ أول ما تظهر فرصة سأرسلها فوراً.",
]

BLACKOUT_ON  = ["🔇 حظر أخبار مفعّل – لا صفقات."]
BLACKOUT_OFF = ["✅ السوق مفتوح بدون حظر."]

def rsi_zone_note(rsi_val, bias):
    if bias == "uptrend":
        if RSI_BUY_LOW <= rsi_val <= RSI_BUY_HIGH:
            return "منطقة شراء مقبولة 👍"
        elif rsi_val > RSI_BUY_HIGH:
            return "تشبع شرائي ⚠️"
        else:
            return "ضعيف"
    elif bias == "downtrend":
        if RSI_SELL_LOW <= rsi_val <= RSI_SELL_HIGH:
            return "منطقة بيع مقبولة 👍"
        elif rsi_val < RSI_SELL_LOW:
            return "تشبع بيعي ⚠️"
        else:
            return "قوي"
    return "غير محدد"

# ---------- دوال البيانات ----------
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
    avg_gain = gain.ewm(alpha=1/RSI_LEN, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/RSI_LEN, adjust=False).mean()
    df["rsi"] = 100 - (100 / (1 + avg_gain / avg_loss))
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/ATR_LEN, adjust=False).mean()
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
    """
    أنماط الاتجاهين:
    - صاعد: شراء من دعم / بيع بعد كسر قمة
    - هابط: بيع من مقاومة / شراء بعد كسر قاع
    """
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

    # ========== الاتجاه الصاعد ==========
    if bias == "uptrend" and macro_filter != "downtrend":
        # 1. شراء من دعم
        at_support = last["low"] <= swing_low * 1.005
        micro_trend_up = last["ema_fast"] > last["ema_mid"]
        if at_support and near_ema and micro_trend_up and rsi_buy_ok and bullish_reject:
            buy_signal = True
            pattern = "🟢 شراء من الدعم"

        # 2. بيع تصحيحي بعد كسر قمة
        broke_high = prev["close"] > swing_high or last["close"] > swing_high
        retesting = abs(last["close"] - swing_high) <= (last["atr"] * 0.7) or near_ema
        if not buy_signal and broke_high and retesting and rsi_sell_ok and bearish_reject:
            sell_signal = True
            pattern = "🔴 بيع تصحيحي بعد كسر قمة"

    # ========== الاتجاه الهابط ==========
    elif bias == "downtrend" and macro_filter != "uptrend":
        # 3. بيع من مقاومة
        at_resistance = last["high"] >= swing_high * 0.995
        micro_trend_down = last["ema_fast"] < last["ema_mid"]
        if at_resistance and near_ema and micro_trend_down and rsi_sell_ok and bearish_reject:
            sell_signal = True
            pattern = "🔴 بيع من المقاومة"

        # 4. شراء تصحيحي بعد كسر قاع
        broke_low = prev["close"] < swing_low or last["close"] < swing_low
        retesting_low = abs(last["close"] - swing_low) <= (last["atr"] * 0.7) or near_ema
        if not sell_signal and broke_low and retesting_low and rsi_buy_ok and bullish_reject:
            buy_signal = True
            pattern = "🟢 شراء تصحيحي بعد كسر قاع"

    return buy_signal, sell_signal, last, pattern, (swing_high, swing_low)

def in_blackout_now():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:
        return False
    hm = now_et.strftime("%H:%M")
    return (US_DATA_WINDOW[0] <= hm <= US_DATA_WINDOW[1]) or (FOMC_WINDOW[0] <= hm <= FOMC_WINDOW[1])

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
        json.dump(state, f)

def main():
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    hour = now_utc.hour
    now_et = datetime.now(ZoneInfo("America/New_York"))

    state = load_state()
    if state.get("date") != today_str:
        state = {
            "date": today_str,
            "last_alert_time": None,
            "last_status_hour": None,
            "signals_today": 0,
            "warned_us_data": False,
            "warned_fomc": False,
            "warned_nfp": False,
            "warned_cpi": False,
            "blackout_start_notified": False,
            "daily_reminder_sent": False,
        }

    # التأكد من وجود المفاتيح
    for key in ["signals_today", "warned_us_data", "warned_fomc", "warned_nfp",
                "warned_cpi", "blackout_start_notified", "daily_reminder_sent"]:
        if key not in state:
            state[key] = 0 if key == "signals_today" else False

    hour_key = f"{today_str}-{hour}"
    already_sent_status = (state.get("last_status_hour") == hour_key)

    # -------- تحذيرات الأخبار (قبل الحدث) ----------
    if now_et.weekday() < 5:
        hm = now_et.strftime("%H:%M")
        if hm == "07:20" and not state["warned_us_data"]:
            send_telegram(f"⚠️ {NEWS_IMPACT['us_data']}")
            state["warned_us_data"] = True
            save_state(state)
        if hm == "12:55" and not state["warned_fomc"]:
            send_telegram(f"⚠️ {NEWS_IMPACT['fomc']}")
            state["warned_fomc"] = True
            save_state(state)
        if now_et.weekday() == 4 and now_et.day <= 7 and not state["warned_nfp"]:
            send_telegram(f"📅 {NEWS_IMPACT['nfp']}")
            state["warned_nfp"] = True
            save_state(state)
        if 10 <= now_et.day <= 14 and not state["warned_cpi"]:
            send_telegram(f"📅 {NEWS_IMPACT['cpi']}")
            state["warned_cpi"] = True
            save_state(state)
        if hm == "07:00" and not state["daily_reminder_sent"]:
            send_telegram("📋 تذكير: راجع التقويم الاقتصادي قبل الجلسة الأمريكية.")
            state["daily_reminder_sent"] = True
            save_state(state)

    # ---------- جلب البيانات ----------
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

    # فحص الإطارات الصغرى
    buy_m15, sell_m15, last_m15, pattern_m15, swings_m15 = check_entry(df_m15, bias_h1, macro_filter)
    buy_m30, sell_m30, last_m30, pattern_m30, swings_m30 = check_entry(df_m30, bias_h1, macro_filter)

    if buy_m15 or sell_m15:
        buy_signal, sell_signal = buy_m15, sell_m15
        last, pattern, swings = last_m15, pattern_m15, swings_m15
        tf_used = "M15"
    elif buy_m30 or sell_m30:
        buy_signal, sell_signal = buy_m30, sell_m30
        last, pattern, swings = last_m30, pattern_m30, swings_m30
        tf_used = "M30"
    else:
        buy_signal = sell_signal = False
        last = df_m15.iloc[-1]
        pattern = None
        swings = (None, None)
        tf_used = "—"

    candle_time = str(last["datetime"])
    blackout = in_blackout_now()

    # إشعار بدء الحظر مع التأثير (مرة واحدة عند الدخول)
    if blackout and not state.get("blackout_start_notified"):
        if now_et.strftime("%H:%M") <= US_DATA_WINDOW[0] or now_et.strftime("%H:%M") <= FOMC_WINDOW[0]:
            # يمكن إرسال تأثير الخبر المناسب
            send_telegram(f"🔇 بدأ حظر الأخبار - {NEWS_IMPACT.get('us_data', '')} {NEWS_IMPACT.get('fomc', '')}")
        state["blackout_start_notified"] = True
        save_state(state)
    elif not blackout:
        state["blackout_start_notified"] = False
        save_state(state)

    # ---------- بناء رسالة الحالة ----------
    parts = [random.choice(OPENERS)]
    parts.append(f"💰 السعر: {last['close']:.2f}")
    parts.append(random.choice(TREND_TALK[bias_h1]))
    parts.append(MACRO_TALK[macro_filter])
    
    rsi_zone = rsi_zone_note(last["rsi"], bias_h1)
    rsi_msg = random.choice(RSI_COMMENTS).format(rsi=last["rsi"], zone=rsi_zone)
    parts.append(rsi_msg)

    if swings[0] is not None:
        parts.append(f"📌 القمة: {swings[0]:.2f} | القاع: {swings[1]:.2f}")

    if pattern:
        parts.append(f"🔔 إشارة محتملة: {pattern} على {tf_used}")
    else:
        if bias_h1 == "uptrend":
            parts.append("🔎 أنتظر دعم قوي أو كسر قمة.")
        elif bias_h1 == "downtrend":
            parts.append("🔎 أنتظر مقاومة قوية أو كسر قاع.")
        else:
            parts.append("🔎 السوق عرضي، لا أنماط واضحة.")

    parts.append(random.choice(BLACKOUT_ON if blackout else BLACKOUT_OFF))

    already_alerted = (state.get("last_alert_time") == candle_time)
    signal_allowed = (buy_signal or sell_signal) and not already_alerted and not blackout
    if signal_allowed and state["signals_today"] >= MAX_SIGNALS_PER_DAY:
        signal_allowed = False

    if not signal_allowed:
        parts.append(random.choice(CLOSERS_IDLE))

    # إرسال التحديث كل ساعة
    if not already_sent_status:
        send_telegram("\n".join(parts))
        state["last_status_hour"] = hour_key
        save_state(state)

    if not signal_allowed:
        return

    # ---------- إرسال الصفقة ----------
    atr = last["atr"]
    stop_dist = atr * ATR_STOP_MULT
    price = last["close"]

    if buy_signal:
        if macro_filter == "downtrend":
            return
        sl = price - stop_dist
        tp1 = price + stop_dist * RR1
        tp2 = price + stop_dist * RR2
        msg = (
            f"🟢🚀 **GOLD BUY**\n"
            f"📌 {pattern}\n"
            f"⏱️ الإطار: {tf_used}\n"
            f"📍 الدخول: {price:.2f}\n"
            f"🛑 وقف الخسارة: {sl:.2f}\n"
            f"🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f}\n"
            "⚠️ المخاطرة 0.5-1% من الحساب."
        )
    else:
        if macro_filter == "uptrend":
            return
        sl = price + stop_dist
        tp1 = price - stop_dist * RR1
        tp2 = price - stop_dist * RR2
        msg = (
            f"🔴🛑 **GOLD SELL**\n"
            f"📌 {pattern}\n"
            f"⏱️ الإطار: {tf_used}\n"
            f"📍 الدخول: {price:.2f}\n"
            f"🛑 وقف الخسارة: {sl:.2f}\n"
            f"🎯 TP1: {tp1:.2f} | TP2: {tp2:.2f}\n"
            "⚠️ المخاطرة 0.5-1% من الحساب."
        )

    send_telegram(msg)
    state["last_alert_time"] = candle_time
    state["signals_today"] += 1
    save_state(state)
    print(f"✅ إشارة: {pattern}")

if __name__ == "__main__":
    main()
