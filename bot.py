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
from telegram.error import Forbidden, TelegramError
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

DAILY_REWARD = 500
DAILY_COOLDOWN_SEC = 24 * 3600
REFILL_AMOUNT = 150
REFILL_COOLDOWN_SEC = 2 * 3600
REFERRAL_BONUS_REFERRER = 250
REFERRAL_BONUS_USER = 200
WELCOME_COINS = 250

BET_OPTIONS = [10, 50, 100, 250, 500, 1000]

DAILY_DIALOGUES = [
    "Lo bhai rozana ka chanda! +{amt} coins aa gaye—udaao mat, dhang se khelo.",
    "Subah-shaam ka kharcha paani set! +{amt} coins jeb mein daal liye hain.",
    "Daily dose aa gayi bhai! +{amt} coins ke saath leaderboard hilane ka time aa gaya.",
]

REFILL_DIALOGUES = [
    "Haaye re kismat! Pocket ekdum khali ho gayi. Ye lo +{amt} coins ki rescue bheekh—ab phir try kar le!",
    "Kangal mode activated tha kya? +{amt} coins de diye hain, is baar samajh ke khelna!",
    "Zero balance dekh kar daya aa gayi! +{amt} coins lo aur wapas line mein aao.",
]

MINES_WIN_DIALOGUES = [
    "Cash Out maara bhai ne! Jahan darr wahan bachat—+{payout} coins (Profit: +{profit}) account mein!",
    "Kya timing hai boss! Bomb phatne se pehle maal utha liya—+{payout} coins jeet gaye!",
    "Smart khel gaya bhai! +{payout} coins ke saath group walon ko jalane ka mauka mil gaya.",
]

MINES_LOSS_DIALOGUES = [
    "Booooom!! 💥 Diamond samajh kar bomb daba diya. {bet} coins hawa mein ud gaye!",
    "Arre bhai! Ek step aur bachna tha bas—bomb ne poora hisaab barabar kar diya (-{bet} coins)!",
    "Kismat ne dhokha de diya! {bet} coins ka blast ho gaya, agla round phir try kar.",
]

COIN_WIN_DIALOGUES = [
    "Sikka kya uchhla, kismat bhi chamak gayi! +{payout} coins jeet gaye!",
    "Akele hi maidaan maar liya bhai ne! +{payout} coins account mein aa gaye.",
    "Sahi call liya boss! +{payout} coins jeb mein.",
]

COIN_LOSS_DIALOGUES = [
    "Ulta pad gaya sikka! {bet} coins gaye—kismat ko dosh de sakte ho.",
    "Akele khelne nikle the, kismat ne saath nahi diya—{bet} coins gaya!",
    "Is baar toss ne bewafai kar di! {bet} coins loss.",
]

EMOJI_WIN_DIALOGUES = [
    "Kya baat hai! Animation ne saath diya aur +{payout} coins baras gaye!",
    "Ekdum nishane par! +{payout} coins ka dhamaka ho gaya.",
    "Lucky roll/spin! +{payout} coins jeet kar score upar le aaye.",
]

EMOJI_LOSS_DIALOGUES = [
    "Bas thoda sa reh gaya! {bet} coins gaye—ek aur try banta hai.",
    "Animation mast tha, par coins ud gaye ({bet} loss)! Agla mauka lo.",
    "Kismat thodi naraz lag rahi hai! {bet} coins gaye.",
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
def is_owner(user_id: Optional[int]) -> bool:
    return OWNER_ID is not None and user_id == OWNER_ID


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
    raw_mult = 0.94 / max(prob, 1e-6)
    return round(max(1.05, min(raw_mult, 25.0)), 2)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("💣 Mines (Solo)", callback_data="menu_mines"),
            InlineKeyboardButton("🪙 Coin Flip", callback_data="menu_flip"),
        ],
        [
            InlineKeyboardButton("🎲 Dice Bet", callback_data="menu_dice"),
            InlineKeyboardButton("🎰 Slots Bet", callback_data="menu_slots"),
        ],
        [
            InlineKeyboardButton("🎁 Daily (24h)", callback_data="act_daily"),
            InlineKeyboardButton("🆘 Refill", callback_data="act_refill"),
        ],
        [
            InlineKeyboardButton("👤 Profile", callback_data="act_profile"),
            InlineKeyboardButton("🏆 Top Ranks", callback_data="act_top"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def bet_keyboard(game_prefix: str, extra: str = "") -> InlineKeyboardMarkup:
    suffix = f":{extra}" if extra else ""
    row1 = [InlineKeyboardButton(str(b), callback_data=f"bet:{game_prefix}:{b}{suffix}") for b in BET_OPTIONS[:3]]
    row2 = [InlineKeyboardButton(str(b), callback_data=f"bet:{game_prefix}:{b}{suffix}") for b in BET_OPTIONS[3:]]
    back_btn = [InlineKeyboardButton("⬅️ Back", callback_data="menu_home")]
    return InlineKeyboardMarkup([row1, row2, back_btn])


def build_mines_keyboard(game: Dict[str, Any], reveal_all: bool = False) -> InlineKeyboardMarkup:
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
                label = "💣" if idx in mines_set else "🔹"
            else:
                label = "⬜"
            row_btns.append(InlineKeyboardButton(label, callback_data=f"mines_pick:{idx}"))
        rows.append(row_btns)

    if not reveal_all:
        mult = calc_mines_multiplier(len(revealed_set), game["mines_count"])
        cashout_amt = int(game["bet"] * mult)
        if len(revealed_set) >= 1:
            rows.append([InlineKeyboardButton(f"💰 Cash Out (+{cashout_amt} coins)", callback_data="mines_cashout")])
        else:
            rows.append([InlineKeyboardButton("💎 Open a tile to unlock Cash Out", callback_data="mines_noop")])
    else:
        rows.append([InlineKeyboardButton("🔄 Play Mines Again", callback_data="menu_mines")])

    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = update.effective_user
    chat = update.effective_chat
    db.register_chat(chat.id, chat.type, chat.title)

    ref_id: Optional[int] = None
    if context.args:
        arg0 = context.args[0].strip()
        if arg0.startswith("ref_"):
            try:
                ref_id = int(arg0.replace("ref_", "", 1))
            except ValueError:
                ref_id = None

    doc, is_new, bonus = db.ensure_user(user.id, user.first_name, user.username, referrer_id=ref_id)
    if is_new and chat.type != ChatType.PRIVATE:
        db.sync_group_score(chat.id, user.id, user.first_name, doc["coins"])

    bonus_note = f"\n🎉 <b>Referral Bonus:</b> +{bonus} coins mil gaye!" if (is_new and bonus > 0) else ""
    text = (
        f"👋 Swagat hai, <b>{user.first_name}</b>!\n"
        f"🎮 <b>Group Arcade &amp; Solo Grind Bot</b>\n\n"
        f"🪙 <b>Balance:</b> <code>{doc['coins']}</code> coins\n"
        f"<i>Akele khelkar coins badhao aur group mein rank chamkao!</i>{bonus_note}\n\n"
        f"<b>Quick Commands:</b>\n"
        f"• /daily — 24hr free coins\n"
        f"• /refill — 0 balance par rescue coins\n"
        f"• /ref — Apna referral link\n"
        f"• /gtop — Group leaderboard | /top — Global top\n"
        f"• /mines &lt;bet&gt;, /flip &lt;bet&gt;, /dice &lt;bet&gt;, /slots &lt;bet&gt;"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = (
        "📖 <b>Kaise Khelein &amp; Coins Badhayein?</b>\n\n"
        "<b>Solo Games:</b>\n"
        "• <code>/mines 100</code> — 5x5 grid mein 💎 dhoondo, 💣 se bacho, Cash Out karo.\n"
        "• <code>/flip 100</code> — Heads/Tails bet lagao.\n"
        "• <code>/dice 100</code> — 🎲 roll par 4, 5, 6 aaya toh jeet!\n"
        "• <code>/slots 100</code> — 🎰 spin par jackpot/win!\n\n"
        "<b>Coins &amp; Ranks:</b>\n"
        "• <code>/bal</code>, <code>/daily</code>, <code>/refill</code>, <code>/ref</code>, <code>/give</code>, <code>/gtop</code>, <code>/top</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_bal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    user = update.effective_user
    db.register_chat(update.effective_chat.id, update.effective_chat.type, update.effective_chat.title)
    doc, _, _ = db.ensure_user(user.id, user.first_name, user.username)
    played = doc.get("games_played", 0)
    won = doc.get("games_won", 0)
    win_rate = int((won / played) * 100) if played > 0 else 0
    text = (
        f"👤 <b>{user.first_name}</b> ka Profile\n"
        f"🪙 <b>Coins:</b> <code>{doc['coins']}</code>\n"
        f"🎮 <b>Played:</b> <code>{played}</code> | <b>Wins:</b> <code>{won}</code> ({win_rate}%)\n"
        f"🤝 <b>Referrals:</b> <code>{doc.get('referrals', 0)}</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def _handle_daily_logic(user_id: int, name: str, username: Optional[str], chat_id: int) -> str:
    doc, _, _ = db.ensure_user(user_id, name, username)
    now = time.time()
    elapsed = now - float(doc.get("last_daily", 0.0))
    if elapsed < DAILY_COOLDOWN_SEC:
        return f"⏳ Bhai abhi daily coins le chuke ho! Agla claim <b>{fmt_duration(DAILY_COOLDOWN_SEC - elapsed)}</b> baad milega."
    db.update_cooldown(user_id, "last_daily", now, add_coins=DAILY_REWARD)
    db.sync_group_score(chat_id, user_id, name, DAILY_REWARD)
    line = random.choice(DAILY_DIALOGUES).format(amt=DAILY_REWARD)
    return f"🎁 <b>Daily Reward</b>\n{line}\n\n🪙 <b>Naya Balance:</b> <code>{doc['coins'] + DAILY_REWARD}</code> coins"


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u, c = update.effective_user, update.effective_chat
    db.register_chat(c.id, c.type, c.title)
    await update.message.reply_text(await _handle_daily_logic(u.id, u.first_name, u.username, c.id), parse_mode=ParseMode.HTML)


async def _handle_refill_logic(user_id: int, name: str, username: Optional[str], chat_id: int) -> str:
    doc, _, _ = db.ensure_user(user_id, name, username)
    if doc["coins"] > 0:
        return f"😏 Tumhare paas abhi <code>{doc['coins']}</code> coins hain! Refill sirf <b>0 coins</b> hone par milta hai."
    now = time.time()
    elapsed = now - float(doc.get("last_refill", 0.0))
    if elapsed < REFILL_COOLDOWN_SEC:
        return f"⏳ Free refill cooldown chal raha hai! <b>{fmt_duration(REFILL_COOLDOWN_SEC - elapsed)}</b> baad try karo."
    db.update_cooldown(user_id, "last_refill", now, add_coins=REFILL_AMOUNT)
    db.sync_group_score(chat_id, user_id, name, REFILL_AMOUNT)
    line = random.choice(REFILL_DIALOGUES).format(amt=REFILL_AMOUNT)
    return f"🆘 <b>Free Refill</b>\n{line}\n\n🪙 <b>Balance:</b> <code>{REFILL_AMOUNT}</code> coins"


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
        f"🤝 <b>Referral Link:</b>\n<code>{link}</code>\n• Referrer: <b>+{REFERRAL_BONUS_REFERRER}</b> | New User: <b>+{REFERRAL_BONUS_USER}</b>\n• Total: <code>{doc.get('referrals', 0)}</code>",
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
        await update.message.reply_text("❌ Itne coins nahi hain tumhare paas!")
        return
    db.adjust_coins(target_doc["user_id"], amount, chat_id=chat.id, name=target_doc.get("name", "Player"))
    await update.message.reply_text(f"💸 <b>{sender.first_name}</b> ne <b>{target_doc.get('name', 'Player')}</b> ko <code>{amount}</code> coins bhej diye!", parse_mode=ParseMode.HTML)


async def cmd_gtop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return
    chat = update.effective_chat
    db.register_chat(chat.id, chat.type, chat.title)
    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text("ℹ️ Group leaderboard ke liye is command ko group mein chalayein, ya <code>/top</code> use karein.", parse_mode=ParseMode.HTML)
        return
    leaders = db.top_group(chat.id, limit=10)
    if not leaders:
        await update.message.reply_text("📉 Is group mein abhi koi score nahi hai.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 <b>{chat.title or 'Group'} — Top 10</b>\n"]
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
    lines = ["🌍 <b>Global Top 10</b>\n"]
    for i, r in enumerate(leaders):
        lines.append(f"{medals[i] if i < 3 else f'<b>{i+1}.</b>'} <b>{r.get('name', 'Player')}</b> — <code>{r.get('coins', 0)}</code> coins")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def start_mines_game(chat_id: int, user_id: int, name: str, username: Optional[str], bet: int, send_fn) -> None:
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
    game = {
        "user_id": user_id,
        "name": name,
        "bet": bet,
        "mines_count": 4,
        "mines": random.sample(range(25), 4),
        "revealed": [],
    }
    ACTIVE_MINES[(chat_id, user_id)] = game
    await send_fn(
        f"💣 <b>Mines (Solo)</b> — <b>{name}</b>\n🪙 <b>Bet:</b> <code>{bet}</code> | <b>Bombs:</b> <code>4</code>\n🪙 <b>Balance:</b> <code>{new_bal}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=build_mines_keyboard(game, reveal_all=False),
    )


async def cmd_mines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return
    u, c = update.effective_user, update.effective_chat
    db.register_chat(c.id, c.type, c.title)
    if not context.args:
        await update.message.reply_text("💣 <b>Mines</b> — Bet chuno:", parse_mode=ParseMode.HTML, reply_markup=bet_keyboard("mines"))
        return
    try:
        await start_mines_game(c.id, u.id, u.first_name, u.username, int(context.args[0]), update.message.reply_text)
    except ValueError:
        await update.message.reply_text("⚠️ Number daalo: <code>/mines 100</code>", parse_mode=ParseMode.HTML)


async def play_coin_flip(chat_id: int, user_id: int, name: str, username: Optional[str], bet: int, choice: str, send_fn) -> None:
    choice_clean = choice.lower() if choice.lower() in ("heads", "tails") else random.choice(["heads", "tails"])
    if bet < 10:
        await send_fn("⚠️ Minimum bet <b>10</b> coins hai.", parse_mode=ParseMode.HTML)
        return
    db.ensure_user(user_id, name, username)
    bal_after_bet = db.adjust_coins(user_id, -bet, chat_id=chat_id, name=name, played_delta=1)
    if bal_after_bet is None:
        await send_fn("❌ Balance kam hai!", parse_mode=ParseMode.HTML)
        return
    outcome = random.choice(["heads", "tails"])
    if outcome == choice_clean:
        payout = bet * 2
        final_bal = db.adjust_coins(user_id, payout, chat_id=chat_id, name=name, won_delta=1)
        status = f"🎉 <b>JEET GAYE!</b> ({outcome.upper()})\n{random.choice(COIN_WIN_DIALOGUES).format(payout=payout)}"
    else:
        final_bal = bal_after_bet
        status = f"💀 <b>HAAR GAYE!</b> ({outcome.upper()})\n{random.choice(COIN_LOSS_DIALOGUES).format(bet=bet)}"
    await send_fn(f"🪙 <b>Coin Flip</b> — <b>{name}</b>\n{status}\n🪙 <b>Balance:</b> <code>{final_bal}</code>", parse_mode=ParseMode.HTML)


async def cmd_flip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return
    u, c = update.effective_user, update.effective_chat
    db.register_chat(c.id, c.type, c.title)
    if not context.args:
        await update.message.reply_text("🪙 <b>Coin Flip</b> — Bet chuno:", parse_mode=ParseMode.HTML, reply_markup=bet_keyboard("flip_pick"))
        return
    try:
        bet = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Use: <code>/flip 100</code>", parse_mode=ParseMode.HTML)
        return
    side = context.args[1].lower() if len(context.args) > 1 else ""
    if side in ("h", "heads", "t", "tails"):
        await play_coin_flip(c.id, u.id, u.first_name, u.username, bet, "heads" if side.startswith("h") else "tails", update.message.reply_text)
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(f"👑 Heads ({bet})", callback_data=f"flip_go:{bet}:heads"),
            InlineKeyboardButton(f"🦅 Tails ({bet})", callback_data=f"flip_go:{bet}:tails"),
        ]])
        await update.message.reply_text(f"🪙 <b>Coin Flip</b> ({bet}) — Heads ya Tails?", parse_mode=ParseMode.HTML, reply_markup=kb)


async def play_emoji_game(update: Update, context: ContextTypes.DEFAULT_TYPE, emoji_char: str, title: str, bet: int) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    u, c = update.effective_user, update.effective_chat
    if bet < 10:
        await context.bot.send_message(c.id, "⚠️ Minimum bet <b>10</b> coins hai.", parse_mode=ParseMode.HTML)
        return
    db.ensure_user(u.id, u.first_name, u.username)
    bal_after_bet = db.adjust_coins(u.id, -bet, chat_id=c.id, name=u.first_name, played_delta=1)
    if bal_after_bet is None:
        await context.bot.send_message(c.id, "❌ Balance kam hai!", parse_mode=ParseMode.HTML)
        return
    dice_msg = await context.bot.send_dice(chat_id=c.id, emoji=emoji_char)
    val = dice_msg.dice.value if dice_msg.dice else 0
    await asyncio.sleep(3.2)
    if emoji_char == "🎲":
        won = val >= 4
        mult = 2.5 if val == 6 else (1.8 if won else 0.0)
    else:
        won = val >= 43 or val in (1, 22, 43, 64)
        mult = 4.0 if val == 64 else (2.0 if won else 0.0)
    if won:
        payout = int(bet * mult)
        final_bal = db.adjust_coins(u.id, payout, chat_id=c.id, name=u.first_name, won_delta=1)
        txt = f"🎉 <b>JEET GAYE!</b> (Val: <code>{val}</code> | {mult}x)\n{random.choice(EMOJI_WIN_DIALOGUES).format(payout=payout)}\n🪙 <b>Balance:</b> <code>{final_bal}</code>"
    else:
        txt = f"📉 <b>HAAR GAYE!</b> (Val: <code>{val}</code>)\n{random.choice(EMOJI_LOSS_DIALOGUES).format(bet=bet)}\n🪙 <b>Balance:</b> <code>{bal_after_bet}</code>"
    await context.bot.send_message(c.id, f"{title} — <b>{u.first_name}</b>\n{txt}", parse_mode=ParseMode.HTML, reply_to_message_id=dice_msg.message_id)


async def cmd_dice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return
    u, c = update.effective_user, update.effective_chat
    db.register_chat(c.id, c.type, c.title)
    if not context.args:
        await update.message.reply_text("🎲 <b>Dice Bet</b> — Amount chuno:", parse_mode=ParseMode.HTML, reply_markup=bet_keyboard("dice"))
        return
    try:
        await play_emoji_game(update, context, "🎲", "🎲 <b>Dice Bet</b>", int(context.args[0]))
    except ValueError:
        await update.message.reply_text("⚠️ Use: <code>/dice 100</code>", parse_mode=ParseMode.HTML)


async def cmd_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.effective_chat:
        return
    u, c = update.effective_user, update.effective_chat
    db.register_chat(c.id, c.type, c.title)
    if not context.args:
        await update.message.reply_text("🎰 <b>Slots Bet</b> — Amount chuno:", parse_mode=ParseMode.HTML, reply_markup=bet_keyboard("slots"))
        return
    try:
        await play_emoji_game(update, context, "🎰", "🎰 <b>Slots Bet</b>", int(context.args[0]))
    except ValueError:
        await update.message.reply_text("⚠️ Use: <code>/slots 100</code>", parse_mode=ParseMode.HTML)


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
        await update.message.reply_text("📣 Reply karke <code>/broadcast</code> likho ya <code>/broadcast &lt;msg&gt;</code>.", parse_mode=ParseMode.HTML)
        return
    targets = db.all_broadcast_targets()
    status_msg = await update.message.reply_text(f"📡 Broadcasting to <code>{len(targets)}</code> chats...", parse_mode=ParseMode.HTML)
    sent = failed = 0
    for cid in targets:
        try:
            if reply_msg:
                await context.bot.copy_message(cid, reply_msg.chat_id, reply_msg.message_id)
            else:
                await context.bot.send_message(cid, f"📢 <b>Announcement</b>\n\n{custom_text}", parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.06)
        except (Forbidden, TelegramError):
            failed += 1
    await status_msg.edit_text(f"✅ Broadcast Done! Sent: <code>{sent}</code> | Failed: <code>{failed}</code>", parse_mode=ParseMode.HTML)


async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user or not query.message:
        return
    user, chat, data = query.from_user, query.message.chat, query.data or ""
    db.register_chat(chat.id, chat.type, chat.title)
    db.ensure_user(user.id, user.first_name, user.username)

    if data == "mines_noop":
        await query.answer("Pehle 1 tile kholo!")
        return
    if data == "menu_home":
        await query.answer()
        doc = db.get_user(user.id) or {"coins": 0}
        await query.edit_message_text(f"🎮 <b>Group Arcade Bot</b>\n🪙 <b>Balance:</b> <code>{doc['coins']}</code>", parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())
        return
    if data in ("menu_mines", "menu_flip", "menu_dice", "menu_slots"):
        await query.answer()
        m_map = {
            "menu_mines": ("💣 <b>Mines</b> — Bet chuno:", "mines"),
            "menu_flip": ("🪙 <b>Coin Flip</b> — Bet chuno:", "flip_pick"),
            "menu_dice": ("🎲 <b>Dice Bet</b> — Bet chuno:", "dice"),
            "menu_slots": ("🎰 <b>Slots Bet</b> — Bet chuno:", "slots"),
        }
        txt, pref = m_map[data]
        await query.edit_message_text(txt, parse_mode=ParseMode.HTML, reply_markup=bet_keyboard(pref))
        return
    if data == "act_daily":
        await query.answer()
        await query.message.reply_text(await _handle_daily_logic(user.id, user.first_name, user.username, chat.id), parse_mode=ParseMode.HTML)
        return
    if data == "act_refill":
        await query.answer()
        await query.message.reply_text(await _handle_refill_logic(user.id, user.first_name, user.username, chat.id), parse_mode=ParseMode.HTML)
        return
    if data == "act_profile":
        await query.answer()
        doc = db.get_user(user.id) or {}
        await query.message.reply_text(f"👤 <b>{user.first_name}</b> | 🪙 <code>{doc.get('coins', 0)}</code> | Wins: <code>{doc.get('games_won', 0)}</code>", parse_mode=ParseMode.HTML)
        return
    if data == "act_top":
        await query.answer()
        lines = ["🏆 <b>Global Top 5</b>"] + [f"{i}. <b>{r.get('name', 'Player')}</b> — <code>{r.get('coins', 0)}</code>" for i, r in enumerate(db.top_global(5), 1)]
        await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return
    if data.startswith("bet:"):
        _, game_type, bet_str = data.split(":", 2)
        bet = int(bet_str)
        await query.answer()
        if game_type == "mines":
            await start_mines_game(chat.id, user.id, user.first_name, user.username, bet, query.message.reply_text)
        elif game_type == "flip_pick":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"👑 Heads ({bet})", callback_data=f"flip_go:{bet}:heads"), InlineKeyboardButton(f"🦅 Tails ({bet})", callback_data=f"flip_go:{bet}:tails")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu_flip")],
            ])
            await query.edit_message_text(f"🪙 <b>Coin Flip</b> ({bet}) — Heads ya Tails?", parse_mode=ParseMode.HTML, reply_markup=kb)
        elif game_type == "dice":
            await play_emoji_game(update, context, "🎲", "🎲 <b>Dice Bet</b>", bet)
        elif game_type == "slots":
            await play_emoji_game(update, context, "🎰", "🎰 <b>Slots Bet</b>", bet)
        return
    if data.startswith("flip_go:"):
        _, bet_str, side = data.split(":", 2)
        await query.answer()
        await play_coin_flip(chat.id, user.id, user.first_name, user.username, int(bet_str), side, query.message.reply_text)
        return

    game_key = (chat.id, user.id)
    if data.startswith("mines_pick:") or data == "mines_cashout":
        game = ACTIVE_MINES.get(game_key)
        if not game:
            for (cid, uid), _ in ACTIVE_MINES.items():
                if cid == chat.id and uid != user.id:
                    await query.answer("🙅 Ye game tumhara nahi hai bhai!", show_alert=True)
                    return
            await query.answer("Ye Mines game expire ho chuka hai.")
            return
        if data == "mines_cashout":
            rev_cnt = len(game["revealed"])
            if rev_cnt < 1:
                await query.answer("Pehle 1 diamond kholo!", show_alert=True)
                return
            ACTIVE_MINES.pop(game_key, None)
            mult = calc_mines_multiplier(rev_cnt, game["mines_count"])
            payout = int(game["bet"] * mult)
            new_bal = db.adjust_coins(user.id, payout, chat_id=chat.id, name=user.first_name, won_delta=1)
            line = random.choice(MINES_WIN_DIALOGUES).format(payout=payout, profit=max(0, payout - game["bet"]))
            await query.answer("💰 Cash Out!")
            await query.edit_message_text(f"💰 <b>Cash Out!</b> — <b>{user.first_name}</b> ({mult}x)\n{line}\n🪙 <b>Balance:</b> <code>{new_bal}</code>", parse_mode=ParseMode.HTML, reply_markup=build_mines_keyboard(game, True))
            return
        idx = int(data.split(":", 1)[1])
        if idx in game["revealed"]:
            await query.answer("Tile pehle hi khul chuka hai!")
            return
        if idx in game["mines"]:
            ACTIVE_MINES.pop(game_key, None)
            doc = db.get_user(user.id) or {"coins": 0}
            await query.answer("💥 BOOM!")
            await query.edit_message_text(f"💥 <b>Blast!</b> — <b>{user.first_name}</b>\n{random.choice(MINES_LOSS_DIALOGUES).format(bet=game['bet'])}\n🪙 <b>Balance:</b> <code>{doc['coins']}</code>", parse_mode=ParseMode.HTML, reply_markup=build_mines_keyboard(game, True))
            return
        game["revealed"].append(idx)
        rev_cnt = len(game["revealed"])
        safe_total = 25 - game["mines_count"]
        mult = calc_mines_multiplier(rev_cnt, game["mines_count"])
        cashout_amt = int(game["bet"] * mult)
        if rev_cnt >= safe_total:
            ACTIVE_MINES.pop(game_key, None)
            new_bal = db.adjust_coins(user.id, cashout_amt, chat_id=chat.id, name=user.first_name, won_delta=1)
            await query.answer("🏆 Full Clear!")
            await query.edit_message_text(f"🏆 <b>Full Clear!</b> — <b>{user.first_name}</b> (+{cashout_amt})\n🪙 <b>Balance:</b> <code>{new_bal}</code>", parse_mode=ParseMode.HTML, reply_markup=build_mines_keyboard(game, True))
            return
        await query.answer(f"💎 Safe! {mult}x")
        await query.edit_message_text(f"💣 <b>Mines</b> — <b>{user.first_name}</b>\n🪙 Bet: <code>{game['bet']}</code> | Mult: <b>{mult}x</b> | Cash Out: <code>{cashout_amt}</code>", parse_mode=ParseMode.HTML, reply_markup=build_mines_keyboard(game, False))


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


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing!")
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()

    for cmd, fn in [
        ("start", cmd_start), ("help", cmd_help), ("bal", cmd_bal),
        ("daily", cmd_daily), ("refill", cmd_refill), ("ref", cmd_ref),
        ("give", cmd_give), ("gtop", cmd_gtop), ("top", cmd_top),
        ("mines", cmd_mines), ("flip", cmd_flip), ("dice", cmd_dice), ("slots", cmd_slots),
        ("broadcast", cmd_broadcast), ("stats", cmd_stats),
        ("addcoins", cmd_addcoins), ("setcoins", cmd_setcoins),
    ]:
        app.add_handler(CommandHandler(cmd, fn))

    app.add_handler(CallbackQueryHandler(on_callback_query))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, track_chat_activity))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
