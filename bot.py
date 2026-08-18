import os
import sqlite3
from datetime import datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo

import requests

# ============================================================
# ABOUT MARKET MT BOT — FINAL XAUUSD LOGIC
#
# DATA:
#   Twelve Data — XAU/USD
#
# TIMEZONE:
#   Europe/Warsaw
#
# PREVIOUS / DAILY RANGE:
#   A trading day is explicitly defined as:
#       23:00 Warsaw -> next day 23:00 Warsaw
#
#   The day label is the calendar date on which that 23:00 close occurs.
#   Example:
#       14 Aug 23:00 -> 15 Aug 23:00 = trading day "15 Aug"
#
#   Monday:
#       Previous trading day = Friday
#
# SESSIONS (independent from Previous Day):
#   Asia      01:00-08:00 Warsaw
#   London    09:00-13:00 Warsaw
#   New York  14:00-18:00 Warsaw
#
# INTRADAY:
#   - London watches current-day Asia High/Low
#   - New York watches current-day London High/Low
#   - Equilibrium zone uses Previous Trading Day Equilibrium
#   - Previous Day High/Low breakout requires a completed M5 close
#
# WEEKENDS:
#   Saturday/Sunday are ignored completely.
# ============================================================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
THREAD_ID = int(os.environ["TELEGRAM_MESSAGE_THREAD_ID"])
TWELVE_API_KEY = os.environ["TWELVE_DATA_API_KEY"]

TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Warsaw"))
SYMBOL = os.getenv("XAU_SYMBOL", "XAU/USD")
DB = "about_market_state.db"

SESSIONS = {
    "ASIA": (1, 8),
    "LONDON": (9, 13),
    "NEW YORK": (14, 18),
}


# -----------------------------
# State
# -----------------------------
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
                market_day,
                current_session,
                reference_session,
                direction
            )
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS market_idea_alerts (
            market_day TEXT NOT NULL,
            setup_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (market_day, setup_key)
        )
    """)

    con.commit()
    return con


# -----------------------------
# Telegram
# -----------------------------
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

    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise RuntimeError(f"Telegram sendMessage failed: {detail}")

    return r.json()


# -----------------------------
# Twelve Data
# -----------------------------
def td_time_series(
    interval,
    start_dt=None,
    end_dt=None,
    outputsize=5000,
):
    url = "https://api.twelvedata.com/time_series"

    params = {
        "symbol": SYMBOL,
        "interval": interval,
        "outputsize": outputsize,
        "timezone": "Europe/Warsaw",
        "apikey": TWELVE_API_KEY,
        "format": "JSON",
    }

    if start_dt is not None:
        params["start_date"] = start_dt.strftime("%Y-%m-%d %H:%M:%S")

    if end_dt is not None:
        params["end_date"] = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()

    data = r.json()

    if data.get("status") == "error":
        raise RuntimeError(
            f"Twelve Data API error: {data.get('message', data)}"
        )

    return data


def parse_candles(data):
    values = data.get("values") or []
    rows = []

    for item in reversed(values):
        dt = datetime.strptime(
            item["datetime"],
            "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=TZ)

        rows.append(
            {
                "dt": dt,
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
            }
        )

    return rows


def load_5m_window(start_dt, end_dt):
    return parse_candles(
        td_time_series(
            "5min",
            start_dt=start_dt,
            end_dt=end_dt,
            outputsize=5000,
        )
    )


# -----------------------------
# Trading-day helpers
# -----------------------------
def previous_weekday(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def trading_day_window(day_label):
    """
    Trading day labelled by its 23:00 closing date.

    Example:
      label Friday 15 Aug
      start = Thursday 14 Aug 23:00
      end   = Friday 15 Aug 23:00
    """
    start = datetime.combine(
        day_label - timedelta(days=1),
        time(23, 0),
        tzinfo=TZ,
    )
    end = datetime.combine(
        day_label,
        time(23, 0),
        tzinfo=TZ,
    )
    return start, end


def session_window(day, start_hour, end_hour):
    start = datetime.combine(
        day,
        time(start_hour, 0),
        tzinfo=TZ,
    )
    end = datetime.combine(
        day,
        time(end_hour, 0),
        tzinfo=TZ,
    )
    return start, end


# -----------------------------
# Range calculations
# -----------------------------
def range_from_rows(rows):
    if not rows:
        return None

    high = max(r["high"] for r in rows)
    low = min(r["low"] for r in rows)
    close = rows[-1]["close"]

    return {
        "high": high,
        "low": low,
        "equilibrium": (high + low) / 2,
        "close": close,
    }


def daily_range_for_label(day_label):
    start, end = trading_day_window(day_label)
    rows = load_5m_window(start, end)

    # The end timestamp is the 23:00 boundary; the candle opening exactly
    # at 23:00 belongs to the next trading day and must not be included.
    rows = [r for r in rows if start <= r["dt"] < end]

    result = range_from_rows(rows)
    return result, rows


def session_range_for_day(rows, day, name):
    start_hour, end_hour = SESSIONS[name]

    rows = [
        r
        for r in rows
        if r["dt"].date() == day
        and start_hour <= r["dt"].hour < end_hour
    ]

    return range_from_rows(rows)


def all_sessions_for_day(rows, day):
    return {
        name: session_range_for_day(rows, day, name)
        for name in ("ASIA", "LONDON", "NEW YORK")
    }


# -----------------------------
# Daily close status
# -----------------------------
def trend_from_previous_day_close(closing_price, previous_day):
    """Classify a completed day using only the prior day's High/Low.

    No equilibrium, EMA, or multi-day structure is used for this status.
    """
    if closing_price > previous_day["high"]:
        return "BULLISH TREND", "Daily close above Previous Day High"

    if closing_price < previous_day["low"]:
        return "BEARISH TREND", "Daily close below Previous Day Low"

    return "INSIDE DAY", "Daily close inside Previous Day High/Low"


def trend_icon(trend):
    return {
        "BULLISH TREND": "📈",
        "BEARISH TREND": "📉",
        "INSIDE DAY": "↔️",
    }.get(trend, "⚪")


# -----------------------------
# Formatting / report
# -----------------------------
def fmt_price(value):
    return f"{value:,.3f}"


def session_text(sessions):
    labels = {
        "ASIA": "🌏 ASIA",
        "LONDON": "🇬🇧 LONDON",
        "NEW YORK": "🇺🇸 NEW YORK",
    }

    parts = []

    for name in ("ASIA", "LONDON", "NEW YORK"):
        parts.append(labels[name])

        data = sessions.get(name)

        if not data:
            parts.append("🔺 HIGH: N/A")
            parts.append("🔻 LOW: N/A")
        else:
            parts.append(f"🔺 HIGH: {fmt_price(data['high'])}")
            parts.append(f"🔻 LOW: {fmt_price(data['low'])}")

        parts.append("")

    return "\n".join(parts).rstrip()


def daily_message(day_label, previous_day, sessions, trend, reason):
    return (
        "🟡 XAUUSD — DAILY MARKET\n\n"
        f"📅 PREVIOUS DAY: {day_label.strftime('%d %B %Y')}\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "📅 PREVIOUS DAY\n\n"
        f"🔺 HIGH: {fmt_price(previous_day['high'])}\n"
        f"🔻 LOW: {fmt_price(previous_day['low'])}\n\n"
        f"⚖️ EQUILIBRIUM: {fmt_price(previous_day['equilibrium'])}\n\n"
        f"{trend_icon(trend)} TREND: {trend}\n"
        f"🧠 {reason}\n\n"
        "━━━━━━━━━━━━━━\n\n"
        f"{session_text(sessions)}\n\n"
        "━━━━━━━━━━━━━━\n\n"
        "⚖️ EQUILIBRIUM = 50% BETWEEN PREVIOUS DAY HIGH & LOW"
    )


# -----------------------------
# Daily report persistence
# -----------------------------
def send_daily_report(
    con,
    day_label,
    previous_day,
    sessions,
    trend,
    reason,
):
    if con.execute(
        "SELECT 1 FROM daily_reports WHERE market_day=?",
        (str(day_label),),
    ).fetchone():
        return False

    telegram_send(
        daily_message(
            day_label,
            previous_day,
            sessions,
            trend,
            reason,
        )
    )

    con.execute(
        "INSERT INTO daily_reports VALUES (?, ?)",
        (
            str(day_label),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    con.commit()
    return True


# -----------------------------
# Equilibrium zone
# -----------------------------
def check_equilibrium_zone(
    con,
    reference_day,
    equilibrium,
    price,
):
    lower = equilibrium - 1.0
    inside = lower <= price <= equilibrium

    row = con.execute(
        "SELECT inside_zone FROM equilibrium_zone_state WHERE market_day=?",
        (str(reference_day),),
    ).fetchone()

    was_inside = bool(row[0]) if row else False

    if inside and not was_inside:
        telegram_send(
            "⚖️ XAUUSD — EQUILIBRIUM ZONE\n\n"
            f"🎯 Equilibrium: {fmt_price(equilibrium)}\n"
            f"📍 Current Price: {fmt_price(price)}\n"
            f"📏 Zone: {fmt_price(lower)} — {fmt_price(equilibrium)}\n\n"
            "Price has entered the $1 Equilibrium Zone."
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
            price,
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    con.commit()

    return 1 if inside and not was_inside else 0


# -----------------------------
# Session breakout alerts
# -----------------------------
def breakout_already_sent(
    con,
    day,
    current_session,
    reference_session,
    direction,
):
    return con.execute(
        """
        SELECT 1
        FROM session_breakout_alerts
        WHERE market_day=?
          AND current_session=?
          AND reference_session=?
          AND direction=?
        """,
        (
            str(day),
            current_session,
            reference_session,
            direction,
        ),
    ).fetchone() is not None


def mark_breakout(
    con,
    day,
    current_session,
    reference_session,
    direction,
):
    con.execute(
        """
        INSERT OR IGNORE INTO session_breakout_alerts
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(day),
            current_session,
            reference_session,
            direction,
            datetime.now(timezone.utc).isoformat(),
        ),
    )

    con.commit()


def breakout_message(
    current_session,
    reference_session,
    level_name,
    level,
    price,
):
    icons = {
        "HIGH": "🚀",
        "LOW": "🔻",
    }

    return (
        f"{icons[level_name]} XAUUSD — SESSION BREAKOUT\n\n"
        f"{current_session} SESSION\n"
        f"📌 Broke {reference_session} {level_name}\n\n"
        f"🎯 Level: {fmt_price(level)}\n"
        f"💰 Current Price: {fmt_price(price)}\n\n"
        "⚠️ Intraday market-structure update."
    )


def check_session_breakouts(
    con,
    day,
    sessions,
    current_price,
    previous_price,
    now,
):
    if current_price is None or previous_price is None:
        return 0

    hour = now.hour + now.minute / 60

    if 9 <= hour < 13:
        current_session = "LONDON"
        reference_session = "ASIA"
    elif 14 <= hour < 18:
        current_session = "NEW YORK"
        reference_session = "LONDON"
    else:
        return 0

    reference = sessions.get(reference_session)

    if not reference:
        return 0

    checks = [
        (
            "HIGH",
            reference["high"],
            previous_price <= reference["high"]
            and current_price > reference["high"],
            "ABOVE",
        ),
        (
            "LOW",
            reference["low"],
            previous_price >= reference["low"]
            and current_price < reference["low"],
            "BELOW",
        ),
    ]

    sent = 0

    for level_name, level, crossed, direction in checks:
        if not crossed:
            continue

        if breakout_already_sent(
            con,
            day,
            current_session,
            reference_session,
            direction,
        ):
            continue

        telegram_send(
            breakout_message(
                current_session,
                reference_session,
                level_name,
                level,
                current_price,
            )
        )

        mark_breakout(
            con,
            day,
            current_session,
            reference_session,
            direction,
        )

        sent += 1

    return sent


# -----------------------------
# Completed M5 candle / previous-day breakouts
# -----------------------------
def latest_completed_5m_candle(rows, now):
    """Return the latest M5 candle only after its five-minute interval ended."""
    completed = [
        row
        for row in rows
        if row["dt"] + timedelta(minutes=5) <= now
    ]
    return completed[-1] if completed else None


def previous_day_breakout_alerts(
    con,
    previous_day,
    previous_day_data,
    candle5,
):
    if not candle5:
        return 0

    close = candle5["close"]
    sent = 0

    if close > previous_day_data["high"]:
        exists = con.execute(
            """
            SELECT 1 FROM level_alerts
            WHERE market_day=? AND level_name=? AND direction=?
            """,
            (
                str(previous_day),
                "HIGH",
                "ABOVE",
            ),
        ).fetchone()

        if not exists:
            telegram_send(
                "🚨 XAUUSD — PREVIOUS DAY HIGH BROKEN\n\n"
                f"🎯 Level: {fmt_price(previous_day_data['high'])}\n"
                f"💰 M5 Close: {fmt_price(close)}\n\n"
                "✅ Confirmed by a completed M5 candle."
            )

            con.execute(
                "INSERT INTO level_alerts VALUES (?, ?, ?, ?)",
                (
                    str(previous_day),
                    "HIGH",
                    "ABOVE",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

            con.commit()
            sent += 1

    if close < previous_day_data["low"]:
        exists = con.execute(
            """
            SELECT 1 FROM level_alerts
            WHERE market_day=? AND level_name=? AND direction=?
            """,
            (
                str(previous_day),
                "LOW",
                "BELOW",
            ),
        ).fetchone()

        if not exists:
            telegram_send(
                "🚨 XAUUSD — PREVIOUS DAY LOW BROKEN\n\n"
                f"🎯 Level: {fmt_price(previous_day_data['low'])}\n"
                f"💰 M5 Close: {fmt_price(close)}\n\n"
                "✅ Confirmed by a completed M5 candle."
            )

            con.execute(
                "INSERT INTO level_alerts VALUES (?, ?, ?, ?)",
                (
                    str(previous_day),
                    "LOW",
                    "BELOW",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

            con.commit()
            sent += 1

    return sent


# -----------------------------
# Main
# -----------------------------

# -----------------------------
# MT Market Ideas / Order Block engine
# -----------------------------
def average_true_range(rows, period=14):
    if len(rows) < period + 1:
        return None

    trs = []
    for i in range(1, len(rows)):
        high = rows[i]["high"]
        low = rows[i]["low"]
        prev_close = rows[i - 1]["close"]
        trs.append(max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        ))

    return sum(trs[-period:]) / period


def detect_order_blocks(rows):
    """
    Conservative 5m order-block detector.

    Bullish OB:
      bearish candle immediately before strong bullish displacement
      that closes above a recent swing high.

    Bearish OB:
      bullish candle immediately before strong bearish displacement
      that closes below a recent swing low.
    """
    if len(rows) < 40:
        return []

    atr = average_true_range(rows, 14)
    if not atr or atr <= 0:
        return []

    zones = []
    start = max(20, len(rows) - 80)

    for i in range(start, len(rows) - 1):
        ob = rows[i]
        impulse = rows[i + 1]

        body = abs(impulse["close"] - impulse["open"])
        if body < 1.5 * atr:
            continue

        recent = rows[max(0, i - 12):i]
        if len(recent) < 8:
            continue

        recent_high = max(x["high"] for x in recent)
        recent_low = min(x["low"] for x in recent)

        if (
            ob["close"] < ob["open"]
            and impulse["close"] > impulse["open"]
            and impulse["close"] > recent_high
        ):
            zones.append({
                "type": "BULLISH",
                "low": ob["low"],
                "high": ob["open"],
                "origin_dt": ob["dt"],
                "impulse_dt": impulse["dt"],
            })

        if (
            ob["close"] > ob["open"]
            and impulse["close"] < impulse["open"]
            and impulse["close"] < recent_low
        ):
            zones.append({
                "type": "BEARISH",
                "low": ob["open"],
                "high": ob["high"],
                "origin_dt": ob["dt"],
                "impulse_dt": impulse["dt"],
            })

    zones.sort(key=lambda z: z["impulse_dt"], reverse=True)
    return zones[:8]


def structure_shift(rows, direction):
    if len(rows) < 8:
        return False

    recent = rows[-4:]
    previous = rows[-8:-4]

    if direction == "LONG":
        return (
            recent[-1]["close"] > max(x["high"] for x in previous)
            and recent[-1]["close"] > recent[-2]["close"]
        )

    return (
        recent[-1]["close"] < min(x["low"] for x in previous)
        and recent[-1]["close"] < recent[-2]["close"]
    )


def setup_targets(entry, direction, previous_day, sessions):
    candidates = []

    if direction == "LONG":
        candidates.append(("Previous Day High", previous_day["high"]))
        for name, data in sessions.items():
            if data and data["high"] > entry:
                candidates.append((f"{name.title()} High", data["high"]))
        candidates = [(n, p) for n, p in candidates if p > entry]
        candidates.sort(key=lambda x: x[1])
    else:
        candidates.append(("Previous Day Low", previous_day["low"]))
        for name, data in sessions.items():
            if data and data["low"] < entry:
                candidates.append((f"{name.title()} Low", data["low"]))
        candidates = [(n, p) for n, p in candidates if p < entry]
        candidates.sort(key=lambda x: x[1], reverse=True)

    return candidates


def select_trade_plan(entry_zone, direction, previous_day, sessions, atr):
    if not atr or atr <= 0:
        return None

    entry_low = entry_zone["low"]
    entry_high = entry_zone["high"]
    entry = (entry_low + entry_high) / 2

    if direction == "LONG":
        sl = entry_low - 0.35 * atr
    else:
        sl = entry_high + 0.35 * atr

    risk = abs(entry - sl)
    if risk <= 0:
        return None

    valid = []
    for name, target in setup_targets(
        entry, direction, previous_day, sessions
    ):
        rr = abs(target - entry) / risk
        if rr >= 2.0:
            valid.append((name, target, rr))

    if not valid:
        return None

    tp1 = valid[0]
    tp2 = valid[-1]

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp1_name": tp1[0],
        "tp1": tp1[1],
        "rr1": tp1[2],
        "tp2_name": tp2[0],
        "tp2": tp2[1],
        "rr2": tp2[2],
    }


def market_idea_message(direction, grade, trend, zone, plan):
    side = "🟢 POTENTIAL LONG" if direction == "LONG" else "🔴 POTENTIAL SHORT"

    return (
        "🧠 MT MARKET IDEA\n\n"
        f"{side}\n"
        f"⭐ QUALITY: {grade}\n"
        f"📈 MARKET TREND: {trend}\n\n"
        "📦 ORDER BLOCK\n"
        f"{fmt_price(zone['low'])} — {fmt_price(zone['high'])}\n\n"
        "📍 ENTRY ZONE\n"
        f"{fmt_price(plan['entry_low'])} — {fmt_price(plan['entry_high'])}\n\n"
        f"🛑 SL: {fmt_price(plan['sl'])}\n\n"
        f"🎯 TP1: {fmt_price(plan['tp1'])} ({plan['tp1_name']})\n"
        f"🎯 R:R: 1:{plan['rr1']:.2f}\n\n"
        f"🎯 TP2: {fmt_price(plan['tp2'])} ({plan['tp2_name']})\n"
        f"🎯 R:R: 1:{plan['rr2']:.2f}\n\n"
        "🧭 LOGIC\n"
        "• Trend-aligned setup\n"
        "• Order block + displacement\n"
        "• Price interacting with the OB\n"
        "• R:R minimum 1:2\n\n"
        "⚠️ Scenario, not a guaranteed trade.\n"
        "Wait for confirmation before acting."
    )


def maybe_market_idea(con, day, rows, previous_day, sessions, trend):
    """
    At most one fresh A+ scenario per detected OB/day.
    Never fights the trend.
    Requires:
      - current price inside a recent aligned OB
      - 5m structure shift
      - minimum R:R 1:2
    """
    if trend not in {"BULLISH TREND", "BEARISH TREND"}:
        return 0

    if len(rows) < 40:
        return 0

    price = rows[-1]["close"]
    atr = average_true_range(rows, 14)
    zones = detect_order_blocks(rows)

    if not zones:
        return 0

    desired = (
        "BULLISH"
        if trend == "BULLISH TREND"
        else "BEARISH"
    )
    direction = (
        "LONG"
        if trend == "BULLISH TREND"
        else "SHORT"
    )

    for zone in zones:
        if zone["type"] != desired:
            continue

        if not (zone["low"] <= price <= zone["high"]):
            continue

        if not structure_shift(rows, direction):
            continue

        plan = select_trade_plan(
            zone,
            direction,
            previous_day,
            sessions,
            atr,
        )

        if not plan:
            continue

        setup_key = (
            f"{direction}|"
            f"{zone['origin_dt'].isoformat()}|"
            f"{zone['impulse_dt'].isoformat()}"
        )

        already = con.execute(
            """
            SELECT 1 FROM market_idea_alerts
            WHERE market_day=? AND setup_key=?
            """,
            (str(day), setup_key),
        ).fetchone()

        if already:
            continue

        telegram_send(
            market_idea_message(
                direction,
                "A+",
                trend,
                zone,
                plan,
            )
        )

        con.execute(
            "INSERT INTO market_idea_alerts VALUES (?, ?, ?)",
            (
                str(day),
                setup_key,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        con.commit()

        return 1

    return 0

def main():
    con = db_connect()
    now = datetime.now(TZ)
    today = now.date()

    # Weekend: do absolutely nothing.
    if today.weekday() >= 5:
        print(
            f"Weekend {today.isoformat()} — bot is inactive."
        )
        return

    # --------------------------------------------------------
    # Determine previous trading day.
    # Monday -> Friday.
    # --------------------------------------------------------
    previous_day = previous_weekday(today)

    # --------------------------------------------------------
    # Load EXACT previous trading day window:
    # previous_day-1 at 23:00 -> previous_day at 23:00.
    # This is the user's definition of Previous Day.
    # --------------------------------------------------------
    previous_day_data, previous_rows = daily_range_for_label(
        previous_day
    )

    if not previous_day_data:
        print(
            f"No intraday data for Previous Day {previous_day}."
        )
        return

    # --------------------------------------------------------
    # Current-day sessions are completely independent.
    # We request current calendar day's 5m data only.
    # --------------------------------------------------------
    today_start = datetime.combine(
        today,
        time(0, 0),
        tzinfo=TZ,
    )
    today_end = now

    today_rows = load_5m_window(today_start, today_end)

    sessions = all_sessions_for_day(today_rows, today)

    # --------------------------------------------------------
    # Daily report:
    #
    # After 23:00, report the just-completed trading day itself.
    # Before 23:00, recovery report is for the previous trading day.
    # This means Monday can recover Friday's report if needed.
    # --------------------------------------------------------
    if now.hour >= 23:
        report_day = today
        report_data, _ = daily_range_for_label(report_day)
        report_sessions = all_sessions_for_day(today_rows, today)
    else:
        report_day = previous_day
        report_data = previous_day_data

        previous_start, previous_end = trading_day_window(
            report_day
        )

        report_rows, _unused = daily_range_for_label(report_day)
        report_sessions = {
            name: session_range_for_day(
                previous_rows,
                report_day,
                name,
            )
            for name in ("ASIA", "LONDON", "NEW YORK")
        }

    # The report status is based only on the completed report-day close
    # relative to the High/Low of the trading day immediately before it.
    report_previous_day = previous_weekday(report_day)
    if report_previous_day == previous_day:
        report_previous_day_data = previous_day_data
    else:
        report_previous_day_data, _ = daily_range_for_label(
            report_previous_day
        )

    if report_data and report_previous_day_data:
        trend, reason = trend_from_previous_day_close(
            report_data["close"],
            report_previous_day_data,
        )
    else:
        trend, reason = "INSIDE DAY", "Previous Day range unavailable"

    daily_sent = False

    if report_data:
        daily_sent = send_daily_report(
            con,
            report_day,
            report_data,
            report_sessions,
            trend,
            reason,
        )

    # --------------------------------------------------------
    # Current price.
    # Use the latest current-day 5m close.
    # --------------------------------------------------------
    current_price = (
        today_rows[-1]["close"] if today_rows else None
    )

    # Intraday market-idea direction follows the same Previous Day High/Low
    # rule, using the latest completed M5 close.  There is no equilibrium or
    # multi-day-structure fallback.
    live_candle5 = latest_completed_5m_candle(today_rows, now)
    if live_candle5:
        live_trend, _ = trend_from_previous_day_close(
            live_candle5["close"],
            previous_day_data,
        )
    else:
        live_trend = "INSIDE DAY"

    eq_alerts = 0
    breakout_alerts = 0
    previous_day_breakouts = 0

    if current_price is not None:
        # Equilibrium = Previous Trading Day 50%.
        eq_alerts = check_equilibrium_zone(
            con,
            previous_day,
            previous_day_data["equilibrium"],
            current_price,
        )

        # Session breakouts:
        # London -> today's Asia
        # New York -> today's London
        previous_price_row = con.execute(
            "SELECT last_price FROM session_breakout_state WHERE market_day=?",
            (str(today),),
        ).fetchone()

        previous_price = (
            float(previous_price_row[0])
            if previous_price_row
            else None
        )

        breakout_alerts = check_session_breakouts(
            con,
            today,
            sessions,
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

        # Previous Day HIGH/LOW breakout -> latest completed M5 close.
        previous_day_breakouts = previous_day_breakout_alerts(
            con,
            previous_day,
            previous_day_data,
            live_candle5,
        )

    # --------------------------------------------------------
    # MT MARKET IDEA
    # Previous Day High/Low direction -> Order Block -> Structure Shift -> R:R >= 1:2
    # --------------------------------------------------------
    market_ideas = maybe_market_idea(
        con,
        today,
        today_rows,
        previous_day_data,
        sessions,
        live_trend,
    )

    session_count = sum(
        1
        for v in sessions.values()
        if v is not None
    )

    print(
        f"Previous trading day: {previous_day}. "
        f"Previous Day HIGH: {fmt_price(previous_day_data['high'])}. "
        f"Previous Day LOW: {fmt_price(previous_day_data['low'])}. "
        f"Equilibrium: {fmt_price(previous_day_data['equilibrium'])}. "
        f"Current date: {today}. "
        f"Report day: {report_day}. "
        f"Daily report sent: {daily_sent}. "
        f"Sessions calculated for {today}: {session_count}/3. "
        f"Equilibrium alerts: {eq_alerts}. "
        f"Session breakout alerts: {breakout_alerts}. "
        f"Previous-day breakout alerts: {previous_day_breakouts}. "
        f"Market ideas: {market_ideas}. "
        f"Latest XAU/USD: "
        f"{fmt_price(current_price) if current_price is not None else 'N/A'}."
    )


if __name__ == "__main__":
    main()
