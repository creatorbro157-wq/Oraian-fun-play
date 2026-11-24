import asyncio
import logging
import os
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify
from pymongo import MongoClient, ReturnDocument
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("GroupArcadeBot")
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = os.getenv("DB_NAME", "group_arcade_bot").strip()
PORT = int(os.getenv("PORT", "10000"))

try:
    OWNER_ID: Optional[int] = int(os.getenv("OWNER_ID", "0").strip())
    if OWNER_ID == 0:
        OWNER_ID = None
except ValueError:
    OWNER_ID = None

ROUND_SECONDS = 10
AUTO_DELETE_RESULT_SEC = 35
DAILY_REWARD = 500
DAILY_COOLDOWN_SEC = 24 * 3600
REFILL_AMOUNT = 150
REFILL_COOLDOWN_SEC = 2 * 3600
REFERRAL_BONUS_REFERRER = 250
REFERRAL_BONUS_USER = 200
WELCOME_COINS = 250

MINES_BOMB_CHOICES = [2, 4, 5, 7, 9]
BET_OPTIONS = [10, 50, 100, 250, 500, 1000]
CARD_RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
CARD_SUITS = ["♠", "♥", "♦", "♣"]

DAILY_DIALOGUES = [
    "Rozana ka dose aa gaya! +{amt} coins account mein—samajh kar khelo.",
    "Naya din, naya mauka! +{amt} coins ke saath board hilao.",
    "Daily coins credited! +{amt} coins lo aur game shuru karo.",
]

REFILL_DIALOGUES = [
    "Balance 0 ho gaya tha—lo +{amt} coins ka rescue refill!",
    "Pocket khali dekhi toh +{amt} coins bhej diye hain. Is baar clean khelo!",
    "Fresh start ke liye +{amt} coins refill mil gaye hain.",
]

MINES_WIN_DIALOGUES = [
    "Time par cash out kiya—+{payout} coins (Profit: +{profit})!",
    "Safe exit! +{payout} coins jeet kar balance upar le aaye.",
    "Smart move! +{payout} coins secure kar liye.",
]

MINES_LOSS_DIALOGUES = [
    "Boom! Bomb hit ho gaya aur {bet} coins chale gaye.",
    "Ek tile ne khel badal diya—{bet} coins loss.",
    "Bomb trigger ho gaya! Agla round phir try karo.",
]


class DatabaseManager:
    def __init__(self, uri: str, db_name: str) -> None:
        self.use_mongo = False
        self.users_col = None
        self.groups_col = None
        self.chats_col = None
        self.mem_users: Dict[int, Dict[str, Any]] = {}
        self.mem_groups: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.mem_chats: set[int] = set()

        if uri:
            try:
                client = MongoClient(uri, serverSelectionTimeoutMS=6000)
                client.admin.command("ping")
                db = client[db_name]
                self.users_col = db["users"]
                self.groups_col = db["group_scores"]
                self.chats_col = db["known_chats"]
                self.users_col.create_index("coins")
                self.groups_col.create_index([("chat_id", 1), ("coins", -1)])
                self.use_mongo = True
                logger.info("Connected to MongoDB Atlas successfully.")
            except Exception as exc:
                logger.warning("MongoDB connection failed (%s). Using in-memory fallback.", exc)
        else:
            logger.info("MONGO_URI not set. Using in-memory store.")

    @staticmethod
    def _default_user(user_id: int, name: str, username: Optional[str]) -> Dict[str, Any]:
        return {
            "user_id": user_id,
            "name": name or "Player",
            "username": username or "",
            "coins": WELCOME_COINS,
            "last_daily": 0.0,
            "last_refill": 0.0,
            "referred_by": None,
            "referrals": 0,
            "games_played": 0,
            "games_won": 0,
            "streak": 0,
            "best_streak": 0,
            "biggest_win": 0,
            "last_bet": 100,
        }

    def ensure_user(
        self, user_id: int, name: str, username: Optional[str], referrer_id: Optional[int] = None
    ) -> Tuple[Dict[str, Any], bool, int]:
        clean_name = (name or "Player")[:60]
        clean_user = (username or "").lstrip("@")

        if self.use_mongo:
            existing = self.users_col.find_one({"user_id": user_id})
            if existing:
                self.users_col.update_one(
                    {"user_id": user_id},
                    {"$set": {"name": clean_name, "username": clean_user}},
                )
                existing["name"] = clean_name
                existing["username"] = clean_user
                return existing, False, 0

            bonus = 0
            valid_ref: Optional[int] = None
            if referrer_id and referrer_id != user_id:
                ref_doc = self.users_col.find_one({"user_id": referrer_id})
                if ref_doc:
                    valid_ref = referrer_id
                    bonus = REFERRAL_BONUS_USER
                    self.users_col.update_one(
                        {"user_id": referrer_id},
                        {"$inc": {"coins": REFERRAL_BONUS_REFERRER, "referrals": 1}},
                    )

            doc = self._default_user(user_id, clean_name, clean_user)
            doc["coins"] = WELCOME_COINS + bonus
            doc["referred_by"] = valid_ref
            self.users_col.insert_one(doc)
            return doc, True, bonus

        if user_id in self.mem_users:
            doc = self.mem_users[user_id]
            doc["name"] = clean_name
            doc["username"] = clean_user
            return doc, False, 0

        bonus = 0
        valid_ref = None
        if referrer_id and referrer_id != user_id and referrer_id in self.mem_users:
            valid_ref = referrer_id
            bonus = REFERRAL_BONUS_USER
            self.mem_users[referrer_id]["coins"] += REFERRAL_BONUS_REFERRER
            self.mem_users[referrer_id]["referrals"] += 1

        doc = self._default_user(user_id, clean_name, clean_user)
        doc["coins"] = WELCOME_COINS + bonus
        doc["referred_by"] = valid_ref
        self.mem_users[user_id] = doc
        return doc, True, bonus

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        if self.use_mongo:
            return self.users_col.find_one({"user_id": user_id})
        return self.mem_users.get(user_id)

    def find_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        uname = username.lstrip("@").lower()
        if not uname:
            return None
        if self.use_mongo:
            return self.users_col.find_one({"username": {"$regex": f"^{uname}$", "$options": "i"}})
        for doc in self.mem_users.values():
            if (doc.get("username") or "").lower() == uname:
                return doc
        return None

    def register_chat(self, chat_id: int, chat_type: str, title: Optional[str] = None) -> None:
        if self.use_mongo:
            self.chats_col.update_one(
                {"chat_id": chat_id},
                {"$set": {"chat_id": chat_id, "type": chat_type, "title": title or ""}},
                upsert=True,
            )
        else:
            self.mem_chats.add(chat_id)

    def sync_group_score(self, chat_id: Optional[int], user_id: int, name: str, delta_coins: int) -> None:
        if not chat_id or chat_id > 0:
            return
        clean_name = (name or "Player")[:60]
        if self.use_mongo:
            self.groups_col.update_one(
                {"chat_id": chat_id, "user_id": user_id},
                {"$set": {"name": clean_name}, "$inc": {"coins": delta_coins}},
                upsert=True,
            )
        else:
            key = (chat_id, user_id)
            if key not in self.mem_groups:
                self.mem_groups[key] = {"chat_id": chat_id, "user_id": user_id, "name": clean_name, "coins": 0}
            self.mem_groups[key]["name"] = clean_name
            self.mem_groups[key]["coins"] += delta_coins

    def record_last_bet(self, user_id: int, bet: int) -> None:
        if bet < 10:
            return
        if self.use_mongo:
            self.users_col.update_one({"user_id": user_id}, {"$set": {"last_bet": bet}})
        elif user_id in self.mem_users:
            self.mem_users[user_id]["last_bet"] = bet

    def record_outcome(
        self,
        user_id: int,
        won: bool,
        payout: int = 0,
        chat_id: Optional[int] = None,
        name: str = "Player",
    ) -> None:
        doc = self.get_user(user_id) or {}
        cur_streak = int(doc.get("streak", 0))
        best_streak = int(doc.get("best_streak", 0))
        biggest_win = int(doc.get("biggest_win", 0))

        if won:
            new_streak = cur_streak + 1
            new_best = max(best_streak, new_streak)
            new_big = max(biggest_win, payout)
        else:
            new_streak = 0
            new_best = best_streak
            new_big = biggest_win

        if self.use_mongo:
            self.users_col.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "streak": new_streak,
                        "best_streak": new_best,
                        "biggest_win": new_big,
                    }
                },
            )
        elif user_id in self.mem_users:
            self.mem_users[user_id]["streak"] = new_streak
            self.mem_users[user_id]["best_streak"] = new_best
            self.mem_users[user_id]["biggest_win"] = new_big

        if won and payout > 0:
            self.adjust_coins(user_id, payout, chat_id=chat_id, name=name, won_delta=1)

    def adjust_coins(
        self,
        user_id: int,
        delta: int,
        chat_id: Optional[int] = None,
        name: str = "Player",
        played_delta: int = 0,
        won_delta: int = 0,
    ) -> Optional[int]:
        if self.use_mongo:
            if delta < 0:
                updated = self.users_col.find_one_and_update(
                    {"user_id": user_id, "coins": {"$gte": abs(delta)}},
                    {"$inc": {"coins": delta, "games_played": played_delta, "games_won": won_delta}},
                    return_document=ReturnDocument.AFTER,
                )
                if not updated:
                    return None
                self.sync_group_score(chat_id, user_id, name, delta)
                return int(updated["coins"])
            updated = self.users_col.find_one_and_update(
                {"user_id": user_id},
                {"$inc": {"coins": delta, "games_played": played_delta, "games_won": won_delta}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            self.sync_group_score(chat_id, user_id, name, delta)
            return int(updated["coins"])

        doc = self.mem_users.get(user_id)
        if not doc:
            doc = self._default_user(user_id, name, "")
            self.mem_users[user_id] = doc
        if delta < 0 and doc["coins"] < abs(delta):
            return None
        doc["coins"] += delta
        doc["games_played"] = doc.get("games_played", 0) + played_delta
        doc["games_won"] = doc.get("games_won", 0) + won_delta
        self.sync_group_score(chat_id, user_id, name, delta)
        return int(doc["coins"])

    def set_coins(self, user_id: int, amount: int) -> bool:
        safe_amt = max(0, amount)
        if self.use_mongo:
            res = self.users_col.update_one({"user_id": user_id}, {"$set": {"coins": safe_amt}})
            return res.matched_count > 0
        if user_id in self.mem_users:
            self.mem_users[user_id]["coins"] = safe_amt
            return True
        return False

    def update_cooldown(self, user_id: int, field: str, ts: float, add_coins: int = 0) -> None:
        if self.use_mongo:
            update_doc: Dict[str, Any] = {"$set": {field: ts}}
            if add_coins:
                update_doc["$inc"] = {"coins": add_coins}
            self.users_col.update_one({"user_id": user_id}, update_doc)
        elif user_id in self.mem_users:
            self.mem_users[user_id][field] = ts
            if add_coins:
                self.mem_users[user_id]["coins"] += add_coins

    def top_global(self, limit: int = 10) -> List[Dict[str, Any]]:
        if self.use_mongo:
            return list(self.users_col.find({}, {"_id": 0}).sort("coins", -1).limit(limit))
        return sorted(self.mem_users.values(), key=lambda x: x.get("coins", 0), reverse=True)[:limit]

    def top_group(self, chat_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        if self.use_mongo:
            return list(
                self.groups_col.find({"chat_id": chat_id}, {"_id": 0}).sort("coins", -1).limit(limit)
            )
        items = [v for (cid, _), v in self.mem_groups.items() if cid == chat_id]
        return sorted(items, key=lambda x: x.get("coins", 0), reverse=True)[:limit]

    def stats(self) -> Dict[str, int]:
        if self.use_mongo:
            users_count = self.users_col.count_documents({})
            chats_count = self.chats_col.count_documents({})
            agg = list(self.users_col.aggregate([{"$group": {"_id": None, "total": {"$sum": "$coins"}}}]))
            total_coins = int(agg[0]["total"]) if agg else 0
            return {"users": users_count, "chats": chats_count, "coins": total_coins}
        total_coins = sum(u.get("coins", 0) for u in self.mem_users.values())
        return {"users": len(self.mem_users), "chats": len(self.mem_chats), "coins": total_coins}

    def all_broadcast_targets(self) -> List[int]:
        targets = set()
        if self.use_mongo:
            for u in self.users_col.find({}, {"user_id": 1}):
                targets.add(int(u["user_id"]))
            for c in self.chats_col.find({}, {"chat_id": 1}):
                targets.add(int(c["chat_id"]))
        else:
            targets.update(self.mem_users.keys())
            targets.update(self.mem_chats)
        return list(targets)


db = DatabaseManager(MONGO_URI, DB_NAME)
ACTIVE_MINES: Dict[Tuple[int, int], Dict[str, Any]] = {}
ACTIVE_ROUNDS: Dict[int, Dict[str, Any]] = {}
LOCKED_SOLO_MESSAGES: Dict[Tuple[int, int], float] = {}


def is_owner(user_id: Optional[int]) -> bool:
    return OWNER_ID is not None and user_id == OWNER_ID


def is_private_chat(chat_id: int) -> bool:
    return chat_id > 0


def fmt_duration(seconds: float) -> str:
    rem = max(1, int(seconds))
    hrs = rem // 3600
    mins = (rem % 3600) // 60
    secs = rem % 60
    if hrs > 0:
        return f"{hrs}h {mins}m"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def calc_mines_multiplier(revealed_count: int, mines_count: int = 4) -> float:
    if revealed_count <= 0:
        return 1.0
    total_tiles = 25
    safe_tiles = total_tiles - mines_count
    prob = 1.0
    for i in range(revealed_count):
        prob *= (safe_tiles - i) / (total_tiles - i)
    raw_mult = 0.95 / max(prob, 1e-6)
    return round(max(1.03, min(raw_mult, 50.0)), 2)


def cleanup_locked_messages() -> None:
    now = time.time()
    expired = [k for k, ts in LOCKED_SOLO_MESSAGES.items() if now - ts > 1800]
    for k in expired:
        LOCKED_SOLO_MESSAGES.pop(k, None)


async def safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


async def schedule_delete_messages(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_ids: List[int], delay_sec: float
) -> None:
    await asyncio.sleep(delay_sec)
    for mid in message_ids:
        await safe_delete_message(context, chat_id, mid)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💣 Mines", callback_data="menu_mines"),
            InlineKeyboardButton("🪙 Coin Flip", callback_data="pick_game:flip"),
        ],
        [
            InlineKeyboardButton("🎲 Dice", callback_data="pick_game:dice"),
            InlineKeyboardButton("🎰 Slots", callback_data="pick_game:slots"),
        ],
        [
            InlineKeyboardButton("🃏 Cards", callback_data="pick_game:cards"),
            InlineKeyboardButton("🔢 Number Guess", callback_data="pick_game:guess"),
        ],
        [
            InlineKeyboardButton("🎡 Lucky Wheel", callback_data="pick_game:color"),
            InlineKeyboardButton("🏀 Sports", callback_data="menu_sports"),
        ],
        [
            InlineKeyboardButton("👤 Profile", callback_data="act_profile"),
            InlineKeyboardButton("🎁 Daily", callback_data="act_daily"),
            InlineKeyboardButton("🏆 Top", callback_data="act_top"),
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="act_help"),
        ],
    ])


def sports_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏀 Basketball", callback_data="pick_game:sport_basket"),
            InlineKeyboardButton("⚽ Football", callback_data="pick_game:sport_football"),
        ],
        [
            InlineKeyboardButton("🎯 Darts", callback_data="pick_game:sport_darts"),
            InlineKeyboardButton("🎳 Bowling", callback_data="pick_game:sport_bowling"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="menu_home"),
            InlineKeyboardButton("❌ Close", callback_data="msg_close"),
        ],
    ])


def mines_bomb_keyboard(user_id: int) -> InlineKeyboardMarkup:
    btns = [
        InlineKeyboardButton(f"💣 {b}", callback_data=f"u:{user_id}:mines_bombs:{b}")
        for b in MINES_BOMB_CHOICES
    ]
    return InlineKeyboardMarkup([
        btns,
        [
            InlineKeyboardButton("⬅️ Back", callback_data="menu_home"),
            InlineKeyboardButton("❌ Close", callback_data="msg_close"),
        ],
    ])


def bet_keyboard(
    user_id: int, game_prefix: str, user_coins: int = 0, extra: str = ""
) -> InlineKeyboardMarkup:
    suffix = f":{extra}" if extra else ""
    half_amt = max(10, user_coins // 2)
    max_amt = max(10, min(user_coins, 50000))
    row1 = [
        InlineKeyboardButton(str(b), callback_data=f"u:{user_id}:bet:{game_prefix}:{b}{suffix}")
        for b in BET_OPTIONS[:3]
    ]
    row2 = [
        InlineKeyboardButton(str(b), callback_data=f"u:{user_id}:bet:{game_prefix}:{b}{suffix}")
        for b in BET_OPTIONS[3:]
    ]
    row3 = [
        InlineKeyboardButton(f"½ Half ({half_amt})", callback_data=f"u:{user_id}:bet:{game_prefix}:{half_amt}{suffix}"),
        InlineKeyboardButton(f"💥 Max ({max_amt})", callback_data=f"u:{user_id}:bet:{game_prefix}:{max_amt}{suffix}"),
    ]
    rows = [row1, row2, row3]
    if user_coins <= 0:
        rows.append([InlineKeyboardButton("🆘 Free refill", callback_data=f"u:{user_id}:refill_now")])
    rows.append([
        InlineKeyboardButton("⬅️ Back", callback_data="menu_home"),
        InlineKeyboardButton("❌ Close", callback_data="msg_close"),
    ])
    return InlineKeyboardMarkup(rows)


def solo_choice_keyboard(user_id: int, game_type: str, bet: int, base_card: str = "none") -> InlineKeyboardMarkup:
    if game_type == "flip":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👑 Heads", callback_data=f"u:{user_id}:sgo:flip:{bet}:heads:none"),
                InlineKeyboardButton("🦅 Tails", callback_data=f"u:{user_id}:sgo:flip:{bet}:tails:none"),
            ],
            [InlineKeyboardButton("❌ Close", callback_data="msg_close")],
        ])
    if game_type == "dice":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔢 Odd (1,3,5)", callback_data=f"u:{user_id}:sgo:dice:{bet}:odd:none"),
                InlineKeyboardButton("🔢 Even (2,4,6)", callback_data=f"u:{user_id}:sgo:dice:{bet}:even:none"),
                InlineKeyboardButton("🎯 High (4-6)", callback_data=f"u:{user_id}:sgo:dice:{bet}:high:none"),
            ],
            [InlineKeyboardButton("❌ Close", callback_data="msg_close")],
        ])
    if game_type == "cards":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⬆️ Higher", callback_data=f"u:{user_id}:sgo:cards:{bet}:high:{base_card}"),
                InlineKeyboardButton("⬇️ Lower", callback_data=f"u:{user_id}:sgo:cards:{bet}:low:{base_card}"),
            ],
            [
                InlineKeyboardButton("🔴 Red", callback_data=f"u:{user_id}:sgo:cards:{bet}:red:{base_card}"),
                InlineKeyboardButton("⚫ Black", callback_data=f"u:{user_id}:sgo:cards:{bet}:black:{base_card}"),
            ],
            [InlineKeyboardButton("❌ Close", callback_data="msg_close")],
        ])
    if game_type == "guess":
        r1 = [InlineKeyboardButton(str(n), callback_data=f"u:{user_id}:sgo:guess:{bet}:{n}:none") for n in range(1, 6)]
        r2 = [InlineKeyboardButton(str(n), callback_data=f"u:{user_id}:sgo:guess:{bet}:{n}:none") for n in range(6, 11)]
        return InlineKeyboardMarkup([r1, r2, [InlineKeyboardButton("❌ Close", callback_data="msg_close")]])
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 Red (2x)", callback_data=f"u:{user_id}:sgo:color:{bet}:red:none"),
            InlineKeyboardButton("🟢 Green (2x)", callback_data=f"u:{user_id}:sgo:color:{bet}:green:none"),
            InlineKeyboardButton("🟣 Violet (4.5x)", callback_data=f"u:{user_id}:sgo:color:{bet}:violet:none"),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="msg_close")],
    ])


def post_solo_keyboard(user_id: int, replay_cb: str, bet: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🔁 Play again ({bet})", callback_data=f"u:{user_id}:{replay_cb}"),
            InlineKeyboardButton("🎮 Games", callback_data="menu_home"),
        ],
        [InlineKeyboardButton("❌ Close", callback_data="msg_close")],
    ])


def round_join_keyboard(game_type: str) -> InlineKeyboardMarkup:
    if game_type == "flip":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("👑 Heads", callback_data="rjoin:flip:heads"),
            InlineKeyboardButton("🦅 Tails", callback_data="rjoin:flip:tails"),
        ]])
    if game_type == "dice":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🔢 Odd (1,3,5)", callback_data="rjoin:dice:odd"),
            InlineKeyboardButton("🔢 Even (2,4,6)", callback_data="rjoin:dice:even"),
            InlineKeyboardButton("🎯 High (4-6)", callback_data="rjoin:dice:high"),
        ]])
    if game_type == "slots":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🎰 Enter 10s Spin", callback_data="rjoin:slots:spin")
        ]])
    if game_type == "cards":
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("⬆️ Higher", callback_data="rjoin:cards:high"),
                InlineKeyboardButton("⬇️ Lower", callback_data="rjoin:cards:low"),
            ],
            [
                InlineKeyboardButton("🔴 Red", callback_data="rjoin:cards:red"),
                InlineKeyboardButton("⚫ Black", callback_data="rjoin:cards:black"),
            ],
        ])
    if game_type == "guess":
        r1 = [InlineKeyboardButton(str(n), callback_data=f"rjoin:guess:{n}") for n in range(1, 6)]
        r2 = [InlineKeyboardButton(str(n), callback_data=f"rjoin:guess:{n}") for n in range(6, 11)]
        return InlineKeyboardMarkup([r1, r2])
    if game_type == "color":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🔴 Red (2x)", callback_data="rjoin:color:red"),
            InlineKeyboardButton("🟢 Green (2x)", callback_data="rjoin:color:green"),
            InlineKeyboardButton("🟣 Violet (4.5x)", callback_data="rjoin:color:violet"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⚡ Take Shot", callback_data=f"rjoin:{game_type}:shot")
    ]])


def round_bet_keyboard(user_id: int, game_type: str, choice: str, user_coins: int) -> InlineKeyboardMarkup:
    half_amt = max(10, user_coins // 2)
    max_amt = max(10, min(user_coins, 50000))
    r1 = [InlineKeyboardButton(str(b), callback_data=f"u:{user_id}:rbet:{game_type}:{choice}:{b}") for b in BET_OPTIONS[:3]]
    r2 = [InlineKeyboardButton(str(b), callback_data=f"u:{user_id}:rbet:{game_type}:{choice}:{b}") for b in BET_OPTIONS[3:]]
    r3 = [
        InlineKeyboardButton(f"½ Half ({half_amt})", callback_data=f"u:{user_id}:rbet:{game_type}:{choice}:{half_amt}"),
        InlineKeyboardButton(f"💥 Max ({max_amt})", callback_data=f"u:{user_id}:rbet:{game_type}:{choice}:{max_amt}"),
    ]
    return InlineKeyboardMarkup([r1, r2, r3])


def build_mines_keyboard(game: Dict[str, Any], reveal_all: bool = False) -> InlineKeyboardMarkup:
    uid = game["user_id"]
    mines_set = set(game["mines"])
    revealed_set = set(game["revealed"])
    rows: List[List[InlineKeyboardButton]] = []

    for r in range(5):
        row_btns: List[InlineKeyboardButton] = []
        for c in range(5):
            idx = r * 5 + c
            if idx in revealed_set:
                label = "💎"
            elif reveal_all:
                label = "💣" if idx in mines_set else "▪"
            else:
                label = "▪"
            row_btns.append(InlineKeyboardButton(label, callback_data=f"u:{uid}:mines_pick:{idx}"))
        rows.append(row_btns)

    if not reveal_all:
        mult = calc_mines_multiplier(len(revealed_set), game["mines_count"])
        cashout_amt = int(game["bet"] * mult)
        if len(revealed_set) >= 1:
            rows.append([InlineKeyboardButton(f"💰 Cash Out (+{cashout_amt} | {mult}x)", callback_data=f"u:{uid}:mines_cashout")])
        else:
            rows.append([InlineKeyboardButton(f"💎 Pick a tile ({game['mines_count']} 💣)", callback_data=f"u:{uid}:mines_noop")])
    else:
        rows.append([
            InlineKeyboardButton(f"🔁 Play again ({game['bet']})", callback_data=f"u:{uid}:replay:mines:{game['bet']}:{game['mines_count']}"),
            InlineKeyboardButton("🎮 Games", callback_data="menu_home"),
        ])
        rows.append([InlineKeyboardButton("❌ Close", callback_data="msg_close")])

    return InlineKeyboardMarkup(rows)


async def resolve_solo_game(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    name: str,
    username: Optional[str],
    game_type: str,
    bet: int,
    choice: str,
    base_card: Optional[str] = None,
) -> None:
    if bet < 10:
        await context.bot.send_message(chat_id, "⚠️ Minimum bet <b>10</b> coins hai.", parse_mode=ParseMode.HTML)
        return
    db.ensure_user(user_id, name, username)
    bal_after_bet = db.adjust_coins(user_id, -bet, chat_id=chat_id, name=name, played_delta=1)
    if bal_after_bet is None:
        await context.bot.send_message(chat_id, "❌ Balance kam hai! <code>/daily</code> ya <code>/refill</code> try karo.", parse_mode=ParseMode.HTML)
        return
    db.record_last_bet(user_id, bet)

    won = False
    payout = 0
    headline = ""
    detail_line = ""
    reply_to_id: Optional[int] = None
    replay_cb = f"replay:{game_type}:{bet}:{choice}"

    if game_type == "flip":
        outcome = random.choice(["heads", "tails"])
        won = (choice == outcome)
        payout = bet * 2 if won else 0
        headline = f"🪙 <b>Coin Flip</b> · bet {bet}"
        detail_line = f"Choice: <b>{choice.upper()}</b> | Result: <b>{outcome.upper()}</b>"

    elif game_type == "dice":
        d_msg = await context.bot.send_dice(chat_id=chat_id, emoji="🎲")
        reply_to_id = d_msg.message_id
        val = d_msg.dice.value if d_msg.dice else random.randint(1, 6)
        if not is_private_chat(chat_id):
            await asyncio.sleep(3.0)
        if choice == "odd":
            won = (val % 2 == 1)
            mult = 1.85 if won else 0.0
        elif choice == "even":
            won = (val % 2 == 0)
            mult = 1.85 if won else 0.0
        else:
            won = (val >= 4)
            mult = 2.4 if val == 6 else (1.8 if won else 0.0)
        payout = int(bet * mult) if won else 0
        headline = f"🎲 <b>Dice ({choice.upper()})</b> · bet {bet}"
        detail_line = f"Roll: <b>{val}</b>"

    elif game_type == "slots":
        s_msg = await context.bot.send_dice(chat_id=chat_id, emoji="🎰")
        reply_to_id = s_msg.message_id
        val = s_msg.dice.value if s_msg.dice else random.randint(1, 64)
        if not is_private_chat(chat_id):
            await asyncio.sleep(3.0)
        won = (val >= 43 or val in (1, 22, 43, 64))
        mult = 4.0 if val == 64 else (2.0 if won else 0.0)
        payout = int(bet * mult) if won else 0
        headline = f"🎰 <b>Slots</b> · bet {bet}"
        detail_line = f"Spin Value: <b>{val}</b> ({mult}x)"

    elif game_type == "cards":
        b_card = base_card if (base_card and base_card != "none") else f"{CARD_RANKS[random.randint(0, len(CARD_RANKS)-2)]}{random.choice(CARD_SUITS)}"
        b_idx = CARD_RANKS.index(b_card[:-1])
        n_idx = random.randint(0, len(CARD_RANKS) - 1)
        n_suit = random.choice(CARD_SUITS)
        n_card = f"{CARD_RANKS[n_idx]}{n_suit}"
        is_red = n_suit in ("♥", "♦")
        if choice == "high":
            won = (n_idx > b_idx)
        elif choice == "low":
            won = (n_idx < b_idx)
        elif choice == "red":
            won = is_red
        else:
            won = (not is_red)
        payout = int(bet * 1.9) if won else 0
        headline = f"🃏 <b>Cards ({choice.upper()})</b> · bet {bet}"
        detail_line = f"Card: <code>{b_card}</code> ➡️ <b>{n_card}</b>"

    elif game_type == "guess":
        secret_num = random.randint(1, 10)
        g_num = int(choice)
        if g_num == secret_num:
            won = True
            payout = bet * 7
            tag = "EXACT 7x"
        elif abs(g_num - secret_num) == 1:
            won = True
            payout = int(bet * 1.5)
            tag = "CLOSE 1.5x"
        else:
            won = False
            payout = 0
            tag = "MISS"
        headline = f"🔢 <b>Number Guess</b> · bet {bet}"
        detail_line = f"Pick: <b>{g_num}</b> | Secret: <b>{secret_num}</b> [{tag}]"

    elif game_type == "color":
        r_p = random.random()
        wheel = "violet" if r_p < 0.14 else ("red" if r_p < 0.57 else "green")
        won = (choice == wheel)
        payout = int(bet * (4.5 if wheel == "violet" else 2.0)) if won else 0
        headline = f"🎡 <b>Lucky Wheel</b> · bet {bet}"
        detail_line = f"Pick: <b>{choice.upper()}</b> | Result: <b>{wheel.upper()}</b>"

    else:
        sport_map = {
            "sport_basket": ("🏀", "Basketball", lambda v: v >= 4, 2.0, "Scored the basket!", "Missed the basket."),
            "sport_football": ("⚽", "Football", lambda v: v >= 3, 1.9, "GOAL! Top corner!", "Saved / Missed goal!"),
            "sport_darts": ("🎯", "Darts", lambda v: v >= 5, 1.9, "Bullseye / High hit!", "Missed the target!"),
            "sport_bowling": ("🎳", "Bowling", lambda v: v >= 5, 2.0, "STRIKE / Big knock!", "Not enough pins."),
        }
        em_char, sp_name, pred_fn, mult_sp, win_txt, lose_txt = sport_map[game_type]
        sp_msg = await context.bot.send_dice(chat_id=chat_id, emoji=em_char)
        reply_to_id = sp_msg.message_id
        val = sp_msg.dice.value if sp_msg.dice else random.randint(1, 6)
        if not is_private_chat(chat_id):
            await asyncio.sleep(3.0)
        won = pred_fn(val)
        payout = int(bet * mult_sp) if won else 0
        headline = f"{em_char} <b>{sp_name}</b> · bet {bet}"
        detail_line = f"{win_txt if won else lose_txt} (Roll: <b>{val}</b>)"

    if won and payout > 0:
        db.record_outcome(user_id, True, payout=payout, chat_id=chat_id, name=name)
        final_bal = (db.get_user(user_id) or {}).get("coins", bal_after_bet + payout)
        outcome_line = f"✅ <b>You won +{payout} coins!</b>"
    else:
        db.record_outcome(user_id, False, chat_id=chat_id, name=name)
        final_bal = bal_after_bet
        outcome_line = f"❌ <b>You lost {bet} coins.</b>"

    res_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"{headline}\n{detail_line}\n{outcome_line}\n🪙 <b>Balance:</b> <code>{final_bal}</code>",
        parse_mode=ParseMode.HTML,
        reply_to_message_id=reply_to_id,
        reply_markup=post_solo_keyboard(user_id, replay_cb, bet),
    )

    if not is_private_chat(chat_id):
        mids = [res_msg.message_id] + ([reply_to_id] if reply_to_id else [])
        asyncio.create_task(schedule_delete_messages(context, chat_id, mids, AUTO_DELETE_RESULT_SEC))


async def run_ten_sec_round(context: ContextTypes.DEFAULT_TYPE, chat_id: int, game_type: str) -> None:
    if chat_id in ACTIVE_ROUNDS:
        return

    title_map = {
        "flip": "🪙 <b>10s Coin Flip Round</b>",
        "dice": "🎲 <b>10s Dice Round</b>",
        "slots": "🎰 <b>10s Jackpot Spin</b>",
        "cards": "🃏 <b>10s Cards Round</b>",
        "guess": "🔢 <b>10s Number Guess (1–10)</b>",
        "color": "🎡 <b>10s Color Wheel</b>",
        "sport_basket": "🏀 <b>10s Basketball Round</b>",
        "sport_football": "⚽ <b>10s Football Round</b>",
        "sport_darts": "🎯 <b>10s Darts Round</b>",
        "sport_bowling": "🎳 <b>10s Bowling Round</b>",
    }
    base_card = None
    if game_type == "cards":
        r_idx = random.randint(0, len(CARD_RANKS) - 2)
        base_card = f"{CARD_RANKS[r_idx]}{random.choice(CARD_SUITS)}"

    round_data: Dict[str, Any] = {
        "game_type": game_type,
        "entries": {},
        "base_card": base_card,
        "end_ts": time.time() + ROUND_SECONDS,
    }
    ACTIVE_ROUNDS[chat_id] = round_data

    extra_line = f"\n🃏 <b>Base Card:</b> <code>{base_card}</code>" if base_card else ""
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"{title_map.get(game_type, '🎮 <b>10s Round</b>')}{extra_line}\n"
            f"⏳ <b>{ROUND_SECONDS}s</b> baaki — apni choice aur bet lock karo!\n"
            f"👥 Joined: <code>0</code>"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=round_join_keyboard(game_type),
    )

    for rem in range(ROUND_SECONDS - 1, 0, -1):
        await asyncio.sleep(1)
        if chat_id not in ACTIVE_ROUNDS:
            return
        if rem in (7, 4, 2):
            try:
                await msg.edit_text(
                    f"{title_map.get(game_type, '🎮 <b>10s Round</b>')}{extra_line}\n"
                    f"⏳ <b>{rem}s</b> baaki — jaldi entry lo!\n"
                    f"👥 Joined: <code>{len(round_data['entries'])}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=round_join_keyboard(game_type),
                )
            except TelegramError:
                pass

    await asyncio.sleep(1)
    finished = ACTIVE_ROUNDS.pop(chat_id, None)
    if not finished:
        return

    await safe_delete_message(context, chat_id, msg.message_id)

    entries: Dict[int, Dict[str, Any]] = finished["entries"]
    if not entries:
        empty_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"{title_map.get(game_type, '🎮 <b>10s Round</b>')}\n⌛ Time khatam—kisi ne entry nahi li.",
            parse_mode=ParseMode.HTML,
        )
        asyncio.create_task(schedule_delete_messages(context, chat_id, [empty_msg.message_id], 15))
        return

    extra_delete_ids: List[int] = []
    if game_type == "flip":
        outcome_str = random.choice(["heads", "tails"])
        reveal_line = f"🪙 Final Toss: <b>{outcome_str.upper()}</b>"
    elif game_type == "dice":
        d_msg = await context.bot.send_dice(chat_id=chat_id, emoji="🎲")
        extra_delete_ids.append(d_msg.message_id)
        val = d_msg.dice.value if d_msg.dice else random.randint(1, 6)
        await asyncio.sleep(2.5)
        reveal_line = f"🎲 Dice Roll: <b>{val}</b> ({'ODD' if val % 2 == 1 else 'EVEN'})"
    elif game_type == "slots":
        s_msg = await context.bot.send_dice(chat_id=chat_id, emoji="🎰")
        extra_delete_ids.append(s_msg.message_id)
        val = s_msg.dice.value if s_msg.dice else random.randint(1, 64)
        await asyncio.sleep(2.5)
        reveal_line = f"🎰 Jackpot Value: <b>{val}</b>"
    elif game_type == "cards":
        next_idx = random.randint(0, len(CARD_RANKS) - 1)
        next_suit = random.choice(CARD_SUITS)
        next_card = f"{CARD_RANKS[next_idx]}{next_suit}"
        base_idx = CARD_RANKS.index(base_card[:-1]) if base_card else 6
        is_red = next_suit in ("♥", "♦")
        reveal_line = f"🃏 Card: <code>{base_card}</code> ➡️ <b>{next_card}</b> ({'Red' if is_red else 'Black'})"
    elif game_type == "guess":
        secret_num = random.randint(1, 10)
        reveal_line = f"🔢 Secret Number: <b>{secret_num}</b>"
    elif game_type == "color":
        r_p = random.random()
        wheel_color = "violet" if r_p < 0.14 else ("red" if r_p < 0.57 else "green")
        reveal_line = f"🎡 Wheel Result: <b>{wheel_color.upper()}</b>"
    else:
        sp_emoji_map = {
            "sport_basket": ("🏀", lambda v: v >= 4, 2.0),
            "sport_football": ("⚽", lambda v: v >= 3, 1.9),
            "sport_darts": ("🎯", lambda v: v >= 5, 1.9),
            "sport_bowling": ("🎳", lambda v: v >= 5, 2.0),
        }
        em_c, sp_pred, sp_mult = sp_emoji_map[game_type]
        sp_m = await context.bot.send_dice(chat_id=chat_id, emoji=em_c)
        extra_delete_ids.append(sp_m.message_id)
        val = sp_m.dice.value if sp_m.dice else random.randint(1, 6)
        await asyncio.sleep(2.5)
        sp_hit = sp_pred(val)
        reveal_line = f"{em_c} Sport Roll: <b>{val}</b> ({'SCORED!' if sp_hit else 'MISSED!'})"

    summary_lines: List[str] = [
        f"{title_map.get(game_type, '🎮 <b>Round Result</b>')}",
        f"{reveal_line}\n",
        "📋 <b>Players Result List:</b>",
    ]

    for idx_p, (uid, pdata) in enumerate(entries.items(), 1):
        pname = pdata["name"]
        bet = pdata["bet"]
        choice = pdata["choice"]
        won = False
        payout = 0
        tag = ""

        if game_type == "flip":
            won = (choice == outcome_str)
            payout = bet * 2 if won else 0
        elif game_type == "dice":
            if choice == "odd":
                won = (val % 2 == 1)
                payout = int(bet * 1.85) if won else 0
            elif choice == "even":
                won = (val % 2 == 0)
                payout = int(bet * 1.85) if won else 0
            else:
                won = (val >= 4)
                payout = int(bet * 2.4) if val == 6 else (int(bet * 1.8) if won else 0)
        elif game_type == "slots":
            won = (val >= 43 or val in (1, 22, 43, 64))
            payout = int(bet * 4.0) if val == 64 else (int(bet * 2.0) if won else 0)
        elif game_type == "cards":
            if choice == "high":
                won = (next_idx > base_idx)
            elif choice == "low":
                won = (next_idx < base_idx)
            elif choice == "red":
                won = is_red
            else:
                won = (not is_red)
            payout = int(bet * 1.9) if won else 0
        elif game_type == "guess":
            g_num = int(choice)
            if g_num == secret_num:
                won = True
                payout = bet * 7
                tag = " (EXACT 7x)"
            elif abs(g_num - secret_num) == 1:
                won = True
                payout = int(bet * 1.5)
                tag = " (CLOSE 1.5x)"
        elif game_type == "color":
            if choice == wheel_color:
                won = True
                payout = int(bet * 4.5) if wheel_color == "violet" else int(bet * 2.0)
        else:
            if sp_hit:
                won = True
                payout = int(bet * sp_mult)

        if won and payout > 0:
            db.record_outcome(uid, True, payout=payout, chat_id=chat_id, name=pname)
            new_b = (db.get_user(uid) or {}).get("coins", 0)
            summary_lines.append(
                f"{idx_p}. <b>{pname}</b> [{choice}] — ✅ Won <code>+{payout}</code>{tag} (Bet: <code>{bet}</code>) | Bal: <code>{new_b}</code>"
            )
        else:
            db.record_outcome(uid, False, chat_id=chat_id, name=pname)
            new_b = (db.get_user(uid) or {}).get("coins", 0)
            summary_lines.append(
                f"{idx_p}. <b>{pname}</b> [{choice}] — ❌ Lost <code>-{bet}</code> | Bal: <code>{new_b}</code>"
            )

    res_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(summary_lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔁 New 10s Round", callback_data=f"round_start:{game_type}"),
                InlineKeyboardButton("🎮 Games", callback_data="menu_home"),
            ],
            [InlineKeyboardButton("❌ Close", callback_data="msg_close")],
        ]),
    )
    asyncio.create_task(
        schedule_delete_messages(context, chat_id, [res_msg.message_id] + extra_delete_ids, AUTO_DELETE_RESULT_SEC)
    )


async def start_mines_game(
    chat_id: int, user_id: int, name: str, username: Optional[str], bet: int, bombs: int, send_fn
) -> None:
    if bombs not in MINES_BOMB_CHOICES:
        bombs = 4
    if bet < 10:
        await send_fn("⚠️ Minimum bet <b>10</b> coins hai.", parse_mode=ParseMode.HTML)
        return
    db.ensure_user(user_id, name, username)
    if (chat_id, user_id) in ACTIVE_MINES:
        await send_fn("⚠️ Tumhara ek Mines game pehle se chal raha hai!")
        return
    new_bal = db.adjust_coins(user_id, -bet, chat_id=chat_id, name=name, played_delta=1)
    if new_bal is None:
        await send_fn("❌ Balance kam hai! <code>/daily</code> ya <code>/refill</code> try karo.", parse_mode=ParseMode.HTML)
        return
    db.record_last_bet(user_id, bet)
    game = {
        "user_id": user_id,
        "name": name,
        "bet": bet,
        "mines_count": bombs,
        "mines": random.sample(range(25), bombs),
        "revealed": [],
    }
    ACTIVE_MINES[(chat_id, user_id)] = game
    await send_fn(
        f"💣 <b>Mines</b> · {bombs} mines · bet {bet}\nOpen safe tiles on a 5x5 field. Every 💎 raises your multiplier.\n🪙 <b>Balance:</b> <code>{new_bal}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=build_mines_keyboard(game, reveal_all=False),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u, c = update.effective_user, update.effective_chat
    db.register_chat(c.id, c.type, c.title)

    ref_id: Optional[int] = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            ref_id = int(context.args[0].replace("ref_", "", 1))
        except ValueError:
            ref_id = None

    doc, is_new, bonus = db.ensure_user(u.id, u.first_name, u.username, referrer_id=ref_id)
    if is_new and c.type != ChatType.PRIVATE:
        db.sync_group_score(c.id, u.id, u.first_name, doc["coins"])

    mode_note = "⚡ <b>DM Mode:</b> Instant play (no 10s timer)!" if is_private_chat(c.id) else "⏳ <b>Group Mode:</b> 10s timer rounds + final result list!"
    bonus_note = f"\n🎉 <b>Referral Bonus:</b> +{bonus} coins!" if (is_new and bonus > 0) else ""
    text = (
        f"🎮 <b>ARCADE HUB</b>\n"
        f"🪙 <b>Balance:</b> <code>{doc['coins']}</code> coins\n"
        f"{mode_note}{bonus_note}\n"
        f"<i>Pick a game and good luck!</i>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = (
        "📖 <b>Arcade Guide</b>\n\n"
        "• <b>Personal Chat (DM):</b> Bina timer ke turant khelo.\n"
        "• <b>Group Chat:</b> 10s round mein sab entry lock karte hain; 10s baad sabka final result list aata hai.\n"
        "• <b>Mines:</b> <code>2, 4, 5, 7, 9</code> bombs chuno aur cash out karo.\n"
        "• <b>Sports:</b> Basketball, Football, Darts, Bowling.\n"
        "• <b>Commands:</b> <code>/bal</code>, <code>/daily</code>, <code>/refill</code>, <code>/ref</code>, <code>/give</code>, <code>/gtop</code>, <code>/top</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_bal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u, c = update.effective_user, update.effective_chat
    db.register_chat(c.id, c.type, c.title)
    doc, _, _ = db.ensure_user(u.id, u.first_name, u.username)
    played = doc.get("games_played", 0)
    won = doc.get("games_won", 0)
    wr = int((won / played) * 100) if played > 0 else 0
    await update.message.reply_text(
        f"👤 <b>{u.first_name}</b>\n"
        f"🪙 <b>Global Coins:</b> <code>{doc['coins']}</code>\n"
        f"🎮 <b>Played:</b> <code>{played}</code> | <b>Wins:</b> <code>{won}</code> ({wr}%)\n"
        f"🔥 <b>Streak:</b> <code>{doc.get('streak', 0)}</code> (Best: <code>{doc.get('best_streak', 0)}</code>)",
        parse_mode=ParseMode.HTML,
    )


async def _handle_daily_logic(user_id: int, name: str, username: Optional[str], chat_id: int) -> str:
    doc, _, _ = db.ensure_user(user_id, name, username)
    now = time.time()
    elapsed = now - float(doc.get("last_daily", 0.0))
    if elapsed < DAILY_COOLDOWN_SEC:
        return f"⏳ Agla daily claim <b>{fmt_duration(DAILY_COOLDOWN_SEC - elapsed)}</b> baad milega."
    db.update_cooldown(user_id, "last_daily", now, add_coins=DAILY_REWARD)
    db.sync_group_score(chat_id, user_id, name, DAILY_REWARD)
    return f"🎁 <b>Daily Reward</b>\n{random.choice(DAILY_DIALOGUES).format(amt=DAILY_REWARD)}\n🪙 <b>Balance:</b> <code>{doc['coins'] + DAILY_REWARD}</code>"


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u, c = update.effective_user, update.effective_chat
    db.register_chat(c.id, c.type, c.title)
    await update.message.reply_text(await _handle_daily_logic(u.id, u.first_name, u.username, c.id), parse_mode=ParseMode.HTML)


async def _handle_refill_logic(user_id: int, name: str, username: Optional[str], chat_id: int) -> str:
    doc, _, _ = db.ensure_user(user_id, name, username)
    if doc["coins"] > 0:
        return f"😏 Tumhare paas abhi <code>{doc['coins']}</code> coins hain! Refill sirf <b>0 coins</b> par milta hai."
    now = time.time()
    elapsed = now - float(doc.get("last_refill", 0.0))
    if elapsed < REFILL_COOLDOWN_SEC:
        return f"⏳ Refill cooldown: <b>{fmt_duration(REFILL_COOLDOWN_SEC - elapsed)}</b> baad try karo."
    db.update_cooldown(user_id, "last_refill", now, add_coins=REFILL_AMOUNT)
    db.sync_group_score(chat_id, user_id, name, REFILL_AMOUNT)
    return f"🆘 <b>Free Refill</b>\n{random.choice(REFILL_DIALOGUES).format(amt=REFILL_AMOUNT)}\n🪙 <b>Balance:</b> <code>{REFILL_AMOUNT}</code>"


async def cmd_refill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u, c = update.effective_user, update.effective_chat
    db.register_chat(c.id, c.type, c.title)
    await update.message.reply_text(await _handle_refill_logic(u.id, u.first_name, u.username, c.id), parse_mode=ParseMode.HTML)


async def cmd_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    doc, _, _ = db.ensure_user(u.id, u.first_name, u.username)
    link = f"https://t.me/{context.bot.username or 'YourBot'}?start=ref_{u.id}"
    await update.message.reply_text(
        f"🤝 <b>Referral Link:</b>\n<code>{link}</code>\n• Referrer: <b>+{REFERRAL_BONUS_REFERRER}</b> | New: <b>+{REFERRAL_BONUS_USER}</b>\n• Total: <code>{doc.get('referrals', 0)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_give(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    sender, chat = update.effective_user, update.effective_chat
    db.register_chat(chat.id, chat.type, chat.title)
    db.ensure_user(sender.id, sender.first_name, sender.username)

    target_doc: Optional[Dict[str, Any]] = None
    amount: Optional[int] = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        tu = update.message.reply_to_message.from_user
        if not tu.is_bot:
            target_doc, _, _ = db.ensure_user(tu.id, tu.first_name, tu.username)
            if context.args:
                try:
                    amount = int(context.args[0])
                except ValueError:
                    amount = None
    elif len(context.args) >= 2:
        target_doc = db.find_user_by_username(context.args[0])
        try:
            amount = int(context.args[1])
        except ValueError:
            amount = None

    if not target_doc or amount is None or amount <= 0 or target_doc["user_id"] == sender.id:
        await update.message.reply_text("Use: Reply karke <code>/give 100</code> ya <code>/give @username 100</code>.", parse_mode=ParseMode.HTML)
        return
    if db.adjust_coins(sender.id, -amount, chat_id=chat.id, name=sender.first_name) is None:
        await update.message.reply_text("❌ Coins kam hain!")
        return
    db.adjust_coins(target_doc["user_id"], amount, chat_id=chat.id, name=target_doc.get("name", "Player"))
    await update.message.reply_text(f"💸 <b>{sender.first_name}</b> ne <b>{target_doc.get('name', 'Player')}</b> ko <code>{amount}</code> coins bheje!", parse_mode=ParseMode.HTML)


async def cmd_gtop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    chat = update.effective_chat
    db.register_chat(chat.id, chat.type, chat.title)
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text("ℹ️ Group score ke liye group mein <code>/gtop</code> chalayein; personal/overall rank ke liye <code>/top</code>.", parse_mode=ParseMode.HTML)
        return
    leaders = db.top_group(chat.id, limit=10)
    if not leaders:
        await update.message.reply_text("📉 Is group ka score abhi 0 hai.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 <b>{chat.title or 'Group'} — Group Score Top 10</b>\n"]
    for i, r in enumerate(leaders):
        lines.append(f"{medals[i] if i < 3 else f'<b>{i+1}.</b>'} <b>{r.get('name', 'Player')}</b> — <code>{r.get('coins', 0)}</code> pts")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    leaders = db.top_global(limit=10)
    if not leaders:
        await update.message.reply_text("📉 Global leaderboard khali hai.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🌍 <b>Global Rank Top 10</b>\n"]
    for i, r in enumerate(leaders):
        lines.append(f"{medals[i] if i < 3 else f'<b>{i+1}.</b>'} <b>{r.get('name', 'Player')}</b> — <code>{r.get('coins', 0)}</code> coins")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_mines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return
    u, c = update.effective_user, update.effective_chat
    db.register_chat(c.id, c.type, c.title)
    db.ensure_user(u.id, u.first_name, u.username)
    if not context.args:
        await update.message.reply_text(
            "💣 <b>Mines</b>\nHow many mines? More mines = bigger multipliers.",
            parse_mode=ParseMode.HTML,
            reply_markup=mines_bomb_keyboard(u.id),
        )
        return
    try:
        bet = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Use: <code>/mines 100 5</code>", parse_mode=ParseMode.HTML)
        return
    bombs = 4
    if len(context.args) > 1:
        try:
            b_val = int(context.args[1])
            if b_val in MINES_BOMB_CHOICES:
                bombs = b_val
        except ValueError:
            pass
    await start_mines_game(c.id, u.id, u.first_name, u.username, bet, bombs, update.message.reply_text)


async def cmd_mycoins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    u, c = update.effective_user, update.effective_chat
    db.ensure_user(u.id, u.first_name, u.username)
    if not context.args:
        await update.message.reply_text("Use: <code>/mycoins 5000</code>", parse_mode=ParseMode.HTML)
        return
    try:
        delta = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Valid number daalo: <code>/mycoins 5000</code>", parse_mode=ParseMode.HTML)
        return
    new_bal = db.adjust_coins(u.id, delta, chat_id=c.id, name=u.first_name)
    await update.message.reply_text(f"👑 Owner Coins Updated (<code>{delta:+}</code>) | Balance: <code>{new_bal}</code>", parse_mode=ParseMode.HTML)


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
        await update.message.reply_text("⚠️ Valid number daalo: <code>/setmycoins 10000</code>", parse_mode=ParseMode.HTML)
        return
    db.set_coins(u.id, amt)
    await update.message.reply_text(f"👑 Owner Balance Set to <code>{max(0, amt)}</code>", parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    st = db.stats()
    await update.message.reply_text(
        f"📊 <b>Stats</b>\n👥 Users: <code>{st['users']}</code> | 💬 Chats: <code>{st['chats']}</code> | 🪙 Coins: <code>{st['coins']}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_addcoins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    if len(context.args) < 2:
        return
    new_bal = db.adjust_coins(int(context.args[0]), int(context.args[1]))
    await update.message.reply_text(f"✅ Updated! Balance: <code>{new_bal}</code>", parse_mode=ParseMode.HTML)


async def cmd_setcoins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    if len(context.args) < 2:
        return
    ok = db.set_coins(int(context.args[0]), int(context.args[1]))
    await update.message.reply_text("✅ Coins set!" if ok else "⚠️ User not found.")


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    reply_msg = update.message.reply_to_message
    custom_text = " ".join(context.args).strip() if context.args else ""
    if not reply_msg and not custom_text:
        await update.message.reply_text(
            "📣 Promotion / Broadcast ke liye kisi message par reply karke <code>/broadcast</code> likho ya <code>/broadcast &lt;msg&gt;</code> bhejo.",
            parse_mode=ParseMode.HTML,
        )
        return
    targets = db.all_broadcast_targets()
    status_msg = await update.message.reply_text(
        f"📡 Broadcasting to <code>{len(targets)}</code> personal &amp; group chats...",
        parse_mode=ParseMode.HTML,
    )
    sent = failed = 0
    for cid in targets:
        try:
            if reply_msg:
                await context.bot.copy_message(cid, reply_msg.chat_id, reply_msg.message_id)
            else:
                await context.bot.send_message(cid, f"📣 <b>Promo &amp; Update</b>\n\n{custom_text}", parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.06)
        except (Forbidden, TelegramError):
            failed += 1
    await status_msg.edit_text(
        f"✅ Broadcast Complete!\n📨 Delivered: <code>{sent}</code> | ⚠️ Skipped: <code>{failed}</code>",
        parse_mode=ParseMode.HTML,
    )


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    cleanup_locked_messages()
    user, chat, data = query.from_user, query.message.chat, query.data or ""
    db.register_chat(chat.id, chat.type, chat.title)
    doc, _, _ = db.ensure_user(user.id, user.first_name, user.username)

    if data == "msg_close":
        await query.answer("Closed")
        await safe_delete_message(context, chat.id, query.message.message_id)
        return

    if data.startswith("u:"):
        parts = data.split(":")
        owner_uid = int(parts[1])
        if user.id != owner_uid:
            await query.answer("🙅 Ye action sirf usi player ke liye hai!", show_alert=True)
            return
        sub = ":".join(parts[2:])

        msg_lock_key = (chat.id, query.message.message_id)
        if sub.startswith("sgo:") or sub.startswith("rbet:"):
            if msg_lock_key in LOCKED_SOLO_MESSAGES:
                await query.answer("Ye button pehle hi use ho chuka hai!", show_alert=True)
                return
            LOCKED_SOLO_MESSAGES[msg_lock_key] = time.time()

        if sub == "mines_noop":
            await query.answer("Pehle 1 diamond tile kholo!")
            return

        if sub == "refill_now":
            await query.answer()
            txt = await _handle_refill_logic(user.id, user.first_name, user.username, chat.id)
            ref_m = await query.message.reply_text(txt, parse_mode=ParseMode.HTML)
            if not is_private_chat(chat.id):
                asyncio.create_task(schedule_delete_messages(context, chat.id, [ref_m.message_id], 20))
            return

        if sub.startswith("mines_bombs:"):
            bombs = int(sub.split(":", 1)[1])
            await query.answer()
            cur_coins = (db.get_user(user.id) or {}).get("coins", 0)
            await query.edit_message_text(
                f"💣 <b>Mines</b> ({bombs} bombs)\n🪙 Balance: <code>{cur_coins}</code>\n👇 Choose your bet:",
                parse_mode=ParseMode.HTML,
                reply_markup=bet_keyboard(user.id, "mines", user_coins=cur_coins, extra=str(bombs)),
            )
            return

        if sub.startswith("bet:"):
            _, g_type, bet_str, extra = (sub.split(":") + ["default"])[:4]
            bet = int(bet_str)
            await query.answer()
            if g_type == "mines":
                bombs = int(extra) if extra.isdigit() else 4
                await safe_delete_message(context, chat.id, query.message.message_id)
                await start_mines_game(chat.id, user.id, user.first_name, user.username, bet, bombs, query.message.reply_text)
            elif g_type == "slots":
                await safe_delete_message(context, chat.id, query.message.message_id)
                await resolve_solo_game(context, chat.id, user.id, user.first_name, user.username, "slots", bet, "spin")
            elif g_type.startswith("sport_"):
                await safe_delete_message(context, chat.id, query.message.message_id)
                await resolve_solo_game(context, chat.id, user.id, user.first_name, user.username, g_type, bet, "shot")
            else:
                b_card = "none"
                extra_txt = ""
                if g_type == "cards":
                    b_card = f"{CARD_RANKS[random.randint(0, len(CARD_RANKS)-2)]}{random.choice(CARD_SUITS)}"
                    extra_txt = f"\n🃏 Base Card: <code>{b_card}</code>"
                await query.edit_message_text(
                    f"🎯 <b>{user.first_name}</b> · bet {bet}{extra_txt}\nApna option chuno:",
                    parse_mode=ParseMode.HTML,
                    reply_markup=solo_choice_keyboard(user.id, g_type, bet, b_card),
                )
            return

        if sub.startswith("sgo:"):
            _, g_type, bet_str, choice, b_card = (sub.split(":") + ["none"])[:5]
            bet = int(bet_str)
            await query.answer()
            await safe_delete_message(context, chat.id, query.message.message_id)
            await resolve_solo_game(
                context, chat.id, user.id, user.first_name, user.username, g_type, bet, choice, base_card=b_card
            )
            return

        if sub.startswith("rbet:"):
            _, g_type, choice, bet_str = sub.split(":", 3)
            bet = int(bet_str)
            rnd = ACTIVE_ROUNDS.get(chat.id)
            if not rnd or time.time() >= rnd["end_ts"]:
                await query.answer("10s time up!", show_alert=True)
                await safe_delete_message(context, chat.id, query.message.message_id)
                return
            if user.id in rnd["entries"]:
                await query.answer("Ek round mein sirf 1 hi bet lagti hai!", show_alert=True)
                return
            new_bal = db.adjust_coins(user.id, -bet, chat_id=chat.id, name=user.first_name, played_delta=1)
            if new_bal is None:
                await query.answer("Coins kam hain!", show_alert=True)
                return
            db.record_last_bet(user.id, bet)
            rnd["entries"][user.id] = {"name": user.first_name, "choice": choice, "bet": bet}
            await query.answer(f"✅ Locked ({bet})!")
            await safe_delete_message(context, chat.id, query.message.message_id)
            conf_msg = await context.bot.send_message(
                chat.id,
                f"✅ <b>{user.first_name}</b> locked <code>{bet}</code> on <b>{choice.upper()}</b>",
                parse_mode=ParseMode.HTML,
            )
            asyncio.create_task(schedule_delete_messages(context, chat.id, [conf_msg.message_id], 7))
            return

        if sub.startswith("replay:"):
            parts_r = sub.split(":")
            g_type = parts_r[1]
            bet = int(parts_r[2])
            extra = parts_r[3] if len(parts_r) > 3 else "4"
            await query.answer(f"🔁 Play again ({bet})!")
            if not is_private_chat(chat.id):
                await safe_delete_message(context, chat.id, query.message.message_id)
            if g_type == "mines":
                bombs = int(extra) if extra.isdigit() else 4
                await start_mines_game(chat.id, user.id, user.first_name, user.username, bet, bombs, query.message.reply_text)
            else:
                await resolve_solo_game(context, chat.id, user.id, user.first_name, user.username, g_type, bet, extra)
            return

        game_key = (chat.id, user.id)
        if sub.startswith("mines_pick:") or sub == "mines_cashout":
            game = ACTIVE_MINES.get(game_key)
            if not game:
                await query.answer("Ye Mines game khatam ho chuka hai.")
                return
            if sub == "mines_cashout":
                rev_cnt = len(game["revealed"])
                if rev_cnt < 1:
                    await query.answer("Pehle 1 diamond kholo!", show_alert=True)
                    return
                ACTIVE_MINES.pop(game_key, None)
                mult = calc_mines_multiplier(rev_cnt, game["mines_count"])
                payout = int(game["bet"] * mult)
                db.record_outcome(user.id, True, payout=payout, chat_id=chat.id, name=user.first_name)
                new_bal = (db.get_user(user.id) or {}).get("coins", 0)
                line = random.choice(MINES_WIN_DIALOGUES).format(payout=payout, profit=max(0, payout - game["bet"]))
                await query.answer("💰 Cash Out!")
                await query.edit_message_text(
                    f"💣 <b>Mines · {game['mines_count']} mines · bet {game['bet']}</b>\n💰 Cashed out at <b>{mult}x</b>\n✅ <b>You won +{payout} coins!</b>\n🪙 <b>Balance:</b> <code>{new_bal}</code>\n<i>{line}</i>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_mines_keyboard(game, True),
                )
                if not is_private_chat(chat.id):
                    asyncio.create_task(schedule_delete_messages(context, chat.id, [query.message.message_id], AUTO_DELETE_RESULT_SEC))
                return

            idx = int(sub.split(":", 1)[1])
            if idx in game["revealed"]:
                await query.answer("Tile pehle hi khul chuka hai!")
                return
            if idx in game["mines"]:
                ACTIVE_MINES.pop(game_key, None)
                db.record_outcome(user.id, False, chat_id=chat.id, name=user.first_name)
                cur_doc = db.get_user(user.id) or {"coins": 0}
                await query.answer("💥 BOOM!")
                await query.edit_message_text(
                    f"💣 <b>Mines · {game['mines_count']} mines · bet {game['bet']}</b>\n💥 {random.choice(MINES_LOSS_DIALOGUES).format(bet=game['bet'])}\n🪙 <b>Balance:</b> <code>{cur_doc['coins']}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_mines_keyboard(game, True),
                )
                if not is_private_chat(chat.id):
                    asyncio.create_task(schedule_delete_messages(context, chat.id, [query.message.message_id], AUTO_DELETE_RESULT_SEC))
                return

            game["revealed"].append(idx)
            rev_cnt = len(game["revealed"])
            safe_total = 25 - game["mines_count"]
            mult = calc_mines_multiplier(rev_cnt, game["mines_count"])
            cashout_amt = int(game["bet"] * mult)
            if rev_cnt >= safe_total:
                ACTIVE_MINES.pop(game_key, None)
                db.record_outcome(user.id, True, payout=cashout_amt, chat_id=chat.id, name=user.first_name)
                new_bal = (db.get_user(user.id) or {}).get("coins", 0)
                await query.answer("🏆 Full Clear!")
                await query.edit_message_text(
                    f"💣 <b>Mines · {game['mines_count']} mines · bet {game['bet']}</b>\n🏆 Full Clear! Won <b>+{cashout_amt}</b> coins ({mult}x)\n🪙 <b>Balance:</b> <code>{new_bal}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_mines_keyboard(game, True),
                )
                if not is_private_chat(chat.id):
                    asyncio.create_task(schedule_delete_messages(context, chat.id, [query.message.message_id], AUTO_DELETE_RESULT_SEC))
                return

            await query.answer(f"💎 Safe! {mult}x")
            await query.edit_message_text(
                f"💣 <b>Mines</b> · {game['mines_count']} mines · bet {game['bet']}\n🪙 Mult: <b>{mult}x</b> | Cash Out: <code>{cashout_amt}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=build_mines_keyboard(game, False),
            )
            return

    if data == "menu_home":
        await query.answer()
        cur_doc = db.get_user(user.id) or {"coins": 0}
        await query.edit_message_text(
            f"🎮 <b>ARCADE HUB</b>\n🪙 <b>Balance:</b> <code>{cur_doc['coins']}</code> coins\n<i>Pick a game and good luck!</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
        return

    if data == "menu_mines":
        await query.answer()
        await query.edit_message_text(
            "💣 <b>Mines</b>\nHow many mines? More mines = bigger multipliers.",
            parse_mode=ParseMode.HTML,
            reply_markup=mines_bomb_keyboard(user.id),
        )
        return

    if data == "menu_sports":
        await query.answer()
        await query.edit_message_text(
            "🏀 <b>Sports</b>\nPick your game:",
            parse_mode=ParseMode.HTML,
            reply_markup=sports_keyboard(),
        )
        return

    if data.startswith("pick_game:"):
        g_type = data.split(":", 1)[1]
        await query.answer()
        if is_private_chat(chat.id):
            cur_coins = (db.get_user(user.id) or {}).get("coins", 0)
            await query.edit_message_text(
                f"🪙 <b>Balance:</b> <code>{cur_coins}</code> coins\n👇 Choose your bet:",
                parse_mode=ParseMode.HTML,
                reply_markup=bet_keyboard(user.id, g_type, user_coins=cur_coins),
            )
        else:
            if chat.id in ACTIVE_ROUNDS:
                await query.answer("Is group mein 10s round pehle se chal raha hai!", show_alert=True)
                return
            asyncio.create_task(run_ten_sec_round(context, chat.id, g_type))
        return

    if data.startswith("round_start:"):
        g_type = data.split(":", 1)[1]
        if is_private_chat(chat.id):
            await query.answer()
            cur_coins = (db.get_user(user.id) or {}).get("coins", 0)
            await query.message.reply_text(
                f"🪙 <b>Balance:</b> <code>{cur_coins}</code> coins\n👇 Choose your bet:",
                parse_mode=ParseMode.HTML,
                reply_markup=bet_keyboard(user.id, g_type, user_coins=cur_coins),
            )
            return
        if chat.id in ACTIVE_ROUNDS:
            await query.answer("10s round pehle se chal raha hai!", show_alert=True)
            return
        await query.answer("10s Round Started!")
        asyncio.create_task(run_ten_sec_round(context, chat.id, g_type))
        return

    if data.startswith("rjoin:"):
        _, g_type, choice = data.split(":", 2)
        rnd = ACTIVE_ROUNDS.get(chat.id)
        if not rnd or time.time() >= rnd["end_ts"]:
            await query.answer("Round khatam ho chuka hai!", show_alert=True)
            return
        if user.id in rnd["entries"]:
            await query.answer("Tum is round mein pehle hi entry le chuke ho!", show_alert=True)
            return
        if doc["coins"] < 10:
            await query.answer("Minimum 10 coins chahiye! /daily ya /refill karo.", show_alert=True)
            return
        await query.answer(f"Choice: {choice.upper()}")
        pick_msg = await query.message.reply_text(
            f"🎯 <b>{user.first_name}</b> — Choice: <b>{choice.upper()}</b> | Bet chuno:",
            parse_mode=ParseMode.HTML,
            reply_markup=round_bet_keyboard(user.id, g_type, choice, doc["coins"]),
        )
        asyncio.create_task(schedule_delete_messages(context, chat.id, [pick_msg.message_id], ROUND_SECONDS + 1))
        return

    if data == "act_daily":
        await query.answer()
        await query.message.reply_text(await _handle_daily_logic(user.id, user.first_name, user.username, chat.id), parse_mode=ParseMode.HTML)
    elif data == "act_profile":
        await query.answer()
        cur_doc = db.get_user(user.id) or {}
        await query.message.reply_text(
            f"👤 <b>{user.first_name}</b> | 🪙 <code>{cur_doc.get('coins', 0)}</code> | Wins: <code>{cur_doc.get('games_won', 0)}</code> | 🔥 <code>{cur_doc.get('streak', 0)}</code>",
            parse_mode=ParseMode.HTML,
        )
    elif data == "act_top":
        await query.answer()
        g_top = db.top_global(5)
        lines = ["🌍 <b>Global Top 5</b>"] + [
            f"{i}. <b>{r.get('name', 'Player')}</b> — <code>{r.get('coins', 0)}</code>"
            for i, r in enumerate(g_top, 1)
        ]
        if chat.id < 0:
            c_top = db.top_group(chat.id, 5)
            lines.append("\n🏆 <b>This Group Top 5</b>")
            lines.extend([
                f"{i}. <b>{r.get('name', 'Player')}</b> — <code>{r.get('coins', 0)}</code>"
                for i, r in enumerate(c_top, 1)
            ])
        await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    elif data == "act_help":
        await query.answer()
        await query.message.reply_text(
            "📖 DM mein bina timer ke khelo; group mein 10s round ke baad sabka result list aata hai.\nCommands: <code>/bal</code>, <code>/daily</code>, <code>/refill</code>, <code>/ref</code>, <code>/give</code>, <code>/gtop</code>, <code>/top</code>",
            parse_mode=ParseMode.HTML,
        )


async def track_chat_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat:
        db.register_chat(update.effective_chat.id, update.effective_chat.type, update.effective_chat.title)


flask_app = Flask(__name__)


@flask_app.route("/")
@flask_app.route("/health")
def health_check():
    return jsonify({"status": "ok", "bot": "running"}), 200


def run_flask() -> None:
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def build_bot_app() -> Application:
    return (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(20.0)
        .read_timeout(30.0)
        .get_updates_connect_timeout(20.0)
        .get_updates_read_timeout(35.0)
        .build()
    )


async def run_bot_forever() -> None:
    while True:
        app = build_bot_app()
        for cmd, fn in [
            ("start", cmd_start), ("help", cmd_help), ("bal", cmd_bal),
            ("daily", cmd_daily), ("refill", cmd_refill), ("ref", cmd_ref),
            ("give", cmd_give), ("gtop", cmd_gtop), ("top", cmd_top),
            ("mines", cmd_mines),
            ("mycoins", cmd_mycoins), ("setmycoins", cmd_setmycoins),
            ("broadcast", cmd_broadcast), ("stats", cmd_stats),
            ("addcoins", cmd_addcoins), ("setcoins", cmd_setcoins),
        ]:
            app.add_handler(CommandHandler(cmd, fn))
        app.add_handler(CallbackQueryHandler(on_callback_query))
        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_chat_activity))

        try:
            logger.info("Initializing Telegram bot...")
            await app.initialize()
            await app.start()
            await app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                poll_interval=1.0,
                timeout=15,
                bootstrap_retries=-1,
            )
            logger.info("Telegram bot polling active.")
            while True:
                await asyncio.sleep(3600)
        except RetryAfter as exc:
            wait_sec = int(exc.retry_after) + 5
            logger.warning("Telegram flood limit hit. Waiting %s seconds...", wait_sec)
            await asyncio.sleep(wait_sec)
        except TimedOut:
            logger.warning("Telegram request timed out. Retrying in 6 seconds...")
            await asyncio.sleep(6)
        except TelegramError as exc:
            logger.error("Telegram error (%s). Retrying in 8 seconds...", exc)
            await asyncio.sleep(8)
        except Exception as exc:
            logger.error("Unexpected error (%s). Retrying in 8 seconds...", exc)
            await asyncio.sleep(8)
        finally:
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
                if app.running:
                    await app.stop()
                await app.shutdown()
            except Exception:
                pass


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing!")

    threading.Thread(target=run_flask, daemon=True).start()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_bot_forever())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
