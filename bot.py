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
    BotCommandScopeAllPrivateChats,
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
# CONFIGURATION & TIMERS
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
DAILY_COOLDOWN_SEC = 24 * 3600  # 24 hours
REFILL_AMOUNT = 250
REFILL_COOLDOWN_SEC = 30 * 60  # 30 minutes
REFERRAL_BONUS_COINS = 500

ROUND_SECONDS = 15  # 15s participate window
BET_OPTION_SECONDS = 2.0  # 2s bet selection window per member after choice
TTT_JOIN_SECONDS = 12
TTT_TURN_SECONDS = 15
AUTO_DELETE_RESULT_SEC = 10  # auto-delete finished match/result message after result
STALE_UI_DELETE_SEC = 20  # auto-delete idle info/selection menus
TIMER_GAME_MAX_AGE_SEC = 45  # auto-delete stuck timer rounds past 45s
TTT_MAX_AGE_SEC = 300  # 5 minutes (300s) timeout for active/stuck Tic Tac Toe

REDEEM_EXPIRY_SEC = 24 * 3600  # 24 hours validity

AVIATOR_STEP_SEC = 0.75
AVIATOR_STEP_INC = 0.2
AVIATOR_MIN_MULTIPLIER = 1.0
AVIATOR_MAX_MULTIPLIER = 10.0
AVIATOR_SAFE_THRESHOLD = 1.5
AVIATOR_SAFE_RETURN_RATIO = 0.5
AVIATOR_GUESS_CHOICES = (1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0)
GUESS_EXACT_MULT = 8.5
GUESS_NEAR_MULT = 1.5
MINES_BOMB_CHOICES = (2, 4, 5, 7, 9)
SLOT_THEMES = {
    "classic": ("🍒 Classic", {"cherry": ("🍒", "2.5x"), "gem": ("💎", "4.0x"), "seven": ("7️⃣", "7.0x")}),
    "neon": ("⚡ Neon", {"cherry": ("⚡", "2.5x"), "gem": ("💠", "4.0x"), "seven": ("🔥", "7.0x")}),
    "royal": ("👑 Royal", {"cherry": ("🍀", "2.5x"), "gem": ("👑", "4.0x"), "seven": ("💰", "7.0x")}),
}
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
# STYLISH COLOR GRADING & KEYBOARD HELPERS
# ==============================================================================
def stylish_btn(
    text: str,
    callback_data: str,
    tone: str = "primary",  # "primary" (blue) | "success" (green) | "danger" (red) | "muted" (none)
) -> InlineKeyboardButton:
    style_map = {
        "primary": "primary",
        "success": "success",
        "danger": "danger",
        "muted": None,
    }
    chosen_style = style_map.get(tone, "primary")
    kwargs: Dict[str, Any] = {}
    if chosen_style:
        kwargs["api_kwargs"] = {"style": chosen_style}
    return InlineKeyboardButton(text=text, callback_data=callback_data, **kwargs)


def add_bot_url_btn(bot_username: Optional[str] = None, label: str = "➕ Add Bot to Group") -> Optional[InlineKeyboardButton]:
    uname = (bot_username or BOT_USERNAME or "").strip().lstrip("@")
    if not uname:
        return None
    return InlineKeyboardButton(text=label, url=f"https://t.me/{uname}?startgroup=true")


def main_menu_keyboard(bot_username: Optional[str] = None) -> InlineKeyboardMarkup:
    add_btn = add_bot_url_btn(bot_username)
    rows: List[List[InlineKeyboardButton]] = [
        [
            stylish_btn("🚀 Aviator", "menu_aviator", "primary"),
            stylish_btn("💣 Mines", "menu_mines", "danger"),
            stylish_btn("❎ TicTacToe", "menu_ttt", "primary"),
        ],
        [
            stylish_btn("✊ R-P-S", "pick_game:rps", "primary"),
            stylish_btn("📈 Hi-Lo", "pick_game:hilo", "primary"),
            stylish_btn("🪙 Flip", "pick_game:flip", "primary"),
        ],
        [
            stylish_btn("🎲 Dice", "pick_game:dice", "primary"),
            stylish_btn("🎰 Slots", "pick_game:slots", "primary"),
            stylish_btn("🔢 Guess", "pick_game:guess", "primary"),
        ],
        [
            stylish_btn("🎡 Wheel", "pick_game:wheel", "primary"),
            stylish_btn("🏀 Sports", "pick_game:sports", "primary"),
            stylish_btn("🪙 Refill", "do_refill", "success"),
        ],
        [
            stylish_btn("🎁 Daily", "do_daily", "success"),
            stylish_btn("💰 Balance", "menu_bal", "success"),
            stylish_btn("👤 Profile", "menu_profile", "primary"),
        ],
        [
            stylish_btn("👥 Refer & Earn", "menu_refer", "success"),
            stylish_btn("🏆 Top", "menu_top", "primary"),
            stylish_btn("❓ Help", "menu_help", "muted"),
        ],
        [
            stylish_btn("👥 Group Top", "menu_gtop", "primary"),
        ],
    ]
    if add_btn:
        rows[-1].append(add_btn)
    return InlineKeyboardMarkup(rows)


def aviator_guess_keyboard(bet: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                stylish_btn("1.5x", f"av_guess:{bet}:1.5", "primary"),
                stylish_btn("2.0x", f"av_guess:{bet}:2.0", "primary"),
                stylish_btn("3.0x", f"av_guess:{bet}:3.0", "success"),
                stylish_btn("4.0x", f"av_guess:{bet}:4.0", "success"),
            ],
            [
                stylish_btn("5.0x", f"av_guess:{bet}:5.0", "primary"),
                stylish_btn("7.0x", f"av_guess:{bet}:7.0", "danger"),
                stylish_btn("10.0x", f"av_guess:{bet}:10.0", "danger"),
            ],
            [stylish_btn("👥 Refer & Earn", "menu_refer", "success"), stylish_btn("🏠 Menu", "back_menu", "muted")],
        ]
    )


def ttt_size_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                stylish_btn("3×3 (2–3P)", "ttt_start:3", "primary"),
                stylish_btn("4×4 (2–4P)", "ttt_start:4", "primary"),
                stylish_btn("5×5 (2–6P)", "ttt_start:5", "primary"),
            ],
            [stylish_btn("⬅️ Main Menu", "back_menu", "muted")],
        ]
    )


def post_game_keyboard(replay_cb: str, back_mode_cb: Optional[str] = None) -> InlineKeyboardMarkup:
    row = [stylish_btn("🔄 Replay", replay_cb, "success")]
    if back_mode_cb:
        row.append(stylish_btn("🔙 Modes", back_mode_cb, "primary"))
    row.append(stylish_btn("🏠 Menu", "back_menu", "muted"))
    return InlineKeyboardMarkup(
        [
            row,
            [stylish_btn("👥 Refer & Earn", "menu_refer", "success")],
        ]
    )


def mines_bombs_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                stylish_btn("2 💣", "mines_bombs:2", "danger"),
                stylish_btn("4 💣", "mines_bombs:4", "danger"),
                stylish_btn("5 💣", "mines_bombs:5", "danger"),
                stylish_btn("7 💣", "mines_bombs:7", "danger"),
                stylish_btn("9 💣", "mines_bombs:9", "danger"),
            ],
            [stylish_btn("👥 Refer & Earn", "menu_refer", "success"), stylish_btn("⬅️ Main Menu", "back_menu", "muted")],
        ]
    )


def bet_amounts_keyboard(
    user_coins: int,
    prefix: str,
    extra_rows: Optional[List[List[InlineKeyboardButton]]] = None,
) -> InlineKeyboardMarkup:
    half_amt = max(MIN_BET, user_coins // 2)
    max_amt = max(MIN_BET, min(user_coins, MAX_BET))
    presets = [10, 50, 100, 250, 500, 1000]
    rows: List[List[InlineKeyboardButton]] = [
        [stylish_btn(f"{amt}", f"{prefix}:{amt}", "success") for amt in presets[:3]],
        [stylish_btn(f"{amt}", f"{prefix}:{amt}", "success") for amt in presets[3:]],
        [
            stylish_btn(f"½ ({half_amt})", f"{prefix}:{half_amt}", "primary"),
            stylish_btn(f"Max ({max_amt})", f"{prefix}:{max_amt}", "danger"),
        ],
    ]
    if extra_rows:
        rows.extend(extra_rows)
    # Referral button right below bet buttons in every game
    rows.append([stylish_btn(f"🎁 Refer & Earn (+{REFERRAL_BONUS_COINS})", "menu_refer", "success")])
    return InlineKeyboardMarkup(rows)


def game_choice_keyboard(game_key: str, round_id: str) -> InlineKeyboardMarkup:
    if game_key == "rps":
        return InlineKeyboardMarkup(
            [
                [
                    stylish_btn("✊ Rock", f"rchoice:{round_id}:rock", "primary"),
                    stylish_btn("✋ Paper", f"rchoice:{round_id}:paper", "primary"),
                    stylish_btn("✌️ Scissor", f"rchoice:{round_id}:scissor", "primary"),
                ]
            ]
        )
    if game_key == "hilo":
        return InlineKeyboardMarkup(
            [
                [
                    stylish_btn("⬆️ High (4–6)", f"rchoice:{round_id}:high", "success"),
                    stylish_btn("⬇️ Low (1–3)", f"rchoice:{round_id}:low", "danger"),
                ]
            ]
        )
    if game_key == "flip":
        return InlineKeyboardMarkup(
            [
                [
                    stylish_btn("🟡 Heads", f"rchoice:{round_id}:heads", "primary"),
                    stylish_btn("⚪ Tails", f"rchoice:{round_id}:tails", "primary"),
                ]
            ]
        )
    if game_key == "dice":
        return InlineKeyboardMarkup(
            [
                [stylish_btn(f"🎲 {n}", f"rchoice:{round_id}:{n}", "primary") for n in (1, 2, 3)],
                [stylish_btn(f"🎲 {n}", f"rchoice:{round_id}:{n}", "primary") for n in (4, 5, 6)],
            ]
        )
    if game_key == "slots":
        cur_theme = "classic"
        r_obj = ROUNDS_BY_ID.get(round_id)
        if r_obj and r_obj.get("slot_theme") in SLOT_THEMES:
            cur_theme = r_obj["slot_theme"]
        t_label, t_map = SLOT_THEMES[cur_theme]
        c_ico, _ = t_map["cherry"]
        g_ico, _ = t_map["gem"]
        s_ico, _ = t_map["seven"]
        return InlineKeyboardMarkup(
            [
                [
                    stylish_btn(f"{c_ico} Cherry (2.5x)", f"rchoice:{round_id}:cherry", "primary"),
                    stylish_btn(f"{g_ico} Gem (4x)", f"rchoice:{round_id}:gem", "success"),
                    stylish_btn(f"{s_ico} Jackpot (7x)", f"rchoice:{round_id}:seven", "danger"),
                ],
                [
                    stylish_btn(f"🎨 Theme: {t_label}", f"stheme:{round_id}", "muted"),
                ],
            ]
        )
    if game_key == "guess":
        return InlineKeyboardMarkup(
            [
                [stylish_btn(f"{n}", f"rchoice:{round_id}:{n}", "primary") for n in (1, 2, 3, 4, 5)],
                [stylish_btn(f"{n}", f"rchoice:{round_id}:{n}", "primary") for n in (6, 7, 8, 9, 10)],
            ]
        )
    if game_key == "wheel":
        return InlineKeyboardMarkup(
            [
                [
                    stylish_btn("🟢 2x", f"rchoice:{round_id}:2x", "success"),
                    stylish_btn("🔵 3x", f"rchoice:{round_id}:3x", "primary"),
                    stylish_btn("🔴 5x", f"rchoice:{round_id}:5x", "danger"),
                ]
            ]
        )
    return InlineKeyboardMarkup(
        [
            [
                stylish_btn("🏀 Hoops", f"rchoice:{round_id}:hoops", "primary"),
                stylish_btn("⚽ Goal", f"rchoice:{round_id}:goal", "success"),
            ],
            [
                stylish_btn("🎯 Bullseye", f"rchoice:{round_id}:target", "danger"),
                stylish_btn("🎳 Strike", f"rchoice:{round_id}:strike", "primary"),
            ],
        ]
    )


def solo_choice_keyboard(game_key: str) -> InlineKeyboardMarkup:
    if game_key == "rps":
        return InlineKeyboardMarkup(
            [
                [
                    stylish_btn("✊ Rock", "schoice:rps:rock", "primary"),
                    stylish_btn("✋ Paper", "schoice:rps:paper", "primary"),
                    stylish_btn("✌️ Scissor", "schoice:rps:scissor", "primary"),
                ],
                [stylish_btn("🏠 Menu", "back_menu", "muted")],
            ]
        )
    if game_key == "hilo":
        return InlineKeyboardMarkup(
            [
                [
                    stylish_btn("⬆️ High (4–6)", "schoice:hilo:high", "success"),
                    stylish_btn("⬇️ Low (1–3)", "schoice:hilo:low", "danger"),
                ],
                [stylish_btn("🏠 Menu", "back_menu", "muted")],
            ]
        )
    if game_key == "flip":
        return InlineKeyboardMarkup(
            [
                [
                    stylish_btn("🟡 Heads", "schoice:flip:heads", "primary"),
                    stylish_btn("⚪ Tails", "schoice:flip:tails", "primary"),
                ],
                [stylish_btn("🏠 Menu", "back_menu", "muted")],
            ]
        )
    if game_key == "dice":
        return InlineKeyboardMarkup(
            [
                [stylish_btn(f"🎲 {n}", f"schoice:dice:{n}", "primary") for n in (1, 2, 3)],
                [stylish_btn(f"🎲 {n}", f"schoice:dice:{n}", "primary") for n in (4, 5, 6)],
                [stylish_btn("🏠 Menu", "back_menu", "muted")],
            ]
        )
    if game_key == "slots":
        return InlineKeyboardMarkup(
            [
                [
                    stylish_btn("🍒 Cherry (2.5x)", "schoice:slots:cherry", "primary"),
                    stylish_btn("💎 Gem (4x)", "schoice:slots:gem", "success"),
                    stylish_btn("7️⃣ Jackpot (7x)", "schoice:slots:seven", "danger"),
                ],
                [stylish_btn("🏠 Menu", "back_menu", "muted")],
            ]
        )
    if game_key == "guess":
        return InlineKeyboardMarkup(
            [
                [stylish_btn(f"{n}", f"schoice:guess:{n}", "primary") for n in (1, 2, 3, 4, 5)],
                [stylish_btn(f"{n}", f"schoice:guess:{n}", "primary") for n in (6, 7, 8, 9, 10)],
                [stylish_btn("🏠 Menu", "back_menu", "muted")],
            ]
        )
    if game_key == "wheel":
        return InlineKeyboardMarkup(
            [
                [
                    stylish_btn("🟢 2x", "schoice:wheel:2x", "success"),
                    stylish_btn("🔵 3x", "schoice:wheel:3x", "primary"),
                    stylish_btn("🔴 5x", "schoice:wheel:5x", "danger"),
                ],
                [stylish_btn("🏠 Menu", "back_menu", "muted")],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [
                stylish_btn("🏀 Hoops", "schoice:sports:hoops", "primary"),
                stylish_btn("⚽ Goal", "schoice:sports:goal", "success"),
            ],
            [
                stylish_btn("🎯 Bullseye", "schoice:sports:target", "danger"),
                stylish_btn("🎳 Strike", "schoice:sports:strike", "primary"),
            ],
            [stylish_btn("🏠 Menu", "back_menu", "muted")],
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
        self.redeem_col = None
        self.mem_users: Dict[int, Dict[str, Any]] = {}
        self.mem_gstats: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.mem_chats: Dict[int, Dict[str, Any]] = {}
        self.mem_redeem: Dict[str, Dict[str, Any]] = {}
        if uri and MongoClient is not None:
            try:
                self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
                self.client.admin.command("ping")
                db = self.client["telegram_game_bot"]
                self.users_col = db["users"]
                self.gstats_col = db["group_stats"]
                self.chats_col = db["chats"]
                self.redeem_col = db["redeem_codes"]
                self.users_col.create_index("user_id", unique=True)
                self.gstats_col.create_index([("chat_id", 1), ("user_id", 1)], unique=True)
                self.chats_col.create_index("chat_id", unique=True)
                self.redeem_col.create_index("code", unique=True)
                self.use_mongo = True
                logger.info("Connected to MongoDB successfully.")
            except Exception as exc:
                logger.warning("MongoDB unavailable, using in-memory store: %s", exc)
                self.use_mongo = False
        else:
            logger.info("MONGO_URI not set; running with in-memory store.")

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
                        "referred_by": None,
                        "referrals_count": 0,
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
                "referred_by": None,
                "referrals_count": 0,
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

    def apply_referral_once(self, new_user_id: int, referrer_id: int) -> bool:
        if new_user_id == referrer_id or referrer_id <= 0:
            return False
        ref_doc = self.get_user(referrer_id)
        if not ref_doc:
            return False
        user_doc = self.get_user(new_user_id)
        if not user_doc or user_doc.get("referred_by") is not None:
            return False
        if self.use_mongo:
            try:
                res = self.users_col.update_one(
                    {"user_id": new_user_id, "referred_by": None},
                    {"$set": {"referred_by": referrer_id}},
                )
                if res.modified_count == 0:
                    return False
                self.users_col.update_one(
                    {"user_id": referrer_id},
                    {"$inc": {"referrals_count": 1}},
                )
                return True
            except PyMongoError:
                return False
        user_doc["referred_by"] = referrer_id
        ref_doc["referrals_count"] = int(ref_doc.get("referrals_count", 0)) + 1
        return True

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
            key,
            {"chat_id": chat_id, "user_id": user_id, "name": safe_name, "net_coins": 0, "played": 0, "won": 0},
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

    # Redeem Code Methods (Owner sets code & coins; 1 claim per user; 24h validity)
    def set_redeem_code(self, code: str, coins: int, created_by: int) -> Dict[str, Any]:
        clean_code = code.strip()
        now = time.time()
        expires_at = now + REDEEM_EXPIRY_SEC
        doc = {
            "code": clean_code,
            "coins": max(1, int(coins)),
            "created_by": created_by,
            "created_at": now,
            "expires_at": expires_at,
            "claimed_by": [],
        }
        if self.use_mongo:
            try:
                self.redeem_col.update_one(
                    {"code": clean_code},
                    {"$set": doc},
                    upsert=True,
                )
            except PyMongoError as exc:
                logger.error("Mongo set_redeem_code error: %s", exc)
        self.mem_redeem[clean_code] = doc
        return doc

    def set_redeem_coins_for_latest(self, coins: int) -> Optional[Dict[str, Any]]:
        safe_coins = max(1, int(coins))
        latest_doc: Optional[Dict[str, Any]] = None
        if self.use_mongo:
            try:
                latest_doc = self.redeem_col.find_one(sort=[("created_at", DESCENDING)])
                if latest_doc:
                    self.redeem_col.update_one(
                        {"_id": latest_doc["_id"]},
                        {"$set": {"coins": safe_coins}},
                    )
                    latest_doc["coins"] = safe_coins
            except PyMongoError as exc:
                logger.error("Mongo set_redeem_coins error: %s", exc)
        if self.mem_redeem:
            mem_latest = max(self.mem_redeem.values(), key=lambda x: float(x.get("created_at", 0.0)))
            mem_latest["coins"] = safe_coins
            if latest_doc is None:
                latest_doc = mem_latest
        return latest_doc

    def get_redeem_code(self, code: str) -> Optional[Dict[str, Any]]:
        clean_code = code.strip()
        if self.use_mongo:
            try:
                doc = self.redeem_col.find_one({"code": clean_code})
                if doc:
                    return doc
            except PyMongoError as exc:
                logger.error("Mongo get_redeem_code error: %s", exc)
        return self.mem_redeem.get(clean_code)

    def claim_redeem_code(self, code: str, user_id: int) -> Tuple[bool, str, int]:
        clean_code = code.strip()
        doc = self.get_redeem_code(clean_code)
        if not doc:
            return False, "❌ Ye redeem code valid nahi hai ya exist nahi karta.", 0
        now = time.time()
        expires_at = float(doc.get("expires_at", float(doc.get("created_at", 0.0)) + REDEEM_EXPIRY_SEC))
        if now > expires_at:
            return False, "⏳ Ye redeem code expire ho chuka hai (24h validity khatam).", 0
        claimed_by = list(doc.get("claimed_by", []))
        if user_id in claimed_by:
            return False, "⚠️ Tum is redeem code ko pehle hi claim kar chuke ho (1 user = 1 claim).", 0
        reward_coins = int(doc.get("coins", 0))
        if self.use_mongo:
            try:
                res = self.redeem_col.find_one_and_update(
                    {
                        "code": clean_code,
                        "expires_at": {"$gt": now},
                        "claimed_by": {"$ne": user_id},
                    },
                    {"$addToSet": {"claimed_by": user_id}},
                    return_document=True,
                )
                if not res:
                    return False, "⚠️ Code expire ho chuka hai ya tum pehle hi claim kar chuke ho.", 0
                reward_coins = int(res.get("coins", reward_coins))
            except PyMongoError as exc:
                logger.error("Mongo claim_redeem_code error: %s", exc)
                return False, "❌ Redeem process mein error aaya, thoda baad try karo.", 0
        claimed_by.append(user_id)
        if clean_code in self.mem_redeem:
            self.mem_redeem[clean_code]["claimed_by"] = claimed_by
        new_bal = self.adjust_coins(user_id, reward_coins)
        bal_val = new_bal if new_bal is not None else reward_coins
        return True, f"🎉 <b>Redeem Successful!</b> Tumhe <b>+{reward_coins}</b> coins mile!\n🪙 <b>Balance:</b> <code>{bal_val}</code>", reward_coins

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

    def get_user_ranks(self, user_id: int, chat_id: Optional[int] = None) -> Tuple[int, int, Optional[int], int]:
        u = self.get_user(user_id) or {}
        u_coins = int(u.get("coins", 0))
        if self.use_mongo and self.users_col is not None:
            try:
                total_users = int(self.users_col.count_documents({}))
                g_rank = int(self.users_col.count_documents({"coins": {"$gt": u_coins}})) + 1
            except PyMongoError:
                all_u = list(self.mem_users.values())
                total_users = len(all_u)
                g_rank = sum(1 for item in all_u if int(item.get("coins", 0)) > u_coins) + 1
        else:
            all_u = list(self.mem_users.values())
            total_users = len(all_u)
            g_rank = sum(1 for item in all_u if int(item.get("coins", 0)) > u_coins) + 1

        if self.use_mongo and self.users_col is not None:
            try:
                all_sorted = list(self.users_col.find({}, {"user_id": 1, "coins": 1, "games_won": 1}))
            except PyMongoError:
                all_sorted = list(self.mem_users.values())
        else:
            all_sorted = list(self.mem_users.values())
        all_sorted.sort(
            key=lambda x: (int(x.get("games_won", 0)), int(x.get("coins", 0))),
            reverse=True,
        )
        top_rank = 1
        for idx, item in enumerate(all_sorted, start=1):
            if int(item.get("user_id", 0)) == user_id:
                top_rank = idx
                break

        grp_rank: Optional[int] = None
        if chat_id is not None and chat_id < 0:
            g_items = self.group_top(chat_id, limit=1000)
            for idx, item in enumerate(g_items, start=1):
                if int(item.get("user_id", 0)) == user_id:
                    grp_rank = idx
                    break

        return g_rank, top_rank, grp_rank, max(1, total_users)

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
PENDING_SOLO_BET: Dict[Tuple[int, int], Dict[str, Any]] = {}
ACTIVE_MINES: Dict[Tuple[int, int], Dict[str, Any]] = {}
ACTIVE_ROUNDS: Dict[str, Dict[str, Any]] = {}
ROUNDS_BY_ID: Dict[str, Dict[str, Any]] = {}
CHAT_ROUNDS: Dict[int, List[str]] = {}
ACTIVE_TTT5: Dict[int, Dict[str, Any]] = {}


def unregister_round(chat_id: int, round_id: str) -> None:
    ACTIVE_ROUNDS.pop(round_id, None)
    ROUNDS_BY_ID.pop(round_id, None)
    rlist = CHAT_ROUNDS.get(chat_id)
    if rlist and round_id in rlist:
        rlist.remove(round_id)
    if not rlist:
        CHAT_ROUNDS.pop(chat_id, None)


def clear_bet_option_for_user(round_obj: Dict[str, Any], user_id: int) -> None:
    bet_opt = round_obj.get("active_bet_option")
    if isinstance(bet_opt, dict) and bet_opt.get("user_id") == user_id:
        task = bet_opt.get("task")
        if task and isinstance(task, asyncio.Task) and not task.done():
            task.cancel()
        round_obj["active_bet_option"] = None


def mention_user(user_id: int, name: str, username: Optional[str] = None) -> str:
    clean_name = (
        (name or "Player")
        .strip()[:40]
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f'<a href="tg://user?id={user_id}">{clean_name}</a>'


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
) -> Optional[asyncio.Task]:
    valid_ids = list(dict.fromkeys(m for m in message_ids if isinstance(m, int) and m > 0))
    if not valid_ids:
        return None

    async def _worker() -> None:
        await asyncio.sleep(max(0, delay))
        for mid in valid_ids:
            deleted = False
            for _ in range(3):
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=mid)
                    deleted = True
                    break
                except BadRequest as exc:
                    err_low = str(exc).lower()
                    if "message to delete not found" in err_low or "message can't be deleted" in err_low:
                        deleted = True
                        break
                    await asyncio.sleep(0.4)
                except (Forbidden, TelegramError):
                    await asyncio.sleep(0.4)
            if not deleted:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=mid,
                        text="🗑 <i>Cleared</i>",
                        parse_mode=ParseMode.HTML,
                    )
                except (BadRequest, Forbidden, TelegramError):
                    pass

    return asyncio.create_task(_worker())


async def stuck_game_watchdog(app: Application) -> None:
    while True:
        await asyncio.sleep(5)
        now = time.time()

        stuck_rounds = [
            (rid, robj)
            for rid, robj in list(ACTIVE_ROUNDS.items())
            if robj.get("phase") != "done"
            and (now - float(robj.get("created_at", now))) > TIMER_GAME_MAX_AGE_SEC
        ]
        for rid, robj in stuck_rounds:
            cid = robj.get("chat_id")
            mid = robj.get("message_id")
            if isinstance(cid, int):
                unregister_round(cid, rid)
                if isinstance(mid, int) and mid > 0:
                    try:
                        await app.bot.delete_message(chat_id=cid, message_id=mid)
                    except (BadRequest, Forbidden, TelegramError):
                        pass

        # Tic Tac Toe: only clean up if stuck/incomplete past TTT_MAX_AGE_SEC (300s / 5 min)
        stuck_ttt = [
            (cid, gobj)
            for cid, gobj in list(ACTIVE_TTT5.items())
            if gobj.get("status") != "done"
            and (now - float(gobj.get("created_at", now))) > TTT_MAX_AGE_SEC
        ]
        for cid, gobj in stuck_ttt:
            ACTIVE_TTT5.pop(cid, None)
            mid = gobj.get("message_id")
            if isinstance(mid, int) and mid > 0:
                try:
                    await app.bot.delete_message(chat_id=cid, message_id=mid)
                except (BadRequest, Forbidden, TelegramError):
                    pass


def format_slot_spin_anim(
    sym: str,
    theme_key: str = "classic",
    won: Optional[bool] = None,
    picked: Optional[str] = None,
) -> str:
    t_name, t_icons = SLOT_THEMES.get(theme_key, SLOT_THEMES["classic"])
    order = ["cherry", "gem", "seven"]
    win_ico, win_mult = t_icons.get(sym, ("🍒", "2.5x"))
    if won is True:
        reels = f"[ {win_ico} | {win_ico} | {win_ico} ]"
        status = f"WIN ({win_mult})"
    elif won is False:
        others = [k for k in order if k != sym]
        r2 = t_icons[random.choice(others)][0]
        r3 = t_icons[sym][0]
        reels = f"[ {win_ico} | {r2} | {r3} ]"
        status = "MISS"
    else:
        reels = f"[ {win_ico} | {win_ico} | {win_ico} ]"
        status = f"SPIN ({win_mult})"
    pick_part = f" · Pick: <b>{picked.upper()}</b>" if picked else ""
    return f"🎰 <b>Slots ({t_name}):</b> <code>{reels}</code> → <b>{sym.upper()}</b> [{status}]{pick_part}"


def format_dice_roll_anim(roll: int, picked: Optional[str] = None) -> str:
    faces = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}
    strip = " ".join(f"{faces[i]}" for i in range(1, 7))
    pick_line = f"Pick: <b>{picked}</b> · " if picked else ""
    return f"🎲 <b>Dice:</b> <code>[{strip}]</code> → <b>{faces.get(roll, '🎲')} {roll}</b> ({pick_line}Rolled)"


def format_sports_anim(sport_key: str, picked: Optional[str] = None) -> str:
    meta = {
        "hoops": ("🏀", "Basketball", "🏀 💨 ──▶ 🏀"),
        "goal": ("⚽", "Football", "⚽ 💨 ──▶ 🥅"),
        "target": ("🎯", "Darts", "🎯 💨 ──▶ 🎯"),
        "strike": ("🎳", "Bowling", "🎳 💨 ──▶ 💥"),
    }
    icon, label, track = meta.get(sport_key, ("🏀", "Basketball", "🏀 💨 ──▶ 🏀"))
    pick_line = f"Pick: <b>{picked.upper()}</b> · " if picked else ""
    return f"{icon} <b>{label}:</b> <code>{track}</code> ({pick_line}Scored)"


def help_text() -> str:
    return (
        "📖 <b>Quick Game &amp; Coin Guide</b>\n\n"
        "<b>🎯 Games</b>\n"
        "• <b>Aviator:</b> Group mein live cash-out round chalta hai; DM mein bet lagao aur target multiplier (<code>1.5x–10.0x</code>) guess karo — crash se pehle target touch hua toh win, <code>1.5x+</code> par crash ho toh safe 50% return, aur <code>1.5x</code> se pehle crash par loss.\n"
        "• <b>Mines:</b> <code>2/4/5/7/9</code> bombs chuno, 💎 tiles kholo, kabhi bhi Cash Out ya Exit karo.\n"
        "• <b>Tic Tac Toe:</b> <code>3×3</code> (2–3P), <code>4×4</code> (2–4P), ya <code>5×5</code> (2–6P) board — <code>12s</code> join &amp; <code>15s</code> turn (stuck ho toh 5m timeout).\n"
        "• <b>Timer Rounds (15s):</b> Choice chuno → <code>2s</code> ke andar bet lock karo (na karne par auto-band taaki baaki log select kar sakein).\n"
        "• <b>Sports:</b> 4 options (🏀 Hoops, ⚽ Goal, 🎯 Bullseye, 🎳 Strike) + Replay / Modes / Menu.\n\n"
        "<b>🪙 Coins, Refer &amp; Redeem</b>\n"
        f"• <code>/daily</code> — <b>{DAILY_REWARD}</b> coins (har <code>24h</code>)\n"
        f"• <code>/refill</code> — <b>{REFILL_AMOUNT}</b> coins (har <code>30m</code>)\n"
        f"• <code>/ref</code> — Invite link (+<b>{REFERRAL_BONUS_COINS}</b> bonus)\n"
        "• <code>/redeem &lt;code&gt;</code> — Special redeem code claim (1x per user, 24h valid)\n"
        "• <code>/profile</code> · <code>/bal</code> · <code>/give &lt;amt&gt;</code> · <code>/gtop</code> · <code>/top</code> · <code>/mines</code>"
    )


def format_referral_card(user_id: int, name: str, bot_username: Optional[str] = None) -> str:
    u = db.get_user(user_id) or {}
    ref_count = int(u.get("referrals_count", 0))
    uname = (bot_username or BOT_USERNAME or "your_bot").strip().lstrip("@")
    link = f"https://t.me/{uname}?start={user_id}"
    return (
        f"👥 <b>Refer &amp; Earn Program</b>\n"
        f"• <b>Your Invite Link:</b>\n<code>{link}</code>\n"
        f"• <b>Bonus:</b> <b>+{REFERRAL_BONUS_COINS}</b> coins per valid referral\n"
        f"• <b>Total Referrals:</b> <code>{ref_count}</code>"
    )


def referral_keyboard(user_id: int, bot_username: Optional[str] = None) -> InlineKeyboardMarkup:
    uname = (bot_username or BOT_USERNAME or "").strip().lstrip("@")
    rows: List[List[InlineKeyboardButton]] = []
    if uname:
        share_url = f"https://t.me/share/url?url=https://t.me/{uname}?start={user_id}&text=Aao%20saath%20mein%20games%20khelein%20aur%20coins%20jeetein!"
        rows.append([InlineKeyboardButton("📤 Share Invite Link", url=share_url)])
        rows.append([InlineKeyboardButton("➕ Add Bot to Group", url=f"https://t.me/{uname}?startgroup=true")])
    rows.append([stylish_btn("🏠 Menu", "back_menu", "muted")])
    return InlineKeyboardMarkup(rows)


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


def format_profile_card(
    user_id: int,
    name: str,
    username: Optional[str],
    chat_id: Optional[int] = None,
) -> str:
    u = db.ensure_user(user_id, name, username)
    coins = int(u.get("coins", 0))
    played = int(u.get("games_played", 0))
    won = int(u.get("games_won", 0))
    loss = int(u["games_loss"]) if "games_loss" in u else max(0, played - won)
    total_calc = max(played, won + loss)
    win_rate = round((won / total_calc) * 100, 1) if total_calc > 0 else 0.0
    streak = int(u.get("streak", 0))
    best_streak = int(u.get("best_streak", 0))
    big_win = int(u.get("biggest_win", 0))
    refs = int(u.get("referrals_count", 0))
    g_rank, top_rank, grp_rank, total_users = db.get_user_ranks(user_id, chat_id=chat_id)
    grp_line = f"• <b>Group Rank:</b> <code>#{grp_rank}</code>\n" if grp_rank is not None else ""
    return (
        f"🪪 <b>Player Profile</b>\n"
        f"• <b>Name:</b> <b>{u.get('first_name', name)}</b>\n"
        f"• <b>ID:</b> <code>{user_id}</code>\n"
        f"• <b>Balance:</b> <code>{coins}</code> coins\n"
        f"• <b>Global Rank (gtop):</b> <code>#{g_rank}</code> / <code>{total_users}</code>\n"
        f"• <b>Top Rank (top):</b> <code>#{top_rank}</code> / <code>{total_users}</code>\n"
        f"{grp_line}"
        f"• <b>Record:</b> <code>{won}W</code> / <code>{loss}L</code> (<code>{win_rate}%</code>)\n"
        f"• <b>Streak (Best):</b> <code>{streak}</code> (<code>{best_streak}</code>)\n"
        f"• <b>Biggest Win:</b> <code>{big_win}</code>\n"
        f"• <b>Referrals:</b> <code>{refs}</code>"
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
# MINES GAME ENGINE
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
                label, tone = "💎", "success"
            elif reveal_all and idx in mines_set:
                label, tone = "💣", "danger"
            elif reveal_all:
                label, tone = "·", "muted"
            else:
                label, tone = "⬜", "muted"
            row_btns.append(stylish_btn(label, f"mclick:{idx}", tone))
        rows.append(row_btns)
    if not finished:
        mult = mines_multiplier(len(rev_set), game["mines_count"])
        payout = int(game["bet"] * mult)
        if len(rev_set) > 0:
            rows.append([stylish_btn(f"💰 Cash Out ({payout} | {mult}x)", "mcashout", "success")])
        else:
            rows.append([stylish_btn("💎 Safe Tile Chuno", "mnop", "muted")])
        rows.append([stylish_btn("🚪 Bahar Jaao (Exit)", "mexit", "danger")])
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
    if bet < MIN_BET or bet > MAX_BET:
        await send_fn(f"⚠️ Bet <b>{MIN_BET}</b> se <b>{MAX_BET}</b> ke beech rakho.", parse_mode=ParseMode.HTML)
        return
    db.ensure_user(user_id, name, username)
    game_key = (chat_id, user_id)
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
# MULTI-SIZE TIC TAC TOE ENGINE (3x3, 4x4, 5x5)
# ==============================================================================
TTT_CONFIG = {
    3: {"max_players": 3, "line_len": 3, "pot_bet": 100},
    4: {"max_players": 4, "line_len": 3, "pot_bet": 100},
    5: {"max_players": 6, "line_len": 3, "pot_bet": 100},
}


def build_ttt_keyboard(game: Dict[str, Any], finished: bool = False) -> Optional[InlineKeyboardMarkup]:
    size = int(game.get("size", 5))
    board: List[Optional[str]] = game["board"]
    rows: List[List[InlineKeyboardButton]] = []
    for r in range(size):
        row_btns: List[InlineKeyboardButton] = []
        for c in range(size):
            idx = r * size + c
            cell = board[idx]
            label = "⬜" if cell is None else cell
            tone = "muted" if cell is None else "primary"
            row_btns.append(stylish_btn(label, f"ttt_move:{idx}", tone))
        rows.append(row_btns)
    max_p = int(game.get("max_players", TTT_MAX_PLAYERS))
    if not finished and game["status"] == "joining" and len(game["players"]) < max_p:
        rows.append([stylish_btn(f"➕ Join ({len(game['players'])}/{max_p})", "ttt_join", "success")])
    return None if finished else InlineKeyboardMarkup(rows)


def count_ttt_lines(board: List[Optional[str]], mark: str, size: int = 5, line_len: int = 3) -> int:
    lines = 0
    for r in range(size):
        for c in range(size - line_len + 1):
            if all(board[r * size + c + k] == mark for k in range(line_len)):
                lines += 1
    for c in range(size):
        for r in range(size - line_len + 1):
            if all(board[(r + k) * size + c] == mark for k in range(line_len)):
                lines += 1
    for r in range(size - line_len + 1):
        for c in range(size - line_len + 1):
            if all(board[(r + k) * size + c + k] == mark for k in range(line_len)):
                lines += 1
    for r in range(size - line_len + 1):
        for c in range(line_len - 1, size):
            if all(board[(r + k) * size + c - k] == mark for k in range(line_len)):
                lines += 1
    return lines


def format_ttt_roster(game: Dict[str, Any]) -> str:
    players: List[Dict[str, Any]] = game["players"]
    max_p = int(game.get("max_players", TTT_MAX_PLAYERS))
    if not players:
        return f"👥 Players (0/{max_p}): Koi nahi"
    parts = [f"{p['mark']} {mention_user(p['user_id'], p['name'], p.get('username'))}" for p in players]
    return f"👥 Players ({len(players)}/{max_p}): " + ", ".join(parts)


async def finish_ttt_game(app: Application, chat_id: int, reason: str = "Board Full") -> None:
    game = ACTIVE_TTT5.pop(chat_id, None)
    if not game:
        return
    game["status"] = "done"
    size = int(game.get("size", 5))
    line_len = int(game.get("line_len", 3))
    players: List[Dict[str, Any]] = game["players"]
    board: List[Optional[str]] = game["board"]
    entry_bet = int(game["bet"])
    total_pot = entry_bet * len(players)
    scores: List[Tuple[int, Dict[str, Any]]] = [
        (count_ttt_lines(board, p["mark"], size=size, line_len=line_len), p) for p in players
    ]
    max_pts = max((s[0] for s in scores), default=0)
    winners = [p for pts, p in scores if pts == max_pts and len(players) > 0]
    lines_out = [
        f"🏁 <b>{size}×{size} Tic Tac Toe Result</b> ({reason})",
        f"💰 <b>Pot:</b> <code>{total_pot}</code> coins",
        "",
    ]
    if winners:
        share = max(1, total_pot // len(winners))
        for p in winners:
            uid = p["user_id"]
            new_bal = db.adjust_coins(uid, share, chat_id=chat_id, name=p["name"], won_delta=1, payout_amount=share)
            lines_out.append(
                f"🏆 {p['mark']} {mention_user(p['user_id'], p['name'], p.get('username'))} jeeta! +{share} coins (Bal: <code>{new_bal}</code>)"
            )
        lines_out.append("")
    lines_out.append("📊 <b>Lines Score:</b>")
    for pts, p in sorted(scores, key=lambda x: x[0], reverse=True):
        lines_out.append(f"• {p['mark']} {p['name']}: <code>{pts}</code> lines")
    old_mid = game.get("message_id")
    if isinstance(old_mid, int) and old_mid > 0:
        schedule_delete_messages(app.bot, chat_id, [old_mid], 2)
    msg = await app.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines_out),
        parse_mode=ParseMode.HTML,
        reply_markup=post_game_keyboard(f"ttt_start:{size}", "menu_ttt"),
    )
    schedule_delete_messages(app.bot, chat_id, [msg.message_id], AUTO_DELETE_RESULT_SEC)


async def ttt_turn_watcher(app: Application, chat_id: int, expected_turn_num: int) -> None:
    try:
        rem_turn = TTT_TURN_SECONDS
        while rem_turn > 0:
            await asyncio.sleep(min(5, rem_turn))
            rem_turn = max(0, rem_turn - min(5, rem_turn))
            game = ACTIVE_TTT5.get(chat_id)
            if not game or game.get("status") != "active" or game.get("turn_num") != expected_turn_num:
                return
            # Only clean up if stuck/incomplete past 5 minutes (300s)
            if (time.time() - float(game.get("created_at", time.time()))) > TTT_MAX_AGE_SEC:
                ACTIVE_TTT5.pop(chat_id, None)
                return
            size = int(game.get("size", 5))
            players = game["players"]
            if players and rem_turn > 0:
                cur_p = players[game["turn_idx"] % len(players)]
                try:
                    await app.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=game["message_id"],
                        text=(
                            f"❎ <b>{size}×{size} Tic Tac Toe</b>\n"
                            f"{format_ttt_roster(game)}\n"
                            f"🎯 Turn: {cur_p['mark']} {mention_user(cur_p['user_id'], cur_p['name'], cur_p.get('username'))} (⏳ <code>{rem_turn}s</code>)"
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_ttt_keyboard(game, finished=False),
                    )
                except (BadRequest, TelegramError):
                    pass
        game = ACTIVE_TTT5.get(chat_id)
        if not game or game.get("status") != "active" or game.get("turn_num") != expected_turn_num:
            return
        if (time.time() - float(game.get("created_at", time.time()))) > TTT_MAX_AGE_SEC:
            ACTIVE_TTT5.pop(chat_id, None)
            return
        players = game["players"]
        if not players:
            return
        cur_player = players[game["turn_idx"] % len(players)]
        empty_indices = [i for i, val in enumerate(game["board"]) if val is None]
        if not empty_indices:
            await finish_ttt_game(app, chat_id, "Board Full")
            return
        chosen_idx = random.choice(empty_indices)
        game["board"][chosen_idx] = cur_player["mark"]
        game["turn_idx"] = (game["turn_idx"] + 1) % len(players)
        game["turn_num"] = expected_turn_num + 1
        if all(x is not None for x in game["board"]):
            await finish_ttt_game(app, chat_id, "Board Full")
            return
        size = int(game.get("size", 5))
        next_p = players[game["turn_idx"] % len(players)]
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=game["message_id"],
                text=(
                    f"❎ <b>{size}×{size} Tic Tac Toe</b> (Auto-move by {mention_user(cur_player['user_id'], cur_player['name'], cur_player.get('username'))})\n"
                    f"{format_ttt_roster(game)}\n"
                    f"🎯 Turn: {next_p['mark']} {mention_user(next_p['user_id'], next_p['name'], next_p.get('username'))} ({TTT_TURN_SECONDS}s)"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=build_ttt_keyboard(game, finished=False),
            )
        except (BadRequest, TelegramError):
            pass
        asyncio.create_task(ttt_turn_watcher(app, chat_id, game["turn_num"]))
    except Exception as exc:
        logger.debug("ttt_turn_watcher error: %s", exc)


async def ttt_join_countdown(app: Application, chat_id: int, message_id: int) -> None:
    try:
        rem_join = TTT_JOIN_SECONDS
        while rem_join > 0:
            await asyncio.sleep(min(3, rem_join))
            rem_join = max(0, rem_join - min(3, rem_join))
            game = ACTIVE_TTT5.get(chat_id)
            if not game or game.get("status") != "joining":
                return
            if (time.time() - float(game.get("created_at", time.time()))) > TTT_MAX_AGE_SEC:
                ACTIVE_TTT5.pop(chat_id, None)
                return
            size = int(game.get("size", 5))
            if rem_join > 0 and message_id:
                try:
                    await app.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=(
                            f"❎ <b>{size}×{size} Tic Tac Toe</b> · Entry: <code>{game['bet']}</code> coins\n"
                            f"{format_ttt_roster(game)}\n"
                            f"⏳ Join karne ke liye <b>{rem_join}s</b> bache hain!"
                        ),
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_ttt_keyboard(game, finished=False),
                    )
                except (BadRequest, TelegramError):
                    pass
        game = ACTIVE_TTT5.get(chat_id)
        if not game or game.get("status") != "joining":
            return
        size = int(game.get("size", 5))
        players = game["players"]
        if len(players) < 2:
            ACTIVE_TTT5.pop(chat_id, None)
            if players:
                p0 = players[0]
                bal = db.adjust_coins(p0["user_id"], int(game["bet"]), chat_id=chat_id, name=p0["name"])
                if message_id:
                    try:
                        await app.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=f"⚠️ Kam se kam 2 players chahiye the. Bet refund ho gayi (Bal: <code>{bal}</code>).",
                            parse_mode=ParseMode.HTML,
                            reply_markup=post_game_keyboard(f"ttt_start:{size}", "menu_ttt"),
                        )
                        schedule_delete_messages(app.bot, chat_id, [message_id], AUTO_DELETE_RESULT_SEC)
                    except (BadRequest, TelegramError):
                        pass
            return
        game["status"] = "active"
        game["turn_idx"] = 0
        game["turn_num"] = 1
        first_p = players[0]
        if message_id:
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        f"❎ <b>{size}×{size} Tic Tac Toe Started!</b>\n"
                        f"{format_ttt_roster(game)}\n"
                        f"🎯 Turn: {first_p['mark']} <b>{first_p['name']}</b> ({TTT_TURN_SECONDS}s)"
                    ),
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_ttt_keyboard(game, finished=False),
                )
            except (BadRequest, TelegramError):
                pass
        asyncio.create_task(ttt_turn_watcher(app, chat_id, 1))
    except Exception as exc:
        logger.debug("ttt_join_countdown error: %s", exc)


# ==============================================================================
# SOLO GAMES ENGINE
# ==============================================================================
def eval_guess_payout(choice: str, target_num: int, bet: int) -> Tuple[float, int, str]:
    try:
        picked_num = int(choice)
    except (ValueError, TypeError):
        return 0.0, 0, "miss"
    diff = abs(picked_num - target_num)
    if diff == 0:
        return GUESS_EXACT_MULT, int(round(bet * GUESS_EXACT_MULT)), "exact"
    if diff == 1:
        return GUESS_NEAR_MULT, int(round(bet * GUESS_NEAR_MULT)), "near"
    return 0.0, 0, "miss"


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
        await send_fn("❌ Balance kam hai! <code>/daily</code> ya <code>/refill</code> use karo.", parse_mode=ParseMode.HTML)
        return
    db.record_last_bet(user_id, bet)

    won = False
    mult = 0.0
    payout = 0
    outcome_text = ""
    sport_pick = ""

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
        outcome_text = f"{'🎉' if won else '💥'} {format_dice_roll_anim(roll, choice)}"
    elif game_key == "slots":
        t_key = random.choice(list(SLOT_THEMES.keys()))
        sym = random.choice(["cherry", "gem", "seven"])
        won = choice == sym
        mult_map = {"cherry": 2.5, "gem": 4.0, "seven": 7.0}
        mult = mult_map[sym] if won else 0.0
        outcome_text = f"{'🎉' if won else '💥'} {format_slot_spin_anim(sym, t_key, won=won, picked=choice)}"
    elif game_key == "guess":
        num = random.randint(1, 10)
        mult, payout, g_mode = eval_guess_payout(choice, num, bet)
        if g_mode == "exact":
            won = True
            outcome_text = f"🎉 Exact Guess! Number: <b>{num}</b> (Picked <b>{choice}</b> · {GUESS_EXACT_MULT}x)"
        elif g_mode == "near":
            won = True
            outcome_text = f"✨ Paas Ka Guess (±1)! Number: <b>{num}</b> (Picked <b>{choice}</b> · {GUESS_NEAR_MULT}x)"
        else:
            outcome_text = f"💥 Number: <b>{num}</b> (Picked <b>{choice}</b>)"
    elif game_key == "wheel":
        pocket = random.choice(["2x", "2x", "3x", "5x", "bust"])
        won = choice == pocket
        mult = {"2x": 2.0, "3x": 3.0, "5x": 5.0}.get(choice, 0.0) if won else 0.0
        outcome_text = f"{'🎉' if won else '💥'} Wheel: <b>{pocket}</b>"
    else:
        sport_sticker_map = {
            "hoops": ("🏀", "Basketball"),
            "goal": ("⚽", "Football Goal"),
            "target": ("🎯", "Dart Bullseye"),
            "strike": ("🎳", "Bowling Strike"),
        }
        sport_pick = random.choice(list(sport_sticker_map.keys()))
        won = choice == sport_pick
        mult = 3.5 if won else 0.0
        outcome_text = f"{'🎉' if won else '💥'} {format_sports_anim(sport_pick, choice)}"

    if game_key != "guess":
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

    if game_key in ("dice", "sports", "slots"):
        if game_key == "dice":
            d_emoji = "🎲"
        elif game_key == "slots":
            d_emoji = "🎰"
        else:
            emoji_map = {"hoops": "🏀", "goal": "⚽", "target": "🎯", "strike": "🎳"}
            d_emoji = emoji_map.get(sport_pick or choice, "🏀")
        try:
            await send_fn(dice_emoji=d_emoji)
        except Exception:
            pass

    await send_fn(
        f"{outcome_text}\n"
        f"💰 <b>Bet:</b> <code>{bet}</code> · <b>Payout:</b> <code>{payout}</code>\n"
        f"🪙 <b>Balance:</b> <code>{final_bal}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=post_game_keyboard(f"schoice:{game_key}:{choice}", f"pick_game:{game_key}"),
    )


def format_aviator_plane_bar(mult_val: float, crashed: bool = False, cashed_out: bool = False) -> str:
    span = max(0.1, AVIATOR_MAX_MULTIPLIER - AVIATOR_MIN_MULTIPLIER)
    ratio = max(0.0, min(1.0, (mult_val - AVIATOR_MIN_MULTIPLIER) / span))
    dots = max(1, min(8, int(round(ratio * 8))))
    bar = "•" * dots
    if crashed:
        return f"[{bar}] 💥 BLAST @ {mult_val:.1f}x"
    if cashed_out:
        return f"[{bar}] ✅ {mult_val:.1f}x"
    return f"[{bar}] ✈️ {mult_val:.1f}x"


async def prompt_aviator_guess(
    chat_id: int,
    user_id: int,
    name: str,
    username: Optional[str],
    bet: int,
    send_fn: Callable[..., Coroutine[Any, Any, Any]],
) -> None:
    if bet < MIN_BET or bet > MAX_BET:
        await send_fn(f"⚠️ Bet <b>{MIN_BET}</b> se <b>{MAX_BET}</b> ke beech rakho.", parse_mode=ParseMode.HTML)
        return
    u_doc = db.ensure_user(user_id, name, username)
    if int(u_doc.get("coins", 0)) < bet:
        await send_fn("❌ Balance kam hai! <code>/daily</code> ya <code>/refill</code> chalao.", parse_mode=ParseMode.HTML)
        return
    PENDING_SOLO_BET[(chat_id, user_id)] = {"mode": "aviator_guess", "bet": bet}
    await send_fn(
        f"🚀 <b>DM Aviator Guess</b> · Bet: <code>{bet}</code>\n"
        f"Target multiplier chuno (<code>1.5x–10.0x</code>) ya number type karo (jaise <code>2.4</code>):\n"
        f"• Target touch hua → Full win (<code>bet × target</code>)\n"
        f"• Crash <code>1.5x+</code> par hua → Safe 50% wapas\n"
        f"• Crash <code>1.5x</code> se pehle hua → Poora loss",
        parse_mode=ParseMode.HTML,
        reply_markup=aviator_guess_keyboard(bet),
    )


async def resolve_solo_aviator(
    chat_id: int,
    user_id: int,
    name: str,
    username: Optional[str],
    bet: int,
    send_fn: Callable[..., Coroutine[Any, Any, Any]],
    target_mult: Optional[float] = None,
) -> None:
    if bet < MIN_BET or bet > MAX_BET:
        await send_fn(f"⚠️ Bet <b>{MIN_BET}</b> se <b>{MAX_BET}</b> ke beech rakho.", parse_mode=ParseMode.HTML)
        return
    if target_mult is None:
        target_mult = 2.0
    target_mult = round(max(AVIATOR_MIN_MULTIPLIER, min(AVIATOR_MAX_MULTIPLIER, float(target_mult))), 1)

    db.ensure_user(user_id, name, username)
    bal = db.adjust_coins(user_id, -bet, chat_id=chat_id, name=name, played_delta=1)
    if bal is None:
        await send_fn("❌ Balance kam hai! <code>/daily</code> ya <code>/refill</code> chalao.", parse_mode=ParseMode.HTML)
        return
    db.record_last_bet(user_id, bet)

    steps_total = int(round((AVIATOR_MAX_MULTIPLIER - AVIATOR_MIN_MULTIPLIER) / AVIATOR_STEP_INC))
    crash_step = random.randint(0, max(1, steps_total))
    crash_m = round(AVIATOR_MIN_MULTIPLIER + crash_step * AVIATOR_STEP_INC, 1)

    av_msg = await send_fn(
        f"🚀 <b>DM Aviator Guess</b> · Bet: <code>{bet}</code> · Target: <code>{target_mult:.1f}x</code>\n"
        f"<code>{format_aviator_plane_bar(AVIATOR_MIN_MULTIPLIER)}</code>\n"
        f"⏳ Plane ud raha hai...",
        parse_mode=ParseMode.HTML,
    )

    cur_m = AVIATOR_MIN_MULTIPLIER
    reached_target = False
    while cur_m <= crash_m + 1e-9:
        if cur_m >= target_mult - 1e-9:
            reached_target = True
            break
        if cur_m > AVIATOR_MIN_MULTIPLIER and av_msg and getattr(av_msg, "edit_text", None):
            try:
                await av_msg.edit_text(
                    f"🚀 <b>DM Aviator Guess</b> · Bet: <code>{bet}</code> · Target: <code>{target_mult:.1f}x</code>\n"
                    f"<code>{format_aviator_plane_bar(cur_m)}</code>\n"
                    f"⏳ Plane ud raha hai...",
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest:
                pass
            except TelegramError:
                pass
        await asyncio.sleep(AVIATOR_STEP_SEC)
        cur_m = round(cur_m + AVIATOR_STEP_INC, 1)

    if reached_target:
        payout = int(round(bet * target_mult))
        final_b = db.adjust_coins(
            user_id,
            payout,
            chat_id=chat_id,
            name=name,
            won_delta=1,
            payout_amount=payout,
        )
        b_val = final_b if final_b is not None else bal + payout
        if av_msg and getattr(av_msg, "edit_text", None):
            try:
                await av_msg.edit_text(
                    f"🎉 <b>Target Hit @ <code>{target_mult:.1f}x</code>!</b> (Crash: <code>{crash_m:.1f}x</code>)\n"
                    f"<code>{format_aviator_plane_bar(target_mult, cashed_out=True)}</code>\n"
                    f"💰 Payout: <b>+{payout}</b> · Bal: <code>{b_val}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=post_game_keyboard(f"av_guess:{bet}:{target_mult:.1f}", "menu_aviator"),
                )
            except (BadRequest, TelegramError):
                pass
        return

    if crash_m >= AVIATOR_SAFE_THRESHOLD - 1e-9:
        safe_payout = int(round(bet * AVIATOR_SAFE_RETURN_RATIO))
        final_b = db.adjust_coins(
            user_id,
            safe_payout,
            chat_id=chat_id,
            name=name,
            won_delta=0,
            payout_amount=safe_payout,
        )
        b_val = final_b if final_b is not None else bal + safe_payout
        if av_msg and getattr(av_msg, "edit_text", None):
            try:
                await av_msg.edit_text(
                    f"🛡️ <b>Crashed @ <code>{crash_m:.1f}x</code></b> (Target: <code>{target_mult:.1f}x</code>)\n"
                    f"<code>{format_aviator_plane_bar(crash_m, crashed=True)}</code>\n"
                    f"✨ <code>1.5x</code> se upar tha — Safe Return <b>+{safe_payout}</b> · Bal: <code>{b_val}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=post_game_keyboard(f"av_guess:{bet}:{target_mult:.1f}", "menu_aviator"),
                )
            except (BadRequest, TelegramError):
                pass
        return

    cur_doc = db.get_user(user_id) or {}
    final_b = int(cur_doc.get("coins", bal))
    if av_msg and getattr(av_msg, "edit_text", None):
        try:
            await av_msg.edit_text(
                f"💥 <b>Crashed @ <code>{crash_m:.1f}x</code>!</b> (Target: <code>{target_mult:.1f}x</code>)\n"
                f"<code>{format_aviator_plane_bar(crash_m, crashed=True)}</code>\n"
                f"💸 Loss: <b>-{bet}</b> · Bal: <code>{final_b}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=post_game_keyboard(f"av_guess:{bet}:{target_mult:.1f}", "menu_aviator"),
            )
        except (BadRequest, TelegramError):
            pass


# ==============================================================================
# GROUP 15s ROUNDS + 2s PER-USER BET OPTION + AVIATOR LIVE CASH-OUT FEED
# ==============================================================================
def format_locked_bets_text(round_obj: Dict[str, Any]) -> str:
    bets: Dict[int, Dict[str, Any]] = round_obj.get("bets", {})
    if not bets:
        return "🔒 <i>Abhi kisi ne bet lock nahi ki hai.</i>"
    lines = ["🔒 <b>Locked Bets:</b>"]
    for item in bets.values():
        lines.append(
            f"• ✅ {mention_user(item.get('user_id', 0), item['name'], item.get('username'))} ne <code>{item['bet']}</code> coins par lock kar diya ({item['choice']})"
        )
    return "\n".join(lines)


def get_round_remaining_sec(round_obj: Dict[str, Any]) -> int:
    ends_at = float(round_obj.get("ends_at", time.time() + ROUND_SECONDS))
    return max(0, int(round(ends_at - time.time())))


def build_round_view(round_obj: Dict[str, Any], user_coins: int = 1000) -> Tuple[str, InlineKeyboardMarkup]:
    rem = get_round_remaining_sec(round_obj)
    game_key = round_obj["game"]
    locked_txt = format_locked_bets_text(round_obj)

    if game_key == "aviator":
        header = f"🚀 <b>Aviator Group Round</b> (⏳ <code>{rem}s</code> left)\nApni bet lock karo!"
        return f"{header}\n\n{locked_txt}", bet_amounts_keyboard(user_coins, f"rbet:{round_obj['id']}:flight")

    bet_opt = round_obj.get("active_bet_option")
    if isinstance(bet_opt, dict) and time.time() < float(bet_opt.get("expires_at", 0.0)):
        opt_uid = int(bet_opt["user_id"])
        opt_name = bet_opt["name"]
        opt_choice = bet_opt["choice"]
        rem_opt = max(0.1, round(float(bet_opt["expires_at"]) - time.time(), 1))
        header = (
            f"🎮 <b>{game_key.upper()} Round</b> (⏳ <code>{rem}s</code> round left)\n"
            f"⚡ <b>{opt_name}</b> ne <b>{opt_choice}</b> chuna — Bet option <b>{rem_opt:.1f}s</b> ke liye khula hai!\n"
            f"<i>(Click kar ke bet lock karo; na karne par apne aap band ho jayega)</i>"
        )
        kb = bet_amounts_keyboard(user_coins, f"rbet:{round_obj['id']}:{opt_choice}")
        return f"{header}\n\n{locked_txt}", kb

    theme_note = ""
    if game_key == "slots" and round_obj.get("slot_theme") in SLOT_THEMES:
        theme_note = f" · Theme: <b>{SLOT_THEMES[round_obj['slot_theme']][0]}</b>"
    header = (
        f"🎮 <b>{game_key.upper()} Round</b> (⏳ <code>{rem}s</code> left){theme_note}\n"
        f"Apni choice chuno (<code>2s</code> bet window milega):"
    )
    return f"{header}\n\n{locked_txt}", game_choice_keyboard(game_key, round_obj["id"])


async def _bet_option_2s_expiry(
    app: Application,
    chat_id: int,
    round_id: str,
    user_id: int,
    token: int,
) -> None:
    try:
        await asyncio.sleep(BET_OPTION_SECONDS)
        round_obj = ROUNDS_BY_ID.get(round_id)
        if not round_obj or round_obj.get("phase") == "done":
            return
        bet_opt = round_obj.get("active_bet_option")
        if not isinstance(bet_opt, dict):
            return
        if int(bet_opt.get("user_id", 0)) != user_id or int(bet_opt.get("token", 0)) != token:
            return
        # 2s elapsed without locking bet -> close bet option automatically so others can pick
        round_obj["active_bet_option"] = None
        msg_id = round_obj.get("message_id")
        if isinstance(msg_id, int) and msg_id > 0:
            txt, kb = build_round_view(round_obj, user_coins=1000)
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=txt,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
            except (BadRequest, TelegramError):
                pass
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.debug("bet_option_2s_expiry error: %s", exc)


def format_aviator_cashout_feed(flight_obj: Dict[str, Any]) -> str:
    cashouts: List[Dict[str, Any]] = flight_obj.get("cashouts", [])
    if not cashouts:
        return "📡 <i>Cash-Out Feed: Abhi kisi ne cash out nahi kiya...</i>"
    lines = ["📡 <b>Live Cash-Out Feed:</b>"]
    for c in cashouts:
        lines.append(
            f"• ✅ {mention_user(c.get('user_id', 0), c['name'], c.get('username'))} ne <code>{c['mult']:.1f}x</code> par Cash Out kar liya! (<code>+{c['payout']}</code> coins)"
        )
    return "\n".join(lines)


async def run_aviator_group_flight(app: Application, chat_id: int, round_obj: Dict[str, Any]) -> None:
    clear_bet_option_for_user(round_obj, int((round_obj.get("active_bet_option") or {}).get("user_id", 0)))
    bets: Dict[int, Dict[str, Any]] = round_obj.get("bets", {})
    round_obj["phase"] = "flight"
    msg_id = round_obj["message_id"]
    if not bets:
        unregister_round(chat_id, round_obj["id"])
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text="🚀 <b>Aviator</b> — Round khatam, kisi player ne bet lock nahi ki.",
                parse_mode=ParseMode.HTML,
            )
            schedule_delete_messages(app.bot, chat_id, [msg_id], AUTO_DELETE_RESULT_SEC)
        except (BadRequest, TelegramError):
            pass
        return

    steps_total = int(round((AVIATOR_MAX_MULTIPLIER - AVIATOR_MIN_MULTIPLIER) / AVIATOR_STEP_INC))
    crash_step = random.randint(2, max(2, steps_total))
    crash_mult = round(AVIATOR_MIN_MULTIPLIER + crash_step * AVIATOR_STEP_INC, 1)
    flight_state = {
        "active": True,
        "mult": AVIATOR_MIN_MULTIPLIER,
        "crash_mult": crash_mult,
        "players": {
            uid: {
                "user_id": uid,
                "name": info["name"],
                "username": info.get("username", ""),
                "bet": info["bet"],
                "cashed_out": False,
                "mult": 0.0,
                "payout": 0,
            }
            for uid, info in round_obj.get("bets", {}).items()
        },
        "cashouts": [],
    }
    round_obj["flight"] = flight_state

    def build_flight_track(mult_val: float, crashed: bool = False) -> str:
        return format_aviator_plane_bar(mult_val, crashed=crashed)

    cur_mult = AVIATOR_MIN_MULTIPLIER
    while cur_mult <= crash_mult + 1e-9:
        if (time.time() - float(round_obj.get("created_at", time.time()))) > TIMER_GAME_MAX_AGE_SEC:
            break
        cur_rounded = round(cur_mult, 1)
        flight_state["mult"] = cur_rounded
        is_blast_step = abs(cur_rounded - crash_mult) < 1e-6
        if is_blast_step:
            flight_state["active"] = False
            live_kb = None
            status_line = f"💥 <b>BLAST! Plane <code>{crash_mult:.1f}x</code> par phat gaya!</b> (Sabka result neeche dekho)"
        else:
            live_kb = InlineKeyboardMarkup(
                [
                    [stylish_btn(f"💸 Cash Out ({cur_rounded:.1f}x)", f"av_cash:{round_obj['id']}", "success")],
                    [
                        stylish_btn("100", f"rbet:{round_obj['id']}:flight:100", "primary"),
                        stylish_btn("250", f"rbet:{round_obj['id']}:flight:250", "primary"),
                        stylish_btn("500", f"rbet:{round_obj['id']}:flight:500", "primary"),
                    ],
                ]
            )
            status_line = "⚡ Crash se pehle Cash Out dabao — baaki log bhi bet laga sakte hain!"
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=(
                    f"{'💥 <b>Aviator BLAST:</b>' if is_blast_step else '🚀 <b>Aviator Live:</b>'} <code>{cur_rounded:.1f}x</code>\n"
                    f"<code>{build_flight_track(cur_rounded, crashed=is_blast_step)}</code>\n"
                    f"{status_line}\n\n"
                    f"{format_aviator_cashout_feed(flight_state)}"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=live_kb,
            )
        except (BadRequest, TelegramError):
            pass
        if is_blast_step:
            await asyncio.sleep(1.2)
            break
        await asyncio.sleep(AVIATOR_STEP_SEC)
        cur_mult = round(cur_mult + AVIATOR_STEP_INC, 1)

    flight_state["active"] = False
    round_obj["phase"] = "done"
    unregister_round(chat_id, round_obj["id"])

    summary_lines = [
        f"💥 <b>BLAST AT <code>{crash_mult:.1f}x</code>! Aviator Crashed!</b>",
        f"<code>{build_flight_track(crash_mult, crashed=True)}</code>",
        "",
        "📋 <b>Final Result (Sabka Ek Saath):</b>",
    ]
    for uid, pdata in flight_state["players"].items():
        bal = db.get_user(uid)
        b_val = int(bal.get("coins", 0)) if bal else 0
        if pdata["cashed_out"]:
            summary_lines.append(
                f"• ✅ {mention_user(uid, pdata['name'], pdata.get('username'))} — Bet <code>{pdata['bet']}</code> | Cash Out <code>{pdata['mult']:.1f}x</code> → <b>+{pdata['payout']}</b> (Bal: <code>{b_val}</code>)"
            )
        else:
            summary_lines.append(
                f"• 💥 {mention_user(uid, pdata['name'], pdata.get('username'))} — Bet <code>{pdata['bet']}</code> | Blast (<code>{crash_mult:.1f}x</code>) mein reh gaya → <b>-{pdata['bet']}</b> (Bal: <code>{b_val}</code>)"
            )
    summary_lines.append("")
    summary_lines.append(f"💥 <b>BLASTED AT <code>{crash_mult:.1f}x</code> — Game Khatam!</b>")
    summary_text = "\n".join(summary_lines)

    edited_ok = False
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=summary_text,
            parse_mode=ParseMode.HTML,
            reply_markup=post_game_keyboard("menu_aviator"),
        )
        edited_ok = True
        schedule_delete_messages(app.bot, chat_id, [msg_id], AUTO_DELETE_RESULT_SEC)
    except (BadRequest, TelegramError):
        pass

    if not edited_ok:
        try:
            c_msg = await app.bot.send_message(
                chat_id=chat_id,
                text=summary_text,
                parse_mode=ParseMode.HTML,
                reply_markup=post_game_keyboard("menu_aviator"),
            )
            if c_msg and getattr(c_msg, "message_id", None):
                schedule_delete_messages(app.bot, chat_id, [c_msg.message_id], AUTO_DELETE_RESULT_SEC)
        except (BadRequest, TelegramError):
            pass


async def finalize_group_round(app: Application, chat_id: int, round_id: str) -> None:
    round_obj = ACTIVE_ROUNDS.get(round_id) or ROUNDS_BY_ID.get(round_id)
    if not round_obj or round_obj.get("id") != round_id or round_obj.get("phase") == "done":
        return
    bet_opt = round_obj.get("active_bet_option")
    if isinstance(bet_opt, dict) and bet_opt.get("task") and not bet_opt["task"].done():
        bet_opt["task"].cancel()
    round_obj["active_bet_option"] = None

    game_key = round_obj["game"]
    if game_key == "aviator":
        await run_aviator_group_flight(app, chat_id, round_obj)
        return
    round_obj["phase"] = "done"
    unregister_round(chat_id, round_id)
    bets: Dict[int, Dict[str, Any]] = round_obj.get("bets", {})
    msg_id = round_obj["message_id"]
    if not bets:
        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=f"⏱️ <b>{game_key.upper()}</b> — {ROUND_SECONDS}s round khatam, kisi ne bet lock nahi ki.",
                parse_mode=ParseMode.HTML,
                reply_markup=post_game_keyboard(f"pick_game:{game_key}"),
            )
            schedule_delete_messages(app.bot, chat_id, [msg_id], AUTO_DELETE_RESULT_SEC)
        except (BadRequest, TelegramError):
            pass
        return

    outcome_label = ""
    winning_choices: set = set()
    mult_map: Dict[str, float] = {}
    guess_target: Optional[int] = None

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
        outcome_label = format_dice_roll_anim(roll)
        winning_choices = {str(roll)}
        mult_map = {str(roll): 5.2}
    elif game_key == "slots":
        t_key = round_obj.get("slot_theme", "classic")
        sym = random.choice(["cherry", "gem", "seven"])
        outcome_label = format_slot_spin_anim(sym, t_key, won=None)
        winning_choices = {sym}
        mult_map = {"cherry": 2.5, "gem": 4.0, "seven": 7.0}
    elif game_key == "guess":
        num = random.randint(1, 10)
        guess_target = num
        outcome_label = f"🔢 Number: <b>{num}</b>"
        winning_choices = {str(num)}
        mult_map = {str(num): GUESS_EXACT_MULT}
    elif game_key == "wheel":
        pocket = random.choice(["2x", "2x", "3x", "5x", "bust"])
        outcome_label = f"🎡 Wheel: <b>{pocket}</b>"
        if pocket != "bust":
            winning_choices = {pocket}
            mult_map = {"2x": 2.0, "3x": 3.0, "5x": 5.0}
    else:
        sport_sticker_map = {
            "hoops": ("🏀", "Basketball"),
            "goal": ("⚽", "Football Goal"),
            "target": ("🎯", "Dart Bullseye"),
            "strike": ("🎳", "Bowling Strike"),
        }
        sport_res = random.choice(list(sport_sticker_map.keys()))
        outcome_label = format_sports_anim(sport_res)
        winning_choices = {sport_res}
        mult_map = {sport_res: 3.5}

    result_lines = [f"🏁 <b>{game_key.upper()} Result</b> · {outcome_label}", ""]
    for uid, bdata in bets.items():
        ch = bdata["choice"]
        bet_amt = int(bdata["bet"])
        if game_key == "guess" and guess_target is not None:
            m, payout, g_mode = eval_guess_payout(ch, guess_target, bet_amt)
            won = g_mode in ("exact", "near")
        else:
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
            f"• {tag} {mention_user(uid, bdata['name'], bdata.get('username'))} ({ch}) — Bet <code>{bet_amt}</code> → Payout <code>{payout}</code> (Bal: <code>{bal}</code>)"
        )

    if game_key in ("dice", "sports", "slots"):
        if game_key == "dice":
            chosen_d = "🎲"
        elif game_key == "slots":
            chosen_d = "🎰"
        else:
            g_dice_map = {"hoops": "🏀", "goal": "⚽", "target": "🎯", "strike": "🎳"}
            chosen_d = next((g_dice_map[k] for k in winning_choices if k in g_dice_map), "🏀")
        try:
            d_msg = await app.bot.send_dice(chat_id=chat_id, emoji=chosen_d)
            if d_msg and getattr(d_msg, "message_id", None):
                schedule_delete_messages(app.bot, chat_id, [d_msg.message_id], AUTO_DELETE_RESULT_SEC)
        except (BadRequest, TelegramError):
            pass

    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text="\n".join(result_lines),
            parse_mode=ParseMode.HTML,
            reply_markup=post_game_keyboard(f"pick_game:{game_key}"),
        )
        schedule_delete_messages(app.bot, chat_id, [msg_id], AUTO_DELETE_RESULT_SEC)
    except (BadRequest, TelegramError):
        pass


async def group_round_timer_task(app: Application, chat_id: int, round_id: str) -> None:
    try:
        step = 4
        while True:
            round_obj = ACTIVE_ROUNDS.get(round_id) or ROUNDS_BY_ID.get(round_id)
            if not round_obj or round_obj.get("id") != round_id or round_obj.get("phase") == "done":
                return
            rem = get_round_remaining_sec(round_obj)
            if rem <= 0:
                break
            await asyncio.sleep(min(step, rem))
            round_obj = ACTIVE_ROUNDS.get(round_id) or ROUNDS_BY_ID.get(round_id)
            if not round_obj or round_obj.get("id") != round_id or round_obj.get("phase") == "done":
                return
            if (time.time() - float(round_obj.get("created_at", time.time()))) > TIMER_GAME_MAX_AGE_SEC:
                unregister_round(chat_id, round_id)
                return
            msg_id = round_obj.get("message_id")
            if not msg_id:
                continue
            new_txt, kb = build_round_view(round_obj, user_coins=1000)
            if round_obj.get("last_text") != new_txt:
                round_obj["last_text"] = new_txt
                try:
                    await app.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=new_txt,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb,
                    )
                except BadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        logger.debug("Round timer edit skipped: %s", exc)
                except TelegramError as exc:
                    logger.debug("Round timer telegram error: %s", exc)
        await finalize_group_round(app, chat_id, round_id)
    except Exception as exc:
        logger.debug("group_round_timer_task error: %s", exc)
        unregister_round(chat_id, round_id)


async def start_group_round(
    app: Application,
    chat_id: int,
    starter_id: int,
    starter_name: str,
    game_key: str,
    send_fn: Callable[..., Coroutine[Any, Any, Any]],
) -> None:
    existing_ids = CHAT_ROUNDS.get(chat_id, [])
    if len(existing_ids) >= 6:
        await send_fn(
            "⚠️ Ek saath 6 rounds chal rahe hain—thoda ruk kar naya round start karo.",
            parse_mode=ParseMode.HTML,
        )
        return
    now = time.time()
    round_id = f"{abs(chat_id)}_{int(now * 1000)}_{random.randint(10, 99)}"
    round_obj: Dict[str, Any] = {
        "id": round_id,
        "chat_id": chat_id,
        "game": game_key,
        "phase": "bet" if game_key == "aviator" else "pick",
        "choices": {},
        "bets": {},
        "active_bet_option": None,
        "bet_opt_token": 0,
        "message_id": 0,
        "created_at": now,
        "ends_at": now + ROUND_SECONDS,
    }
    ACTIVE_ROUNDS[round_id] = round_obj
    ROUNDS_BY_ID[round_id] = round_obj
    CHAT_ROUNDS.setdefault(chat_id, []).append(round_id)

    if game_key == "slots":
        round_obj["slot_theme"] = random.choice(list(SLOT_THEMES.keys()))

    body_txt, markup = build_round_view(round_obj, user_coins=1000)
    msg = await send_fn(
        f"Started by <b>{starter_name}</b> ({ROUND_SECONDS}s window)\n{body_txt}",
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
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
    is_new = db.get_user(u.id) is None
    db.ensure_user(u.id, u.first_name, u.username)
    if chat:
        db.record_chat(chat.id, chat.title or u.first_name, chat.type)

    ref_msg = ""
    if context.args and is_new:
        try:
            ref_id = int(context.args[0].strip())
            if db.apply_referral_once(u.id, ref_id):
                db.adjust_coins(u.id, REFERRAL_BONUS_COINS)
                db.adjust_coins(ref_id, REFERRAL_BONUS_COINS)
                ref_msg = f"\n🎁 Referral bonus: Tumhe aur referrer dono ko <b>+{REFERRAL_BONUS_COINS}</b> coins mile!"
        except ValueError:
            pass

    uname = BOT_USERNAME or (context.bot.username if context.bot else None)
    await update.message.reply_text(
        f"👋 Namaste <b>{u.first_name}</b>! Game shuru karne ke liye neeche se mode chuno:{ref_msg}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_keyboard(uname),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    cid = update.effective_chat.id if update.effective_chat else update.message.chat_id
    m = await update.message.reply_text(help_text(), parse_mode=ParseMode.HTML)
    schedule_delete_messages(context.bot, cid, [m.message_id], STALE_UI_DELETE_SEC)


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    cid = update.effective_chat.id if update.effective_chat else update.message.chat_id
    m = await update.message.reply_text(
        format_profile_card(u.id, u.first_name, u.username, chat_id=cid),
        parse_mode=ParseMode.HTML,
    )
    schedule_delete_messages(context.bot, cid, [m.message_id], STALE_UI_DELETE_SEC)


async def cmd_bal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    cid = update.effective_chat.id if update.effective_chat else update.message.chat_id
    m = await update.message.reply_text(
        format_balance_card(u.id, u.first_name, u.username),
        parse_mode=ParseMode.HTML,
    )
    schedule_delete_messages(context.bot, cid, [m.message_id], STALE_UI_DELETE_SEC)


async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    cid = update.effective_chat.id if update.effective_chat else update.message.chat_id
    _, msg = await claim_daily_logic(u.id, u.first_name, u.username)
    m = await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    schedule_delete_messages(context.bot, cid, [m.message_id], STALE_UI_DELETE_SEC)


async def cmd_refill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    cid = update.effective_chat.id if update.effective_chat else update.message.chat_id
    _, msg = await claim_refill_logic(u.id, u.first_name, u.username)
    m = await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    schedule_delete_messages(context.bot, cid, [m.message_id], STALE_UI_DELETE_SEC)


async def cmd_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    db.ensure_user(u.id, u.first_name, u.username)
    uname = BOT_USERNAME or (context.bot.username if context.bot else None)
    await update.message.reply_text(
        format_referral_card(u.id, u.first_name, uname),
        parse_mode=ParseMode.HTML,
        reply_markup=referral_keyboard(u.id, uname),
    )


async def cmd_give(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    sender = update.effective_user
    db.ensure_user(sender.id, sender.first_name, sender.username)
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        await update.message.reply_text(
            "⚠️ Jis user ko coins dene hain uske message par reply karke <code>/give 100</code> likho.",
            parse_mode=ParseMode.HTML,
        )
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
    cid = update.effective_chat.id if update.effective_chat else update.message.chat_id
    rows = db.global_top(10)
    if not rows:
        m = await update.message.reply_text("🌍 Leaderboard abhi khali hai.")
        schedule_delete_messages(context.bot, cid, [m.message_id], STALE_UI_DELETE_SEC)
        return
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = ["🌍 <b>Global Rank Top 10</b>", ""]
    for idx, r in enumerate(rows):
        prefix = medals.get(idx, f"{idx + 1}.")
        lines.append(f"{prefix} <b>{r.get('first_name', 'Player')}</b> — <code>{int(r.get('coins', 0))}</code> coins")
    m = await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    schedule_delete_messages(context.bot, cid, [m.message_id], STALE_UI_DELETE_SEC)


async def cmd_gtop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    chat = update.effective_chat
    if chat.type == "private":
        await cmd_top(update, context)
        return
    rows = db.group_top(chat.id, 10)
    if not rows:
        m = await update.message.reply_text("👥 Is group ka leaderboard abhi khali hai.")
        schedule_delete_messages(context.bot, chat.id, [m.message_id], STALE_UI_DELETE_SEC)
        return
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = [f"👥 <b>{chat.title or 'Group'} Top 10</b>", ""]
    for idx, r in enumerate(rows):
        prefix = medals.get(idx, f"{idx + 1}.")
        lines.append(f"{prefix} <b>{r.get('name', 'Player')}</b> — <code>{int(r.get('net_coins', 0))}</code> net")
    m = await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    schedule_delete_messages(context.bot, chat.id, [m.message_id], STALE_UI_DELETE_SEC)


async def cmd_mines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or not update.message:
        return
    u = update.effective_user
    chat_id = update.effective_chat.id
    user_doc = db.ensure_user(u.id, u.first_name, u.username)
    PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": "mines_bet", "bombs": 4}
    m = await update.message.reply_text(
        "💣 <b>Mines</b> — Bombs ya bet amount chuno (ya number type karo):",
        parse_mode=ParseMode.HTML,
        reply_markup=bet_amounts_keyboard(
            int(user_doc.get("coins", 0)),
            "sbet:mines:4",
            extra_rows=[[stylish_btn(f"{b} 💣", f"mines_bombs:{b}", "danger") for b in MINES_BOMB_CHOICES]],
        ),
    )
    schedule_delete_messages(context.bot, chat_id, [m.message_id], STALE_UI_DELETE_SEC)


async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    u = update.effective_user
    db.ensure_user(u.id, u.first_name, u.username)
    if not context.args:
        await update.message.reply_text("Use: <code>/redeem xyz</code>", parse_mode=ParseMode.HTML)
        return
    code_str = context.args[0].strip()
    _, msg, _ = db.claim_redeem_code(code_str, u.id)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ==============================================================================
# OWNER-ONLY COMMANDS (INCLUDING REDEEM CODE SETUP)
# ==============================================================================
async def cmd_redeemcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    u = update.effective_user
    if not context.args:
        await update.message.reply_text(
            "Use: <code>/redeemcode xyz</code> ya <code>/redeemcode xyz 1000</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    code_str = context.args[0].strip()
    existing = db.get_redeem_code(code_str)
    default_coins = int(existing.get("coins", 500)) if existing else 500
    if len(context.args) >= 2:
        try:
            default_coins = max(1, int(context.args[1]))
        except ValueError:
            await update.message.reply_text("⚠️ Valid coins number daalo.", parse_mode=ParseMode.HTML)
            return
    doc = db.set_redeem_code(code_str, default_coins, u.id)
    await update.message.reply_text(
        f"🎟️ <b>Redeem Code Set!</b>\n"
        f"• <b>Code:</b> <code>{doc['code']}</code>\n"
        f"• <b>Coins:</b> <code>{doc['coins']}</code>\n"
        f"• <b>Limit:</b> Ek user ke liye <b>1 baar</b>\n"
        f"• <b>Validity:</b> <b>24 hours</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_setredeemcoins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: <code>/setredeemcoins 1000</code>", parse_mode=ParseMode.HTML)
        return
    try:
        coins_val = int(context.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Valid number daalo.", parse_mode=ParseMode.HTML)
        return
    if coins_val <= 0:
        await update.message.reply_text("⚠️ Coins 0 se zyada rakho.", parse_mode=ParseMode.HTML)
        return
    updated = db.set_redeem_coins_for_latest(coins_val)
    if not updated:
        await update.message.reply_text("⚠️ Pehle <code>/redeemcode xyz</code> se code set karo.", parse_mode=ParseMode.HTML)
        return
    await update.message.reply_text(
        f"🪙 <b>Redeem Coins Updated!</b>\n"
        f"• <b>Code:</b> <code>{updated['code']}</code>\n"
        f"• <b>Reward Coins:</b> <code>{updated['coins']}</code>\n"
        f"• <b>Validity:</b> <b>24 hours</b> (1 claim per user)",
        parse_mode=ParseMode.HTML,
    )


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


async def cmd_setmywins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    u = update.effective_user
    db.ensure_user(u.id, u.first_name, u.username)
    if not context.args:
        await update.message.reply_text("Use: <code>/setmywins 25</code>", parse_mode=ParseMode.HTML)
        return
    try:
        wins_val = max(0, int(context.args[0]))
    except ValueError:
        await update.message.reply_text("⚠️ Valid number daalo.", parse_mode=ParseMode.HTML)
        return
    doc = db.get_user(u.id) or {}
    cur_played = int(doc.get("games_played", 0))
    new_played = max(cur_played, wins_val)
    if db.use_mongo and db.users_col is not None:
        try:
            db.users_col.update_one(
                {"user_id": u.id},
                {"$set": {"games_won": wins_val, "games_played": new_played}},
            )
        except PyMongoError:
            pass
    if u.id in db.mem_users:
        db.mem_users[u.id]["games_won"] = wins_val
        db.mem_users[u.id]["games_played"] = new_played
    await update.message.reply_text(
        f"👑 Owner Wins Set to <code>{wins_val}</code> (Played: <code>{new_played}</code>)",
        parse_mode=ParseMode.HTML,
    )


async def cmd_setmyloss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not is_owner(update.effective_user.id):
        return
    u = update.effective_user
    db.ensure_user(u.id, u.first_name, u.username)
    if not context.args:
        await update.message.reply_text("Use: <code>/setmyloss 10</code>", parse_mode=ParseMode.HTML)
        return
    try:
        loss_val = max(0, int(context.args[0]))
    except ValueError:
        await update.message.reply_text("⚠️ Valid number daalo.", parse_mode=ParseMode.HTML)
        return
    doc = db.get_user(u.id) or {}
    cur_wins = int(doc.get("games_won", 0))
    new_played = cur_wins + loss_val
    if db.use_mongo and db.users_col is not None:
        try:
            db.users_col.update_one(
                {"user_id": u.id},
                {"$set": {"games_loss": loss_val, "games_played": new_played}},
            )
        except PyMongoError:
            pass
    if u.id in db.mem_users:
        db.mem_users[u.id]["games_loss"] = loss_val
        db.mem_users[u.id]["games_played"] = new_played
    await update.message.reply_text(
        f"👑 Owner Loss Set to <code>{loss_val}</code> (Wins: <code>{cur_wins}</code> · Played: <code>{new_played}</code>)",
        parse_mode=ParseMode.HTML,
    )


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
        await update.message.reply_text(
            "Use: kisi message par reply karke <code>/broadcast</code> ya <code>/broadcast &lt;text&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
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
    user_doc = db.ensure_user(u.id, u.first_name, u.username)
    if chat:
        db.record_chat(chat.id, chat.title or u.first_name, chat.type)
    bot_uname = BOT_USERNAME or (context.bot.username if context.bot else None)

    async def reply_cb(
        text: str = "",
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        parse_mode: str = ParseMode.HTML,
        dice_emoji: Optional[str] = None,
    ) -> Any:
        if dice_emoji:
            if query.message:
                try:
                    return await query.message.reply_dice(emoji=dice_emoji)
                except (BadRequest, TelegramError):
                    pass
            try:
                return await context.application.bot.send_dice(chat_id=chat_id, emoji=dice_emoji)
            except (BadRequest, TelegramError):
                return None
        if query.message:
            return await query.message.reply_text(
                text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        return await context.application.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )

    try:
        if data == "back_menu":
            await query.answer()
            if query.message:
                try:
                    await query.message.edit_text(
                        f"👋 Namaste <b>{u.first_name}</b>! Mode chuno:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=main_menu_keyboard(bot_uname),
                    )
                except (BadRequest, TelegramError):
                    pass
            return

        if data == "menu_refer":
            await query.answer()
            m_ref = await reply_cb(
                format_referral_card(u.id, u.first_name, bot_uname),
                reply_markup=referral_keyboard(u.id, bot_uname),
            )
            if m_ref and getattr(m_ref, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_ref.message_id], STALE_UI_DELETE_SEC)
            return

        if data == "do_daily":
            ok, msg = await claim_daily_logic(u.id, u.first_name, u.username)
            await query.answer("Claimed!" if ok else "Cooldown", show_alert=not ok)
            m_info = await reply_cb(msg)
            if m_info and getattr(m_info, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
            return

        if data == "do_refill":
            ok, msg = await claim_refill_logic(u.id, u.first_name, u.username)
            await query.answer("Refilled!" if ok else "Cooldown", show_alert=not ok)
            m_info = await reply_cb(msg)
            if m_info and getattr(m_info, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
            return

        if data == "menu_bal":
            await query.answer()
            m_info = await reply_cb(format_balance_card(u.id, u.first_name, u.username))
            if m_info and getattr(m_info, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
            return

        if data == "menu_profile":
            await query.answer()
            m_info = await reply_cb(format_profile_card(u.id, u.first_name, u.username, chat_id=chat_id))
            if m_info and getattr(m_info, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
            return

        if data == "menu_help":
            await query.answer()
            m_info = await reply_cb(help_text())
            if m_info and getattr(m_info, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
            return

        if data == "menu_top":
            await query.answer()
            rows = db.global_top(10)
            medals = {0: "🥇", 1: "🥈", 2: "🥉"}
            lines = ["🌍 <b>Global Rank Top 10</b>", ""]
            for idx, r in enumerate(rows):
                lines.append(f"{medals.get(idx, f'{idx + 1}.')} <b>{r.get('first_name', 'Player')}</b> — <code>{int(r.get('coins', 0))}</code> coins")
            m_info = await reply_cb("\n".join(lines))
            if m_info and getattr(m_info, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
            return

        if data == "menu_gtop":
            await query.answer()
            if is_private:
                m_info = await reply_cb("👥 Group Top ke liye kisi group mein <code>/gtop</code> chalao.")
                if m_info and getattr(m_info, "message_id", None):
                    schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
                return
            rows = db.group_top(chat_id, 10)
            medals = {0: "🥇", 1: "🥈", 2: "🥉"}
            lines = [f"👥 <b>{chat.title or 'Group'} Top 10</b>", ""]
            for idx, r in enumerate(rows):
                lines.append(f"{medals.get(idx, f'{idx + 1}.')} <b>{r.get('name', 'Player')}</b> — <code>{int(r.get('net_coins', 0))}</code> net")
            m_info = await reply_cb("\n".join(lines))
            if m_info and getattr(m_info, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
            return

        if data == "menu_ttt":
            await query.answer()
            m_ttt = await reply_cb(
                "❎ <b>Tic Tac Toe</b> — Board size chuno:\n• <code>3×3</code>: 2–3 players\n• <code>4×4</code>: 2–4 players\n• <code>5×5</code>: 2–6 players",
                reply_markup=ttt_size_keyboard(),
            )
            if m_ttt and getattr(m_ttt, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_ttt.message_id], STALE_UI_DELETE_SEC)
            return

        if data == "menu_mines":
            await query.answer()
            PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": "mines_bet", "bombs": 4}
            m_info = await reply_cb("💣 <b>Mines</b> — Kitne bombs chahiye?", reply_markup=mines_bombs_keyboard())
            if m_info and getattr(m_info, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
            return

        if data.startswith("mines_bombs:"):
            await query.answer()
            bombs = int(data.split(":", 1)[1])
            PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": "mines_bet", "bombs": bombs}
            m_info = await reply_cb(
                f"💣 <b>{bombs} Mines</b> selected — Bet amount chuno (ya number type karo):",
                reply_markup=bet_amounts_keyboard(int(user_doc.get("coins", 0)), f"sbet:mines:{bombs}"),
            )
            if m_info and getattr(m_info, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
            return

        if data == "menu_aviator":
            await query.answer()
            if is_private:
                PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": "aviator_solo_bet"}
                m_info = await reply_cb(
                    "🚀 <b>DM Aviator Guess</b> — Pehle bet amount chuno (ya number type karo):",
                    reply_markup=bet_amounts_keyboard(int(user_doc.get("coins", 0)), "sbet:aviator:0"),
                )
                if m_info and getattr(m_info, "message_id", None):
                    schedule_delete_messages(context.application.bot, chat_id, [m_info.message_id], STALE_UI_DELETE_SEC)
            else:
                await start_group_round(context.application, chat_id, u.id, u.first_name, "aviator", reply_cb)
            return

        if data.startswith("av_guess:"):
            await query.answer()
            _, bet_str, mult_str = data.split(":", 2)
            PENDING_SOLO_BET.pop((chat_id, u.id), None)
            await resolve_solo_aviator(
                chat_id,
                u.id,
                u.first_name,
                u.username,
                int(bet_str),
                reply_cb,
                target_mult=float(mult_str),
            )
            return

        if data.startswith("pick_game:"):
            await query.answer()
            game_key = data.split(":", 1)[1]
            if is_private:
                PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": f"solo_game:{game_key}", "choice": None}
                m_ch = await reply_cb(
                    f"🎮 <b>{game_key.upper()}</b> — Pehle apni choice chuno:",
                    reply_markup=solo_choice_keyboard(game_key),
                )
                if m_ch and getattr(m_ch, "message_id", None):
                    schedule_delete_messages(context.application.bot, chat_id, [m_ch.message_id], STALE_UI_DELETE_SEC)
            else:
                await start_group_round(context.application, chat_id, u.id, u.first_name, game_key, reply_cb)
            return

        if data.startswith("schoice:"):
            await query.answer()
            _, gkey, ch_val = data.split(":", 2)
            PENDING_SOLO_BET[(chat_id, u.id)] = {"mode": f"solo_game:{gkey}", "choice": ch_val}
            m_bet = await reply_cb(
                f"🎮 <b>{gkey.upper()}</b> · Pick: <b>{ch_val}</b>\nBet amount chuno (ya number type karo):",
                reply_markup=bet_amounts_keyboard(int(user_doc.get("coins", 0)), f"sbet:{gkey}:0"),
            )
            if m_bet and getattr(m_bet, "message_id", None):
                schedule_delete_messages(context.application.bot, chat_id, [m_bet.message_id], STALE_UI_DELETE_SEC)
            return

        if data.startswith("sbet:"):
            await query.answer()
            _, mode_key, extra_str, amt_str = data.split(":", 3)
            amt = int(amt_str)
            pending = PENDING_SOLO_BET.pop((chat_id, u.id), {})
            if mode_key == "mines":
                await start_mines_game(chat_id, u.id, u.first_name, u.username, amt, int(extra_str), reply_cb)
            elif mode_key == "aviator":
                await prompt_aviator_guess(chat_id, u.id, u.first_name, u.username, amt, reply_cb)
            else:
                default_choice = pending.get("choice")
                if not default_choice:
                    default_map = {
                        "rps": "rock",
                        "hilo": "high",
                        "flip": "heads",
                        "dice": "6",
                        "slots": "gem",
                        "guess": "7",
                        "wheel": "2x",
                        "sports": "goal",
                    }
                    default_choice = default_map.get(mode_key, "heads")
                await resolve_solo_game(chat_id, u.id, u.first_name, u.username, mode_key, default_choice, amt, reply_cb)
            return

        if data.startswith("stheme:"):
            round_id = data.split(":", 1)[1]
            round_obj = ROUNDS_BY_ID.get(round_id)
            if not round_obj or round_obj.get("game") != "slots" or round_obj.get("phase") == "done":
                await query.answer("Theme abhi badla nahi ja sakta.", show_alert=True)
                return
            order = list(SLOT_THEMES.keys())
            cur_t = round_obj.get("slot_theme", "classic")
            next_t = order[(order.index(cur_t) + 1) % len(order)] if cur_t in order else "classic"
            round_obj["slot_theme"] = next_t
            await query.answer(f"Theme: {SLOT_THEMES[next_t][0]}")
            if query.message:
                txt_st, kb_st = build_round_view(round_obj, user_coins=int(user_doc.get("coins", 1000)))
                round_obj["last_text"] = txt_st
                try:
                    await query.message.edit_text(
                        txt_st,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_st,
                    )
                except (BadRequest, TelegramError):
                    pass
            return

        if data.startswith("rchoice:"):
            _, round_id, choice = data.split(":", 2)
            round_obj = ROUNDS_BY_ID.get(round_id)
            if not round_obj or round_obj.get("phase") == "done" or get_round_remaining_sec(round_obj) <= 0:
                await query.answer("Round khatam ho chuka hai.", show_alert=True)
                return
            if u.id in round_obj.get("bets", {}):
                await query.answer("Tumhari bet is round mein pehle se lock hai!", show_alert=True)
                return

            # Cancel any previous 2s bet option timer before setting for this user
            prev_opt = round_obj.get("active_bet_option")
            if isinstance(prev_opt, dict) and prev_opt.get("task") and not prev_opt["task"].done():
                prev_opt["task"].cancel()

            token = int(round_obj.get("bet_opt_token", 0)) + 1
            round_obj["bet_opt_token"] = token
            expires_at = time.time() + BET_OPTION_SECONDS
            task = asyncio.create_task(
                _bet_option_2s_expiry(context.application, chat_id, round_id, u.id, token)
            )
            round_obj["active_bet_option"] = {
                "user_id": u.id,
                "name": u.first_name,
                "choice": choice,
                "expires_at": expires_at,
                "token": token,
                "task": task,
            }
            round_obj["choices"][u.id] = {"name": u.first_name, "choice": choice}

            await query.answer(f"{choice} chuna — 2s mein bet lock karo!")
            if query.message:
                txt_v, kb_v = build_round_view(round_obj, user_coins=int(user_doc.get("coins", 1000)))
                try:
                    await query.message.edit_text(
                        txt_v,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_v,
                    )
                except (BadRequest, TelegramError):
                    pass
            return

        if data.startswith("rbet:"):
            _, round_id, choice, amt_str = data.split(":", 3)
            round_obj = ROUNDS_BY_ID.get(round_id)
            is_mid_flight = (
                round_obj is not None
                and round_obj.get("game") == "aviator"
                and round_obj.get("phase") == "flight"
                and bool((round_obj.get("flight") or {}).get("active"))
            )
            if not round_obj or (round_obj.get("phase") == "done" and not is_mid_flight):
                await query.answer("Round khatam ho chuka hai.", show_alert=True)
                return
            if not is_mid_flight and get_round_remaining_sec(round_obj) <= 0:
                await query.answer("Round ka time khatam ho gaya!", show_alert=True)
                return

            bet_opt = round_obj.get("active_bet_option")
            if round_obj.get("game") != "aviator" and not is_mid_flight:
                if not isinstance(bet_opt, dict) or time.time() > float(bet_opt.get("expires_at", 0.0)):
                    round_obj["active_bet_option"] = None
                    await query.answer("2s bet option band ho gaya! Pehle apni choice chuno.", show_alert=True)
                    if query.message:
                        txt_v, kb_v = build_round_view(round_obj, user_coins=1000)
                        try:
                            await query.message.edit_text(txt_v, parse_mode=ParseMode.HTML, reply_markup=kb_v)
                        except (BadRequest, TelegramError):
                            pass
                    return
                if int(bet_opt.get("user_id", 0)) != u.id:
                    await query.answer(f"Abhi {bet_opt.get('name')} ka 2s bet option chal raha hai!", show_alert=True)
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
            round_obj["bets"][u.id] = {
                "user_id": u.id,
                "name": u.first_name,
                "username": u.username or "",
                "choice": choice,
                "bet": bet_amt,
            }
            # Close this user's 2s bet option immediately after locking bet so others can pick
            clear_bet_option_for_user(round_obj, u.id)

            flight_live = round_obj.get("flight")
            if round_obj.get("game") == "aviator" and flight_live and flight_live.get("active"):
                flight_live["players"][u.id] = {
                    "user_id": u.id,
                    "name": u.first_name,
                    "username": u.username or "",
                    "bet": bet_amt,
                    "cashed_out": False,
                    "mult": 0.0,
                    "payout": 0,
                }
            await query.answer(f"{bet_amt} coins lock!")
            if query.message and not is_mid_flight:
                txt_v, kb_v = build_round_view(round_obj, user_coins=1000)
                round_obj["last_text"] = txt_v
                try:
                    await query.message.edit_text(
                        txt_v,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_v,
                    )
                except (BadRequest, TelegramError):
                    pass
            return

        if data.startswith("av_cash:"):
            round_id = data.split(":", 1)[1]
            round_obj = ROUNDS_BY_ID.get(round_id)
            flight = round_obj.get("flight") if round_obj else None
            if not round_obj or not flight or not flight.get("active"):
                try:
                    await query.answer("Blast ho chuka hai / Flight end ho gayi!", show_alert=True)
                except BadRequest:
                    pass
                return

            pdata = flight["players"].get(u.id)
            if not pdata:
                try:
                    await query.answer("Is flight mein tumhari bet nahi hai.", show_alert=True)
                except BadRequest:
                    pass
                return
            if pdata["cashed_out"]:
                try:
                    await query.answer("Tum pehle hi Cash Out kar chuke ho!", show_alert=True)
                except BadRequest:
                    pass
                return
            mult = float(flight["mult"])
            payout = int(round(pdata["bet"] * mult))
            pdata["cashed_out"] = True
            pdata["mult"] = mult
            pdata["payout"] = payout
            flight["cashouts"].append(
                {
                    "user_id": u.id,
                    "name": u.first_name,
                    "username": u.username or "",
                    "mult": mult,
                    "payout": payout,
                }
            )
            db.adjust_coins(u.id, payout, chat_id=chat_id, name=u.first_name, won_delta=1, payout_amount=payout)
            try:
                await query.answer(f"Cash Out @ {mult:.1f}x (+{payout})!")
            except BadRequest:
                pass
            if query.message:
                try:
                    live_cash_kb = InlineKeyboardMarkup(
                        [
                            [stylish_btn(f"💸 Cash Out ({mult:.1f}x)", f"av_cash:{round_id}", "success")],
                            [
                                stylish_btn("100", f"rbet:{round_id}:flight:100", "primary"),
                                stylish_btn("250", f"rbet:{round_id}:flight:250", "primary"),
                                stylish_btn("500", f"rbet:{round_id}:flight:500", "primary"),
                            ],
                        ]
                    )
                    await query.message.edit_text(
                        f"🚀 <b>Aviator Live:</b> <code>{mult:.1f}x</code>\n"
                        f"<code>{format_aviator_plane_bar(mult)}</code>\n"
                        f"⚡ Crash se pehle Cash Out dabao — baaki log bhi bet laga sakte hain!\n\n"
                        f"{format_aviator_cashout_feed(flight)}",
                        parse_mode=ParseMode.HTML,
                        reply_markup=live_cash_kb,
                    )
                except (BadRequest, TelegramError):
                    pass
            return

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
            if idx < 0 or idx >= 25:
                return
            if idx in game["revealed"]:
                await query.answer("Ye tile pehle hi khul chuka hai.")
                return
            if idx in game["mines"]:
                ACTIVE_MINES.pop((chat_id, u.id), None)
                await query.answer("💥 Mine phat gaya!", show_alert=True)
                if query.message:
                    try:
                        await query.message.edit_text(
                            f"💥 <b>Boom! Mine Hit</b> — {mention_user(u.id, u.first_name, u.username)}\n"
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
            safe_total = 25 - game["mines_count"]
            if len(game["revealed"]) >= safe_total:
                ACTIVE_MINES.pop((chat_id, u.id), None)
                bal = db.adjust_coins(u.id, payout, chat_id=chat_id, name=u.first_name, won_delta=1, payout_amount=payout)
                if query.message:
                    try:
                        await query.message.edit_text(
                            f"🏆 <b>Board Clear!</b> {mention_user(u.id, u.first_name, u.username)} ne <code>{payout}</code> coins ({mult}x) jeete!\n"
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

        if data == "mexit":
            game = ACTIVE_MINES.get((chat_id, u.id))
            if not game:
                await query.answer("Ye Mines game ab active nahi hai.", show_alert=True)
                return
            if game["user_id"] != u.id:
                await query.answer("Ye board sirf iske owner ka hai!", show_alert=True)
                return
            ACTIVE_MINES.pop((chat_id, u.id), None)
            if not game["revealed"]:
                refund_amt = int(game["bet"])
                bal = db.adjust_coins(u.id, refund_amt, chat_id=chat_id, name=u.first_name)
                await query.answer("Game se bahar aa gaye, bet wapas mil gayi.")
                status_line = f"🚪 <b>Bahar Aa Gaye</b> — {mention_user(u.id, u.first_name, u.username)}\nBet <code>{refund_amt}</code> refund ho gayi · Bal: <code>{bal}</code>"
            else:
                mult = mines_multiplier(len(game["revealed"]), game["mines_count"])
                payout = int(game["bet"] * mult)
                bal = db.adjust_coins(u.id, payout, chat_id=chat_id, name=u.first_name, won_delta=1, payout_amount=payout)
                await query.answer(f"Exit & Cash Out: {payout} coins!")
                status_line = f"🚪 <b>Exit &amp; Cash Out!</b> {mention_user(u.id, u.first_name, u.username)} ne <code>{mult}x</code> par <code>{payout}</code> coins nikale · Bal: <code>{bal}</code>"
            if query.message:
                try:
                    await query.message.edit_text(
                        status_line,
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_mines_keyboard(game, reveal_all=True, finished=True),
                    )
                    schedule_delete_messages(context.application.bot, chat_id, [query.message.message_id], AUTO_DELETE_RESULT_SEC)
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
                        f"💰 <b>Cashed Out!</b> {mention_user(u.id, u.first_name, u.username)} ne <code>{mult}x</code> par <code>{payout}</code> coins nikale!\n"
                        f"🪙 <b>Balance:</b> <code>{bal}</code>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_mines_keyboard(game, reveal_all=True, finished=True),
                    )
                    schedule_delete_messages(context.application.bot, chat_id, [query.message.message_id], AUTO_DELETE_RESULT_SEC)
                except (BadRequest, TelegramError):
                    pass
            return

        if data.startswith("ttt_start:"):
            await query.answer()
            if is_private:
                await reply_cb("❎ <b>Tic Tac Toe</b> group chat ke liye best hai—kisi group mein yeh button dabao.")
                return
            existing_ttt = ACTIVE_TTT5.get(chat_id)
            if existing_ttt and (time.time() - float(existing_ttt.get("created_at", time.time()))) > TTT_MAX_AGE_SEC:
                ACTIVE_TTT5.pop(chat_id, None)
                existing_ttt = None
            if existing_ttt:
                await reply_cb("⚠️ Is group mein Tic Tac Toe pehle se chal raha hai.")
                return
            size = int(data.split(":", 1)[1])
            cfg = TTT_CONFIG.get(size, TTT_CONFIG[5])
            max_p = int(cfg["max_players"])
            line_len = int(cfg["line_len"])
            entry_bet = int(cfg["pot_bet"])
            bal = db.adjust_coins(u.id, -entry_bet, chat_id=chat_id, name=u.first_name, played_delta=1)
            if bal is None:
                await reply_cb(f"❌ Tic Tac Toe start karne ke liye kam se kam <b>{entry_bet}</b> coins chahiye.")
                return
            game = {
                "chat_id": chat_id,
                "size": size,
                "max_players": max_p,
                "line_len": line_len,
                "bet": entry_bet,
                "status": "joining",
                "board": [None] * (size * size),
                "players": [{"user_id": u.id, "name": u.first_name, "username": u.username or "", "mark": TTT_MARKS[0]}],
                "turn_idx": 0,
                "turn_num": 0,
                "message_id": 0,
                "created_at": time.time(),
            }
            ACTIVE_TTT5[chat_id] = game
            msg = await reply_cb(
                f"❎ <b>{size}×{size} Tic Tac Toe</b> · Entry: <code>{entry_bet}</code> coins\n"
                f"{format_ttt_roster(game)}\n"
                f"⏳ Join karne ke liye <b>{TTT_JOIN_SECONDS}s</b> bache hain!",
                reply_markup=build_ttt_keyboard(game, finished=False),
            )
            mid = msg.message_id if msg and getattr(msg, "message_id", None) else 0
            if mid:
                game["message_id"] = mid
            asyncio.create_task(ttt_join_countdown(context.application, chat_id, mid))
            return

        if data == "ttt_join":
            game = ACTIVE_TTT5.get(chat_id)
            if not game or game.get("status") != "joining":
                await query.answer("Join time khatam ho chuka hai.", show_alert=True)
                return
            if any(p["user_id"] == u.id for p in game["players"]):
                await query.answer("Tum pehle se join kar chuke ho!", show_alert=True)
                return
            max_p = int(game.get("max_players", TTT_MAX_PLAYERS))
            if len(game["players"]) >= max_p:
                await query.answer(f"Max players ({max_p}) full ho chuke hain.", show_alert=True)
                return
            bal = db.adjust_coins(u.id, -int(game["bet"]), chat_id=chat_id, name=u.first_name, played_delta=1)
            if bal is None:
                await query.answer("Coins kam hain!", show_alert=True)
                return
            mark = TTT_MARKS[len(game["players"]) % len(TTT_MARKS)]
            game["players"].append({"user_id": u.id, "name": u.first_name, "username": u.username or "", "mark": mark})
            await query.answer(f"Joined as {mark}!")
            size = int(game.get("size", 5))
            if query.message:
                try:
                    await query.message.edit_text(
                        f"❎ <b>{size}×{size} Tic Tac Toe</b> · Entry: <code>{game['bet']}</code> coins\n"
                        f"{format_ttt_roster(game)}\n"
                        f"⏳ Join karne ke liye <b>{TTT_JOIN_SECONDS}s</b> window chal rahi hai!",
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_ttt_keyboard(game, finished=False),
                    )
                except (BadRequest, TelegramError):
                    pass
            return

        if data.startswith("ttt_move:"):
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
            if idx < 0 or idx >= len(game["board"]) or game["board"][idx] is not None:
                await query.answer("Ye box pehle se bhara hai!", show_alert=True)
                return
            game["board"][idx] = cur_p["mark"]
            game["turn_idx"] = (game["turn_idx"] + 1) % len(players)
            game["turn_num"] = int(game.get("turn_num", 1)) + 1
            await query.answer("Move locked!")
            if all(x is not None for x in game["board"]):
                await finish_ttt_game(context.application, chat_id, "Board Full")
                return
            size = int(game.get("size", 5))
            next_p = players[game["turn_idx"] % len(players)]
            if query.message:
                try:
                    await query.message.edit_text(
                        f"❎ <b>{size}×{size} Tic Tac Toe</b>\n"
                        f"{format_ttt_roster(game)}\n"
                        f"🎯 Turn: {next_p['mark']} {mention_user(next_p['user_id'], next_p['name'], next_p.get('username'))} ({TTT_TURN_SECONDS}s)",
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_ttt_keyboard(game, finished=False),
                    )
                except (BadRequest, TelegramError):
                    pass
            asyncio.create_task(ttt_turn_watcher(context.application, chat_id, game["turn_num"]))
            return

    except (ValueError, IndexError):
        await query.answer("Invalid action data.", show_alert=True)


# ==============================================================================
# PLAIN TEXT HANDLER (Custom numeric bet / multiplier input)
# ==============================================================================
async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or not update.message or not update.message.text:
        return
    txt = update.message.text.strip()
    u = update.effective_user
    chat_id = update.effective_chat.id
    pending = PENDING_SOLO_BET.get((chat_id, u.id))
    if not pending:
        return

    mode = pending.get("mode", "")
    if mode == "aviator_guess":
        try:
            target_val = float(txt)
        except ValueError:
            return
        if target_val < AVIATOR_MIN_MULTIPLIER or target_val > AVIATOR_MAX_MULTIPLIER:
            await update.message.reply_text(
                f"⚠️ Multiplier <b>{AVIATOR_MIN_MULTIPLIER:.1f}x</b> se <b>{AVIATOR_MAX_MULTIPLIER:.1f}x</b> ke beech rakho.",
                parse_mode=ParseMode.HTML,
            )
            return
        PENDING_SOLO_BET.pop((chat_id, u.id), None)
        await resolve_solo_aviator(
            chat_id,
            u.id,
            u.first_name,
            u.username,
            int(pending.get("bet", 100)),
            update.message.reply_text,
            target_mult=target_val,
        )
        return

    if not txt.isdigit():
        return
    PENDING_SOLO_BET.pop((chat_id, u.id), None)
    amt = int(txt)
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
    elif mode == "aviator_solo_bet":
        await prompt_aviator_guess(
            chat_id,
            u.id,
            u.first_name,
            u.username,
            amt,
            update.message.reply_text,
        )
    elif mode.startswith("solo_game:"):
        gkey = mode.split(":", 1)[1]
        await resolve_solo_game(
            chat_id,
            u.id,
            u.first_name,
            u.username,
            gkey,
            pending.get("choice") or "heads",
            amt,
            update.message.reply_text,
        )


# ==============================================================================
# ERROR HANDLER & BOT COMMAND MENU SCOPING
# ==============================================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception while processing update:", exc_info=context.error)


MEMBER_COMMANDS = [
    BotCommand("start", "Main menu kholein"),
    BotCommand("help", "Simple game & coin guide"),
    BotCommand("bal", "Apna balance aur stats dekhein"),
    BotCommand("profile", "Apna player profile dekhein"),
    BotCommand("daily", "Har 24h mein 2000 coins lein"),
    BotCommand("refill", "Har 30m mein 250 coins lein"),
    BotCommand("ref", "Apna invite link lein"),
    BotCommand("redeem", "Redeem code claim karein"),
    BotCommand("give", "Reply karke coins bhejein"),
    BotCommand("gtop", "Group leaderboard dekhein"),
    BotCommand("top", "Global leaderboard dekhein"),
    BotCommand("mines", "Mines board shuru karein"),
]


async def setup_bot_commands(app: Application) -> None:
    try:
        await app.bot.set_my_commands(MEMBER_COMMANDS, scope=BotCommandScopeDefault())
        await app.bot.set_my_commands(MEMBER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
        await app.bot.set_my_commands(MEMBER_COMMANDS, scope=BotCommandScopeAllGroupChats())
        logger.info("Member bot commands configured successfully.")
    except TelegramError as exc:
        logger.warning("Failed to set bot commands: %s", exc)


async def post_init(app: Application) -> None:
    await setup_bot_commands(app)
    asyncio.create_task(stuck_game_watchdog(app))


# ==============================================================================
# APPLICATION ENTRYPOINT
# ==============================================================================
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    threading.Thread(target=run_health_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("bal", cmd_bal))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("refill", cmd_refill))
    app.add_handler(CommandHandler("ref", cmd_ref))
    app.add_handler(CommandHandler("redeem", cmd_redeem))
    app.add_handler(CommandHandler("give", cmd_give))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("gtop", cmd_gtop))
    app.add_handler(CommandHandler("mines", cmd_mines))
    app.add_handler(CommandHandler("redeemcode", cmd_redeemcode))
    app.add_handler(CommandHandler("setredeemcoins", cmd_setredeemcoins))
    app.add_handler(CommandHandler("mycoins", cmd_mycoins))
    app.add_handler(CommandHandler("setmycoins", cmd_setmycoins))
    app.add_handler(CommandHandler("setmywins", cmd_setmywins))
    app.add_handler(CommandHandler("setmyloss", cmd_setmyloss))
    app.add_handler(CommandHandler("addcoins", cmd_addcoins))
    app.add_handler(CommandHandler("setcoins", cmd_setcoins))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))
    app.add_error_handler(error_handler)
    logger.info("Starting Telegram Game Bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
