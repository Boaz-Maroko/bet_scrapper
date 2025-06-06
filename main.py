import logging
import asyncio
import aiohttp
from fastapi import FastAPI, HTTPException
import uvicorn
from datetime import datetime, timezone, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Configuration (with API keys inline as requested)
TOKEN = "7598759444:AAHdQzzORYT06ZM-JBduzmfEqTVFoLtjCBg"
WEBHOOK_URL = "https://bet-scrapper-78du.onrender.com/webhook"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = 10000

# API Configuration
BANGBET_API_URL = "https://bet-api.bangbet.com/api/bet/match/list"
BANGBET_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0",
    "Origin": "https://www.bangbet.com",
    "Referer": "https://www.bangbet.com/",
}
BANGBET_PAYLOAD = {
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

# Global state
active_chats = set()
tracked_matches = {}
bot_application = None

# Initialize logging
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# FastAPI app
http_app = FastAPI()

# Helper functions
def format_odds_change_message(prev_odds, new_odds, home, away, tournament):
    labels = ["Home Win", "Draw", "Away Win"]
    changes = []
    for i, (prev, new) in enumerate(zip(prev_odds, new_odds)):
        arrow = "⬆️" if new > prev else "⬇️" if new < prev else "➡️"
        changes.append(f"{labels[i]}: {prev} {arrow} {new}")
    changes_string = "\n".join(changes)
    return (
        f"⚽️ Odds Update: {home} vs {away}\n"
        f"🏆 Tournament: {tournament}\n"
        f"{changes_string}"
    )

def format_over_under_changes(prev_ou, new_ou):
    changes = []
    for total in sorted(new_ou.keys()):
        new = new_ou.get(total)
        prev = prev_ou.get(total) if prev_ou else None
        if not prev or prev != new:
            arrows = [
                "🆕" if not prev else 
                "⬆️" if new[i] > prev[i] else 
                "⬇️" if new[i] < prev[i] else "➡️" 
                for i in range(2)
            ]
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

        async with session.post(ou_url, headers=BANGBET_HEADERS, json=payload) as response:
            if response.status != 200:
                logger.warning(f"Failed to fetch O/U odds (status {response.status})")
                return {}

            data = await response.json()
            ou_odds = {}
            for market in data["data"]["marketList"]:
                if market["name"] == "Over/Under":
                    for sub_market in market["markets"]:
                        if spec := sub_market.get("specifiers", "").startswith("total="):
                            try:
                                total = float(spec.split("=")[1].replace("&", ""))
                                if 0.5 <= total <= 5.5 and len(sub_market.get("outcomes", [])) == 2:
                                    ou_odds[total] = (sub_market["outcomes"][0]["odds"], 
                                                     sub_market["outcomes"][1]["odds"])
                            except Exception as e:
                                logger.error(f"Error parsing specifier: {e}")
            return ou_odds
    except Exception as e:
        logger.error(f"Exception fetching O/U odds: {e}")
        return {}

async def fetch_all_matches_async():
    page, matches_data = 1, {}
    async with aiohttp.ClientSession() as session:
        while True:
            logger.info(f"Fetching match list (page {page})")
            payload = BANGBET_PAYLOAD.copy()
            payload.update({"page": page, "pageNo": page})

            async with session.post(BANGBET_API_URL, headers=BANGBET_HEADERS, json=payload) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch matches (status {response.status})")
                    break

                try:
                    data = await response.json()
                    if not (matches := data["data"]["data"]):
                        logger.info("No more matches found")
                        break

                    EAT = timezone(timedelta(hours=3))
                    now = datetime.now(EAT)
                    
                    for match in matches:
                        try:
                            if not (scheduled_time_ms := match.get("scheduledTime")):
                                continue
                                
                            scheduled_datetime = datetime.fromtimestamp(scheduled_time_ms / 1000, tz=EAT)
                            if scheduled_datetime.date() != now.date():
                                continue

                            if not (one_x_two := next(
                                (m for group in match.get("marketList", []) 
                                 for m in group.get("markets", []) 
                                 if m.get("name") == "1x2"), None)):
                                continue

                            if len(one_x_two.get("outcomes", [])) < 3:
                                continue

                            home, draw, away = (one_x_two["outcomes"][i]["odds"] for i in range(3))
                            if None in (home, draw, away):
                                continue

                            matches_data[match["id"]] = {
                                "tournament": match.get("tournamentName", "Unknown"),
                                "home": match.get("homeTeamName", "Home"),
                                "away": match.get("awayTeamName", "Away"),
                                "lastUpdateTime": match.get("lastUpdateTime", 0),
                                "odds": (home, draw, away)
                            }
                        except KeyError as e:
                            logger.error(f"KeyError processing match: {e}")
                            continue

                    page += 1
                except Exception as e:
                    logger.error(f"Error parsing match data: {e}")
                    break

    return matches_data

async def monitor_changes_telegram(bot, chat_id, interval=60):
    global tracked_matches
    first_run = True
    
    async with aiohttp.ClientSession() as session:
        while chat_id in active_chats:
            try:
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                current_matches = await fetch_all_matches_async()
                
                if first_run:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"[{current_time}] Monitoring started. Tracking {len(current_matches)} matches."
                    )
                    first_run = False

                messages = []
                for match_id, info in current_matches.items():
                    info["ou_odds"] = await fetch_over_under_odds(session, match_id)
                    
                    if (prev := tracked_matches.get(match_id)) and (
                        info["odds"] != prev["odds"] or 
                        info["ou_odds"] != prev.get("ou_odds")
                    ):
                        msg = format_odds_change_message(
                            prev["odds"], info["odds"],
                            info["home"], info["away"], info["tournament"]
                        )
                        
                        if ou_change := format_over_under_changes(prev.get("ou_odds", {}), info["ou_odds"]):
                            msg += f"\n\n📊 Over/Under Odds:\n{ou_change}"
                        
                        messages.append(msg)

                    tracked_matches[match_id] = info

                if not messages and not first_run:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"[{current_time}] No odds changed."
                    )
                else:
                    for msg in messages:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=f"[{current_time}]\n{msg}"
                        )

                await asyncio.sleep(interval)
            except Exception as e:
                logger.error(f"Monitoring error: {e}")
                await asyncio.sleep(interval)

# Telegram command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_chats:
        await context.bot.send_message(chat_id, "Odds are already being tracked")
        return
    
    active_chats.add(chat_id)
    await context.bot.send_message(chat_id, "Starting Odds monitor...")
    asyncio.create_task(monitor_changes_telegram(context.bot, chat_id))

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_chats:
        active_chats.remove(chat_id)
        await context.bot.send_message(chat_id, "Updates stopped")
    else:
        await context.bot.send_message(chat_id, "No active tracking")

# Bot initialization
async def initialize_bot():
    global bot_application
    
    bot_application = (
        ApplicationBuilder()
        .token(TOKEN)
        .build()
    )
    
    # Register handlers
    bot_application.add_handler(CommandHandler("start", start))
    bot_application.add_handler(CommandHandler("stop", stop))
    
    # Set webhook
    async with aiohttp.ClientSession() as session:
        url = f"https://api.telegram.org/bot{TOKEN}/setWebhook?url={WEBHOOK_URL}"
        async with session.get(url) as response:
            result = await response.json()
            logger.info(f"Webhook setup result: {result}")
            if not result.get('ok'):
                raise RuntimeError(f"Failed to set webhook: {result.get('description')}")

# FastAPI endpoints
@http_app.on_event("startup")
async def startup_event():
    """Initialize the bot when FastAPI starts"""
    await initialize_bot()
    logger.info("Bot initialization complete")

@http_app.get("/")
async def health_check():
    return {
        "status": "running",
        "active_chats": len(active_chats),
        "tracked_matches": len(tracked_matches),
        "bot_initialized": bot_application is not None
    }

@http_app.post("/webhook")
async def handle_webhook(update: dict):
    """Handle incoming Telegram updates"""
    if not bot_application:
        logger.error("Bot application not initialized!")
        raise HTTPException(status_code=503, detail="Service not ready")
    
    try:
        telegram_update = Update.de_json(update, bot_application.bot)
        await bot_application.process_update(telegram_update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error processing update: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    # Start the server
    uvicorn.run(
        http_app,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
        loop="asyncio",
        reload=False
    )