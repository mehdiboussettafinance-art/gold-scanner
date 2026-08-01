"""
News Monitor — تقويم اقتصادي حقيقي (Financial Modeling Prep)
يشتغل مستقل تمامًا عن بوت الأسعار (ما يلمس Twelve Data إطلاقًا).
- ملخص كل 4 ساعات لأخبار اليوم
- تذكير قبل الخبر بساعة، وتذكير ثاني قبله بـ30 دقيقة
- رسالة نتيجة بعد صدور الخبر (فعلي مقابل متوقع)
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
FMP_API_KEY = os.environ["FMP_API_KEY"]

STATE_FILE = "news_state.json"

NEWS_WARNING_60_MIN = 60
NEWS_WARNING_30_MIN = 30
RESULT_CHECK_DELAY_MIN = 10
SUMMARY_HOURS_UTC = [0, 4, 8, 12, 16, 20]

EVENT_DIRECTION_HINTS = {
    "cpi": "أعلى من المتوقع = دولار أقوى غالبًا (سلبي للذهب) | أقل من المتوقع = العكس",
    "nfp": "أعلى من المتوقع = دولار أقوى غالبًا (سلبي للذهب) | أقل من المتوقع = العكس",
    "ppi": "أعلى من المتوقع = دولار أقوى غالبًا (سلبي للذهب) | أقل من المتوقع = العكس",
    "retail sales": "أعلى من المتوقع = دولار أقوى غالبًا (سلبي للذهب) | أقل من المتوقع = العكس",
    "gdp": "أعلى من المتوقع = دولار أقوى غالبًا (سلبي للذهب) | أقل من المتوقع = العكس",
    "unemployment": "أعلى من المتوقع = دولار أضعف غالبًا (إيجابي للذهب) | أقل من المتوقع = العكس",
    "jobless claims": "أعلى من المتوقع = دولار أضعف غالبًا (إيجابي للذهب) | أقل من المتوقع = العكس",
    "fed": "قرار متشدد (رفع فائدة/تصريح متشدد) = دولار أقوى (سلبي للذهب) والعكس صحيح",
}

IMPACT_SCORE_MAP = {"High": 9, "Medium": 5, "Low": 2}


def impact_to_score(impact_label):
    return IMPACT_SCORE_MAP.get(impact_label, 3)


def get_direction_hint(event_name):
    name_lower = event_name.lower()
    for key, hint in EVENT_DIRECTION_HINTS.items():
        if key in name_lower:
            return hint
    return "الاتجاه المعتاد يعتمد على نوع الخبر — راجع السياق العام."


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    full_message = "📢 Forex News\n" + message
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": full_message}, timeout=15)
    except Exception as e:
        print(f"Telegram error: {e}")


def is_weekend():
    return datetime.now(ZoneInfo("America/New_York")).weekday() >= 5


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, default=str)


def fetch_today_high_impact_events():
    today_et = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    url = "https://financialmodelingprep.com/api/v3/economic_calendar"
    params = {"from": today_et, "to": today_et, "apikey": FMP_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=30)
        data = r.json()
    except Exception as e:
        print(f"خطأ بجلب التقويم الاقتصادي: {e}")
        return []

    if not isinstance(data, list):
        print(f"رد غير متوقع من FMP: {data}")
        return []

    events = []
    for item in data:
        try:
            country = item.get("country", "")
            impact = item.get("impact", "")
            if country != "US" or impact not in ("High", "Medium", "Low"):
                continue
            event_time = datetime.strptime(item["date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            events.append({
                "event": item.get("event", "حدث اقتصادي"),
                "time_utc": event_time,
                "impact": impact,
                "score": impact_to_score(impact),
                "actual": item.get("actual"),
                "estimate": item.get("estimate"),
                "previous": item.get("previous"),
            })
        except Exception as e:
            print(f"تجاهل حدث غير مفهوم الصيغة: {e}")
            continue
    return events


def get_or_refresh_events(state):
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    if state.get("events_date") != today_str:
        events = fetch_today_high_impact_events()
        state["events_date"] = today_str
        state["todays_events"] = [
            {"event": e["event"], "time_utc": e["time_utc"].isoformat(), "impact": e["impact"],
             "score": e["score"], "actual": e["actual"], "estimate": e["estimate"], "previous": e["previous"]}
            for e in events
        ]
        state["warned_60_keys"] = []
        state["warned_30_keys"] = []
        state["result_sent_keys"] = []
        state["summary_sent_hours"] = []
        print(f"تحديث تقويم اليوم: {len(events)} حدث عالي التأثير")
    return state


def check_and_warn_upcoming_news(state):
    now_utc = datetime.now(timezone.utc)
    warned60 = set(state.get("warned_60_keys", []))
    warned30 = set(state.get("warned_30_keys", []))

    for e in state.get("todays_events", []):
        if e.get("score", 0) < 7:
            continue  # الأخبار المتوسطة/البسيطة تظهر بالملخص بس، بدون تذكير فردي
        event_time = datetime.fromisoformat(e["time_utc"])
        key = f"{e['event']}_{e['time_utc']}"
        minutes_until = (event_time - now_utc).total_seconds() / 60
        local_time = event_time.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M")

        if 0 <= minutes_until <= NEWS_WARNING_60_MIN and key not in warned60:
            send_telegram(
                f"📰 تذكير: خبر مهم خلال ساعة تقريبًا\n"
                f"الحدث: {e['event']}\n"
                f"الوقت: {local_time} بتوقيت نيويورك ({event_time.strftime('%H:%M')} UTC)\n"
                f"⚠️ تأثير عالي على الدولار/الذهب."
            )
            warned60.add(key)

        if 0 <= minutes_until <= NEWS_WARNING_30_MIN and key not in warned30:
            send_telegram(
                f"⏰ تذكير أخير: باقي 30 دقيقة على خبر مهم!\n"
                f"الحدث: {e['event']}\n"
                f"الوقت: {local_time} بتوقيت نيويورك\n"
                f"🔇 تجنب فتح صفقات جديدة الآن."
            )
            warned30.add(key)

    state["warned_60_keys"] = list(warned60)
    state["warned_30_keys"] = list(warned30)


def check_and_send_results(state):
    now_utc = datetime.now(timezone.utc)
    result_sent = set(state.get("result_sent_keys", []))
    pending = []

    for e in state.get("todays_events", []):
        event_time = datetime.fromisoformat(e["time_utc"])
        key = f"{e['event']}_{e['time_utc']}"
        minutes_since = (now_utc - event_time).total_seconds() / 60
        # نفحص بس خلال ساعتين بعد الخبر، لو تأخر أكثر من كذا نعتبره فات ونوقف المحاولة
        if RESULT_CHECK_DELAY_MIN <= minutes_since <= 120 and key not in result_sent:
            pending.append((key, e, event_time))

    if not pending:
        return

    fresh_events = fetch_today_high_impact_events()  # طلب FMP واحد بس، يغطي كل الأحداث المعلقة سوا

    for key, e, event_time in pending:
        match = next((f for f in fresh_events
                      if f["event"] == e["event"] and f["time_utc"] == event_time), None)
        actual = match["actual"] if match else None
        estimate = match["estimate"] if match else e.get("estimate")
        previous = match["previous"] if match else e.get("previous")

        if actual is None:
            continue

        hint = get_direction_hint(e["event"])
        send_telegram(
            f"✅ نتيجة الخبر: {e['event']}\n"
            f"⚡ قوة التأثير: {e.get('score', '?')}/10 ({e['impact']})\n"
            f"📊 الفعلي: {actual}\n"
            f"📈 المتوقع: {estimate}\n"
            f"📉 السابق: {previous}\n"
            f"💡 {hint}\n"
            f"⚠️ هذا تفسير عام، السوق أحيانًا يتحرك عكس التوقع المعتاد."
        )
        result_sent.add(key)

    state["result_sent_keys"] = list(result_sent)


def send_4h_news_summary(state):
    now_utc = datetime.now(timezone.utc)
    if now_utc.hour not in SUMMARY_HOURS_UTC:
        return
    hour_key = f"{state.get('events_date')}-{now_utc.hour}"
    sent_hours = state.get("summary_sent_hours", [])
    if hour_key in sent_hours:
        return

    events = state.get("todays_events", [])
    if not events:
        send_telegram("📅 ملخص اليوم: ما فيه أخبار أمريكية عالية التأثير مسجلة اليوم.")
    else:
        lines = ["📅 ملخص أخبار اليوم المؤثرة على الدولار/الذهب:"]
        for e in sorted(events, key=lambda x: x["time_utc"]):
            t = datetime.fromisoformat(e["time_utc"]).astimezone(ZoneInfo("America/New_York")).strftime("%H:%M")
            lines.append(f"• {e['event']} — {t} (نيويورك) — قوة التأثير: {e['score']}/10 ({e['impact']})")
        send_telegram("\n".join(lines))

    sent_hours.append(hour_key)
    state["summary_sent_hours"] = sent_hours


def main():
    if is_weekend():
        print("Weekend — market closed, skipping news monitor.")
        return

    state = load_state()
    state = get_or_refresh_events(state)
    check_and_warn_upcoming_news(state)
    check_and_send_results(state)
    send_4h_news_summary(state)
    save_state(state)
    print("News monitor run complete.")


if __name__ == "__main__":
    main()
