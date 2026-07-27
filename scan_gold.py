"""
XAUUSD Trading Bot - الإصدار النهائي (استراتيجية مزدوجة)
بناءً على تحليل إشارات التليجرام ونص المتداول.

الاستراتيجية:
1. شراء عند الدعم: مناطق سعرية رئيسية (قيعان سابقة) + ارتداد من EMA20 + شمعة رفض بوليش.
2. بيع تصحيحي: بعد كسر القمة الأخيرة، ننتظر إعادة اختبار المنطقة + شمعة رفض بيريش.

الميزات: تحديثات ساعة بالعربية، حظر أخبار، حد يومي، حفظ الحالة.
"""

import os
import json
import random
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ---------- المتغيرات البيئية ----------
TWELVE_DATA_KEY = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
STATE_FILE = "state.json"

# ---------- الإطارات الزمنية ----------
MACRO_INTERVAL, MACRO_OUTPUTSIZE = "4h", 300   # H4 للفلتر
TREND_INTERVAL, TREND_OUTPUTSIZE = "1h", 300   # H1 للاتجاه
ENTRY_INTERVAL, ENTRY_OUTPUTSIZE = "15min", 500 # M15 للدخول الأساسي
SWING_LOOKBACK = 20  # عدد الشموع لاستخراج القمة/القاع الأخير

# ---------- إعدادات المؤشرات ----------
EMA_MACRO = 200
EMA_TREND_H1 = 50
EMA_MACRO_H1 = 200
EMA_FAST = 20
RSI_LEN = 14
ATR_LEN = 14

# شروط الدخول
PULLBACK_ATR_MULT = 1.0
REJECTION_WICK_RATIO = 1.3
RSI_BUY_LOW, RSI_BUY_HIGH = 35, 55
RSI_SELL_LOW, RSI_SELL_HIGH = 45, 65
ATR_STOP_MULT = 1.5
RR1, RR2 = 1.0, 2.0

# الحد اليومي
MAX_SIGNALS_PER_DAY = 2

# أوقات حظر الأخبار (بتوقيت نيويورك)
US_DATA_WINDOW = ("08:20", "09:10")
FOMC_WINDOW = ("13:55", "14:50")

# ---------- رسائل التليجرام (بالعربية) ----------
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
    "😴 بس هذا وضعنا الحين، ولا فيه إشارة جاهزة.",
    "🤷 لسا نراقب، ما توفرت كل الشروط سوا.",
    "⏳ صبر شوي، أفحص كل ربع ساعة، أول فرصة أخبرك.",
    "🔍 نكمل المراقبة على الفريم الأصغر، صبرك يستاهل.",
]
BLACKOUT_ON = ["🔇 فيه حظر أخبار شغال حاليًا، حتى لو صارت إشارة بنؤجلها."]
BLACKOUT_OFF = ["🔊 ما فيه حظر أخبار حاليًا، الطريق مفتوح لو صارت إشارة."]

def rsi_note(rsi_val, direction_hint):
    if direction_hint == "uptrend":
        if RSI_BUY_LOW <= rsi_val <= RSI_BUY_HIGH:
            return "بمنطقة الشراء المقبولة 👍"
        return "برا منطقتنا المفضلة حاليًا."
    elif direction_hint == "downtrend":
        if RSI_SELL_LOW <= rsi_val <= RSI_SELL_HIGH:
            return "بمنطقة البيع المقبولة 👍"
        return "برا منطقتنا المفضلة حاليًا."
    return "السوق عرضي، RSI ما يعطي إشارة قوية."

# ---------- دوال جلب البيانات والمؤشرات ----------
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
    """استخراج أعلى قمة وأدنى قاع خلال آخر N شمعة."""
    if len(df) < lookback:
        return None, None
    recent = df.iloc[-lookback:-1]  # نستثني الشمعة الحالية
    swing_high = recent["high"].max()
    swing_low = recent["low"].min()
    return swing_high, swing_low

def check_entry_on_timeframe(df, bias, macro_filter):
    """
    التحقق من شرطين:
    1. شراء عند الدعم (سعر قريب من القاع + EMA20 + شمعة رفض بوليش).
    2. بيع تصحيحي (كسر القمة + إعادة اختبار + شمعة رفض بيريش).
    """
    if len(df) < 30:
        return False, False, None, None, None

    df = add_ema(df, EMA_FAST, "ema_fast")
    df = add_rsi_atr(df)
    last = df.iloc[-1]
    
    # 1. استخراج القمة والقاع الأخيرين
    swing_high, swing_low = find_swing_high_low(df)
    if swing_high is None or swing_low is None:
        return False, False, None, None, None

    # 2. حساب شمعة الرفض
    body = abs(last["close"] - last["open"])
    lw = min(last["open"], last["close"]) - last["low"]
    uw = last["high"] - max(last["open"], last["close"])
    bullish_reject = (last["close"] > last["open"]) and (lw >= body * REJECTION_WICK_RATIO)
    bearish_reject = (last["close"] < last["open"]) and (uw >= body * REJECTION_WICK_RATIO)

    # 3. المسافة إلى EMA20
    dist_to_ema = abs(last["close"] - last["ema_fast"])
    near_ema = dist_to_ema <= (last["atr"] * PULLBACK_ATR_MULT)

    # 4. مناطق RSI
    rsi_buy_ok = RSI_BUY_LOW <= last["rsi"] <= RSI_BUY_HIGH
    rsi_sell_ok = RSI_SELL_LOW <= last["rsi"] <= RSI_SELL_HIGH

    # ---------- النمط 1: شراء عند الدعم ----------
    # الشروط: سعر قريب من القاع الأخير + قريب من EMA20 + شمعة رفع بوليش + RSI مناسب
    at_support = last["low"] <= swing_low * 1.005  # في حدود 0.5% من القاع
    buy_signal = (
        bias == "uptrend" and
        macro_filter != "downtrend" and
        at_support and
        near_ema and
        rsi_buy_ok and
        bullish_reject
    )

    # ---------- النمط 2: بيع تصحيحي بعد اختراق القمة ----------
    # الشروط: السعر كسر القمة الأخيرة (اختراق) + عاد لإعادة الاختبار + شمعة رفض بيريش
    broke_high = last["close"] > swing_high
    # نتحقق من أن السعر الحالي قريب من القمة المكسورة أو EMA20 (إعادة اختبار)
    retesting = abs(last["close"] - swing_high) <= (last["atr"] * 0.5) or near_ema
    
    sell_signal = (
        bias == "uptrend" and   # نبيع تصحيحي في اتجاه صاعد فقط (كما قال المتداول)
        macro_filter != "downtrend" and
        broke_high and
        retesting and
        rsi_sell_ok and
        bearish_reject
    )

    # تحديد النمط
    if buy_signal:
        pattern_type = "شراء من الدعم 🟢"
    elif sell_signal:
        pattern_type = "بيع تصحيحي بعد اختراق 🔴"
    else:
        pattern_type = None

    return buy_signal, sell_signal, last, pattern_type, (swing_high, swing_low)

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
        state = {
            "date": today_str,
            "last_alert_time": None,
            "overlap_start_sent": False,
            "overlap_end_warned": False,
            "last_status_hour": None,
            "signals_today": 0,
            "warned_us_data": False,
            "warned_fomc": False
        }

    if "signals_today" not in state:
        state["signals_today"] = 0

    hour_key = f"{today_str}-{hour}"
    already_sent_status = state.get("last_status_hour") == hour_key

    # ----- تحذيرات الأخبار (قبل ساعة) -----
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() < 5:
        hm = now_et.strftime("%H:%M")
        if hm == "07:20" and not state.get("warned_us_data"):
            send_telegram("⚠️ تنبيه: بعد ساعة يبدأ حظر أخبار البيانات الأمريكية (08:20 - 09:10 ET). يفضّل عدم الدخول.")
            state["warned_us_data"] = True
            save_state(state)
        if hm == "12:55" and not state.get("warned_fomc"):
            send_telegram("⚠️ تنبيه: بعد ساعة يبدأ حظر أخبار الفيدرالي (13:55 - 14:50 ET). يفضّل عدم الدخول.")
            state["warned_fomc"] = True
            save_state(state)

    # ----- جلب البيانات -----
    try:
        df4h = fetch_candles(MACRO_INTERVAL, MACRO_OUTPUTSIZE)
        df1h = fetch_candles(TREND_INTERVAL, TREND_OUTPUTSIZE)
        df_m15 = fetch_candles(ENTRY_INTERVAL, ENTRY_OUTPUTSIZE)
        # نستخدم M30 كاحتياطي أيضاً
        df_m30 = fetch_candles("30min", ENTRY_OUTPUTSIZE)
    except Exception as e:
        print(f"خطأ في جلب البيانات: {e}")
        return

    if len(df4h) < 50 or len(df1h) < 50 or len(df_m15) < 30 or len(df_m30) < 30:
        print("بيانات غير كافية.")
        save_state(state)
        return

    macro_filter = get_macro_filter(df4h)
    bias_h1 = get_trend_bias_h1(df1h)

    # فحص M15 أولاً، ثم M30
    buy_m15, sell_m15, last_m15, pattern_m15, swings_m15 = check_entry_on_timeframe(df_m15, bias_h1, macro_filter)
    buy_m30, sell_m30, last_m30, pattern_m30, swings_m30 = check_entry_on_timeframe(df_m30, bias_h1, macro_filter)

    # نعطي الأولوية لـ M15 إذا وجدت إشارة
    if buy_m15 or sell_m15:
        buy_signal, sell_signal = buy_m15, sell_m15
        last = last_m15
        pattern = pattern_m15
        swings = swings_m15
    elif buy_m30 or sell_m30:
        buy_signal, sell_signal = buy_m30, sell_m30
        last = last_m30
        pattern = pattern_m30
        swings = swings_m30
    else:
        buy_signal = sell_signal = False
        last = df_m15.iloc[-1]  # للعرض
        pattern = None
        swings = (None, None)

    candle_time = str(last["datetime"])
    blackout = in_blackout_now()

    # ----- رسالة الحالة الساعية -----
    parts = [random.choice(OPENERS)]
    parts.append(f"💰 السعر الحالي: {last['close']:.2f}")
    parts.append(random.choice(TREND_TALK[bias_h1]))
    if swings[0] is not None:
        parts.append(f"📈 آخر قمة: {swings[0]:.2f}  |  آخر قاع: {swings[1]:.2f}")
    if last is not None:
        parts.append(f"📊 RSI (M15): {last['rsi']:.1f} — {rsi_note(last['rsi'], bias_h1)}")
    parts.append(random.choice(BLACKOUT_ON if blackout else BLACKOUT_OFF))

    already_alerted = state.get("last_alert_time") == candle_time
    signal_allowed = (buy_signal or sell_signal) and not already_alerted and not blackout
    if signal_allowed and state.get("signals_today", 0) >= MAX_SIGNALS_PER_DAY:
        signal_allowed = False

    if not signal_allowed:
        parts.append(random.choice(CLOSERS_IDLE))

    if not already_sent_status:
        send_telegram("\n".join(parts))
        state["last_status_hour"] = hour_key
        save_state(state)

    if not signal_allowed:
        if buy_signal or sell_signal:
            reason = "الحد اليومي" if state.get("signals_today", 0) >= MAX_SIGNALS_PER_DAY else "مكرر/حظر"
            print(f"الإشارة ملغاة: {reason}")
        else:
            print("لا توجد إشارة هذا الساعة.")
        return

    # ----- تنفيذ الإشارة -----
    atr = last["atr"]
    stop_dist = atr * ATR_STOP_MULT
    price = last["close"]

    if buy_signal:
        if macro_filter == "downtrend":
            print("تم إلغاء الشراء بسبب فلتر H4.")
            return
        sl = price - stop_dist
        tp1 = price + stop_dist * RR1
        tp2 = price + stop_dist * RR2
        msg = (
            f"🟢🚀 GOLD BUY SIGNAL (XAUUSD)\n"
            f"📌 النمط: {pattern}\n"
            f"📍 الدخول: {price:.2f}\n"
            f"🛑 وقف الخسارة: {sl:.2f}\n"
            f"🎯 TP1 (1:{RR1}): {tp1:.2f}\n"
            f"🎯🎯 TP2 (1:{RR2}): {tp2:.2f}\n"
            "⚠️ المخاطرة 0.5-1% من الحساب. تأكد من الشارت قبل الدخول ✅"
        )
    else:  # sell_signal
        if macro_filter == "uptrend":
            print("تم إلغاء البيع بسبب فلتر H4.")
            return
        sl = price + stop_dist
        tp1 = price - stop_dist * RR1
        tp2 = price - stop_dist * RR2
        msg = (
            f"🔴🛑 GOLD SELL SIGNAL (XAUUSD)\n"
            f"📌 النمط: {pattern}\n"
            f"📍 الدخول: {price:.2f}\n"
            f"🛑 وقف الخسارة: {sl:.2f}\n"
            f"🎯 TP1 (1:{RR1}): {tp1:.2f}\n"
            f"🎯🎯 TP2 (1:{RR2}): {tp2:.2f}\n"
            "⚠️ المخاطرة 0.5-1% من الحساب. تأكد من الشارت قبل الدخول ✅"
        )

    send_telegram(msg)
    state["last_alert_time"] = candle_time
    state["signals_today"] = state.get("signals_today", 0) + 1
    save_state(state)
    print(f"✅ تم إرسال إشارة {pattern}")

if __name__ == "__main__":
    main()
