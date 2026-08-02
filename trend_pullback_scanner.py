"""
XAUUSD Trend-Pullback Scanner PRO — 15min
مع وقف خسارة وأهداف + نصائح نفسية + تنبيهات أخبار
+ آلية إعادة المحاولة + رسائل افتتاح/إغلاق السوق
"""

import os
import json
import random
import time
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------- Secrets ----------
TWELVE_DATA_KEY  = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
STATE_FILE = "trend_pullback_state.json"

# ---------- إعدادات الاستراتيجية ----------
EMA_FAST   = 20
EMA_TREND  = 50
EMA_MACRO  = 200
RSI_LEN    = 14
RSI_LOW    = 35
RSI_HIGH   = 55
PULLBACK_ATR_MULT = 0.6
ATR_LEN    = 14

ATR_STOP_MULT = 1.5
RR1 = 1.0
RR2 = 2.0

# ---------- أوقات السوق (UTC) ----------
MARKET_OPEN_HOUR = 22   # الأحد الساعة 22:00
MARKET_CLOSE_HOUR = 22  # الجمعة الساعة 22:00

# ---------- نصائح نفسية ----------
PSYCH_TIPS = [
    "تنفس بعمق، لا تدع العواطف تتحكم فيك.",
    "الثقة في الاستراتيجية أهم من ربح صفقة واحدة.",
    "التداول ماراثون وليس سباق سريع.",
    "الخسارة جزء من اللعبة، تقبلها وتعلم منها.",
    "التزم بخطتك، فأنت من وضعها في وقت هدوءك.",
    "لا تطارد السعر، السوق سيعطيك فرصة أخرى.",
    "ركز على العملية وليس المكسب.",
    "الشاشة ليست قدرك، ابتعد قليلاً إذا توترت."
]

# ---------- فترات حظر الأخبار (بتوقيت نيويورك) ----------
US_DATA_WINDOW = ("08:20", "09:10")
FOMC_WINDOW    = ("13:55", "14:50")

# ---------- رموز إشارات ----------
EMOJI_BUY   = "🟢"
EMOJI_SELL  = "🔴"
EMOJI_BLOCK = "⚠️"
EMOJI_NEWS  = "📰"
EMOJI_CHECK = "✅"


def init_state_file():
    """ينشئ ملف الحالة إذا لم يكن موجوداً"""
    if not os.path.exists(STATE_FILE):
        initial_state = {
            "last_alerted_candle": None,
            "news_date": None,
            "warned_us_data": False,
            "warned_fomc": False,
            "weekly_news_sent": False,
            "market_open_week": None,   # يحفظ تاريخ بداية الأسبوع الذي أُرسلت فيه رسالة الافتتاح
            "market_close_week": None,  # يحفظ تاريخ نهاية الأسبوع الذي أُرسلت فيه رسالة الإغلاق
        }
        with open(STATE_FILE, "w") as f:
            json.dump(initial_state, f)
        print("تم إنشاء ملف الحالة الجديد")


def in_news_blackout():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    hm = now_et.strftime("%H:%M")
    if US_DATA_WINDOW[0] <= hm <= US_DATA_WINDOW[1]:
        return True, "بيانات اقتصادية أمريكية (CPI/NFP/PPI/مبيعات التجزئة)"
    if FOMC_WINDOW[0] <= hm <= FOMC_WINDOW[1]:
        return True, "اجتماع أو تصريح الفيدرالي (FOMC)"
    return False, None


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)
    except Exception as e:
        print(f"فشل إرسال رسالة تيليجرام: {e}")


def fetch_candles(interval, outputsize, retries=3, delay=5):
    """جلب الشموع مع إعادة المحاولة عند الفشل"""
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": SYMBOL, "interval": interval, "outputsize": outputsize,
              "apikey": TWELVE_DATA_KEY, "order": "ASC"}
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=30)
            data = r.json()
            if "values" not in data:
                raise RuntimeError(f"Twelve Data error: {data}")
            df = pd.DataFrame(data["values"])
            df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize("UTC")
            for col in ["open", "high", "low", "close"]:
                df[col] = df[col].astype(float)
            return df.sort_values("datetime").reset_index(drop=True)
        except Exception as e:
            print(f"محاولة {attempt} فشلت: {e}")
            if attempt < retries:
                time.sleep(delay)
            else:
                send_telegram("⚠️ فشل جلب بيانات XAUUSD بعد عدة محاولات. ربما فاتتنا إشارة.")
                raise


def drop_unclosed_candle(df, interval_minutes=15):
    """يحذف الشمعة الحالية إن لم تكن قد أغلقت بعد"""
    now_utc = datetime.now(timezone.utc)
    last_open = df.iloc[-1]["datetime"]
    if last_open.tzinfo is None:
        last_open = last_open.tz_localize("UTC")
    expected_close = last_open + pd.Timedelta(minutes=interval_minutes)
    if now_utc < expected_close:
        print(f"آخر شمعة ({last_open}) لسا ما قفلت (تقفل عند {expected_close}). نحذفها.")
        return df.iloc[:-1].reset_index(drop=True)
    return df


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, default=str)


def compute_indicators(df):
    close = df["close"]
    df["ema20"]  = close.ewm(span=EMA_FAST, adjust=False).mean()
    df["ema50"]  = close.ewm(span=EMA_TREND, adjust=False).mean()
    df["ema200"] = close.ewm(span=EMA_MACRO, adjust=False).mean()

    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/RSI_LEN, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/RSI_LEN, adjust=False).mean()
    rs = avg_gain / avg_loss
    df["rsi"] = 100 - (100 / (1 + rs))

    high, low = df["high"], df["low"]
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = true_range.ewm(alpha=1/ATR_LEN, adjust=False).mean()
    return df


def check_signal(df):
    row = df.iloc[-1]
    prev = df.iloc[-2]

    uptrend   = row["close"] > row["ema50"] and row["ema50"] > row["ema200"]
    downtrend = row["close"] < row["ema50"] and row["ema50"] < row["ema200"]

    dist = abs(row["close"] - row["ema20"])
    near_pullback = dist <= (row["atr"] * PULLBACK_ATR_MULT)

    rsi_long_zone  = RSI_LOW <= row["rsi"] <= RSI_HIGH and row["rsi"] > prev["rsi"]
    rsi_short_zone = (100 - RSI_HIGH) <= row["rsi"] <= (100 - RSI_LOW) and row["rsi"] < prev["rsi"]

    bodySize = abs(row["close"] - row["open"])
    lowerWick = min(row["open"], row["close"]) - row["low"]
    upperWick = row["high"] - max(row["open"], row["close"])

    bullishPin    = lowerWick >= bodySize * 1.5 and row["close"] > row["open"]
    bullishEngulf = (row["close"] > row["open"] and row["open"] <= prev["close"]
                     and row["close"] >= prev["open"] and prev["close"] < prev["open"])
    bullishReject = bullishPin or bullishEngulf

    bearishPin    = upperWick >= bodySize * 1.5 and row["close"] < row["open"]
    bearishEngulf = (row["close"] < row["open"] and row["open"] >= prev["close"]
                     and row["close"] <= prev["open"] and prev["close"] > prev["open"])
    bearishReject = bearishPin or bearishEngulf

    longRaw  = uptrend and near_pullback and rsi_long_zone and bullishReject
    shortRaw = downtrend and near_pullback and rsi_short_zone and bearishReject
    return longRaw, shortRaw


def get_important_events_for_week():
    today = datetime.now(ZoneInfo("America/New_York")).date()
    first_day = today.replace(day=1)
    first_friday = first_day + timedelta(days=(4 - first_day.weekday() + 7) % 7)
    events = []
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)
    if monday <= first_friday <= friday:
        events.append(f"تقرير الوظائف غير الزراعية (NFP) – الجمعة {first_friday.strftime('%d/%m')} 08:30 صباحاً بتوقيت نيويورك")
    events.append("بيانات التضخم CPI / مبيعات التجزئة – تابع المفكرة الاقتصادية للتأكيد.")
    events.append("أي خطاب لرئيس الفيدرالي أو اجتماع FOMC – يُعلن في الموقع الرسمي.")
    return events


def check_weekly_news(state):
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() == 0 and now_et.hour == 9 and not state.get("weekly_news_sent"):
        events = get_important_events_for_week()
        msg = f"{EMOJI_NEWS} أهم أخبار الأسبوع التي قد تؤثر على الذهب:\n" + "\n".join(f"• {e}" for e in events)
        msg += "\n\n⚠️ هذه تواريخ تقديرية، يُنصح بمراجعة مفكرة اقتصادية موثوقة."
        send_telegram(msg)
        state["weekly_news_sent"] = True
        save_state(state)


def check_news_warnings(state):
    now_et = datetime.now(ZoneInfo("America/New_York"))
    hour_min = now_et.strftime("%H:%M")
    if hour_min == "07:20" and not state.get("warned_us_data"):
        send_telegram(f"{EMOJI_NEWS} سيبدأ حظر الأخبار (بيانات أمريكية) خلال 60 دقيقة. تجنب الدخول في صفقات جديدة.")
        state["warned_us_data"] = True
        save_state(state)
    if hour_min == "12:55" and not state.get("warned_fomc"):
        send_telegram(f"{EMOJI_NEWS} سيبدأ حظر الأخبار (اجتماع الفيدرالي) خلال 60 دقيقة. تجنب الدخول في صفقات جديدة.")
        state["warned_fomc"] = True
        save_state(state)


def is_market_open(now_utc):
    """يعيد True إذا كان السوق مفتوحاً الآن (بين الأحد 22:00 والجمعة 22:00)"""
    # حساب بداية الأسبوع (الاثنين 00:00) لكن السوق يفتح الأحد 22:00
    weekday = now_utc.weekday()
    hour = now_utc.hour
    # الأحد (6) بعد 22:00 مفتوح
    if weekday == 6 and hour >= MARKET_OPEN_HOUR:
        return True
    # الاثنين (0) إلى الخميس (3) مفتوح طوال اليوم
    if 0 <= weekday <= 3:
        return True
    # الجمعة (4) قبل 22:00 مفتوح
    if weekday == 4 and hour < MARKET_CLOSE_HOUR:
        return True
    return False


def get_current_week_monday(now_utc):
    """يرجع تاريخ الاثنين من الأسبوع الحالي (يستخدم لتوحيد معرفات الأسابيع)"""
    return (now_utc - timedelta(days=now_utc.weekday())).date()


def check_market_open_close_messages(state, now_utc):
    """ترسل رسالة افتتاح أو إغلاق السوق إذا لزم الأمر"""
    if is_market_open(now_utc):
        # السوق مفتوح – تحقق من رسالة الافتتاح
        week_monday = get_current_week_monday(now_utc)
        if state.get("market_open_week") != str(week_monday):
            send_telegram(f"🔔 سوق الفوركس افتتح الآن! (الأحد 22:00 UTC)\n💡 استعد لبدء أسبوع تداول جديد. الإشارات ستصلك تباعاً.")
            state["market_open_week"] = str(week_monday)
            save_state(state)
    else:
        # السوق مغلق – تحقق من رسالة الإغلاق (إذا كان الوقت بعد إغلاق الجمعة)
        # الرسالة ترسل مرة واحدة بعد الجمعة 22:00 وحتى نهاية الأحد 21:59
        if now_utc.weekday() == 4 and now_utc.hour >= MARKET_CLOSE_HOUR or now_utc.weekday() == 5 or (now_utc.weekday() == 6 and now_utc.hour < MARKET_OPEN_HOUR):
            week_monday = get_current_week_monday(now_utc)  # الاثنين من نفس الأسبوع
            if state.get("market_close_week") != str(week_monday):
                send_telegram(f"🔕 سوق الفوركس أُغلق للتو (الجمعة 22:00 UTC).\n🛑 تم إيقاف فحص الإشارات حتى الأحد 22:00 UTC.")
                state["market_close_week"] = str(week_monday)
                save_state(state)


def main():
    init_state_file()

    now_utc = datetime.now(timezone.utc)

    # رسائل افتتاح / إغلاق السوق (تعمل حتى لو السوق مقفل)
    state = load_state()
    check_market_open_close_messages(state, now_utc)

    # إذا السوق مقفل نتوقف هنا (ما عدا أول مرة بعد الإغلاق أرسلت الرسالة أعلاه)
    if not is_market_open(now_utc):
        print("السوق مقفل حالياً – لا يتم فحص الإشارات.")
        return

    # السوق مفتوح – نكمل الفحص الطبيعي
    df = fetch_candles("15min", 500)
    df = drop_unclosed_candle(df, interval_minutes=15)
    if len(df) < 201:
        print("بيانات غير كافية")
        return

    df = compute_indicators(df)
    last_candle_time = df.iloc[-1]["datetime"]
    candle_time_str = last_candle_time.strftime("%Y-%m-%d %H:%M")

    # إعادة تعيين تحذيرات اليوم عند منتصف الليل بتوقيت نيويورك
    today_et_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    if state.get("news_date") != today_et_str:
        state["news_date"] = today_et_str
        state["warned_us_data"] = False
        state["warned_fomc"] = False
        state["weekly_news_sent"] = False

    check_weekly_news(state)
    check_news_warnings(state)

    last_alerted = state.get("last_alerted_candle")
    if last_alerted == candle_time_str:
        print("الإشارة سبق إرسالها لهذه الشمعة")
        return

    longRaw, shortRaw = check_signal(df)
    blackout, blackout_reason = in_news_blackout()
    row = df.iloc[-1]
    price = row["close"]
    atr_val = row["atr"]

    if longRaw or shortRaw:
        direction = "long" if longRaw else "short"
        if direction == "long":
            sl = price - atr_val * ATR_STOP_MULT
            tp1 = price + (atr_val * ATR_STOP_MULT) * RR1
            tp2 = price + (atr_val * ATR_STOP_MULT) * RR2
        else:
            sl = price + atr_val * ATR_STOP_MULT
            tp1 = price - (atr_val * ATR_STOP_MULT) * RR1
            tp2 = price - (atr_val * ATR_STOP_MULT) * RR2

        tip = random.choice(PSYCH_TIPS)

        if not blackout:
            emoji = EMOJI_BUY if direction == "long" else EMOJI_SELL
            action = "شراء" if direction == "long" else "بيع"
            msg = (f"🎯 Principal Strategy\n"
                   f"[TREND-PULLBACK] 📈\n"
                   f"{emoji} {action} XAUUSD على فريم 15 دقيقة\n"
                   f"السعر الآن: {price:.2f}\n"
                   f"وقف الخسارة: {sl:.2f} 🛑\n"
                   f"الهدف 1 (1:{RR1:.1f}): {tp1:.2f} 🎯\n"
                   f"الهدف 2 (1:{RR2:.1f}): {tp2:.2f} 🚀\n"
                   f"الوقت: {candle_time_str} UTC\n\n"
                   f"🧘 نصيحة: \"{tip}\"")
            send_telegram(msg)
        else:
            action = "شراء" if direction == "long" else "بيع"
            msg = (f"🎯 Principal Strategy\n"
                   f"{EMOJI_BLOCK} إشارة {action} مُنعت بسبب حظر الأخبار\n"
                   f"📰 الخبر: {blackout_reason}\n"
                   f"السعر: {price:.2f}\n"
                   f"الوقت: {candle_time_str} UTC\n\n"
                   f"🧘 نصيحة: \"{tip}\"")
            send_telegram(msg)

        state["last_alerted_candle"] = candle_time_str
        save_state(state)
    else:
        print("لا توجد إشارة جديدة")

    save_state(state)


if __name__ == "__main__":
    main()
