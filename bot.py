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
