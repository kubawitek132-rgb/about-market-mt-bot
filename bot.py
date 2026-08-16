
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ============================================================
# ABOUT MARKET MT BOT — XAUUSD V2 / Twelve Data
#
# Daily report at 23:00 Europe/Warsaw:
#   - Daily High
#   - Daily Low
#   - 50% Equilibrium
#   - Multi-day trend
#   - Asia / London / New York High & Low
#
# Intraday:
#   - Equilibrium zone: Equilibrium-$1.00 .. Equilibrium
#   - London: Asia High/Low breakouts
#   - New York: London High/Low breakouts
#   - Previous day High/Low confirmed on 15m close
#
# DATA SOURCE:
# Twelve Data, symbol XAU/USD.
#
# This is a market-structure/reference tool, not financial advice.
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_THREAD_ID = int(os.environ["TELEGRAM_MESSAGE_THREAD_ID"])
TWELVE_API_KEY = os.environ["TWELVE_DATA_API_KEY"]

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))
SYMBOL = os.getenv("XAU_SYMBOL", "XAU/USD")
DB = "about_market_state.db"

SESSIONS = {
    "ASIA": (1, 8),
    "LONDON": (9, 13),
    "NEW YORK": (14, 18),
}


def db_connect():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            market_day TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS level_alerts (
            market_day TEXT NOT NULL,
            level_name TEXT NOT NULL,
            direction TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (market_day, level_name, direction)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS equilibrium_zone_state (
            market_day TEXT PRIMARY KEY,
            inside_zone INTEGER NOT NULL DEFAULT 0,
            last_price REAL,
            updated_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS session_breakout_state (
            market_day TEXT PRIMARY KEY,
            last_price REAL,
            updated_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS session_breakout_alerts (
            market_day TEXT NOT NULL,
            current_session TEXT NOT NULL,
            reference_session TEXT NOT NULL,
            direction TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (
                market_day, current_session, reference_session, direction
            )
        )
    """)
    con.commit()
    return con


def telegram_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "message_thread_id": TELEGRAM_THREAD_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    r.raise_for_status()


def td_request(interval, outputsize=5000):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "timezone": "Europe/Warsaw",
        "apikey": TWELVE_API_KEY,
        "format": "JSON",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "error":
        raise RuntimeError(data.get("message", "Twelve Data API error"))
    return data


def candles(interval="5min", outputsize=5000):
    data = td_request(interval, outputsize)
    values = data.get("values") or []
    rows = []

    for item in reversed(values):
        dt = datetime.strptime(
            item["datetime"],
            "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=TZ)

        rows.append({
            "dt": dt,
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"]),
        })

    return rows


def market_day(dt):
    return dt.date() if dt.hour >= 23 else (dt - timedelta(days=1)).date()


def daily_ranges(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(market_day(row["dt"]), []).append(row)

    result = {}
    for d, items in grouped.items():
        high = max(x["high"] for x in items)
        low = min(x["low"] for x in items)
        equilibrium = low + (high - low) / 2
        items = sorted(items, key=lambda x: x["dt"])
        result[d] = {
            "day": d,
            "high": high,
            "low": low,
            "equilibrium": equilibrium,
            "close": items[-1]["close"],
        }
    return result


def session_ranges(rows, target_market_day):
    result = {}
    for name, (start_hour, end_hour) in SESSIONS.items():
        items = [
            r for r in rows
            if market_day(r["dt"]) == target_market_day
            and start_hour <= r["dt"].hour < end_hour
        ]
        if not items:
            result[name] = None
        else:
            result[name] = {
                "high": max(r["high"] for r in items),
                "low": min(r["low"] for r in items),
            }
    return result


def fmt_price(v):
    return f"{v:,.2f}"


def trend_from_structure(days):
    if len(days) < 3:
        return "NEUTRAL", "Not enough completed days"

    a, b, c = days[-3], days[-2], days[-1]

    bull = (
        b["high"] >= a["high"] and b["low"] >= a["low"] and
        c["high"] >= b["high"] and c["low"] >= b["low"] and
        (c["high"] > a["high"] or c["low"] > a["low"])
    )
    bear = (
        b["high"] <= a["high"] and b["low"] <= a["low"] and
        c["high"] <= b["high"] and c["low"] <= b["low"] and
        (c["high"] < a["high"] or c["low"] < a["low"])
    )

    if bull:
        return "BULLISH", "Higher-high / higher-low structure"
    if bear:
        return "BEARISH", "Lower-high / lower-low structure"
    if c["close"] > b["close"] and c["close"] > c["equilibrium"]:
        return "BULLISH", "Price above equilibrium with rising close"
    if c["close"] < b["close"] and c["close"] < c["equilibrium"]:
        return "BEARISH", "Price below equilibrium with falling close"
    return "NEUTRAL", "Mixed structure"


def trend_icon(t):
    return {"BULLISH": "📈", "BEARISH": "📉", "NEUTRAL": "⚪"}.get(t, "⚪")


def session_lines(sd):
    labels = {
        "ASIA": "🌏 ASIAN SESSION",
        "LONDON": "🇬🇧 LONDON SESSION",
        "NEW YORK": "🇺🇸 NEW YORK SESSION",
    }
    lines = []
    for name in ("ASIA", "LONDON", "NEW YORK"):
        lines.append(labels[name])
        d = sd.get(name)
        if d is None:
            lines += ["🔺 HIGH: N/A", "🔻 LOW: N/A"]
        else:
            lines += [
                f"🔺 HIGH: {fmt_price(d['high'])}",
                f"🔻 LOW: {fmt_price(d['low'])}",
            ]
        lines.append("")
    return "\n".join(lines).rstrip()


def daily_message(d, sd, trend, reason):
    return (
        "🟡 XAUUSD — DAILY MARKET\n\n"
        f"📅 {d['day'].strftime('%d %B %Y')}\n\n"
        f"🔺 HIGH: {fmt_price(d['high'])}\n"
        f"🔻 LOW: {fmt_price(d['low'])}\n\n"
        f"⚖️ EQUILIBRIUM: {fmt_price(d['equilibrium'])}\n\n"
        f"{trend_icon(trend)} TREND: {trend}\n"
        f"🧠 {reason}\n\n"
        "━━━━━━━━━━━━━━\n\n"
        f"{session_lines(sd)}\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "50% OF DAILY RANGE"
    )


def send_daily_report(con, day, d, sd, trend, reason):
    if con.execute(
        "SELECT 1 FROM daily_reports WHERE market_day=?",
        (str(day),),
    ).fetchone():
        return False
    telegram_send(daily_message(d, sd, trend, reason))
    con.execute(
        "INSERT INTO daily_reports VALUES (?, ?)",
        (str(day), datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    return True


def check_equilibrium_zone(con, day, eq, price):
    lower = eq - 1.0
    inside = lower <= price <= eq
    row = con.execute(
        "SELECT inside_zone FROM equilibrium_zone_state WHERE market_day=?",
        (str(day),),
    ).fetchone()
    was_inside = bool(row[0]) if row else False

    if inside and not was_inside:
        telegram_send(
            "⚖️ XAUUSD — EQUILIBRIUM ZONE\n\n"
            f"🎯 Equilibrium: {fmt_price(eq)}\n"
            f"📍 Current Price: {fmt_price(price)}\n"
            f"📏 Zone: {fmt_price(lower)} — {fmt_price(eq)}\n\n"
            "Price has entered the $1 Equilibrium Zone.\n\n"
            "⚠️ Watch price reaction around 50% of the daily range."
        )

    con.execute("""
        INSERT INTO equilibrium_zone_state
            (market_day, inside_zone, last_price, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(market_day) DO UPDATE SET
            inside_zone=excluded.inside_zone,
            last_price=excluded.last_price,
            updated_at=excluded.updated_at
    """, (str(day), int(inside), price, datetime.now(timezone.utc).isoformat()))
    con.commit()
    return 1 if inside and not was_inside else 0


def breakout_message(current_session, reference_session, level_name, level, price):
    sl = {"LONDON": "🇬🇧 LONDON SESSION", "NEW YORK": "🇺🇸 NEW YORK SESSION"}[current_session]
    rl = {"ASIA": "🌏 ASIAN SESSION", "LONDON": "🇬🇧 LONDON SESSION"}[reference_session]
    icon = "🚀" if level_name == "HIGH" else "🔻"
    return (
        f"{icon} XAUUSD — SESSION BREAKOUT\n\n"
        f"{sl}\n"
        f"📌 Broke {rl} {level_name}\n\n"
        f"🎯 Level: {fmt_price(level)}\n"
        f"💰 Current Price: {fmt_price(price)}\n\n"
        f"⚡ Price crossed the previous {reference_session.title()} "
        f"{level_name} during the {current_session.title()} session.\n\n"
        "⚠️ Intraday market-structure update."
    )


def sent_breakout(con, day, cur, ref, direction):
    return con.execute(
        """SELECT 1 FROM session_breakout_alerts
           WHERE market_day=? AND current_session=?
           AND reference_session=? AND direction=?""",
        (str(day), cur, ref, direction),
    ).fetchone() is not None


def mark_breakout(con, day, cur, ref, direction):
    con.execute(
        "INSERT OR IGNORE INTO session_breakout_alerts VALUES (?, ?, ?, ?, ?)",
        (str(day), cur, ref, direction, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()


def check_session_breakouts(con, day, sd, current_price, previous_price, now):
    if current_price is None or previous_price is None:
        return 0

    hour = now.hour + now.minute / 60
    if 9 <= hour < 13:
        cur, ref = "LONDON", "ASIA"
    elif 14 <= hour < 18:
        cur, ref = "NEW YORK", "LONDON"
    else:
        return 0

    r = sd.get(ref)
    if not r:
        return 0

    checks = [
        ("HIGH", r["high"],
         previous_price <= r["high"] < current_price, "ABOVE"),
        ("LOW", r["low"],
         previous_price >= r["low"] > current_price, "BELOW"),
    ]

    sent = 0
    for name, level, crossed, direction in checks:
        if not crossed or sent_breakout(con, day, cur, ref, direction):
            continue
        telegram_send(breakout_message(cur, ref, name, level, current_price))
        mark_breakout(con, day, cur, ref, direction)
        sent += 1
    return sent


def previous_day_confirmations(con, day, d, rows15):
    """
    Confirm previous-day High/Low break using latest completed 15m close.
    This is retained separately from session breakouts.
    """
    if not rows15:
        return 0

    c = rows15[-1]["close"]
    sent = 0

    if c > d["high"]:
        exists = con.execute(
            "SELECT 1 FROM level_alerts WHERE market_day=? AND level_name=? AND direction=?",
            (str(day), "HIGH", "ABOVE"),
        ).fetchone()
        if not exists:
            telegram_send(
                "🚨 XAUUSD — KEY LEVEL UPDATE\n\n"
                "🔓 PREVIOUS DAY HIGH BROKEN\n\n"
                f"🎯 Level: {fmt_price(d['high'])}\n"
                f"💰 15m Close: {fmt_price(c)}\n\n"
                "⚠️ Confirmation based on a 15m candle close.\n"
                "Market-structure update, not a trade signal."
            )
            con.execute(
                "INSERT INTO level_alerts VALUES (?, ?, ?, ?)",
                (str(day), "HIGH", "ABOVE", datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
            sent += 1

    if c < d["low"]:
        exists = con.execute(
            "SELECT 1 FROM level_alerts WHERE market_day=? AND level_name=? AND direction=?",
            (str(day), "LOW", "BELOW"),
        ).fetchone()
        if not exists:
            telegram_send(
                "🚨 XAUUSD — KEY LEVEL UPDATE\n\n"
                "🔓 PREVIOUS DAY LOW BROKEN\n\n"
                f"🎯 Level: {fmt_price(d['low'])}\n"
                f"💰 15m Close: {fmt_price(c)}\n\n"
                "⚠️ Confirmation based on a 15m candle close.\n"
                "Market-structure update, not a trade signal."
            )
            con.execute(
                "INSERT INTO level_alerts VALUES (?, ?, ?, ?)",
                (str(day), "LOW", "BELOW", datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
            sent += 1

    return sent


def main():
    con = db_connect()
    now = datetime.now(TZ)

    # 5m data for daily/session calculations.
    rows5 = candles("5min", 5000)
    ranges = daily_ranges(rows5)

    current_day = market_day(now)
    completed = sorted(d for d in ranges if d < current_day)

    if not completed:
        print("No completed market day available.")
        return

    reference_day = completed[-1]
    reference = ranges[reference_day]

    # The daily report uses the latest completed market day.
    # Intraday session breakouts use the CURRENT market day's sessions:
    # London watches today's Asia range; New York watches today's London range.
    current_session_data = session_ranges(rows5, current_day)

    trend_days = [ranges[d] for d in completed[-5:]]
    trend, reason = trend_from_structure(trend_days)

    # Always attempt to publish the latest completed daily report.
    # The database prevents duplicates, so if GitHub misses the 23:00 run,
    # the next 5-minute run will still publish the report.
    daily_sent = send_daily_report(
        con, reference_day, reference,
        session_ranges(rows5, reference_day),
        trend, reason
    )

    # 1m price polling for Equilibrium / session breakouts.
    rows1 = candles("1min", 50)
    current_price = rows1[-1]["close"] if rows1 else None

    row = con.execute(
        "SELECT last_price FROM session_breakout_state WHERE market_day=?",
        (str(reference_day),),
    ).fetchone()
    previous_price = float(row[0]) if row else None

    eq_alerts = 0
    breakout_alerts = 0

    if current_price is not None:
        eq_alerts = check_equilibrium_zone(
            con, reference_day, reference["equilibrium"], current_price
        )
        breakout_alerts = check_session_breakouts(
            con, current_day, current_session_data,
            current_price, previous_price, now
        )

        con.execute("""
            INSERT INTO session_breakout_state
                (market_day, last_price, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(market_day) DO UPDATE SET
                last_price=excluded.last_price,
                updated_at=excluded.updated_at
        """, (str(reference_day), current_price, datetime.now(timezone.utc).isoformat()))
        con.commit()

    # 15m confirmation for previous-day High/Low.
    rows15 = candles("15min", 200)
    completed15 = [
        r for r in rows15
        if r["dt"] + timedelta(minutes=15) <= now
    ]
    key_alerts = previous_day_confirmations(
        con, reference_day, reference, completed15
    )

    print(
        f"Completed day: {reference_day}. "
        f"Current market day: {current_day}. "
        f"Trend: {trend}. "
        f"Daily report sent: {daily_sent}. "
        f"Equilibrium alerts: {eq_alerts}. "
        f"Session breakout alerts: {breakout_alerts}. "
        f"Previous-day key alerts: {key_alerts}."
    )


if __name__ == "__main__":
    main()
