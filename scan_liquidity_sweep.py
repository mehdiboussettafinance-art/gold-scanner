"""
STRATEGY 2 — Asian Range Liquidity Sweep + MSS + FVG (XAUUSD)
Talks to you every hour, including "off duty" hours outside the trading
sessions, so there's never a silent hour.

LIMITATIONS: "GMT" treated as UTC; spread not verified (OHLC data only);
swing/MSS detection uses closed 1m candles only.
"""

import os
import json
import random
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TWELVE_DATA_KEY  = os.environ["TWELVE_DATA_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "XAU/USD"
STATE_FILE = "sweep_state.json"

ASIAN_START_H, ASIAN_END_H = 0, 6
LONDON_START_H, LONDON_END_H = 7, 10
LATE_CUTOFF_H = 11

MIN_SWEEP_DIST, SL_BUFFER = 0.30, 0.50
RISK_REWARD, BE_TRIGGER_RR = 3.0, 1.5
FVG_MAX_MINUTES, ENTRY_EXPIRY_MINUTES = 15, 10

US_DATA_WINDOW = ("08:20", "09:10")
FOMC_WINDOW    = ("13:55", "14:50")

STATUS_MINUTE_INTERVAL = 30

OFFDUTY_LINES = [
    "😴 برا أوقات شغلي الحين (آسيا 00:00-06:00 أو لندن 07:00-11:00 UTC)، خذ راحتك.",
    "🌙 مافيه شي أراقبه هالوقت، بس تابعني، رح أرجع نشيط بالوقت المحدد.",
    "☕ استراحة! سوق الذهب برا نافذتي حاليًا، بشتغل بجد قريب.",
    "🛌 هذا وقت راحتي بالجدول، بس ما نسيتك، بحدثك أول ما أرجع أشتغل.",
]

FUN_EMOJIS = ["🕵️", "🔎", "🧭", "🛰️", "📡", "🌊"]
GREEN_EMOJIS = ["🟢", "✅", "🚀"]


def hours_until_next_asian(hour):
    return 24 - hour if hour >= ASIAN_START_H else ASIAN_START_H - hour


def fetch_candles(interval, outputsize):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": SYMBOL, "interval": interval, "outputsize": outputsize,
              "apikey": TWELVE_DATA_KEY, "order": "ASC"}
    r = requests.get(url, params=params, timeout=30)
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data}")
    df = pd.DataFrame(data["values"])
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize("UTC")
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df.sort_values("datetime").reset_index(drop=True)


def in_news_blackout():
    now_et = datetime.now(ZoneInfo("America/New_York"))
    hm = now_et.strftime("%H:%M")
    return US_DATA_WINDOW[0] <= hm <= US_DATA_WINDOW[1] or FOMC_WINDOW[0] <= hm <= FOMC_WINDOW[1]


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    full_msg = "[STRATEGY 2 - Liquidity Sweep] " + random.choice(FUN_EMOJIS) + "\n" + message
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": full_msg}, timeout=15)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, default=str)


def fresh_daily_state(today_str):
    return {
        "date": today_str, "asian_high": None, "asian_low": None, "sweep": None,
        "pending_signal": None, "trade_taken_today": False,
        "asian_start_sent": False, "london_start_sent": False, "closing_soon_sent": False,
        "last_status_minute_block": None, "last_offduty_hour": None,
    }


def find_last_swing(df1m, direction):
    n = len(df1m)
    for i in range(n - 3, 1, -1):
        if direction == "high":
            if (df1m["high"][i] > df1m["high"][i - 1] and df1m["high"][i] > df1m["high"][i - 2]
                    and df1m["high"][i] > df1m["high"][i + 1] and df1m["high"][i] > df1m["high"][i + 2]):
                return df1m["high"][i]
        else:
            if (df1m["low"][i] < df1m["low"][i - 1] and df1m["low"][i] < df1m["low"][i - 2]
                    and df1m["low"][i] < df1m["low"][i + 1] and df1m["low"][i] < df1m["low"][i + 2]):
                return df1m["low"][i]
    return None


def check_mss_and_fvg(df1m, sweep_type):
    if len(df1m) < 6:
        return False, None
    last = df1m.iloc[-1]
    if sweep_type == "sell":
        sw = find_last_swing(df1m.iloc[:-1].reset_index(drop=True), "low")
        if sw is None or last["close"] >= sw:
            return False, None
        c1, c3 = df1m.iloc[-3], df1m.iloc[-1]
        return (True, c1["low"]) if c1["low"] > c3["high"] else (False, None)
    else:
        sw = find_last_swing(df1m.iloc[:-1].reset_index(drop=True), "high")
        if sw is None or last["close"] <= sw:
            return False, None
        c1, c3 = df1m.iloc[-3], df1m.iloc[-1]
        return (True, c1["high"]) if c1["high"] < c3["low"] else (False, None)


def main():
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    weekday = now_utc.weekday()
    hour, minute = now_utc.hour, now_utc.minute

    state = load_state()
    if state.get("date") != today_str:
        state = fresh_daily_state(today_str)
        save_state(state)

    if weekday >= 5:
        print("Weekend — market closed.")
        return

    # --- Off-duty hours: hourly personality check-in, no market work ---
    if hour >= LATE_CUTOFF_H:
        if state.get("last_offduty_hour") != hour:
            hrs_left = hours_until_next_asian(hour)
            recap = "✅ أخذنا صفقة اليوم" if state.get("trade_taken_today") else "🤷 ما صارت صفقة اليوم"
            send_telegram(f"{random.choice(OFFDUTY_LINES)}\n{recap}. أرجع أشتغل خلال {hrs_left} ساعة تقريبًا ⏰")
            state["last_offduty_hour"] = hour
            save_state(state)
        return

    if hour == ASIAN_START_H and not state.get("asian_start_sent"):
        send_telegram("🌏 بدأت جلسة آسيا! برسم نطاق السعر لين الساعة 6 صباحًا UTC 📐")
        state["asian_start_sent"] = True
        save_state(state)

    if hour == LONDON_START_H and minute < 15 and not state.get("london_start_sent"):
        ah, al = state.get("asian_high"), state.get("asian_low")
        range_txt = f"النطاق: {al:.2f} - {ah:.2f} 📏" if ah and al else "النطاق لسا ما تحدد ⚠️"
        send_telegram(f"🇬🇧🔥 بدأت نافذة لندن! ندور على اصطياد سيولة\n{range_txt}\nبنراقب كل دقيقة 🕵️")
        state["london_start_sent"] = True
        save_state(state)

    if hour == LATE_CUTOFF_H - 1 and minute >= 30 and not state.get("closing_soon_sent"):
        pend = "فيه صفقة معلقة نراقبها 👀" if state.get("pending_signal") else "لين الحين ما فيه شي، الاحتمال يضعف 🤏"
        send_telegram(f"⏰ باقي حوالي 30 دقيقة على إغلاق نافذة الدخول اليوم! {pend}")
        state["closing_soon_sent"] = True
        save_state(state)

    if LONDON_START_H <= hour < LATE_CUTOFF_H:
        minute_block = (hour * 60 + minute) // STATUS_MINUTE_INTERVAL
        if state.get("last_status_minute_block") != minute_block:
            if state.get("pending_signal") and state["pending_signal"]["status"] == "pending":
                status_txt = f"🎯 فيه أمر معلق عند {state['pending_signal']['entry']:.2f}، نراقب لو يتلمس"
            elif state.get("sweep"):
                status_txt = f"🌊 صار اصطياد سيولة ({state['sweep']['type']})، نستنى تأكيد MSS+FVG"
            elif state.get("trade_taken_today"):
                status_txt = "✅ خلصنا صفقة اليوم، بنستريح لين بكرة"
            else:
                status_txt = f"{random.choice(FUN_EMOJIS)} لسا نراقب النطاق، ما فيه اصطياد واضح لين الحين"
            send_telegram(f"📡 تحديث دوري: {status_txt}")
            state["last_status_minute_block"] = minute_block
            save_state(state)

    if ASIAN_START_H <= hour <= ASIAN_END_H:
        df15 = fetch_candles("15min", 60)
        today_asia = df15[(df15["datetime"].dt.date.astype(str) == today_str)
                           & (df15["datetime"].dt.hour >= ASIAN_START_H)
                           & (df15["datetime"].dt.hour < ASIAN_END_H)]
        if len(today_asia) > 0:
            state["asian_high"] = float(today_asia["high"].max())
            state["asian_low"] = float(today_asia["low"].min())
            save_state(state)
        return

    if hour < LONDON_START_H:
        print("Before London window.")
        return

    if state.get("trade_taken_today"):
        print("Trade already taken today.")
        return

    if state.get("asian_high") is None:
        print("Asian range not available yet.")
        return

    pending = state.get("pending_signal")
    if pending and pending.get("status") == "pending":
        expires = datetime.fromisoformat(pending["expires"])
        df1m = fetch_candles("1min", 15)
        touched = ((pending["direction"] == "sell" and df1m["high"].max() >= pending["entry"])
                   or (pending["direction"] == "buy" and df1m["low"].min() <= pending["entry"]))
        if touched:
            pending["status"] = "filled"
            state["trade_taken_today"] = True
            save_state(state)
            send_telegram(f"{random.choice(GREEN_EMOJIS)} ENTRY TRIGGERED — {pending['direction'].upper()} 🎉\n"
                          f"Entry: {pending['entry']:.2f}\nStop Loss: {pending['sl']:.2f} 🛑\n"
                          f"Take Profit: {pending['tp']:.2f} 🎯\n"
                          f"حرك الوقف للتعادل عند 1:{BE_TRIGGER_RR} 🔒\nتأكد من السبريد الفعلي قبل التنفيذ ⚠️")
            return
        if now_utc >= expires:
            pending["status"] = "expired"
            state["pending_signal"] = pending
            save_state(state)
            send_telegram(f"⌛ انتهت صلاحية الأمر بدون تنفيذ ({pending['direction'].upper()} @ {pending['entry']:.2f}) 🤷")
            return
        print("Pending signal still active.")
        return

    sweep = state.get("sweep")
    if not sweep:
        df15 = fetch_candles("15min", 10)
        last15 = df15.iloc[-1]
        if last15["high"] >= state["asian_high"] + MIN_SWEEP_DIST:
            sweep = {"type": "sell", "time": now_utc.isoformat(), "extreme": float(last15["high"])}
        elif last15["low"] <= state["asian_low"] - MIN_SWEEP_DIST:
            sweep = {"type": "buy", "time": now_utc.isoformat(), "extreme": float(last15["low"])}
        if sweep:
            state["sweep"] = sweep
            save_state(state)
            send_telegram(f"🌊🚨 اصطياد سيولة! احتمال إعداد {sweep['type'].upper()} يتكوّن\n"
                          f"نراقب MSS + FVG خلال {FVG_MAX_MINUTES} دقيقة القادمة ⏱️")
        return

    sweep_time = datetime.fromisoformat(sweep["time"])
    if now_utc - sweep_time > timedelta(minutes=FVG_MAX_MINUTES):
        state["sweep"] = None
        save_state(state)
        return

    if in_news_blackout():
        print("News blackout, holding off.")
        return

    df1m = fetch_candles("1min", 15)
    found, entry = check_mss_and_fvg(df1m, sweep["type"])
    if found:
        if sweep["type"] == "sell":
            sl = sweep["extreme"] + SL_BUFFER
            risk = sl - entry
            tp = entry - risk * RISK_REWARD
        else:
            sl = sweep["extreme"] - SL_BUFFER
            risk = entry - sl
            tp = entry + risk * RISK_REWARD
        pending_signal = {"direction": sweep["type"], "entry": float(entry), "sl": float(sl), "tp": float(tp),
                           "created": now_utc.isoformat(),
                           "expires": (now_utc + timedelta(minutes=ENTRY_EXPIRY_MINUTES)).isoformat(),
                           "status": "pending"}
        state["pending_signal"] = pending_signal
        save_state(state)
        send_telegram(f"{random.choice(GREEN_EMOJIS)} MSS + FVG CONFIRMED — {sweep['type'].upper()} جاهز 🔥\n"
                      f"Pending Limit Entry: {entry:.2f}\nStop Loss: {sl:.2f} 🛑\n"
                      f"Take Profit (1:{RISK_REWARD:g}): {tp:.2f} 🎯\n"
                      f"ينتهي لو ما انلمس خلال {ENTRY_EXPIRY_MINUTES} دقايق ⏳\nتأكد من السبريد يدويًا ⚠️")


if __name__ == "__main__":
    main()
