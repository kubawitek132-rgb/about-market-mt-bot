
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


def session_ranges(rows, calendar_date):
    """
    Session ranges are independent from the Previous Day range.

    All sessions are based strictly on the user's Europe/Warsaw clock:
      Asia:      01:00-08:00
      London:    09:00-13:00
      New York:  14:00-18:00

    The end time is exclusive.
    """
    result = {}
    for name, (start_hour, end_hour) in SESSIONS.items():
        items = [
            r for r in rows
            if r["dt"].date() == calendar_date
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
    return f"{v:,.3f}"


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


def daily_message(previous_day, previous_data, session_data, trend, reason):
    return (
        "🟡 XAUUSD — DAILY MARKET\n\n"
        f"📅 REPORT: {previous_day.strftime('%d %B %Y')}\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "📅 PREVIOUS DAY\n\n"
        f"🔺 HIGH: {fmt_price(previous_data['high'])}\n"
        f"🔻 LOW: {fmt_price(previous_data['low'])}\n\n"
        f"⚖️ EQUILIBRIUM: {fmt_price(previous_data['equilibrium'])}\n\n"
        f"{trend_icon(trend)} TREND: {trend}\n"
        f"🧠 {reason}\n\n"
        "━━━━━━━━━━━━━━\n\n"
        f"{session_lines(session_data)}\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "⚖️ EQUILIBRIUM = 50% BETWEEN PREVIOUS DAY HIGH & LOW"
    )


def send_daily_report(con, previous_day, previous_data, session_data, trend, reason):
    if con.execute(
        "SELECT 1 FROM daily_reports WHERE market_day=?",
        (str(previous_day),),
    ).fetchone():
        return False

    telegram_send(
        daily_message(
            previous_day,
            previous_data,
            session_data,
            trend,
            reason,
        )
    )

    con.execute(
        "INSERT INTO daily_reports VALUES (?, ?)",
        (str(previous_day), datetime.now(timezone.utc).isoformat()),
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



def aggregate_15m_from_5m(rows5):
    """
    Aggregate the same 5-minute source into completed 15-minute candles.
    """
    buckets = {}
    now = datetime.now(TZ)

    for row in rows5:
        minute = (row["dt"].minute // 15) * 15
        bucket_dt = row["dt"].replace(
            minute=minute,
            second=0,
            microsecond=0,
        )

        bucket = buckets.get(bucket_dt)
        if bucket is None:
            buckets[bucket_dt] = {
                "dt": bucket_dt,
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "last_dt": row["dt"],
            }
        else:
            bucket["high"] = max(bucket["high"], row["high"])
            bucket["low"] = min(bucket["low"], row["low"])
            if row["dt"] >= bucket["last_dt"]:
                bucket["close"] = row["close"]
                bucket["last_dt"] = row["dt"]

    out = []
    for bucket_dt, bucket in sorted(buckets.items()):
        if bucket_dt + timedelta(minutes=15) <= now:
            out.append({
                "dt": bucket_dt,
                "open": bucket["open"],
                "high": bucket["high"],
                "low": bucket["low"],
                "close": bucket["close"],
            })

    return out


def main():
    con = db_connect()
    now = datetime.now(TZ)
    today = now.date()

    # One 5-minute request per workflow run.
    rows5 = candles("5min", 5000)
    if not rows5:
        print("No XAU/USD 5m data returned.")
        return

    # --------------------------------------------------------
    # MODULE 1: PREVIOUS DAY HIGH / LOW
    # Previous day is the completed 23:00 -> 23:00 Warsaw range.
    # --------------------------------------------------------
    ranges = daily_ranges(rows5)
    current_market_day = market_day(now)
    completed_days = sorted(
        d for d in ranges
        if d < current_market_day
    )

    if not completed_days:
        print("No completed Previous Day range available.")
        return

    previous_day = completed_days[-1]
    previous_day_data = ranges[previous_day]

    # Equilibrium ALWAYS comes only from Previous Day High/Low.
    trend_days = [ranges[d] for d in completed_days[-5:]]
    trend, reason = trend_from_structure(trend_days)

    # --------------------------------------------------------
    # MODULE 2: INDEPENDENT SESSION HIGH / LOW
    # Sessions are based only on the current calendar date/time.
    # --------------------------------------------------------
    current_sessions = session_ranges(rows5, today)

    # Send the daily report once per completed Previous Day.
    daily_sent = send_daily_report(
        con,
        previous_day,
        previous_day_data,
        current_sessions,
        trend,
        reason,
    )

    # Current observed price from the same 5m feed.
    current_price = rows5[-1]["close"]

    # --------------------------------------------------------
    # INTRADAY SESSION BREAKOUTS
    # London watches CURRENT DAY Asia; New York watches CURRENT DAY London.
    # --------------------------------------------------------
    row = con.execute(
        "SELECT last_price FROM session_breakout_state WHERE market_day=?",
        (str(today),),
    ).fetchone()
    previous_price = float(row[0]) if row else None

    breakout_alerts = check_session_breakouts(
        con,
        today,
        current_sessions,
        current_price,
        previous_price,
        now,
    )

    con.execute(
        """
        INSERT INTO session_breakout_state
            (market_day, last_price, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(market_day) DO UPDATE SET
            last_price=excluded.last_price,
            updated_at=excluded.updated_at
        """,
        (
            str(today),
            current_price,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    con.commit()

    # --------------------------------------------------------
    # EQUILIBRIUM ALERT
    # Based ONLY on Previous Day Equilibrium.
    # --------------------------------------------------------
    eq_alerts = check_equilibrium_zone(
        con,
        previous_day,
        previous_day_data["equilibrium"],
        current_price,
    )

    # --------------------------------------------------------
    # PREVIOUS DAY HIGH / LOW 15M CONFIRMATION
    # Uses same 5m feed aggregated locally.
    # --------------------------------------------------------
    rows15 = aggregate_15m_from_5m(rows5)
    key_alerts = previous_day_confirmations(
        con,
        previous_day,
        previous_day_data,
        rows15,
    )

    print(
        f"Previous day: {previous_day}. "
        f"Current date: {today}. "
        f"Trend: {trend}. "
        f"Daily report sent: {daily_sent}. "
        f"Sessions calculated: {sum(1 for v in current_sessions.values() if v is not None)}/3. "
        f"Equilibrium alerts: {eq_alerts}. "
        f"Session breakout alerts: {breakout_alerts}. "
        f"Previous-day key alerts: {key_alerts}. "
        f"Latest XAU/USD close: {fmt_price(current_price)}."
    )


if __name__ == "__main__":
    main()
