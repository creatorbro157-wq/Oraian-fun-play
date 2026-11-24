import asyncio
import logging
import os
import random
import threading
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from flask import Flask
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from pymongo import DESCENDING, MongoClient
    from pymongo.errors import PyMongoError
except ImportError:  # pragma: no cover
    MongoClient = None
    DESCENDING = -1
    PyMongoError = Exception


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==============================================================================
# CONFIGURATION & TIMERS (2x applied where agreed; Daily kept at 24h)
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
MONGO_URI = os.getenv("MONGO_URI", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
PORT = int(os.getenv("PORT", "8080") or "8080")

STARTING_COINS = 1000
MIN_BET = 10
MAX_BET = 50000

DAILY_REWARD = 2000
DAILY_COOLDOWN_SEC = 24 * 3600  # 24 hours (unchanged)

REFILL_AMOUNT = 250
REFILL_COOLDOWN_SEC = 30 * 60  # 30 minutes

ROUND_SECONDS = 20  # 2x from 10s
TTT_JOIN_SECONDS = 12  # 2x from 6s
TTT_TURN_SECONDS = 20  # 2x from 10s
AUTO_DELETE_RESULT_SEC = 70  # 2x from 35s

AVIATOR_STEP_SEC = 0.45
AVIATOR_STEP_INC = 0.2
AVIATOR_MIN_MULTIPLIER = 1.0
AVIATOR_MAX_MULTIPLIER = 7.0

MINES_BOMB_CHOICES = (2, 4, 5, 7, 9)
TTT_MARKS = ("❌", "⭕", "🔷", "🔶", "⭐", "🍀")
TTT_MAX_PLAYERS = 6


# ==============================================================================
# FLASK HEALTH SERVER (Render / UptimeRobot friendly)
# ==============================================================================
health_app = Flask(__name__)


@health_app.route("/")
@health_app.route("/health")
def health_check() -> Tuple[str, int]:
    return "OK - Bot is running", 200


def run_health_server() -> None:
    health_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ==============================================================================
# BUTTON STYLING & KEYBOARD HELPERS
# ==============================================================================
def btn(text: str, callback_data: str, style: Optional[str] = None) -> InlineKeyboardButton:
    kwargs: Dict[str, Any] = {}
    if style in ("primary", "success", "danger"):
        kwargs["api_kwargs"] = {"style": style}
    return InlineKeyboardButton(text=text, callback_data=callback_data, **kwargs)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                btn("🚀 Aviator", "menu_aviator", "primary"),
                btn("💣 Mines", "menu_mines", "danger"),
                btn("❎ 5x5 TTT", "start_ttt5", "primary"),
            ],
            [
                btn("✊ R-P-S", "pick_game:rps", "primary"),
                btn("📈 Hi-Lo", "pick_game:hilo", "primary"),
                btn("🪙 Flip", "pick_game:flip", "primary"),
            ],
            [
                btn("🎲 Dice", "pick_game:dice", "primary"),
                btn("🎰 Slots", "pick_game:slots", "primary"),
                btn("🔢 Guess", "pick_game:guess", "primary"),
            ],
            [
                btn("🎡 Wheel", "pick_game:wheel", "primary"),
                btn("🏀 Sports", "pick_game:sports", "primary"),
                btn("🪙 Refill", "do_refill", "success"),
            ],
            [
                btn("🎁 Daily", "do_daily", "success"),
                btn("💰 Balance", "menu_bal", "success"),
                btn("🏆 Top", "menu_top", "primary"),
            ],
            [
                btn("👥 Group Top", "menu_gtop", "primary"),
                btn("❓ Help", "menu_help", None),
            ],
        ]
    )


def mines_bombs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                btn("2 💣", "mines_bombs:2", "danger"),
                btn("4 💣", "mines_bombs:4", "danger"),
                btn("5 💣", "mines_bombs:5", "danger"),
                btn("7 💣", "mines_bombs:7", "danger"),
                btn("9 💣", "mines_bombs:9", "danger"),
            ],
            [btn("⬅️ Main Menu", "back_menu", None)],
        ]
    )


def bet_amounts_keyboard(user_coins: int, prefix: str, extra_rows: Optional[List[List[InlineKeyboardButton]]] = None) -> InlineKeyboardMarkup:
    half_amt = max(MIN_BET, user_coins // 2)
    max_amt = max(MIN_BET, min(user_coins, MAX_BET))
    presets = [10, 50, 100, 250, 500, 1000]
    rows: List[List[InlineKeyboardButton]] = []
    row1: List[InlineKeyboardButton] = []
    for amt in presets[:3]:
        row1.append(btn(f"{amt}", f"{prefix}:{amt}", "success"))
    rows.append(row1)

    row2: List[InlineKeyboardButton] = []
    for amt in presets[3:]:
        row2.append(btn(f"{amt}", f"{prefix}:{amt}", "success"))
    rows.append(row2)

    rows.append(
        [
            btn(f"½ ({half_amt})", f"{prefix}:{half_amt}", "primary"),
            btn(f"Max ({max_amt})", f"{prefix}:{max_amt}", "danger"),
        ]
    )
    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(rows)


def game_choice_keyboard(game_key: str, round_id: str) -> InlineKeyboardMarkup:
    if game_key == "rps":
        return InlineKeyboardMarkup(
            [
                [
                    btn("✊ Rock", f"rchoice:{round_id}:rock", "primary"),
                    btn("✋ Paper", f"rchoice:{round_id}:paper", "primary"),
                    btn("✌️ Scissor", f"rchoice:{round_id}:scissor", "primary"),
                ]
            ]
        )
    if game_key == "hilo":
        return InlineKeyboardMarkup(
            [
                [
                    btn("⬆️ High (4–6)", f"rchoice:{round_id}:high", "success"),
                    btn("⬇️ Low (1–3)", f"rchoice:{round_id}:low", "danger"),
                ]
            ]
        )
    if game_key == "flip":
        return InlineKeyboardMarkup(
            [
                [
                    btn("🟡 Heads", f"rchoice:{round_id}:heads", "primary"),
                    btn("⚪ Tails", f"rchoice:{round_id}:tails", "primary"),
                ]
            ]
        )
    if game_key == "dice":
        return InlineKeyboardMarkup(
            [
                [
                    btn(f"🎲 {n}", f"rchoice:{round_id}:{n}", "primary")
                    for n in (1, 2, 3)
                ],
                [
                    btn(f"🎲 {n}", f"rchoice:{round_id}:{n}", "primary")
                    for n in (4, 5, 6)
                ],
            ]
        )
    if game_key == "slots":
        return InlineKeyboardMarkup(
            [
                [
                    btn("🍒 Cherry", f"rchoice:{round_id}:cherry", "primary"),
                    btn("💎 Gem", f"rchoice:{round_id}:gem", "success"),
                    btn("7️⃣ Jackpot", f"rchoice:{round_id}:seven", "danger"),
                ]
            ]
        )
    if game_key == "guess":
        return InlineKeyboardMarkup(
            [
                [btn(f"{n}", f"rchoice:{round_id}:{n}", "primary") for n in (1, 2, 3, 4, 5)],
                [btn(f"{n}", f"rchoice:{round_id}:{n}", "primary") for n in (6, 7, 8, 9, 10)],
            ]
        )
    if game_key == "wheel":
        return InlineKeyboardMarkup(
            [
                [
                    btn("🟢 2x", f"rchoice:{round_id}:2x", "success"),
                    btn("🔵 3x", f"rchoice:{round_id}:3x", "primary"),
                    btn("🔴 5x", f"rchoice:{round_id}:5x", "danger"),
                ]
            ]
        )
    # sports
    return InlineKeyboardMarkup(
        [
            [
                btn("🏀 Hoops", f"rchoice:{round_id}:hoops", "primary"),
                btn("⚽ Goal", f"rchoice:{round_id}:goal", "success"),
            ],
            [
                btn("🎯 Bullseye", f"rchoice:{round_id}:target", "danger"),
                btn("🎳 Strike", f"rchoice:{round_id}:strike", "primary"),
            ],
        ]
    )


# ==============================================================================
# STORAGE LAYER (MongoDB with safe in-memory fallback)
# ==============================================================================
class Database:
    def __init__(self, uri: str) -> None:
        self.use_mongo = False
        self.client: Optional[MongoClient] = None
        self.users_col = None
        self.gstats_col = None
        self.chats_col = None

        self.mem_users: Dict[int, Dict[str, Any]] = {}
        self.mem_gstats: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.mem_chats: Dict[int, Dict[str, Any]] = {}

        if uri and MongoClient is not None:
            try:
                self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
                self.client.admin.command("ping")
                db = self.client["telegram_game_bot"]
                self.users_col = db["users"]
                self.gstats_col = db["group_stats"]
                self.chats_col = db["chats"]
                self.users_col.create_index("user_id", unique=True)
                self.gstats_col.create_index([("chat_id", 1), ("user_id", 1)], unique=True)
                self.chats_col.create_index("chat_id", unique=True)
                self.use_mongo = True
                logger.info("Connected to MongoDB successfully.")
            except Exception as exc:
                logger.warning("MongoDB unavailable, using in-memory store: %s", exc)
                self.use_mongo = False

    def ensure_user(self, user_id: int, first_name: str, username: Optional[str]) -> Dict[str, Any]:
        safe_name = (first_name or "Player").strip()[:40]
        now = time.time()
        if self.use_mongo:
            try:
                doc = self.users_col.find_one({"user_id": user_id})
                if not doc:
                    doc = {
                        "user_id": user_id,
                        "first_name": safe_name,
                        "username": username or "",
                        "coins": STARTING_COINS,
                        "last_bet": 100,
                        "last_daily": 0.0,
                        "last_refill": 0.0,
                        "games_played": 0,
                        "games_won": 0,
                        "streak": 0,
                        "best_streak": 0,
                        "biggest_win": 0,
                        "created_at": now,
                    }
                    self.users_col.insert_one(doc)
                else:
                    self.users_col.update_one(
                        {"user_id": user_id},
                        {"$set": {"first_name": safe_name, "username": username or ""}},
                    )
                    doc["first_name"] = safe_name
                    doc["username"] = username or ""
                return doc
            except PyMongoError as exc:
                logger.error("Mongo ensure_user error: %s", exc)

        if user_id not in self.mem_users:
            self.mem_users[user_id] = {
                "user_id": user_id,
                "first_name": safe_name,
                "username": username or "",
                "coins": STARTING_COINS,
                "last_bet": 100,
                "last_daily": 0.0,
                "last_refill": 0.0,
                "games_played": 0,
                "games_won": 0,
                "streak": 0,
                "best_streak": 0,
                "biggest_win": 0,
                "created_at": now,
            }
        else:
            self.mem_users[user_id]["first_name"] = safe_name
            self.mem_users[user_id]["username"] = username or ""
        return self.mem_users[user_id]

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        if self.use_mongo:
            try:
                return self.users_col.find_one({"user_id": user_id})
            except PyMongoError as exc:
                logger.error("Mongo get_user error: %s", exc)
        return self.mem_users.get(user_id)

    def record_chat(self, chat_id: int, title: str, chat_type: str) -> None:
        safe_title = (title or "Chat").strip()[:60]
        if self.use_mongo:
            try:
                self.chats_col.update_one(
                    {"chat_id": chat_id},
                    {"$set": {"chat_id": chat_id, "title": safe_title, "type": chat_type}},
                    upsert=True,
                )
                return
            except PyMongoError as exc:
                logger.error("Mongo record_chat error: %s", exc)
        self.mem_chats[chat_id] = {"chat_id": chat_id, "title": safe_title, "type": chat_type}

    def record_last_bet(self, user_id: int, bet: int) -> None:
        clamped = max(MIN_BET, min(MAX_BET, int(bet)))
        if self.use_mongo:
            try:
                self.users_col.update_one({"user_id": user_id}, {"$set": {"last_bet": clamped}})
                return
            except PyMongoError:
                pass
        if user_id in self.mem_users:
            self.mem_users[user_id]["last_bet"] = clamped

    def adjust_coins(
        self,
        user_id: int,
        delta: int,
        chat_id: Optional[int] = None,
        name: Optional[str] = None,
        played_delta: int = 0,
        won_delta: int = 0,
        payout_amount: int = 0,
    ) -> Optional[int]:
        """Atomically adjusts coins; returns None if delta < 0 and balance would drop below 0."""
        user = self.get_user(user_id)
        if not user:
            return None
        current_coins = int(user.get("coins", 0))
        if delta < 0 and current_coins + delta < 0:
            return None

        new_coins = max(0, current_coins + delta)
        prev_streak = int(user.get("streak", 0))
        prev_best = int(user.get("best_streak", 0))
        prev_big = int(user.get("biggest_win", 0))

        if won_delta > 0:
            new_streak = prev_streak + 1
        elif played_delta > 0 and delta <= 0 and payout_amount <= 0:
            new_streak = 0
        else:
            new_streak = prev_streak

        new_best = max(prev_best, new_streak)
        new_big = max(prev_big, payout_amount)

        if self.use_mongo:
            try:
                res = self.users_col.find_one_and_update(
                    {"user_id": user_id, "coins": {"$gte": -delta if delta < 0 else 0}},
                    {
                        "$inc": {
                            "coins": delta,
                            "games_played": played_delta,
                            "games_won": won_delta,
                        },
                        "$set": {
                            "streak": new_streak,
                            "best_streak": new_best,
                            "biggest_win": new_big,
                        },
                    },
                    return_document=True,
                )
                if not res:
                    return None
                final_coins = int(res["coins"])
            except PyMongoError as exc:
                logger.error("Mongo adjust_coins error: %s", exc)
                return None
        else:
            user["coins"] = new_coins
            user["games_played"] = int(user.get("games_played", 0)) + played_delta
            user["games_won"] = int(user.get("games_won", 0)) + won_delta
            user["streak"] = new_streak
            user["best_streak"] = new_best
            user["biggest_win"] = new_big
            final_coins = new_coins

        if chat_id is not None and chat_id < 0:
            self._update_group_stats(
                chat_id=chat_id,
                user_id=user_id,
                name=name or user.get("first_name", "Player"),
                coins_delta=delta,
                played_delta=played_delta,
                won_delta=won_delta,
            )
        return final_coins

    def set_coins(self, user_id: int, amount: int, sync_realistic_stats: bool = False) -> int:
        safe_amt = max(0, int(amount))
        extra_set: Dict[str, Any] = {"coins": safe_amt}
        if sync_realistic_stats and safe_amt >= 1000:
            fake_wins = max(14, safe_amt // 250)
            fake_played = fake_wins + random.randint(5, 14)
            fake_streak = random.randint(3, 8)
            fake_big = max(500, safe_amt // 4)
            if self.use_mongo:
                try:
                    self.users_col.update_one(
                        {"user_id": user_id},
                        {
                            "$set": {"coins": safe_amt},
                            "$max": {
                                "games_played": fake_played,
                                "games_won": fake_wins,
                                "streak": fake_streak,
                                "best_streak": fake_streak,
                                "biggest_win": fake_big,
                            },
                        },
                    )
                    return safe_amt
                except PyMongoError:
                    pass
            if user_id in self.mem_users:
                doc = self.mem_users[user_id]
                doc["coins"] = safe_amt
                doc["games_played"] = max(int(doc.get("games_played", 0)), fake_played)
                doc["games_won"] = max(int(doc.get("games_won", 0)), fake_wins)
                doc["streak"] = max(int(doc.get("streak", 0)), fake_streak)
                doc["best_streak"] = max(int(doc.get("best_streak", 0)), fake_streak)
                doc["biggest_win"] = max(int(doc.get("biggest_win", 0)), fake_big)
                return safe_amt

        if self.use_mongo:
            try:
                self.users_col.update_one({"user_id": user_id}, {"$set": extra_set})
                return safe_amt
            except PyMongoError:
                pass
        if user_id in self.mem_users:
            self.mem_users[user_id].update(extra_set)
        return safe_amt

    def _update_group_stats(
        self,
        chat_id: int,
        user_id: int,
        name: str,
        coins_delta: int,
        played_delta: int,
        won_delta: int,
    ) -> None:
        safe_name = (name or "Player").strip()[:40]
        if self.use_mongo:
            try:
                self.gstats_col.update_one(
                    {"chat_id": chat_id, "user_id": user_id},
                    {
                        "$set": {"name": safe_name},
                        "$inc": {
                            "net_coins": coins_delta,
                            "played": played_delta,
                            "won": won_delta,
                        },
                    },
                    upsert=True,
                )
                return
            except PyMongoError:
                pass
        key = (chat_id, user_id)
        entry = self.mem_gstats.setdefault(
            key, {"chat_id": chat_id, "user_id": user_id, "name": safe_name, "net_coins": 0, "played": 0, "won": 0}
        )
        entry["name"] = safe_name
        entry["net_coins"] = int(entry.get("net_coins", 0)) + coins_delta
        entry["played"] = int(entry.get("played", 0)) + played_delta
        entry["won"] = int(entry.get("won", 0)) + won_delta

    def set_cooldown_timestamp(self, user_id: int, field: str, ts: float) -> None:
        if self.use_mongo:
            try:
                self.users_col.update_one({"user_id": user_id}, {"$set": {field: ts}})
                return
            except PyMongoError:
                pass
        if user_id in self.mem_users:
            self.mem_users[user_id][field] = ts

    def global_top(self, limit: int = 10) -> List[Dict[str, Any]]:
        if self.use_mongo:
            try:
                return list(self.users_col.find().sort("coins", DESCENDING).limit(limit))
            except PyMongoError:
                pass
        return sorted(self.mem_users.values(), key=lambda x: int(x.get("coins", 0)), reverse=True)[:limit]

    def group_top(self, chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        if self.use_mongo:
            try:
                return list(
                    self.gstats_col.find({"chat_id": chat_id}).sort("net_coins", DESCENDING).limit(limit)
                )
            except PyMongoError:
                pass
        items = [v for (cid, _), v in self.mem_gstats.items() if cid == chat_id]
        return sorted(items, key=lambda x: int(x.get("net_coins", 0)), reverse=True)[:limit]

    def stats_summary(self) -> Dict[str, int]:
        if self.use_mongo:
            try:
                users_count = self.users_col.count_documents({})
                chats_count = self.chats_col.count_documents({})
                pipeline = [{"$group": {"_id": None, "total": {"$sum": "$coins"}}}]
                agg = list(self.users_col.aggregate(pipeline))
                total_coins = int(agg[0]["total"]) if agg else 0
                return {"users": users_count, "chats": chats_count, "coins": total_coins}
            except PyMongoError:
                pass
        return {
            "users": len(self.mem_users),
            "chats": len(self.mem_chats),
            "coins": sum(int(u.get("coins", 0)) for u in self.mem_users.values()),
        }

    def all_chat_ids(self) -> List[int]:
        ids = set()
        if self.use_mongo:
            try:
                for doc in self.users_col.find({}, {"user_id": 1}):
                    ids.add(int(doc["user_id"]))
                for doc in self.chats_col.find({}, {"chat_id": 1}):
                    ids.add(int(doc["chat_id"]))
                return list(ids)
            except PyMongoError:
                pass
        ids.update(self.mem_users.keys())
        ids.update(self.mem_chats.keys())
        return list(ids)


db = Database(MONGO_URI)


# ==============================================================================
# ACTIVE GAME STATES
# ==============================================================================
# Solo bet flow per (chat_id, user_id): {"mode": "mines_bet" | "aviator_solo_bet", "bombs": int}
PENDING_SOLO_BET: Dict[Tuple[int, int], Dict[str, Any]] = {}

# Mines per (chat_id, user_id) -> independent board state
ACTIVE_MINES: Dict[Tuple[int, int], Dict[str, Any]] = {}

# Group 20s rounds per chat_id
# round_id -> {"chat_id": int, "game": str, "phase": "pick" | "bet" | "flight" | "done",
#              "choices": {uid: {"name": str, "choice": str}},
#              "bets": {uid: {"name": str, "choice": str, "bet": int}},
#              "message_id": int, "created_at": float}
ACTIVE_ROUNDS: Dict[int, Dict[str, Any]] = {}
ROUNDS_BY_ID: Dict[str, Dict[str, Any]] = {}

# 5x5 TicTacToe per chat_id -> supports 2..6 players (3-4+ smooth)
ACTIVE_TTT5: Dict[int, Dict[str, Any]] = {}


def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def fmt_duration(seconds: int) -> str:
    seconds = max(1, int(seconds))
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hrs > 0:
        return f"{hrs}h {mins}m"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def schedule_delete_messages(
    bot,
    chat_id: int,
    message_ids: List[int],
    delay: int = AUTO_DELETE_RESULT_SEC,
) -> None:
    valid_ids = [m for m in message_ids if isinstance(m, int) and m > 0]
    if not valid_ids:
        return

    async def _worker() -> None:
        await asyncio.sleep(max(1, delay))
        for mid in valid_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except (BadRequest, Forbidden, TelegramError):
                pass

    asyncio.create_task(_worker())


def help_text() -> str:
    return (
        "📖 <b>Game Guide (Simple &amp; Clear)</b>\n\n"
        "<b>🎮 Modes &amp; Players:</b>\n"
        "• <b>Aviator</b> (Single / Group Live Flight): <code>1.0x</code> se <code>0.2x</code> steps mein <code>7.0x</code> tak—crash se pehle <b>Cash Out</b> dabao. Group mein <code>20s</code> entry ke baad cash-out feed neeche dikhti hai.\n"
        "• <b>Mines</b> (Single / Group Parallel 2–4+): <code>2, 4, 5, 7, 9</code> bombs chuno, safe 💎 tiles kholo aur cash out karo. Group mein har player ka board alag hota hai.\n"
        "• <b>5x5 Tic Tac Toe</b> (Group Multiplayer 2–6 / 3–4+): <code>12s</code> join window, alag-alag marks, <code>20s</code> turn—zyada lines banane wala jeetega.\n"
        "• <b>R-P-S, Hi-Lo, Flip, Dice, Slots, Guess, Wheel &amp; Sports</b> (Solo DM / Group <code>20s</code> Round): Group mein <code>20s</code> round + sabki live bet lock list + end mein result.\n\n"
        "<b>🪙 Coins &amp; Commands:</b>\n"
        f"• <code>/daily</code> — Har <code>24h</code> mein <b>{DAILY_REWARD}</b> coins.\n"
        f"• <code>/refill</code> (ya <b>🪙 Refill</b>) — Har <code>30m</code> mein <b>{REFILL_AMOUNT}</b> coins.\n"
        "• <code>/bal</code>, <code>/give &lt;amt&gt;</code> (reply), <code>/gtop</code>, <code>/top</code>, <code>/ref</code>, <code>/mines</code>."
    )


# ==============================================================================
# COINS, DAILY & REFILL HELPERS
# ==============================================================================
async def claim_daily_logic(user_id: int, first_name: str, username: Optional[str]) -> Tuple[bool, str]:
    user = db.ensure_user(user_id, first_name, username)
    now = time.time()
    last_daily = float(user.get("last_daily", 0.0))
    elapsed = now - last_daily
    if elapsed < DAILY_COOLDOWN_SEC:
        rem = int(DAILY_COOLDOWN_SEC - elapsed)
        return False, f"⏳ Daily reward <b>{fmt_duration(rem)}</b> baad milega."

    db.set_cooldown_timestamp(user_id, "last_daily", now)
    new_bal = db.adjust_coins(user_id, DAILY_REWARD)
    bal_val = new_bal if new_bal is not None else int(user.get("coins", 0)) + DAILY_REWARD
    return True, (
        f"🎁 <b>Daily Claimed!</b> +{DAILY_REWARD} coins mile.\n"
        f"🪙 <b>Balance:</b> <code>{bal_val}</code>"
    )


async def claim_refill_logic(user_id: int, first_name: str, username: Optional[str]) -> Tuple[bool, str]:
    user = db.ensure_user(user_id, first_name, username)
    now = time.time()
    last_refill = float(user.get("last_refill", 0.0))
    elapsed = now - last_refill
    if elapsed < REFILL_COOLDOWN_SEC:
        rem = int(REFILL_COOLDOWN_SEC - elapsed)
        return False, f"⏳ Refill har <b>30m</b> mein hota hai. Agla refill <b>{fmt_duration(rem)}</b> baad."

    db.set_cooldown_timestamp(user_id, "last_refill", now)
    new_bal = db.adjust_coins(user_id, REFILL_AMOUNT)
    bal_val = new_bal if new_bal is not None else int(user.get("coins", 0)) + REFILL_AMOUNT
    return True, (
        f"🪙 <b>Refill Done!</b> +{REFILL_AMOUNT} coins mile.\n"
        f"🪙 <b>Balance:</b> <code>{bal_val}</code>"
    )


def format_balance_card(user_id: int, name: str, username: Optional[str]) -> str:
    u = db.ensure_user(user_id, name, username)
    coins = int(u.get("coins", 0))
    played = int(u.get("games_played", 0))
    won = int(u.get("games_won", 0))
    streak = int(u.get("streak", 0))
    best_streak = int(u.get("best_streak", 0))
    big_win = int(u.get("biggest_win", 0))
    return (
        f"👤 <b>{u.get('first_name', name)}</b>\n"
        f"🪙 <b>Balance:</b> <code>{coins}</code> coins\n"
        f"🎮 <b>Played / Won:</b> <code>{played}</code> / <code>{won}</code>\n"
        f"🔥 <b>Streak (Best):</b> <code>{streak}</code> (<code>{best_streak}</code>)\n"
        f"💎 <b>Biggest Win:</b> <code>{big_win}</code>"
    )


# ==============================================================================
# MINES GAME ENGINE (Independent per player; clean reset on restart)
# ==============================================================================
def mines_multiplier(revealed_count: int, mines_count: int) -> float:
    if revealed_count <= 0:
        return 1.0
    safe_total = 25 - mines_count
    if safe_total <= 0:
        return 1.0
    mult = 1.0
    for i in range(revealed_count):
        mult *= (25 - i) / max(1, safe_total - i)
    return round(max(1.05, mult * 0.96), 2)


def build_mines_keyboard(game: Dict[str, Any], reveal_all: bool = False, finished: bool = False) -> InlineKeyboardMarkup:
    mines_set = set(game["mines"])
    rev_set = set(game["revealed"])
    rows: List[List[InlineKeyboardButton]] = []

    for r in range(5):
        row_btns: List[InlineKeyboardButton] = []
        for c in range(5):
            idx = r * 5 + c
            if idx in rev_set:
                label = "💎"
                style = "success"
            elif reveal_all and idx in mines_set:
                label = "💣"
                style = "danger"
            elif reveal_all:
                label = "·"
                style = None
            else:
                label = "⬜"
                style = None
            row_btns.append(btn(label, f"mclick:{idx}", style))
        rows.append(row_btns)

    if not finished:
        mult = mines_multiplier(len(rev_set), game["mines_count"])
        payout = int(game["bet"] * mult)
        if len(rev_set) > 0:
            rows.append([btn(f"💰 Cash Out ({payout} | {mult}x)", "mcashout", "success")])
        else:
            rows.append([btn("💎 Safe Tile Chuno", "mnop", None)])

    return InlineKeyboardMarkup(rows)


async def start_mines_game(
    chat_id: int,
    user_id: int,
    name: str,
    username: Optional[str],
    bet: int,
    bombs: int,
    send_fn: Callable[..., Coroutine[Any, Any, Any]],
) -> None:
    if bombs not in MINES_BOMB_CHOICES:
        bombs = 4
    if bet < MIN_BET:
        await send_fn(f"⚠️ Minimum bet <b>{MIN_BET}</b> coins hai.", parse_mode=ParseMode.HTML)
        return

    db.ensure_user(user_id, name, username)
    game_key = (chat_id, user_id)

    # Clean any stale/old board for this user in this chat so it never gets stuck
    ACTIVE_MINES.pop(game_key, None)
    PENDING_SOLO_BET.pop(game_key, None)

    new_bal = db.adjust_coins(user_id, -bet, chat_id=chat_id, name=name, played_delta=1)
    if new_bal is None:
        await send_fn(
            "❌ Coins kam hain! Free coins ke liye <code>/daily</code> ya <code>/refill</code> chalao.",
            parse_mode=ParseMode.HTML,
        )
        return

    db.record_last_bet(user_id, bet)
    game = {
        "chat_id": chat_id,
        "user_id": user_id,
        "name": name,
        "bet": bet,
        "mines_count": bombs,
        "mines": random.sample(range(25), bombs),
        "revealed": [],
    }
    ACTIVE_MINES[game_key] = game
    await send_fn(
        f"💣 <b>Mines</b> · {bombs} mines · bet <code>{bet}</code> · <b>{name}</b>\n"
        f"Safe 💎 tiles kholo aur cash out karo.\n"
        f"🪙 <b>Balance:</b> <code>{new_bal}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=build_mines_keyboard(game, reveal_all=False, finished=False),
    )


# ==============================================================================
# 5x5 TIC TAC TOE ENGINE (Supports 2..6 players; ideal for 3-4+ players)
# ==============================================================================
def build_ttt5_keyboard(game: Dict[str, Any], finished: bool = False) -> InlineKeyboardMarkup:
    board: List[Optional[str]] = game["board"]
    rows: List[List[InlineKeyboardButton]] = []
    for r in range(5):
        row_btns: List[InlineKeyboardButton] = []
        for c in range(5):
            idx = r * 5 + c
            cell = board[idx]
            if cell is None:
                label = "⬜"
                style = None
            else:
                label = cell
                style = "primary"
            row_btns.append(btn(label, f"ttt5_move:{idx}", style))
        rows.append(row_btns)

    if not finished and game["status"] == "joining":
        rows.append([btn("➕ Join Match", "ttt5_join", "success")])

    return InlineKeyboardMarkup(rows)


def count_ttt5_lines(board: List[Optional[str]], mark: str) -> int:
    lines = 0
    # Horizontal & Vertical runs of >= 3
    for r in range(5):
        for c in range(3):
            if board[r * 5 + c] == mark and board[r * 5 + c + 1] == mark and board[r * 5 + c + 2] == mark:
                lines += 1
    for c in range(5):
        for r in range(3):
            if board[r * 5 + c] == mark and board[(r + 1) * 5 + c] == mark and board[(r + 2) * 5 + c] == mark:
                lines += 1
    # Diagonals of length >= 3
    for r in range(3):
        for c in range(3):
            if board[r * 5 + c] == mark and board[(r + 1) * 5 + c + 1] == mark and board[(r + 2) * 5 + c + 2] == mark:
                lines += 1
    for r in range(3):
        for c in range(2, 5):
            if board[r * 5 + c] == mark and board[(r + 1) * 5 + c - 1] == mark and board[(r + 2) * 5 + c - 2] == mark:
                lines += 1
    return lines


def format_ttt5_roster(game: Dict[str, Any]) -> str:
    players: List[Dict[str, Any]] = game["players"]
    if not players:
        return "👥 Players (0/6): Koi nahi"
    parts = [f"{p['mark']} <b>{p['name']}</b>" for p in players]
    return f"👥 Players ({len(players)}/{TTT_MAX_PLAYERS}): " + ", ".join(parts)


async def finish_ttt5_game(app: Application, chat_id: int, reason: str = "Board Full") -> None:
    game = ACTIVE_TTT5.pop(chat_id, None)
    if not game:
        return
    game["status"] = "done"
    players: List[Dict[str, Any]] = game["players"]
    board: List[Optional[str]] = game["board"]
    entry_bet = int(game["bet"])
    total_pot = entry_bet * len(players)

    scores: List[Tuple[int, Dict[str, Any]]] = []
    for p in players:
        pts = count_ttt5_lines(board, p["mark"])
        scores.append((pts, p))

    max_pts = max((s[0] for s in scores), default=0)
    winners = [p for pts, p in scores if pts == max_pts and len(players) > 0]

    lines_out = [
        f"🏁 <b>5x5 Tic Tac Toe Over</b> ({reason})",
        f"💰 <b>Pot:</b> <code>{total_pot}</code> coins",
        "",
    ]

    if winners:
        share = max(1, total_pot // len(winners))
        for p in winners:
            uid = p["user_id"]
            new_bal = db.adjust_coins(uid, share, chat_id=chat_id, name=p["name"], won_delta=1, payout_amount=share)
            lines_out.append(
                f"🏆 {p['mark']} <b>{p['name']}</b> jeeta! +{share} coins (Bal: <code>{new_bal}</code>)"
            )

    lines_out.append("")
    lines_out.append("📊 <b>Lines Score:</b>")
    for pts, p in sorted(scores, key=lambda x: x[0], reverse=True):
        lines_out.append(f"• {p['mark']} {p['name']}: <code>{pts}</code> lines")

    msg = await app.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines_out),
        parse_mode=ParseMode.HTML,
        reply_markup=build_ttt5_keyboard(game, finished=True),
    )
    schedule_delete_messages(app.bot, chat_id, [msg.message_id], AUTO_DELETE_RESULT_SEC)


async def ttt5_turn_watcher(app: Application, chat_id: int, expected_turn_num: int) -> None:
    await asyncio.sleep(TTT_TURN_SECONDS)
    game = ACTIVE_TTT5.get(chat_id)
    if not game or game.get("status") != "active" or game.get("turn_num") != expected_turn_num:
        return

    players = game["players"]
    if not players:
        return
    cur_player = players[game["turn_idx"] % len(players)]
    empty_indices = [i for i, val in enumerate(game["board"]) if val is None]
    if not empty_indices:
        await finish_ttt5_game(app, chat_id, "Board Full")
        return

    chosen_idx = random.choice(empty_indices)
    game["board"][chosen_idx] = cur_player["mark"]
    game["turn_idx"] = (game["turn_idx"] + 1) % len(players)
    game["turn_num"] = expected_turn_num + 1

    if all(x is not None for x in game["board"]):
        await finish_ttt5_game(app, chat_id, "Board Full")
        return

    next_p = players[game["turn_idx"] % len(players)]
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=game["message_id"],
            text=(
                f"❎ <b>5x5 Tic Tac Toe</b> (Auto-move by {cur_player['name']})\n"
                f"{format_ttt5_roster(game)}\n"
                f"🎯 Turn: {next_p['mark']} <b>{next_p['name']}</b> ({TTT_TURN_SECONDS}s)"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=build_ttt5_keyboard(game, finished=False),
        )
    except (BadRequest, TelegramError):
        pass

    asyncio.create_task(ttt5_turn_watcher(app, chat_id, game["turn_num"]))


async def ttt5_join_countdown(app: Application, chat_id: int, message_id: int) -> None:
    await asyncio.sleep(TTT_JOIN_SECONDS)
    game = ACTIVE_TTT5.get(chat_id)
    if not game or game.get("status") != "joining":
        return

    players = game["players"]
    if len(players) < 2:
        # Refund creator if not enough players joined
        ACTIVE_TTT5.pop(chat_id, None)
        if players:
            p0 = players[0]
            bal = db.adjust_coins(p0["user_id"], int(game["bet"]), chat_id=chat_id, name=p0["name"])
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"⚠️ Kam se kam 2 players chahiye the. Bet refund ho gayi (Bal: <code>{bal}</code>).",
                    parse_mode=ParseMode.HTML,
                )
            except (BadRequest, TelegramError):
                pass
        return

    game["status"] = "active"
    game["turn_idx"] = 0
    game["turn_num"] = 1
    first_p = players[0]
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"❎ <b>5x5 Tic Tac Toe Started!</b>\n"
                f"{format_ttt5_roster(game)}\n"
                f"🎯 Turn: {first_p['mark']} <b>{first_p['name']}</b> ({TTT_TURN_SECONDS}s)"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=build_ttt5_keyboard(game, finished=False),
        )
    except (BadRequest, TelegramError):
        pass

    asyncio.create_task(ttt5_turn_watcher(app, chat_id, 1))


# ==============================================================================
# SOLO GAMES ENGINE (Instant play in DM)
# ==============================================================================
async def resolve_solo_game(
    chat_id: int,
    user_id: int,
    name: str,
    username: Optional[str],
    game_key: str,
    choice: str,
    bet: int,
    send_fn: Callable[..., Coroutine[Any, Any, Any]],
) -> None:
    if bet < MIN_BET or bet > MAX_BET:
        await send_fn(f"⚠️ Bet <b>{MIN_BET}</b> se <b>{MAX_BET}</b> ke beech rakho.", parse_mode=ParseMode.HTML)
        return

    db.ensure_user(user_id, name, username)
    new_bal = db.adjust_coins(user_id, -bet, chat_id=chat_id, name=name, played_delta=1)
    if new_bal is None:
        await send_fn(
            "❌ Balance kam hai! <code>/daily</code> ya <code>/refill</code> use karo.",
            parse_mode=ParseMode.HTML,
        )
        return

    db.record_last_bet(user_id, bet)
    won = False
    mult = 0.0
    outcome_text = ""

    if game_key == "rps":
        bot_pick = random.choice(["rock", "paper", "scissor"])
        beats = {"rock": "scissor", "paper": "rock", "scissor": "paper"}
        if choice == bot_pick:
            mult = 1.0
            outcome_text = f"🤝 Draw ({bot_pick}) — bet wapas!"
        elif beats[choice] == bot_pick:
            won = True
            mult = 1.95
            outcome_text = f"🎉 Jeet gaye! Tum: <b>{choice}</b> vs Bot: <b>{bot_pick}</b>"
        else:
            outcome_text = f"💥 Haar gaye! Tum: <b>{choice}</b> vs Bot: <b>{bot_pick}</b>"
    elif game_key == "hilo":
        roll = random.randint(1, 6)
        won = (choice == "high" and roll >= 4) or (choice == "low" and roll <= 3)
        mult = 1.9 if won else 0.0
        outcome_text = f"{'🎉' if won else '💥'} Roll: <b>{roll}</b> ({choice.upper()})"
    elif game_key == "flip":
        res_side = random.choice(["heads", "tails"])
        won = choice == res_side
        mult = 1.95 if won else 0.0
        outcome_text = f"{'🎉' if won else '💥'} Coin: <b>{res_side.upper()}</b>"
    elif game_key == "dice":
        roll = random.randint(1, 6)
        won = str(roll) == choice
        mult = 5.2 if won else 0.0
        outcome_text = f"{'🎉' if won else '💥'} Dice: <b>{roll}</b> (Picked <b>{choice}</b>)"
    elif game_key == "slots":
        sym = random.choice(["cherry", "gem", "seven"])
        won = choice == sym
        mult_map = {"cherry": 2.5, "gem": 4.0, "seven": 7.0}
        mult = mult_map[sym] if won else 0.0
        outcome_text = f"{'🎉' if won else '💥'} Slots: <b>{sym.upper()}</b>"
    elif game_key == "guess":
        num = random.randint(1, 10)
        won = str(num) == choice
        mult = 8.5 if won else 0.0
        outcome_text = f"{'🎉' if won else '💥'} Number: <b>{num}</b> (Picked <b>{choice}</b>)"
    elif game_key == "wheel":
        pocket = random.choice(["2x", "2x", "3x", "5x", "bust"])
        won = choice == pocket
        mult = {"2x": 2.0, "3x": 3.0, "5x": 5.0}.get(choice, 0.0) if won else 0.0
        outcome_text = f"{'🎉' if won else '💥'} Wheel: <b>{pocket}</b>"
    else:  # sports
        sport_pick = random.choice(["hoops", "goal", "target", "strike"])
        won = choice == sport_pick
        mult = 3.5 if won else 0.0
        outcome_text = f"{'🎉' if won else '💥'} Shot: <b>{sport_pick.upper()}</b>"

    payout = int(round(bet * mult))
    if payout > 0:
        final_bal = db.adjust_coins(
            user_id,
            payout,
            chat_id=chat_id,
            name=name,
            won_delta=1 if won else 0,
            payout_amount=payout if won else 0,
        )
    else:
        final_bal = new_bal

    await send_fn(
        f"{outcome_text}\n"
        f"💰 <b>Bet:</b> <code>{bet}</code> · <b>Payout:</b> <code>{payout}</code>\n"
        f"🪙 <b>Balance:</b> <code>{final_bal}</code>",
        parse_mode=ParseMode.HTML,
    )


# ==============================================================================
# GROUP 20s ROUNDS + SHARED BET SLOT + AVIATOR LIVE CASH-OUT FEED
# ==============================================================================
def format_locked_bets_text(round_obj: Dict[str, Any]) -> str:
    bets: Dict[int, Dict[str, Any]] = round_obj.get("bets", {})
    if not bets:
        return "🔒 <i>Abhi kisi ne bet lock nahi ki hai.</i>"
    lines = ["🔒 <b>Locked Bets:</b>"]
    for item in bets.values():
        lines.append(f"• ✅ <b>{item['name']}</b> ne <code>{item['bet']}</code> coins par lock kar diya ({item['choice']})")
    return "\n".join(lines)


def format_aviator_cashout_feed(flight_obj: Dict[str, Any]) -> str:
    cashouts: List[Dict[str, Any]] = flight_obj.get("cashouts", [])
    if not cashouts:
        return "📡 <i>Cash-Out Feed: Abhi kisi ne cash out nahi kiya...</i>"
    lines = ["📡 <b>Live Cash-Out Feed:</b>"]
    for c in cashouts:
        lines.append(
            f"• ✅ <b>{c['name']}</b> ne <code>{c['mult']:.1f}x</code> par Cash Out kar liya! (<code>+{c['payout']}</code> coins)"
        )
    return "\n".join(lines)


async def run_aviator_group_flight(app: Application, chat_id: int, round_obj: Dict[str, Any]) -> None:
    bets: Dict[int, Dict[str, Any]] = round_obj.get("bets", {})
    round_obj["phase"] = "flight"
    msg_id = round_obj["message_id"]

    if not bets:
        ACTIVE_ROUNDS.pop(chat_id, None)
        ROUNDS_BY_ID.pop(round_obj["id"], None)
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="🚀 <b>Aviator</b> — 20s khatam, kisi player ne bet lock nahi ki.",
                parse_mode=ParseMode.HTML,
            )
        except (BadRequest, TelegramError):
            pass
        return

    # Pick crash point between 1.0x and 7.0x in 0.2x increments
    steps_total = int(round((AVIATOR_MAX_MULTIPLIER - AVIATOR_MIN_MULTIPLIER) / AVIATOR_STEP_INC))
    crash_step = random.randint(0, steps_total)
    crash_mult = round(AVIATOR_MIN_MULTIPLIER + crash_step * AVIATOR_STEP_INC, 1)

    flight_state = {
        "active": True,
        "mult": AVIATOR_MIN_MULTIPLIER,
        "players": {
            uid: {"name": info["name"], "bet": info["bet"], "cashed_out": False, "mult": 0.0, "payout": 0}
            for uid, info in bets.items()
        },
        "cashouts": [],
    }
    round_obj["flight"] = flight_state

    cur_mult = AVIATOR_MIN_MULTIPLIER
    while cur_mult <= crash_mult + 1e-9:
        flight_state["mult"] = round(cur_mult, 1)
        cashout_btn = InlineKeyboardMarkup(
            [[btn(f"💸 Cash Out ({cur_mult:.1f}x)", f"av_cash:{round_obj['id']}", "success")]]
        )
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=(
                    f"🚀 <b>Aviator Live:</b> <code>{cur_mult:.1f}x</code>\n"
                    f"⚡ Crash hone se pehle Cash Out dabao!\n\n"
                    f"{format_aviator_cashout_feed(flight_state)}"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=cashout_btn,
            )
        except (BadRequest, TelegramError):
            pass

        # If everyone has already cashed out, end early smoothly
        if all(p["cashed_out"] for p in flight_state["players"].values()):
            break

        await asyncio.sleep(AVIATOR_STEP_SEC)
        cur_mult = round(cur_mult + AVIATOR_STEP_INC, 1)

    flight_state["active"] = False
    ACTIVE_ROUNDS.pop(chat_id, None)
    ROUNDS_BY_ID.pop(round_obj["id"], None)

    summary_lines = [
        f"💥 <b>Aviator Crashed at <code>{crash_mult:.1f}x</code>!</b>",
        "",
        "📋 <b>Final Result (Sabka Ek Saath):</b>",
    ]
    for uid, pdata in flight_state["players"].items():
        if pdata["cashed_out"]:
            bal = db.get_user(uid)
            b_val = int(bal.get("coins", 0)) if bal else 0
            summary_lines.append(
                f"• ✅ <b>{pdata['name']}</b> — Bet <code>{pdata['bet']}</code> | Cash Out <code>{pdata['mult']:.1f}x</code> → <b>+{pdata['payout']}</b> (Bal: <code>{b_val}</code>)"
            )
        else:
            bal = db.get_user(uid)
            b_val = int(bal.get("coins", 0)) if bal else 0
            summary_lines.append(
                f"• 💥 <b>{pdata['name']}</b> — Bet <code>{pdata['bet']}</code> | Crash mein reh gaya → <b>-{pdata['bet']}</b> (Bal: <code>{b_val}</code>)"
            )

    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text="\n".join(summary_lines),
            parse_mode=ParseMode.HTML,
        )
        schedule_delete_messages(app.bot, chat_id, [msg_id], AUTO_DELETE_RESULT_SEC)
    except (BadRequest, TelegramError):
        pass


async def finalize_group_round(app: Application, chat_id: int, round_id: str) -> None:
    round_obj = ACTIVE_ROUNDS.get(chat_id)
    if not round_obj or round_obj.get("id") != round_id or round_obj.get("phase") == "done":
        return

    game_key = round_obj["game"]
    if game_key == "aviator":
        await run_aviator_group_flight(app, chat_id, round_obj)
        return

    round_obj["phase"] = "done"
    ACTIVE_ROUNDS.pop(chat_id, None)
    ROUNDS_BY_ID.pop(round_id, None)

    bets: Dict[int, Dict[str, Any]] = round_obj.get("bets", {})
    msg_id = round_obj["message_id"]

    if not bets:
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=f"⏱️ <b>{game_key.upper()}</b> — {ROUND_SECONDS}s round khatam, kisi ne bet lock nahi ki.",
                parse_mode=ParseMode.HTML,
            )
        except (BadRequest, TelegramError):
            pass
        return

    # Roll round outcome once for everyone in the round
    outcome_label = ""
    winning_choices: set = set()
    mult_map: Dict[str, float] = {}

    if game_key == "rps":
        bot_pick = random.choice(["rock", "paper", "scissor"])
        beats = {"rock": "scissor", "paper": "rock", "scissor": "paper"}
        outcome_label = f"✊ Bot ne <b>{bot_pick}</b> chuna"
        for c in ("rock", "paper", "scissor"):
            if c == bot_pick:
                mult_map[c] = 1.0
                winning_choices.add(c)
            elif beats[c] == bot_pick:
                mult_map[c] = 1.95
                winning_choices.add(c)
            else:
                mult_map[c] = 0.0
    elif game_key == "hilo":
        roll = random.randint(1, 6)
        win_side = "high" if roll >= 4 else "low"
        outcome_label = f"📈 Dice Roll: <b>{roll}</b> ({win_side.upper()})"
        winning_choices = {win_side}
        mult_map = {win_side: 1.9}
    elif game_key == "flip":
        side = random.choice(["heads", "tails"])
        outcome_label = f"🪙 Coin: <b>{side.upper()}</b>"
        winning_choices = {side}
        mult_map = {side: 1.95}
    elif game_key == "dice":
        roll = random.randint(1, 6)
        outcome_label = f"🎲 Dice: <b>{roll}</b>"
        winning_choices = {str(roll)}
        mult_map = {str(roll): 5.2}
    elif game_key == "slots":
        sym = random.choice(["cherry", "gem", "seven"])
        outcome_label = f"🎰 Slots: <b>{sym.upper()}</b>"
        winning_choices = {sym}
        mult_map = {"cherry": 2.5, "gem": 4.0, "seven": 7.0}
    elif game_key == "guess":
        num = random.randint(1, 10)
        outcome_label = f"🔢 Number: <b>{num}</b>"
        winning_choices = {str(num)}
        mult_map = {str(num): 8.5}
    elif game_key == "wheel":
        pocket = random.choice(["2x", "2x", "3x", "5x", "bust"])
        outcome_label = f"🎡 Wheel: <b>{pocket}</b>"
        if pocket != "bust":
            winning_choices = {pocket}
            mult_map = {"2x": 2.0, "3x": 3.0, "5x": 5.0}
    else:  # sports
        sport_res = random.choice(["hoops", "goal", "target", "strike"])
        outcome_label = f"🏀 Shot Result: <b>{sport_res.upper()}</b>"
        winning_choices = {sport_res}
        mult_map = {sport_res: 3.5}

    result_lines = [f"🏁 <b>{game_key.upper()} Result</b> · {outcome_label}", ""]
    for uid, bdata in bets.items():
        ch = bdata["choice"]
        bet_amt = int(bdata["bet"])
        m = mult_map.get(ch, 0.0)
        payout = int(round(bet_amt * m))
        won = ch in winning_choices and m > 1.0
        if payout > 0:
            bal = db.adjust_coins(
                uid,
                payout,
                chat_id=chat_id,
                name=bdata["name"],
                won_delta=1 if won else 0,
                payout_amount=payout if won else 0,
            )
        else:
            u_doc = db.get_user(uid)
            bal = int(u_doc.get("coins", 0)) if u_doc else 0

        tag = "🎉" if won else ("🤝" if payout == bet_amt else "💥")
        result_lines.append(
            f"• {tag} <b>{bdata['name']}</b> ({ch}) — Bet <code>{bet_amt}</code> → Payout <code>{payout}</code> (Bal: <code>{bal}</code>)"
        )

    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text="\n".join(result_lines),
            parse_mode=ParseMode.HTML,
        )
        schedule_delete_messages(app.bot, chat_id, [msg_id], AUTO_DELETE_RESULT_SEC)
    except (BadRequest, TelegramError):
        pass


async def group_round_timer_task(app: Application, chat_id: int, round_id: str) -> None:
    await asyncio.sleep(ROUND_SECONDS)
    await finalize_group_round(app, chat_id, round_id)


async def start_group_round(
    app: Application,
    chat_id: int,
    starter_id: int,
    starter_name: str,
    game_key: str,
    send_fn: Callable[..., Coroutine[Any, Any, Any]],
) -> None:
    if chat_id in ACTIVE_ROUNDS:
        cur = ACTIVE_ROUNDS[chat_id]
        await send_fn(
            f"⚠️ Is group mein abhi <b>{cur['game'].upper()}</b> ka round chal raha hai—pehle khatam hone do.",
            parse_mode=ParseMode.HTML,
        )
        return

    round_id = f"{abs(chat_id)}_{int(time.time() * 1000)}_{random.randint(10, 99)}"
    round_obj: Dict[str, Any] = {
        "id": round_id,
        "chat_id": chat_id,
        "game": game_key,
        "phase": "bet" if game_key == "aviator" else "pick",
        "choices": {},
        "bets": {},
        "message_id": 0,
        "created_at": time.time(),
    }
    ACTIVE_ROUNDS[chat_id] = round_obj
    ROUNDS_BY_ID[round_id] = round_obj

    if game_key == "aviator":
        markup = bet_amounts_keyboard(1000, f"rbet:{round_id}:flight")
        msg = await send_fn(
            f"🚀 <b>Aviator Group Round</b> ({ROUND_SECONDS}s entry)\n"
            f"Started by <b>{starter_name}</b> — apni bet lock karo!\n\n"
            f"{format_locked_bets_text(round_obj)}",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    else:
        msg = await send_fn(
            f"🎮 <b>{game_key.upper()} Round</b> ({ROUND_SECONDS}s)\n"
            f"Started by <b>{starter_name}</b> — pehle apni choice chuno:\n\n"
            f"{format_locked_bets_text(round_obj)}",
            parse_mode=ParseMode.HTML,
            reply_markup=game_choice_keyboard(game_key, round_id),
        )

    if msg and getattr(msg, "message_id", None):
        round_obj["message_id"] = msg.message_id

    asyncio.create_task(group_round_timer_task(app, chat_id, round_id))


# ==============================================================================
# COMMAND HANDLERS
# ==============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    chat = update.effective_chat
    db.ensure_user(u.id, u.first_name, u.username)
    if chat:
        db.record_chat(chat.id, chat.title or u.first_name, chat.type)

    await update.message.reply_text(
        f"👋 Namaste <b>{u.first_name}</b>! Game shuru karne ke liye neeche se mode chuno:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(help_text(), parse_mode=ParseMode.HTML)


async def cmd_bal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    await update.message.reply_text(
        format_balance_card(u.id, u.first_name, u.username),
        parse_mode=ParseMode.HTML,
    )


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    _, msg = await claim_daily_logic(u.id, u.first_name, u.username)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_refill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    _, msg = await claim_refill_logic(u.id, u.first_name, u.username)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    uname = BOT_USERNAME or (context.bot.username or "your_bot")
    link = f"https://t.me/{uname}?start={u.id}"
    await update.message.reply_text(
        f"🔗 <b>Your Invite Link:</b>\n<code>{link}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_give(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    sender = update.effective_user
    db.ensure_user(sender.id, sender.first_name, sender.username)

    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text("⚠️ Jis user ko coins dene hain uske message par reply karke <code>/give 100</code> likho.", parse_mode=ParseMode.HTML)
        return
    target = update.message.reply_to_message.from_user
    if target.is_bot or target.id == sender.id:
        await update.message.reply_text("⚠️ Valid player ke message par reply karo.", parse_mode=ParseMode.HTML)
        return
    if not context.args:
        await update.message.reply_text("Use: <code>/give 100</code>", parse_mode=ParseMode.HTML)
        return
    try:
        amt = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Valid number daalo.", parse_mode=ParseMode.HTML)
        return
    if amt <= 0:
        await update.message.reply_text("⚠️ Amount 0 se zyada hona chahiye.", parse_mode=ParseMode.HTML)
        return

    db.ensure_user(target.id, target.first_name, target.username)
    new_sender_bal = db.adjust_coins(sender.id, -amt)
    if new_sender_bal is None:
        await update.message.reply_text("❌ Tumhare paas itne coins nahi hain.", parse_mode=ParseMode.HTML)
        return
    db.adjust_coins(target.id, amt)
    await update.message.reply_text(
        f"💸 <b>{sender.first_name}</b> ne <b>{target.first_name}</b> ko <code>{amt}</code> coins bheje!\n"
        f"🪙 Tumhara balance: <code>{new_sender_bal}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    rows = db.global_top(10)
    if not rows:
        await update.message.reply_text("🌍 Leaderboard abhi khali hai.")
        return
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = ["🌍 <b>Global Rank Top 10</b>", ""]
    for idx, r in enumerate(rows):
        prefix = medals.get(idx, f"{idx + 1}.")
        lines.append(f"{prefix} <b>{r.get('first_name', 'Player')}</b> — <code>{int(r.get('coins', 0))}</code> coins")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_gtop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat = update.effective_chat
    if chat.type == "private":
        await cmd_top(update, context)
        return
    rows = db.group_top(chat.id, 10)
    if not rows:
        await update.message.reply_text("👥 Is group ka leaderboard abhi khali hai.")
        return
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = [f"👥 <b>{chat.title or 'Group'} Top 10</b>", ""]
    for idx, r in enumerate(rows):
        prefix = medals.get(idx, f"{idx + 1}.")
        lines.append(f"{prefix} <b>{r.get('name', 'Player')}</b> — <code>{int(r.get('net_coins', 0))}</code> net")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_mines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or not update.message:
        return
    u = update.effective_user
    chat_id = update.effective_chat.id
    db.ensure_user(u.id, u.first_name, u.username)
    PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": "mines_bet", "bombs": 4}
    await update.message.reply_text(
        "💣 <b>Mines</b> — Pehle mines count chuno (ya neeche se bet amount chuno; custom bet type bhi kar sakte ho):",
        parse_mode=ParseMode.HTML,
        reply_markup=bet_amounts_keyboard(
            int(db.get_user(u.id).get("coins", 0)),
            "sbet:mines:4",
            extra_rows=[
                [
                    btn(f"{b} 💣", f"mines_bombs:{b}", "danger")
                    for b in MINES_BOMB_CHOICES
                ]
            ],
        ),
    )


# ==============================================================================
# OWNER-ONLY COMMANDS (Hidden from / menu; checked via is_owner)
# ==============================================================================
async def cmd_mycoins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    u = update.effective_user
    db.ensure_user(u.id, u.first_name, u.username)
    if not context.args:
        await update.message.reply_text("Use: <code>/mycoins 5000</code> ya <code>/mycoins -1000</code>", parse_mode=ParseMode.HTML)
        return
    try:
        delta = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Valid number daalo.", parse_mode=ParseMode.HTML)
        return
    cur = int(db.get_user(u.id).get("coins", 0))
    new_bal = db.set_coins(u.id, cur + delta, sync_realistic_stats=(cur + delta >= 1000))
    await update.message.reply_text(f"👑 Owner Coins: <code>{new_bal}</code>", parse_mode=ParseMode.HTML)


async def cmd_setmycoins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    u = update.effective_user
    db.ensure_user(u.id, u.first_name, u.username)
    if not context.args:
        await update.message.reply_text("Use: <code>/setmycoins 10000</code>", parse_mode=ParseMode.HTML)
        return
    try:
        amt = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Valid number daalo.", parse_mode=ParseMode.HTML)
        return
    new_bal = db.set_coins(u.id, amt, sync_realistic_stats=True)
    await update.message.reply_text(f"👑 Owner Balance Set to <code>{new_bal}</code>", parse_mode=ParseMode.HTML)


async def cmd_addcoins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Use: <code>/addcoins &lt;user_id&gt; &lt;amount&gt;</code>", parse_mode=ParseMode.HTML)
        return
    try:
        uid = int(context.args[0])
        delta = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Valid integers daalo.", parse_mode=ParseMode.HTML)
        return
    if not db.get_user(uid):
        db.ensure_user(uid, "Player", None)
    cur = int(db.get_user(uid).get("coins", 0))
    new_bal = db.set_coins(uid, cur + delta, sync_realistic_stats=False)
    await update.message.reply_text(f"✅ User <code>{uid}</code> coins: <code>{new_bal}</code>", parse_mode=ParseMode.HTML)


async def cmd_setcoins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Use: <code>/setcoins &lt;user_id&gt; &lt;amount&gt;</code>", parse_mode=ParseMode.HTML)
        return
    try:
        uid = int(context.args[0])
        amt = int(context.args[1])
    except ValueError:
        await update.message.reply_text("⚠️ Valid integers daalo.", parse_mode=ParseMode.HTML)
        return
    if not db.get_user(uid):
        db.ensure_user(uid, "Player", None)
    new_bal = db.set_coins(uid, amt, sync_realistic_stats=False)
    await update.message.reply_text(f"✅ User <code>{uid}</code> balance set to <code>{new_bal}</code>", parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    s = db.stats_summary()
    await update.message.reply_text(
        f"📊 <b>Bot Stats</b>\n"
        f"• Users: <code>{s['users']}</code>\n"
        f"• Chats: <code>{s['chats']}</code>\n"
        f"• Total Coins: <code>{s['coins']}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    reply = update.message.reply_to_message
    text_arg = " ".join(context.args).strip() if context.args else ""
    if not reply and not text_arg:
        await update.message.reply_text("Use: kisi message par reply karke <code>/broadcast</code> ya <code>/broadcast &lt;text&gt;</code>", parse_mode=ParseMode.HTML)
        return

    chat_ids = db.all_chat_ids()
    sent = 0
    failed = 0
    for cid in chat_ids:
        try:
            if reply:
                await context.bot.copy_message(chat_id=cid, from_chat_id=reply.chat_id, message_id=reply.message_id)
            else:
                await context.bot.send_message(chat_id=cid, text=text_arg, parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.05)
        except (Forbidden, BadRequest, TelegramError):
            failed += 1

    await update.message.reply_text(f"📣 Broadcast done: ✅ <code>{sent}</code> | ❌ <code>{failed}</code>", parse_mode=ParseMode.HTML)


# ==============================================================================
# CALLBACK QUERY ROUTER
# ==============================================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return
    u = query.from_user
    chat = query.message.chat if query.message else None
    chat_id = chat.id if chat else u.id
    is_private = chat.type == "private" if chat else True
    data = query.data or ""

    db.ensure_user(u.id, u.first_name, u.username)
    if chat:
        db.record_chat(chat.id, chat.title or u.first_name, chat.type)

    async def reply_cb(text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> Any:
        if query.message:
            return await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return None

    # Menu & Utility actions
    if data == "back_menu":
        await query.answer()
        if query.message:
            try:
                await query.message.edit_text(
                    f"👋 Namaste <b>{u.first_name}</b>! Mode chuno:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_menu_keyboard(),
                )
            except (BadRequest, TelegramError):
                pass
        return

    if data == "do_daily":
        ok, msg = await claim_daily_logic(u.id, u.first_name, u.username)
        await query.answer("Claimed!" if ok else "Cooldown", show_alert=not ok)
        await reply_cb(msg)
        return

    if data == "do_refill":
        ok, msg = await claim_refill_logic(u.id, u.first_name, u.username)
        await query.answer("Refilled!" if ok else "Cooldown", show_alert=not ok)
        await reply_cb(msg)
        return

    if data == "menu_bal":
        await query.answer()
        await reply_cb(format_balance_card(u.id, u.first_name, u.username))
        return

    if data == "menu_help":
        await query.answer()
        await reply_cb(help_text())
        return

    if data == "menu_top":
        await query.answer()
        rows = db.global_top(10)
        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = ["🌍 <b>Global Rank Top 10</b>", ""]
        for idx, r in enumerate(rows):
            lines.append(f"{medals.get(idx, f'{idx + 1}.')} <b>{r.get('first_name', 'Player')}</b> — <code>{int(r.get('coins', 0))}</code> coins")
        await reply_cb("\n".join(lines))
        return

    if data == "menu_gtop":
        await query.answer()
        if is_private:
            await reply_cb("👥 Group Top ke liye kisi group mein <code>/gtop</code> chalao.")
            return
        rows = db.group_top(chat_id, 10)
        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = [f"👥 <b>{chat.title or 'Group'} Top 10</b>", ""]
        for idx, r in enumerate(rows):
            lines.append(f"{medals.get(idx, f'{idx + 1}.')} <b>{r.get('name', 'Player')}</b> — <code>{int(r.get('net_coins', 0))}</code> net")
        await reply_cb("\n".join(lines))
        return

    # Mines Bomb selection
    if data == "menu_mines":
        await query.answer()
        PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": "mines_bet", "bombs": 4}
        await reply_cb("💣 <b>Mines</b> — Kitne bombs chahiye?", reply_markup=mines_bombs_keyboard())
        return

    if data.startswith("mines_bombs:"):
        await query.answer()
        bombs = int(data.split(":", 1)[1])
        PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": "mines_bet", "bombs": bombs}
        user_coins = int(db.get_user(u.id).get("coins", 0))
        await reply_cb(
            f"💣 <b>{bombs} Mines</b> selected — Bet amount chuno (ya number type karo):",
            reply_markup=bet_amounts_keyboard(user_coins, f"sbet:mines:{bombs}"),
        )
        return

    # Aviator menu
    if data == "menu_aviator":
        await query.answer()
        if is_private:
            PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": "aviator_solo_bet"}
            user_coins = int(db.get_user(u.id).get("coins", 0))
            await reply_cb(
                "🚀 <b>Solo Aviator</b> — Bet amount chuno (ya number type karo):",
                reply_markup=bet_amounts_keyboard(user_coins, "sbet:aviator:0"),
            )
        else:
            await start_group_round(context.application, chat_id, u.id, u.first_name, "aviator", reply_cb)
        return

    # Group Round game selection (pick_game:<game>)
    if data.startswith("pick_game:"):
        await query.answer()
        game_key = data.split(":", 1)[1]
        if is_private:
            PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": f"solo_game:{game_key}"}
            user_coins = int(db.get_user(u.id).get("coins", 0))
            await reply_cb(
                f"🎮 <b>{game_key.upper()}</b> — Bet amount chuno (ya number type karo):",
                reply_markup=bet_amounts_keyboard(user_coins, f"sbet:{game_key}:0"),
            )
        else:
            await start_group_round(context.application, chat_id, u.id, u.first_name, game_key, reply_cb)
        return

    # Solo Bet execution (sbet:<mode>:<extra>:<amount>)
    if data.startswith("sbet:"):
        await query.answer()
        _, mode_key, extra_str, amt_str = data.split(":", 3)
        amt = int(amt_str)
        PENDING_SOLO_BET.pop((chat_id, u.id), None)
        if mode_key == "mines":
            await start_mines_game(chat_id, u.id, u.first_name, u.username, amt, int(extra_str), reply_cb)
        elif mode_key == "aviator":
            # Quick solo aviator flight
            bal = db.adjust_coins(u.id, -amt, chat_id=chat_id, name=u.first_name, played_delta=1)
            if bal is None:
                await reply_cb("❌ Balance kam hai! <code>/daily</code> ya <code>/refill</code> chalao.")
                return
            crash_m = round(random.uniform(1.2, 4.5), 1)
            target_m = round(random.uniform(1.1, min(3.5, crash_m)), 1)
            if target_m < crash_m:
                payout = int(round(amt * target_m))
                final_b = db.adjust_coins(u.id, payout, chat_id=chat_id, name=u.first_name, won_delta=1, payout_amount=payout)
                await reply_cb(
                    f"🚀 Cash Out at <b>{target_m:.1f}x</b> (Crash: {crash_m:.1f}x)\n"
                    f"💰 Payout: <code>{payout}</code> · Bal: <code>{final_b}</code>"
                )
            else:
                await reply_cb(f"💥 Crashed at <b>{crash_m:.1f}x</b>! Loss: <code>{amt}</code> · Bal: <code>{bal}</code>")
        else:
            await reply_cb("🎮 Choice ke liye group round ya command use karo.")
        return

    # Group Round: Choice selection (rchoice:<round_id>:<choice>)
    if data.startswith("rchoice:"):
        _, round_id, choice = data.split(":", 2)
        round_obj = ROUNDS_BY_ID.get(round_id)
        if not round_obj or round_obj.get("phase") != "pick":
            await query.answer("Round khatam ya bet stage mein hai.", show_alert=True)
            return

        round_obj["choices"][u.id] = {"name": u.first_name, "choice": choice}
        await query.answer(f"Choice: {choice}")

        user_coins = int(db.get_user(u.id).get("coins", 0))
        bet_kb = bet_amounts_keyboard(user_coins, f"rbet:{round_id}:{choice}")
        if query.message:
            try:
                await query.message.edit_text(
                    f"🎮 <b>{round_obj['game'].upper()} Round</b> ({ROUND_SECONDS}s)\n"
                    f"<b>{u.first_name}</b> ne <b>{choice}</b> chuna — ab bet amount chuno:\n\n"
                    f"{format_locked_bets_text(round_obj)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=bet_kb,
                )
            except (BadRequest, TelegramError):
                pass
        return

    # Group Round: Lock bet (rbet:<round_id>:<choice>:<amount>)
    if data.startswith("rbet:"):
        _, round_id, choice, amt_str = data.split(":", 3)
        round_obj = ROUNDS_BY_ID.get(round_id)
        if not round_obj or round_obj.get("phase") not in ("pick", "bet"):
            await query.answer("Round khatam ho chuka hai.", show_alert=True)
            return

        if u.id in round_obj["bets"]:
            await query.answer("Tumhari bet pehle se lock hai!", show_alert=True)
            return

        bet_amt = int(amt_str)
        if bet_amt < MIN_BET or bet_amt > MAX_BET:
            await query.answer(f"Bet {MIN_BET}-{MAX_BET} rakho.", show_alert=True)
            return

        new_bal = db.adjust_coins(u.id, -bet_amt, chat_id=chat_id, name=u.first_name, played_delta=1)
        if new_bal is None:
            await query.answer("Coins kam hain! /refill ya /daily chalao.", show_alert=True)
            return

        db.record_last_bet(u.id, bet_amt)
        round_obj["bets"][u.id] = {"name": u.first_name, "choice": choice, "bet": bet_amt}
        await query.answer(f"{bet_amt} coins lock!")

        # Keep the shared bet slot alive with updated locked-bets list
        if query.message:
            if round_obj["game"] == "aviator":
                kb = bet_amounts_keyboard(1000, f"rbet:{round_id}:flight")
                header = f"🚀 <b>Aviator Group Round</b> ({ROUND_SECONDS}s entry)\nApni bet lock karo!"
            else:
                kb = game_choice_keyboard(round_obj["game"], round_id)
                header = f"🎮 <b>{round_obj['game'].upper()} Round</b> ({ROUND_SECONDS}s)\nBaaki members apni choice &amp; bet lock kar sakte hain:"

            try:
                await query.message.edit_text(
                    f"{header}\n\n{format_locked_bets_text(round_obj)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            except (BadRequest, TelegramError):
                pass
        return

    # Aviator live flight Cash Out (av_cash:<round_id>)
    if data.startswith("av_cash:"):
        round_id = data.split(":", 1)[1]
        round_obj = ROUNDS_BY_ID.get(round_id)
        flight = round_obj.get("flight") if round_obj else None
        if not round_obj or not flight or not flight.get("active"):
            await query.answer("Flight end ho chuki hai.", show_alert=True)
            return

        pdata = flight["players"].get(u.id)
        if not pdata:
            await query.answer("Is flight mein tumhari bet nahi hai.", show_alert=True)
            return
        if pdata["cashed_out"]:
            await query.answer("Tum pehle hi Cash Out kar chuke ho!", show_alert=True)
            return

        mult = float(flight["mult"])
        payout = int(round(pdata["bet"] * mult))
        pdata["cashed_out"] = True
        pdata["mult"] = mult
        pdata["payout"] = payout
        flight["cashouts"].append({"user_id": u.id, "name": u.first_name, "mult": mult, "payout": payout})

        db.adjust_coins(u.id, payout, chat_id=chat_id, name=u.first_name, won_delta=1, payout_amount=payout)
        await query.answer(f"Cash Out @ {mult:.1f}x (+{payout})!")

        if query.message:
            cashout_btn = InlineKeyboardMarkup(
                [[btn(f"💸 Cash Out ({mult:.1f}x)", f"av_cash:{round_id}", "success")]]
            )
            try:
                await query.message.edit_text(
                    f"🚀 <b>Aviator Live:</b> <code>{mult:.1f}x</code>\n"
                    f"⚡ Crash hone se pehle Cash Out dabao!\n\n"
                    f"{format_aviator_cashout_feed(flight)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=cashout_btn,
                )
            except (BadRequest, TelegramError):
                pass
        return

    # Mines board interactions
    if data == "mnop":
        await query.answer()
        return

    if data.startswith("mclick:"):
        game = ACTIVE_MINES.get((chat_id, u.id))
        if not game:
            await query.answer("Ye Mines board expire ho chuka hai. Naya /mines chalao.", show_alert=True)
            return
        if game["user_id"] != u.id:
            await query.answer("Ye board sirf iske owner ka hai!", show_alert=True)
            return

        idx = int(data.split(":", 1)[1])
        if idx in game["revealed"]:
            await query.answer("Ye tile pehle hi khul chuka hai.")
            return

        if idx in game["mines"]:
            ACTIVE_MINES.pop((chat_id, u.id), None)
            await query.answer("💥 Mine phat gaya!", show_alert=True)
            if query.message:
                try:
                    await query.message.edit_text(
                        f"💥 <b>Boom! Mine Hit</b> — <b>{u.first_name}</b>\n"
                        f"Bet <code>{game['bet']}</code> gaya.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_mines_keyboard(game, reveal_all=True, finished=True),
                    )
                    schedule_delete_messages(context.application.bot, chat_id, [query.message.message_id], AUTO_DELETE_RESULT_SEC)
                except (BadRequest, TelegramError):
                    pass
            return

        game["revealed"].append(idx)
        mult = mines_multiplier(len(game["revealed"]), game["mines_count"])
        payout = int(game["bet"] * mult)
        await query.answer(f"💎 Safe! {mult}x")

        # Auto cashout if all safe tiles opened
        safe_total = 25 - game["mines_count"]
        if len(game["revealed"]) >= safe_total:
            ACTIVE_MINES.pop((chat_id, u.id), None)
            bal = db.adjust_coins(u.id, payout, chat_id=chat_id, name=u.first_name, won_delta=1, payout_amount=payout)
            if query.message:
                try:
                    await query.message.edit_text(
                        f"🏆 <b>Board Clear!</b> <b>{u.first_name}</b> ne <code>{payout}</code> coins ({mult}x) jeete!\n"
                        f"🪙 <b>Balance:</b> <code>{bal}</code>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_mines_keyboard(game, reveal_all=True, finished=True),
                    )
                    schedule_delete_messages(context.application.bot, chat_id, [query.message.message_id], AUTO_DELETE_RESULT_SEC)
                except (BadRequest, TelegramError):
                    pass
            return

        if query.message:
            try:
                await query.message.edit_text(
                    f"💣 <b>Mines</b> · {game['mines_count']} mines · bet <code>{game['bet']}</code> · <b>{u.first_name}</b>\n"
                    f"✨ Current: <code>{mult}x</code> (Payout: <code>{payout}</code>)",
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_mines_keyboard(game, reveal_all=False, finished=False),
                )
            except (BadRequest, TelegramError):
                pass
        return

    if data == "mcashout":
        game = ACTIVE_MINES.get((chat_id, u.id))
        if not game or game["user_id"] != u.id or not game["revealed"]:
            await query.answer("Pehle kam se kam 1 safe tile kholo.", show_alert=True)
            return
        ACTIVE_MINES.pop((chat_id, u.id), None)
        mult = mines_multiplier(len(game["revealed"]), game["mines_count"])
        payout = int(game["bet"] * mult)
        bal = db.adjust_coins(u.id, payout, chat_id=chat_id, name=u.first_name, won_delta=1, payout_amount=payout)
        await query.answer(f"Cashed out {payout}!")
        if query.message:
            try:
                await query.message.edit_text(
                    f"💰 <b>Cashed Out!</b> <b>{u.first_name}</b> ne <code>{mult}x</code> par <code>{payout}</code> coins nikale!\n"
                    f"🪙 <b>Balance:</b> <code>{bal}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_mines_keyboard(game, reveal_all=True, finished=True),
                )
                schedule_delete_messages(context.application.bot, chat_id, [query.message.message_id], AUTO_DELETE_RESULT_SEC)
            except (BadRequest, TelegramError):
                pass
        return

    # 5x5 TicTacToe start & join & move
    if data == "start_ttt5":
        await query.answer()
        if is_private:
            await reply_cb("❎ <b>5x5 Tic Tac Toe</b> group chat ke liye best hai—kisi group mein yeh button dabao.")
            return
        if chat_id in ACTIVE_TTT5:
            await reply_cb("⚠️ Is group mein 5x5 Tic Tac Toe pehle se chal raha hai.")
            return

        entry_bet = 100
        bal = db.adjust_coins(u.id, -entry_bet, chat_id=chat_id, name=u.first_name, played_delta=1)
        if bal is None:
            await reply_cb("❌ 5x5 Tic Tac Toe start karne ke liye kam se kam <b>100</b> coins chahiye.")
            return

        game = {
            "chat_id": chat_id,
            "bet": entry_bet,
            "status": "joining",
            "board": [None] * 25,
            "players": [{"user_id": u.id, "name": u.first_name, "mark": TTT_MARKS[0]}],
            "turn_idx": 0,
            "turn_num": 0,
            "message_id": 0,
        }
        ACTIVE_TTT5[chat_id] = game
        msg = await reply_cb(
            f"❎ <b>5x5 Tic Tac Toe</b> · Entry: <code>{entry_bet}</code> coins\n"
            f"{format_ttt5_roster(game)}\n"
            f"⏳ Join karne ke liye <b>{TTT_JOIN_SECONDS}s</b> bache hain!",
            reply_markup=build_ttt5_keyboard(game, finished=False),
        )
        if msg and getattr(msg, "message_id", None):
            game["message_id"] = msg.message_id
            asyncio.create_task(ttt5_join_countdown(context.application, chat_id, msg.message_id))
        return

    if data == "ttt5_join":
        game = ACTIVE_TTT5.get(chat_id)
        if not game or game.get("status") != "joining":
            await query.answer("Join time khatam ho chuka hai.", show_alert=True)
            return
        if any(p["user_id"] == u.id for p in game["players"]):
            await query.answer("Tum pehle se join kar chuke ho!", show_alert=True)
            return
        if len(game["players"]) >= TTT_MAX_PLAYERS:
            await query.answer("Max players (6) full ho chuke hain.", show_alert=True)
            return

        bal = db.adjust_coins(u.id, -int(game["bet"]), chat_id=chat_id, name=u.first_name, played_delta=1)
        if bal is None:
            await query.answer("Coins kam hain!", show_alert=True)
            return

        mark = TTT_MARKS[len(game["players"]) % len(TTT_MARKS)]
        game["players"].append({"user_id": u.id, "name": u.first_name, "mark": mark})
        await query.answer(f"Joined as {mark}!")

        if query.message:
            try:
                await query.message.edit_text(
                    f"❎ <b>5x5 Tic Tac Toe</b> · Entry: <code>{game['bet']}</code> coins\n"
                    f"{format_ttt5_roster(game)}\n"
                    f"⏳ Join karne ke liye <b>{TTT_JOIN_SECONDS}s</b> window chal rahi hai!",
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_ttt5_keyboard(game, finished=False),
                )
            except (BadRequest, TelegramError):
                pass
        return

    if data.startswith("ttt5_move:"):
        game = ACTIVE_TTT5.get(chat_id)
        if not game or game.get("status") != "active":
            await query.answer("Match active nahi hai.", show_alert=True)
            return
        players = game["players"]
        cur_p = players[game["turn_idx"] % len(players)]
        if cur_p["user_id"] != u.id:
            await query.answer(f"Abhi {cur_p['name']} ({cur_p['mark']}) ki turn hai!", show_alert=True)
            return

        idx = int(data.split(":", 1)[1])
        if game["board"][idx] is not None:
            await query.answer("Ye box pehle se bhara hai!", show_alert=True)
            return

        game["board"][idx] = cur_p["mark"]
        game["turn_idx"] = (game["turn_idx"] + 1) % len(players)
        game["turn_num"] = int(game.get("turn_num", 1)) + 1
        await query.answer("Move locked!")

        if all(x is not None for x in game["board"]):
            await finish_ttt5_game(context.application, chat_id, "Board Full")
            return

        next_p = players[game["turn_idx"] % len(players)]
        if query.message:
            try:
                await query.message.edit_text(
                    f"❎ <b>5x5 Tic Tac Toe</b>\n"
                    f"{format_ttt5_roster(game)}\n"
                    f"🎯 Turn: {next_p['mark']} <b>{next_p['name']}</b> ({TTT_TURN_SECONDS}s)",
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_ttt5_keyboard(game, finished=False),
                )
            except (BadRequest, TelegramError):
                pass

        asyncio.create_task(ttt5_turn_watcher(context.application, chat_id, game["turn_num"]))
        return


# ==============================================================================
# PLAIN TEXT HANDLER (Custom numeric bet input if user types an amount)
# ==============================================================================
async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or not update.message or not update.message.text:
        return
    txt = update.message.text.strip()
    if not txt.isdigit():
        return
    u = update.effective_user
    chat_id = update.effective_chat.id
    pending = PENDING_SOLO_BET.pop((chat_id, u.id), None)
    if not pending:
        return

    amt = int(txt)
    mode = pending.get("mode", "")
    if mode == "mines_bet":
        await start_mines_game(
            chat_id,
            u.id,
            u.first_name,
            u.username,
            amt,
            int(pending.get("bombs", 4)),
            update.message.reply_text,
        )


# ==============================================================================
# BOT COMMAND MENU SCOPING (Owner commands excluded)
# ==============================================================================
MEMBER_COMMANDS = [
    BotCommand("start", "Main menu kholein"),
    BotCommand("help", "Simple game & coin guide"),
    BotCommand("bal", "Apna balance aur stats dekhein"),
    BotCommand("daily", "Har 24h mein 2000 coins lein"),
    BotCommand("refill", "Har 30m mein 250 coins lein"),
    BotCommand("ref", "Apna invite link lein"),
    BotCommand("give", "Reply karke coins bhejein"),
    BotCommand("gtop", "Group leaderboard dekhein"),
    BotCommand("top", "Global leaderboard dekhein"),
    BotCommand("mines", "Mines board shuru karein"),
]


async def setup_bot_commands(app: Application) -> None:
    try:
        await app.bot.set_my_commands(MEMBER_COMMANDS, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(MEMBER_COMMANDS, scope=BotCommandScopeAllGroupChats())
        logger.info("Member bot commands configured successfully.")
    except TelegramError as exc:
        logger.warning("Failed to set bot commands: %s", exc)


async def post_init(app: Application) -> None:
    await setup_bot_commands(app)


# ==============================================================================
# APPLICATION ENTRYPOINT
# ==============================================================================
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")

    threading.Thread(target=run_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Member commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("bal", cmd_bal))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("refill", cmd_refill))
    app.add_handler(CommandHandler("ref", cmd_ref))
    app.add_handler(CommandHandler("give", cmd_give))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("gtop", cmd_gtop))
    app.add_handler(CommandHandler("mines", cmd_mines))

    # Owner commands (not listed in BotCommand menu; guarded by is_owner)
    app.add_handler(CommandHandler("mycoins", cmd_mycoins))
    app.add_handler(CommandHandler("setmycoins", cmd_setmycoins))
    app.add_handler(CommandHandler("addcoins", cmd_addcoins))
    app.add_handler(CommandHandler("setcoins", cmd_setcoins))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    # Callbacks & custom bet numbers
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))

    logger.info("Starting Telegram Game Bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
