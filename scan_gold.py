"""
XAUUSD Trading Bot - الإصدار النهائي المُحسَّن
استراتيجية مركّزة:
- شراء من دعم (Support Buy)
- بيع تصحيحي بعد كسر قمة وإعادة اختبار (Corrective Sell)

ميزات جديدة:
- رسائل عربية متنوعة جداً مع تفاصيل كاملة عن وضع السوق
- تحذيرات قبل كل النوافذ الإخبارية المهمة للذهب
- تحديث كل ساعة يشرح ماذا يفعل البوت ولماذا
"""

import os
import json
import random
import requests
import pandas as pd
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ---------- المتغيرات البيئية ----------
TWELVE_DATA_KEY = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
STATE_FILE = "state.json"

# ---------- الإطارات ----------
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

# شروط الدخول
PULLBACK_ATR_MULT = 1.0
REJECTION_WICK_RATIO = 1.3
RSI_BUY_LOW, RSI_BUY_HIGH = 35, 55
RSI_SELL_LOW, RSI_SELL_HIGH = 45, 65
ATR_STOP_MULT = 1.5
RR1, RR2 = 1.0, 2.0
MAX_SIGNALS_PER_DAY = 2

# نوافذ الأخبار (بتوقيت نيويورك)
US_DATA_WINDOW = ("08:20", "09:10")   # بيانات أمريكية يومية
FOMC_WINDOW = ("13:55", "14:50")      # اجتماع الفيدرالي
# نوافذ إضافية مهمة للذهب
NFP_ALERT_DAYS = [1, 2, 3, 4, 5]      # الجمعة الأولى من الشهر (سيتم التعامل معها لاحقاً)
CPI_ALERT_DAYS = [10, 11, 12, 13, 14] # تواريخ تقريبية للـ CPI (سيتم تنبيه عام)

# ---------- مكتبة الرسائل (مُوسَّعة جداً) ----------
OPENERS = [
    "👋 أهلًا، أنا هنا وأراقب السوق لحظة بلحظة.",
    "🕓 مرت ساعة كاملة، اسمع آخر المستجدات.",
    "📌 تقرير سريع ومختصر قبل ما تسألني.",
    "🧠 خلاصة تحليلي للساعة الأخيرة:",
    "🔔 أنا معك، ما نسيتك، وهذا اللي شفته.",
    "📋 التقرير الدوري للساعة جاهز:",
    "💭 بعد المراقبة الدقيقة، هذا اللي عندي:",
    "📡 إشارة من غرفة العمليات …",
    "⏰ حان وقت التحديث، تفضل:",
    "🗞️ أهم ما في السوق خلال الساعة الماضية:",
]

TREND_TALK = {
    "uptrend": [
        "🔵 الاتجاه العام على H1 صاعد بقوة.",
        "📈 الذهب في موجة صاعدة واضحة على الساعة.",
        "🟢 السوق متمسك بالصعود حسب تحليلي.",
        "🚀 الترند على H1 يعطي إشارات إيجابية."
    ],
    "downtrend": [
        "🔴 الاتجاه العام على H1 هابط بوضوح.",
        "📉 الذهب يسير في موجة هابطة على الساعة.",
        "🔻 السوق في حالة هبوط حسب تحليلي.",
        "🧯 الترند على H1 سلبي."
    ],
    "ranging": [
        "🌊 السوق حاليًا في حالة تذبذب على H1.",
        "😐 الحركة عرضية على الساعة، بدون اتجاه واضح.",
        "⏸️ البوصلة واقفة، الذهب في نطاق عرضي.",
        "🤷‍♂️ لا صعود ولا هبوط، السوق يتنفس."
    ],
}

MACRO_TALK = {
    "uptrend": "🟩 الفلتر الكبير (4H فوق EMA200): يسمح بشراء فقط.",
    "downtrend": "🟥 الفلتر الكبير (4H تحت EMA200): يسمح ببيع فقط.",
    "neutral": "⬜ الفلتر الكبير محايد: لا قيود."
}

RSI_COMMENTS = [
    "📊 مؤشر RSI على إطار الدخول: {rsi:.1f} — {zone}.",
    "⚙️ قراءة RSI: {rsi:.1f} ({zone}).",
    "🎚️ الـ RSI عند {rsi:.1f}: {zone}.",
]

SUPPORT_RESISTANCE = [
    "📍 أقرب قاع رئيسي: {low:.2f} | أقرب قمة: {high:.2f}",
    "🧱 الدعم: {low:.2f} | المقاومة: {high:.2f}",
    "📌 المستويات المهمة: دعم {low:.2f} / مقاومة {high:.2f}",
]

CLOSERS_IDLE = [
    "😴 لا توجد إشارة جاهزة الآن. قاعد أراقب.",
    "🤷 ما توفرت كل الشروط، لسا في الانتظار.",
    "⏳ صبرك جميل، أول ما تظهر فرصة راح أخبرك.",
    "🔍 تحت المجهر، ما فاتني شيء.",
    "🕵️ أتابع بصمت، لو شفت شي ببعتلك فوراً.",
    "📭 لغاية الآن مفيش إعداد مكتمل.",
    "💤 الهدوء قبل العاصفة، ما عندي إشارة دلوقتي.",
    "🔕 السوق مش واضح، خليني أستنى شوية.",
]

BLACKOUT_ON = [
    "🔇 في حظر أخبار حاليًا، مفيش إشارات هتتبعت.",
    "🚫 وقت أخبار مهمة، البوت ساكت ومش هيدخل.",
]
BLACKOUT_OFF = [
    "🔊 مفيش حظر أخبار، الطريق مفتوح لأي إشارة.",
    "✅ السوق مفتوح بدون حظر، ممكن ندخل."
]

NEWS_WARNINGS = {
    "us_data": "⚠️ بعد ساعة بالضبط هتبدأ بيانات أمريكية قوية (8:20-9:10 ET) – ممنوع الدخول.",
    "fomc": "⚠️ بعد ساعة اجتماع الفيدرالي (13:55-14:50 ET) – السوق هيكون متلخبط، بلاش صفقات.",
    "nfp_reminder": "📅 اليوم جمعة أولى من الشهر – غالبًا فيه بيانات التوظيف (NFP) الساعة 8:30 ET. خليك حذر!",
    "cpi_reminder": "📅 الأسبوع ده فيه بيانات تضخم (CPI) مهمة، تابع التقويم الاقتصادي.",
    "daily_check": "📋 قبل ما تبدأ الجلسة الأمريكية، راجع التقويم الاقتصادي عشان أي أخبار مفاجئة."
}

def rsi_zone_note(rsi_val, direction_hint):
    if direction_hint == "uptrend":
        if RSI_BUY_LOW <= rsi_val <= RSI_BUY_HIGH:
            return "في منطقة الشراء المقبولة 👍"
        elif rsi_val > RSI_BUY_HIGH:
            return "في تشبع شرائي ⚠️"
        else:
            return "ضعيف وتحت منطقة الشراء 🥶"
    elif direction_hint == "downtrend":
        if RSI_SELL_LOW <= rsi_val <= RSI_SELL_HIGH:
            return "في منطقة البيع المقبولة 👍"
        elif rsi_val < RSI_SELL_LOW:
            return "في تشبع بيعي ⚠️"
        else:
            return "قوي وفوق منطقة البيع 🥵"
    return "السوق عرضي، RSI مش واضح."

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

    # شراء من دعم
    at_support = last["low"] <= swing_low * 1.005
    micro_trend_up = last["ema_fast"] > last["ema_mid"]
    support_buy = (
        bias == "uptrend" and
        macro_filter != "downtrend" and
        at_support and
        near_ema and
        micro_trend_up and
        rsi_buy_ok and
        bullish_reject
    )

    # بيع تصحيحي بعد كسر قمة
    broke_high = prev["close"] > swing_high or last["close"] > swing_high
    retesting = abs(last["close"] - swing_high) <= (last["atr"] * 0.7) or near_ema
    corrective_sell = (
        bias == "uptrend" and
        macro_filter != "downtrend" and
        broke_high and
        retesting and
        rsi_sell_ok and
        bearish_reject
    )

    buy_signal = support_buy
    sell_signal = corrective_sell

    if support_buy:
        pattern = "🟢 شراء من الدعم"
    elif corrective_sell:
        pattern = "🔴 بيع تصحيحي بعد كسر القمة"
    else:
        pattern = None

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
        print(f"Telegram send error: {e}")

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
            "daily_reminder_sent": False,
        }

    for key in ["signals_today", "warned_us_data", "warned_fomc", "warned_nfp", "warned_cpi", "daily_reminder_sent"]:
        if key not in state:
            state[key] = 0 if key == "signals_today" else False

    hour_key = f"{today_str}-{hour}"
    already_sent_status = (state.get("last_status_hour") == hour_key)

    # ====== تحذيرات الأخبار ======
    if now_et.weekday() < 5:
        hm = now_et.strftime("%H:%M")
        # تحذير بيانات يومية
        if hm == "07:20" and not state["warned_us_data"]:
            send_telegram(NEWS_WARNINGS["us_data"])
            state["warned_us_data"] = True
            save_state(state)
        # تحذير FOMC
        if hm == "12:55" and not state["warned_fomc"]:
            send_telegram(NEWS_WARNINGS["fomc"])
            state["warned_fomc"] = True
            save_state(state)
        # الجمعة الأولى = NFP تقريبًا
        if now_et.weekday() == 4 and now_et.day <= 7 and not state["warned_nfp"]:
            send_telegram(NEWS_WARNINGS["nfp_reminder"])
            state["warned_nfp"] = True
            save_state(state)
        # تذكير ببيانات التضخم في منتصف الشهر
        if 10 <= now_et.day <= 14 and not state["warned_cpi"]:
            send_telegram(NEWS_WARNINGS["cpi_reminder"])
            state["warned_cpi"] = True
            save_state(state)
        # تذكير يومي قبل الجلسة الأمريكية
        if hm == "07:00" and not state["daily_reminder_sent"]:
            send_telegram(NEWS_WARNINGS["daily_check"])
            state["daily_reminder_sent"] = True
            save_state(state)

    # ====== جلب البيانات ======
    try:
        df4h = fetch_candles(MACRO_INTERVAL, MACRO_OUTPUTSIZE)
        df1h = fetch_candles(TREND_INTERVAL, TREND_OUTPUTSIZE)
        df_m15 = fetch_candles(ENTRY_INTERVAL, ENTRY_OUTPUTSIZE)
        df_m30 = fetch_candles("30min", ENTRY_OUTPUTSIZE)
    except Exception as e:
        print(f"خطأ جلب البيانات: {e}")
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

    # ====== بناء رسالة الحالة ======
    parts = [random.choice(OPENERS)]
    parts.append(f"💰 السعر الآن: {last['close']:.2f}")
    parts.append(random.choice(TREND_TALK[bias_h1]))
    parts.append(MACRO_TALK[macro_filter])

    # تفاصيل المؤشرات
    rsi_zone = rsi_zone_note(last["rsi"], bias_h1)
    rsi_msg = random.choice(RSI_COMMENTS).format(rsi=last["rsi"], zone=rsi_zone)
    parts.append(rsi_msg)

    if swings[0] is not None:
        parts.append(random.choice(SUPPORT_RESISTANCE).format(low=swings[1], high=swings[0]))

    # ماذا يفعل البوت الآن
    if pattern:
        parts.append(f"🔔 تم التعرف على نمط: {pattern} (على {tf_used})")
    else:
        if bias_h1 == "uptrend":
            parts.append("🔎 أنتظر سعر يلامس الدعم مع شمعة رفض للشراء، أو كسر قمة وإعادة اختبار للبيع.")
        elif bias_h1 == "downtrend":
            parts.append("🔎 الاتجاه هابط، لكن الاستراتيجية الحالية تركز على الصاعد، لذلك لا إشارات متوقعة.")
        else:
            parts.append("🔎 السوق عرضي، ما عندي أنماط واضحة حالياً.")

    parts.append(random.choice(BLACKOUT_ON if blackout else BLACKOUT_OFF))

    already_alerted = (state.get("last_alert_time") == candle_time)
    signal_allowed = (buy_signal or sell_signal) and not already_alerted and not blackout
    if signal_allowed and state["signals_today"] >= MAX_SIGNALS_PER_DAY:
        signal_allowed = False

    if not signal_allowed:
        parts.append(random.choice(CLOSERS_IDLE))

    # إرسال التحديث كل ساعة إذا لم يرسل بعد
    if not already_sent_status:
        send_telegram("\n".join(parts))
        state["last_status_hour"] = hour_key
        save_state(state)

    if not signal_allowed:
        if buy_signal or sell_signal:
            reason = "الحد اليومي" if state["signals_today"] >= MAX_SIGNALS_PER_DAY else "مكرر/حظر"
            print(f"الإشارة ملغاة: {reason}")
        else:
            print("لا توجد إشارة هذا الساعة.")
        return

    # ====== إرسال الإشارة ======
    atr = last["atr"]
    stop_dist = atr * ATR_STOP_MULT
    price = last["close"]

    if buy_signal:
        if macro_filter == "downtrend":
            print("شراء ملغى بالماكرو.")
            return
        sl = price - stop_dist
        tp1 = price + stop_dist * RR1
        tp2 = price + stop_dist * RR2
        msg = (
            f"🟢🚀 **GOLD BUY SIGNAL**\n"
            f"📌 النمط: {pattern}\n"
            f"⏱️ الإطار: {tf_used}\n"
            f"📍 الدخول: {price:.2f}\n"
            f"🛑 وقف الخسارة: {sl:.2f}\n"
            f"🎯 TP1 (1:{RR1}): {tp1:.2f}\n"
            f"🎯🎯 TP2 (1:{RR2}): {tp2:.2f}\n"
            "⚠️ المخاطرة 0.5-1% من الحساب. تأكد من الشارت قبل الدخول."
        )
    else:  # sell
        if macro_filter == "uptrend":
            print("بيع ملغى بالماكرو.")
            return
        sl = price + stop_dist
        tp1 = price - stop_dist * RR1
        tp2 = price - stop_dist * RR2
        msg = (
            f"🔴🛑 **GOLD SELL SIGNAL**\n"
            f"📌 النمط: {pattern}\n"
            f"⏱️ الإطار: {tf_used}\n"
            f"📍 الدخول: {price:.2f}\n"
            f"🛑 وقف الخسارة: {sl:.2f}\n"
            f"🎯 TP1 (1:{RR1}): {tp1:.2f}\n"
            f"🎯🎯 TP2 (1:{RR2}): {tp2:.2f}\n"
            "⚠️ المخاطرة 0.5-1% من الحساب. تأكد من الشارت قبل الدخول."
        )

    send_telegram(msg)
    state["last_alert_time"] = candle_time
    state["signals_today"] += 1
    save_state(state)
    print(f"✅ إشارة مرسلة: {pattern}")

if __name__ == "__main__":
    main()
