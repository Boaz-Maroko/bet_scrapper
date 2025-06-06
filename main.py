import logging
import asyncio
import aiohttp
from uuid import uuid4
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, InlineQueryHandler

# Telegram bot token
TOKEN = "7598759444:AAHdQzzORYT06ZM-JBduzmfEqTVFoLtjCBg"
run = True

# Setup logging
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO 
)
logger = logging.getLogger(__name__)

# BangBet API setup
url = "https://bet-api.bangbet.com/api/bet/match/list"
headers = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://www.bangbet.com",
    "Referer": "https://www.bangbet.com/",
}

base_payload = {
    "sportId": "sr:sport:1",
    "groupIndex": "0",
    "producer": 3,
    "position": 17,
    "highLight": True,
    "showMarket": True,
    "timeZone": "+3",
    "sortType": 1,
    "pageSize": 30,
    "country": "ke",
    "specialMarketMatch": False,
    "isMyTeam": False,
    "dataGroup": False
}

tracked_matches = {}  # match_id -> dict


def format_odds_change_message(prev_odds, new_odds, home, away, tournament):
    labels = ["Home Win", "Draw", "Away Win"]
    changes = []
    for i, (prev, new) in enumerate(zip(prev_odds, new_odds)):
        if new > prev:
            arrow = "⬆️"
        elif new < prev:
            arrow = "⬇️"
        else:
            arrow = "➡️"
        changes.append(f"{labels[i]}: {prev} {arrow} {new}")

    changes_str = '\n'.join(changes)
    return (
        f"⚽️ Odds Update: {home} vs {away}\n"
        f"🏆 Tournament: {tournament}\n"
        f"{changes_str}"
    )


def format_over_under_changes(prev_ou, new_ou):
    changes = []
    for total in sorted(new_ou.keys()):
        new = new_ou.get(total)
        prev = prev_ou.get(total) if prev_ou else None
        if not prev or prev != new:
            arrows = []
            for i in range(2):
                if not prev:
                    arrow = "🆕"
                elif new[i] > prev[i]:
                    arrow = "⬆️"
                elif new[i] < prev[i]:
                    arrow = "⬇️"
                else:
                    arrow = "➡️"
                arrows.append(arrow)
            changes.append(f"O/U {total}: Over {new[0]} {arrows[0]} | Under {new[1]} {arrows[1]}")
    return "\n".join(changes)


async def fetch_over_under_odds(session, match_id):
    try:
        logger.info(f"Fetching Over/Under odds for match {match_id}")
        ou_url = "https://bet-api.bangbet.com/api/bet/match/odds"
        payload = {
            "sportId": "sr:sport:1",
            "matchId": match_id,
            "producer": 3,
            "position": 16,
            "country": "ke"
        }

        async with session.post(ou_url, headers=headers, json=payload) as response:
            if response.status != 200:
                logger.warning(f"Failed to fetch O/U odds for match {match_id} (status {response.status})")
                return {}

            data = await response.json()
            markets = data["data"]["marketList"]

            ou_odds = {}
            for market in markets:
                if market["name"] == "Over/Under":
                    for sub_market in market["markets"]:
                        spec = sub_market.get("specifiers", "")
                        if spec.startswith("total="):
                            try:
                                total = float(spec.split("=")[1].replace("&", ""))
                                if 0.5 <= total <= 5.5:
                                    outcomes = sub_market.get("outcomes", [])
                                    if len(outcomes) == 2:
                                        over = outcomes[0]["odds"]
                                        under = outcomes[1]["odds"]
                                        ou_odds[total] = (over, under)
                            except Exception as e:
                                logger.error(f"Failed to parse total in specifier: {spec} — {e}")
            return ou_odds
    except Exception as e:
        logger.exception(f"Exception while fetching O/U odds for match {match_id}")
        return {}


async def fetch_all_matches_async():
    page = 1
    matches_data = {}

    async with aiohttp.ClientSession() as session:
        while True:
            logger.info(f"Fetching match list (page {page})")
            payload = base_payload.copy()
            payload["page"] = page
            payload["pageNo"] = page

            async with session.post(url, headers=headers, json=payload) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch match list (status {response.status})")
                    break

                try:
                    data = await response.json()
                    matches = data["data"]["data"]
                except Exception as e:
                    logger.error(f"Failed to parse match list JSON: {e}")
                    break

                if not matches:
                    logger.info("No more matches found.")
                    break

                for match in matches:
                    try:
                        match_id = match["id"]
                        last_update = match.get("lastUpdateTime", 0)
                        scheduled_time_ms = match.get("scheduledTime")
                        if not scheduled_time_ms:
                            continue  # skip if missing

                        EAT = timezone(timedelta(hours=3))
                        now = datetime.now(EAT)
                        scheduled_datetime = datetime.fromtimestamp(scheduled_time_ms / 1000, tz=EAT)
                        if scheduled_datetime.date() != now.date():
                            continue  # skip matches not scheduled for today
                        tournament = match.get("tournamentName", "Unknown Tournament")
                        home = match.get("homeTeamName", "Home")
                        away = match.get("awayTeamName", "Away")

                        market_list = match.get("marketList", [])
                        one_x_two_market = None
                        for group in market_list:
                            for market in group.get("markets", []):
                                if market.get("name") == "1x2":
                                    one_x_two_market = market
                                    break
                            if one_x_two_market:
                                break

                        if not one_x_two_market:
                            logger.debug(f"Skipping match {match_id}: No 1X2 market")
                            continue

                        outcomes = one_x_two_market.get("outcomes", [])
                        if len(outcomes) < 3:
                            logger.debug(f"Skipping match {match_id}: Incomplete 1X2 odds")
                            continue

                        home_odds = outcomes[0].get("odds")
                        draw_odds = outcomes[1].get("odds")
                        away_odds = outcomes[2].get("odds")

                        if None in (home_odds, draw_odds, away_odds):
                            logger.debug(f"Skipping match {match_id}: Missing odds")
                            continue

                        matches_data[match_id] = {
                            "tournament": tournament,
                            "home": home,
                            "away": away,
                            "lastUpdateTime": last_update,
                            "odds": (home_odds, draw_odds, away_odds)
                        }
                    except KeyError as e:
                        logger.error(f"KeyError processing match: {e}")
                        continue

                page += 1

    return matches_data


async def monitor_changes_telegram(bot, chat_id, interval_seconds=60):
    global tracked_matches
    first_run = True
    async with aiohttp.ClientSession() as session:
        global run
        while run:
            try:
                current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"[{current_time_str}] Polling BangBet matches...")
                current_matches = await fetch_all_matches_async()
                logger.info(f"Fetched {len(current_matches)} matches")

                changes_found = False
                messages = []

                if first_run:
                    await bot.send_message(chat_id=chat_id, text=f"[{current_time_str}] Monitoring started. Tracking {len(current_matches)} matches.")
                    # Optionally, send a short summary or the first odds snapshot here.
                    first_run = False

                for match_id, info in current_matches.items():
                    ou_odds = await fetch_over_under_odds(session, match_id)
                    info["ou_odds"] = ou_odds

                    prev = tracked_matches.get(match_id)
                    if prev:
                        if info["lastUpdateTime"] == prev["lastUpdateTime"] and info["odds"] == prev["odds"] and info["ou_odds"] == prev["ou_odds"]:
                            continue

                        if info["odds"] != prev["odds"] or info["ou_odds"] != prev.get("ou_odds"):
                            changes_found = True
                            msg = format_odds_change_message(
                                prev["odds"], info["odds"],
                                info["home"], info["away"], info["tournament"]
                            )

                            ou_change = format_over_under_changes(prev.get("ou_odds", {}), info["ou_odds"])
                            if ou_change:
                                msg += "\n\n📊 Over/Under Odds:\n" + ou_change

                            messages.append(msg)

                    tracked_matches[match_id] = info

                if not changes_found and not first_run:
                    try:
                        await bot.send_message(chat_id=chat_id, text=f"[{current_time_str}] No odds changed.")
                        logger.info(f"[{current_time_str}] No odds changed message sent.")
                    except Exception as e:
                        logger.error(f"Failed to send 'no odds changed' message: {e}")
                else:
                    for msg in messages:
                        try:
                            await bot.send_message(chat_id=chat_id, text=f"[{current_time_str}]\n{msg}")
                            logger.info("Sent odds change message.")
                        except Exception as e:
                            logger.error(f"Failed to send odds change message: {e}")

                await asyncio.sleep(interval_seconds)
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")
                await asyncio.sleep(interval_seconds)


# Telegram Handlers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global run
    run = True
    chat_id = update.effective_chat.id
    logger.info(f"/start command received in chat {chat_id}")
    await context.bot.send_message(chat_id=chat_id, text="Starting Odds monitor...")
    context.application.create_task(monitor_changes_telegram(context.bot, chat_id))

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global run
    run = False
    await context.bot.send_message(update.effective_chat.id, "Stopped sending updates")



async def inline_caps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.inline_query.query
    if not query:
        return

    results = [
        InlineQueryResultArticle(
            id=str(uuid4()),
            title='Caps',
            input_message_content=InputTextMessageContent(query.upper())
        )
    ]
    await context.bot.answer_inline_query(inline_query_id=update.inline_query.id, results=results)


if __name__ == "__main__":
    logger.info("Bot is starting...")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('stop', stop))
    app.add_handler(InlineQueryHandler(inline_caps))
    app.run_polling(5)
