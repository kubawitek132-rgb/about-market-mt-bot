# ABOUT MARKET MT BOT — XAUUSD V1

Purpose:
- Report the completed XAUUSD daily High and Low at the 23:00 Europe/Warsaw boundary.
- Calculate exact 50% Equilibrium.
- Classify the multi-day market structure as BULLISH, BEARISH or NEUTRAL.
- During the following day, monitor the previous day's High, Low and Equilibrium.
- Send a key-level update only after a completed 15-minute candle confirms the move.
- Send an Equilibrium Zone alert when the latest observed price enters the band from Equilibrium - $1.00 to Equilibrium.

Daily boundary:
23:00 Europe/Warsaw.

Data:
Yahoo Finance chart feed using XAUUSD=X as a spot-gold reference.

Telegram secrets:
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TELEGRAM_MESSAGE_THREAD_ID

Important:
This bot is a market-structure/reference tool, not a trading signal or financial advice.

Deploy:
1. Create the bot and add it as administrator to the ABOUT MARKET MT BOT topic.
2. Create the three Telegram secrets in GitHub.
3. Replace bot.py.
4. Add requirements.txt.
5. Replace/add .github/workflows/about_market.yml.
6. Run the workflow manually once and inspect the log.

Equilibrium zone alerts are based on the latest price observed by the 5-minute GitHub Actions polling cycle; they cannot guarantee tick-by-tick detection.


## Session High / Low

The daily report also includes session ranges using Europe/Warsaw time:

- ASIA: 01:00–08:00
- LONDON: 09:00–13:00
- NEW YORK: 14:00–18:00

The end time is exclusive. These session ranges are calculated from the
same XAUUSD 5-minute reference data used by the bot.


## Intraday Session Breakouts

- **London 09:00–13:00 Europe/Warsaw:** alerts when price crosses above the Asia High or below the Asia Low.
- **New York 14:00–18:00 Europe/Warsaw:** alerts when price crosses above the London High or below the London Low.

The alert is based on a price crossing between two polling observations.
Each direction is alerted at most once per current session/day.

The workflow runs every 5 minutes, so this is polling-based and cannot guarantee
capture of a very brief break-and-reversal between workflow runs.
