import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ============================================================
# ABOUT MARKET MT BOT — XAUUSD V1
#
# Daily report at 23:00 Europe/Warsaw:
#   - High
#   - Low
#   - 50% Equilibrium
#   - Bullish / Bearish / Neutral trend
#
# Intraday:
#   - Watches previous day's High / Low / Equilibrium
#   - Sends a key-level update only after a confirmed 15m close
#   - Avoids repeated alerts for the same level/day
#
# DATA:
# Yahoo Finance chart feed, XAUUSD=X (spot gold reference).
#
# IMPORTANT:
# This is a market-structure/reference bot, not a trading signal.
# ============================================================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
THREAD_ID = int(os.environ["TELEGRAM_MESSAGE_THREAD_ID"])
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))

SYMBOL = os.getenv("XAUUSD_SYMBOL", "XAUUSD=X")
DB = "about_market_state.db"

# Telegram / market polling is handled by GitHub Actions.
# Keep workflow at every 5 minutes.
LOOKBACK_DAYS = 8

# User-defined XAUUSD session windows in Europe/Warsaw.
# End time is exclusive: e.g. Asia 01:00-08:00 means candles from
# 01:00:00 through 07:59:59.
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
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": CHAT_ID,
            "message_thread_id": THREAD_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    r.raise_for_status()


def yahoo_chart(interval="5m", range_value="8d"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}"
    r = requests.get(
        url,
        params={
            "interval": interval,
            "range": range_value,
            "includePrePost": "false",
            "events": "div,splits",
        },
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()

    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError("Yahoo Finance returned no chart data.")

    return result[0]


def candles(interval="5m", range_value="8d"):
    result = yahoo_chart(interval, range_value)
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]

    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []

    rows = []
    for i, ts in enumerate(timestamps):
        try:
            o = opens[i]
            h = highs[i]
            l = lows[i]
            c = closes[i]
        except IndexError:
            continue

        if any(v is None for v in (o, h, l, c)):
            continue

        dt = datetime.fromtimestamp(ts, timezone.utc).astimezone(TZ)

        rows.append({
            "dt": dt,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
        })

    return rows


def market_day(dt):
    """
    Trading day boundary is 23:00 Europe/Warsaw.

    A candle at/after 23:00 belongs to the new market day.
    A candle before 23:00 belongs to the date of the current session.
    """
    if dt.hour >= 23:
        return dt.date()
    return (dt - timedelta(days=1)).date()


def daily_ranges(rows):
    grouped = {}

    for row in rows:
        d = market_day(row["dt"])
        grouped.setdefault(d, []).append(row)

    result = {}
    for d, items in grouped.items():
        if not items:
            continue

        high = max(x["high"] for x in items)
        low = min(x["low"] for x in items)
        equilibrium = low + (high - low) / 2

        # The "close" is the latest available close for that market day.
        items_sorted = sorted(items, key=lambda x: x["dt"])
        close = items_sorted[-1]["close"]

        result[d] = {
            "day": d,
            "high": high,
            "low": low,
            "equilibrium": equilibrium,
            "close": close,
            "bars": len(items),
        }

    return result


def fmt_price(value):
    return f"{value:,.2f}"



def session_ranges(rows, target_market_day):
    """
    Calculate High/Low for the user's three fixed Warsaw-time sessions.

    The market-day convention remains 23:00 Europe/Warsaw, so all three
    sessions from 01:00-18:00 belong to the market day that ended at 23:00
    on that calendar date.
    """
    result = {}

    for name, (start_hour, end_hour) in SESSIONS.items():
        items = [
            r for r in rows
            if market_day(r["dt"]) == target_market_day
            and (
                r["dt"].hour > start_hour
                or (r["dt"].hour == start_hour and r["dt"].minute >= 0)
            )
            and r["dt"].hour < end_hour
        ]

        if not items:
            result[name] = None
            continue

        result[name] = {
            "high": max(r["high"] for r in items),
            "low": min(r["low"] for r in items),
        }

    return result


def session_report_lines(session_data):
    labels = {
        "ASIA": "🌏 ASIAN SESSION",
        "LONDON": "🇬🇧 LONDON SESSION",
        "NEW YORK": "🇺🇸 NEW YORK SESSION",
    }

    lines = []
    for name in ("ASIA", "LONDON", "NEW YORK"):
        lines.append(labels[name])

        data = session_data.get(name)
        if data is None:
            lines.append("🔺 HIGH: N/A")
            lines.append("🔻 LOW: N/A")
        else:
            lines.append(f"🔺 HIGH: {fmt_price(data['high'])}")
            lines.append(f"🔻 LOW: {fmt_price(data['low'])}")

        lines.append("")

    return "\n".join(lines).rstrip()


def trend_from_structure(days):
    """
    Uses the latest 3 completed market days.

    Bullish:
      latest high >= previous high AND latest low >= previous low,
      with at least one strict improvement.

    Bearish:
      latest high <= previous high AND latest low <= previous low,
      with at least one strict deterioration.

    Otherwise neutral.

    This intentionally avoids pretending that one candle alone defines
    a multi-day trend.
    """
    if len(days) < 3:
        return "NEUTRAL", "Not enough completed days"

    a, b, c = days[-3], days[-2], days[-1]

    bullish_steps = (
        b["high"] >= a["high"] and
        b["low"] >= a["low"] and
        c["high"] >= b["high"] and
        c["low"] >= b["low"]
    )
    bearish_steps = (
        b["high"] <= a["high"] and
        b["low"] <= a["low"] and
        c["high"] <= b["high"] and
        c["low"] <= b["low"]
    )

    if bullish_steps and (
        c["high"] > a["high"] or c["low"] > a["low"]
    ):
        return "BULLISH", "Higher-high / higher-low structure"

    if bearish_steps and (
        c["high"] < a["high"] or c["low"] < a["low"]
    ):
        return "BEARISH", "Lower-high / lower-low structure"

    # A secondary check using the last two closes helps classify a
    # directional day without forcing a trend when structure is mixed.
    if c["close"] > b["close"] and c["close"] > c["equilibrium"]:
        return "BULLISH", "Price above equilibrium with rising close"

    if c["close"] < b["close"] and c["close"] < c["equilibrium"]:
        return "BEARISH", "Price below equilibrium with falling close"

    return "NEUTRAL", "Mixed structure"


def trend_icon(trend):
    return {
        "BULLISH": "📈",
        "BEARISH": "📉",
        "NEUTRAL": "⚪",
    }.get(trend, "⚪")


def daily_message(d, session_data):
    trend, reason = trend_from_structure(d)

    return (
        "🟡 XAUUSD — DAILY MARKET\n\n"
        f"📅 {d['day'].strftime('%d %B %Y')}\n\n"
        f"🔺 HIGH: {fmt_price(d['high'])}\n"
        f"🔻 LOW: {fmt_price(d['low'])}\n\n"
        f"⚖️ EQUILIBRIUM: {fmt_price(d['equilibrium'])}\n\n"
        f"{trend_icon(trend)} TREND: {trend}\n"
        f"🧠 {reason}\n\n"
        "━━━━━━━━━━━━━━\n\n"
        f"{session_report_lines(session_data)}\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "50% OF DAILY RANGE"
    )


def send_daily_report(con, completed_day, data, session_data):
    day_key = str(completed_day)

    if con.execute(
        "SELECT 1 FROM daily_reports WHERE market_day=?",
        (day_key,),
    ).fetchone():
        return False

    telegram_send(daily_message(data, session_data))

    con.execute(
        "INSERT INTO daily_reports VALUES (?, ?)",
        (day_key, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    return True


def level_already_sent(con, day_key, level, direction):
    return con.execute(
        """SELECT 1 FROM level_alerts
           WHERE market_day=? AND level_name=? AND direction=?""",
        (str(day_key), level, direction),
    ).fetchone() is not None


def mark_level(con, day_key, level, direction):
    con.execute(
        "INSERT OR IGNORE INTO level_alerts VALUES (?, ?, ?, ?)",
        (
            str(day_key),
            level,
            direction,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    con.commit()


def key_level_message(day, level_name, level_value, close_value, direction):
    if level_name == "HIGH":
        text = (
            "🔓 PREVIOUS DAY HIGH BROKEN\n\n"
            "📈 Market is trading above the previous daily high."
        )
    elif level_name == "LOW":
        text = (
            "🔓 PREVIOUS DAY LOW BROKEN\n\n"
            "📉 Market is trading below the previous daily low."
        )
    else:
        if direction == "ABOVE":
            text = (
                "⚖️ EQUILIBRIUM FLIP\n\n"
                "📈 Price closed back above the 50% level."
            )
        else:
            text = (
                "⚖️ EQUILIBRIUM FLIP\n\n"
                "📉 Price closed back below the 50% level."
            )

    return (
        "🚨 XAUUSD — KEY LEVEL UPDATE\n\n"
        f"📅 Reference day: {day}\n\n"
        f"{text}\n\n"
        f"📍 Level: {fmt_price(level_value)}\n"
        f"💰 15m Close: {fmt_price(close_value)}\n\n"
        "⚠️ Confirmation is based on a 15m candle close.\n"
        "This is a market-structure update, not a trade signal."
    )



def check_equilibrium_zone(con, reference_day, equilibrium, current_price):
    """
    Immediate-style equilibrium alert based on the latest available market
    price polled by the workflow.

    Zone:
        Equilibrium - $1 <= price <= Equilibrium

    A single alert is sent when price ENTERS the zone. After price leaves the
    zone, a future re-entry can trigger another alert.
    """
    lower = equilibrium - 1.0
    inside = lower <= current_price <= equilibrium

    row = con.execute(
        "SELECT inside_zone FROM equilibrium_zone_state WHERE market_day=?",
        (str(reference_day),),
    ).fetchone()

    was_inside = bool(row[0]) if row else False

    if inside and not was_inside:
        telegram_send(
            "⚖️ XAUUSD — EQUILIBRIUM ZONE\n\n"
            f"🎯 Equilibrium: {fmt_price(equilibrium)}\n"
            f"📍 Current Price: {fmt_price(current_price)}\n"
            f"📏 Zone: {fmt_price(lower)} — {fmt_price(equilibrium)}\n\n"
            "Price has entered the $1 Equilibrium Zone.\n\n"
            "⚠️ Watch price reaction around 50% of the daily range."
        )

    con.execute(
        """
        INSERT INTO equilibrium_zone_state
            (market_day, inside_zone, last_price, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(market_day) DO UPDATE SET
            inside_zone=excluded.inside_zone,
            last_price=excluded.last_price,
            updated_at=excluded.updated_at
        """,
        (
            str(reference_day),
            int(inside),
            current_price,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    con.commit()

    return 1 if inside and not was_inside else 0



def session_breakout_message(current_session, reference_session,
                             level_name, level_value, current_price):
    session_label = {
        "LONDON": "🇬🇧 LONDON SESSION",
        "NEW YORK": "🇺🇸 NEW YORK SESSION",
    }[current_session]

    reference_label = {
        "ASIA": "🌏 ASIAN SESSION",
        "LONDON": "🇬🇧 LONDON SESSION",
    }[reference_session]

    icon = "🚀" if level_name == "HIGH" else "🔻"

    return (
        f"{icon} XAUUSD — SESSION BREAKOUT\n\n"
        f"{session_label}\n"
        f"📌 Broke {reference_label} {level_name}\n\n"
        f"🎯 Level: {fmt_price(level_value)}\n"
        f"💰 Current Price: {fmt_price(current_price)}\n\n"
        f"⚡ Price has crossed the previous {reference_session.title()} "
        f"{level_name} during the {current_session.title()} session.\n\n"
        "⚠️ Intraday market-structure update."
    )


def session_breakout_already_sent(con, market_day_key,
                                  current_session, reference_session,
                                  direction):
    return con.execute(
        """
        SELECT 1 FROM session_breakout_alerts
        WHERE market_day=? AND current_session=?
          AND reference_session=? AND direction=?
        """,
        (
            str(market_day_key),
            current_session,
            reference_session,
            direction,
        ),
    ).fetchone() is not None


def mark_session_breakout(con, market_day_key,
                          current_session, reference_session, direction):
    con.execute(
        """
        INSERT OR IGNORE INTO session_breakout_alerts
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(market_day_key),
            current_session,
            reference_session,
            direction,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    con.commit()


def check_session_breakouts(con, market_day_key, session_data,
                            current_price, previous_price, now):
    """
    London 09:00-13:00 -> Asia High/Low.
    New York 14:00-18:00 -> London High/Low.

    Alert only on a confirmed CROSS between two polling observations:
      High: previous <= level and current > level
      Low:  previous >= level and current < level

    One alert per direction/session/day.
    """
    if current_price is None or previous_price is None:
        return 0

    hour = now.hour + now.minute / 60.0

    if 9 <= hour < 13:
        current_session, reference_session = "LONDON", "ASIA"
    elif 14 <= hour < 18:
        current_session, reference_session = "NEW YORK", "LONDON"
    else:
        return 0

    reference = session_data.get(reference_session)
    if not reference:
        return 0

    sent = 0
    checks = [
        ("HIGH", reference["high"],
         previous_price <= reference["high"] and current_price > reference["high"],
         "ABOVE"),
        ("LOW", reference["low"],
         previous_price >= reference["low"] and current_price < reference["low"],
         "BELOW"),
    ]

    for level_name, level_value, crossed, direction in checks:
        if not crossed:
            continue

        if session_breakout_already_sent(
            con, market_day_key, current_session, reference_session, direction
        ):
            continue

        telegram_send(
            session_breakout_message(
                current_session,
                reference_session,
                level_name,
                level_value,
                current_price,
            )
        )
        mark_session_breakout(
            con, market_day_key, current_session, reference_session, direction
        )
        sent += 1

    return sent


def check_key_levels(con, previous_day, previous_data, latest_15m):
    """
    Confirm a level only after a 15m candle closes beyond it.

    We use the latest completed 15m candle, not the currently forming candle.
    """
    if not latest_15m:
        return 0

    latest = latest_15m[-1]
    close = latest["close"]
    day_key = previous_day
    sent = 0

    levels = [
        ("HIGH", previous_data["high"]),
        ("LOW", previous_data["low"]),
        ("EQUILIBRIUM", previous_data["equilibrium"]),
    ]

    # Previous High: confirmed close above.
    if close > previous_data["high"]:
        if not level_already_sent(con, day_key, "HIGH", "ABOVE"):
            telegram_send(
                key_level_message(
                    day_key,
                    "HIGH",
                    previous_data["high"],
                    close,
                    "ABOVE",
                )
            )
            mark_level(con, day_key, "HIGH", "ABOVE")
            sent += 1

    # Previous Low: confirmed close below.
    if close < previous_data["low"]:
        if not level_already_sent(con, day_key, "LOW", "BELOW"):
            telegram_send(
                key_level_message(
                    day_key,
                    "LOW",
                    previous_data["low"],
                    close,
                    "BELOW",
                )
            )
            mark_level(con, day_key, "LOW", "BELOW")
            sent += 1

    # Equilibrium flips are useful context, but only alert once per side.
    if close > previous_data["equilibrium"]:
        if not level_already_sent(con, day_key, "EQUILIBRIUM", "ABOVE"):
            telegram_send(
                key_level_message(
                    day_key,
                    "EQUILIBRIUM",
                    previous_data["equilibrium"],
                    close,
                    "ABOVE",
                )
            )
            mark_level(con, day_key, "EQUILIBRIUM", "ABOVE")
            sent += 1

    if close < previous_data["equilibrium"]:
        if not level_already_sent(con, day_key, "EQUILIBRIUM", "BELOW"):
            telegram_send(
                key_level_message(
                    day_key,
                    "EQUILIBRIUM",
                    previous_data["equilibrium"],
                    close,
                    "BELOW",
                )
            )
            mark_level(con, day_key, "EQUILIBRIUM", "BELOW")
            sent += 1

    return sent


def main():
    con = db_connect()
    now = datetime.now(TZ)

    # We use 5m data for daily range construction.
    rows = candles("5m", "8d")
    ranges = daily_ranges(rows)

    # The last market day is not necessarily complete yet.
    # A market day completes at 23:00 Warsaw.
    today_market_day = market_day(now)

    completed = sorted(
        d for d in ranges.keys()
        if d < today_market_day
    )

    if not completed:
        print("No completed XAUUSD market day available yet.")
        return

    latest_completed_day = completed[-1]
    latest_completed = ranges[latest_completed_day]

    # User-defined session High/Low for the completed market day.
    session_data = session_ranges(rows, latest_completed_day)

    # If current time is after 23:00, the latest completed day is the day
    # that just ended, so send the daily report once.
    daily_sent = False
    if now.hour >= 23:
        daily_sent = send_daily_report(
            con,
            latest_completed_day,
            latest_completed,
            session_data,
        )

    # Key-level tracking uses the previous completed day as the reference
    # during the current trading day.
    reference_day = latest_completed_day
    reference = latest_completed

    # Latest observed price for immediate Equilibrium-zone and session-breakout alerts.
    current_rows = candles("1m", "1d")
    current_price = current_rows[-1]["close"] if current_rows else None

    equilibrium_alerts = 0
    session_breakout_alerts = 0

    previous_price_row = con.execute(
        "SELECT last_price FROM session_breakout_state WHERE market_day=?",
        (str(reference_day),),
    ).fetchone()
    previous_price = float(previous_price_row[0]) if previous_price_row else None

    if current_price is not None:
        equilibrium_alerts = check_equilibrium_zone(
            con,
            reference_day,
            reference["equilibrium"],
            current_price,
        )

        session_breakout_alerts = check_session_breakouts(
            con,
            reference_day,
            session_data,
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
                str(reference_day),
                current_price,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        con.commit()

    # Key-level High/Low confirmations still use completed 15m candles.
    intraday_rows = candles("15m", "3d")

    # Only use completed 15m candles.
    completed_15m = [
        r for r in intraday_rows
        if r["dt"] + timedelta(minutes=15) <= now
    ]

    level_sent = check_key_levels(
        con,
        reference_day,
        reference,
        completed_15m,
    )

    trend_days = [ranges[d] for d in completed[-5:]]
    trend, reason = trend_from_structure(trend_days)

    print(
        f"XAUUSD data: {len(rows)} x 5m candles. "
        f"Completed market days: {len(completed)}. "
        f"Trend: {trend}. "
        f"Daily report sent: {daily_sent}. "
        f"Sessions calculated: {sum(1 for v in session_data.values() if v is not None)}/3. "
        f"Key-level alerts sent: {level_sent}. "
        f"Equilibrium-zone alerts sent: {equilibrium_alerts}. "
        f"Session-breakout alerts sent: {session_breakout_alerts}."
    )


if __name__ == "__main__":
    main()
