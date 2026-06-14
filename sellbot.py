"""
PostBot - 发布 + 售卖机器人
管理员私聊发作品 → 设三档价格 → 发布到群/频道（带购买按钮）
买家点按钮 → 选支付方式 → 上传凭证+地址 → 管理员审核
"""

import logging
import asyncio
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from flask import Flask, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, Update
from telegram.error import Conflict, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("postbot")

TOKEN = os.environ.get("POSTBOT_TOKEN", "8819236422:AAHTyn8UDDm_BmYe2tDQUGV5zljZoZybHR8")
MASTER_ID = int(os.environ.get("POSTBOT_MASTER", "8807178282"))
PORT = int(os.environ.get("PORT", 8080))
DB_PATH = os.environ.get("POSTBOT_DB", "postbot_data.db")
ADMIN_WEB_KEY = os.environ.get("ADMIN_WEB_KEY", "postbot2024")
WEB_BASE_URL = os.environ.get("WEB_BASE_URL", "https://sellbot2-l0x0.onrender.com")

# 支付信息（也可用 /setpay 命令修改）
DEFAULT_PAY = {
    "usdt": os.environ.get("USDT_ADDRESS", "请设置USDT地址"),
    "kpay": os.environ.get("KPAY_PHONE", "请设置KPay手机号"),
    "wavepay": os.environ.get("WAVEPAY_PHONE", "请设置WavePay手机号"),
    "admin_username": os.environ.get("ADMIN_USERNAME", "请设置管理员用户名"),
    "usdt_rate": os.environ.get("USDT_RATE", "4200"),
}

flask_app = Flask(__name__)

# 多图收集：等所有图片到齐再进入设价（防抖 2.5 秒）
_content_tasks: dict[int, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS targets (
                chat_id INTEGER PRIMARY KEY, title TEXT, chat_type TEXT, added_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS prefs (
                user_id INTEGER PRIMARY KEY, default_target INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_type TEXT, file_id TEXT, caption TEXT,
                price1 REAL, price2 REAL, price3 REAL,
                created_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER, buyer_id INTEGER, buyer_name TEXT,
                qty INTEGER, price REAL, payment_method TEXT,
                proof_file_id TEXT, address TEXT, status TEXT,
                created_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS buyer_sessions (
                user_id INTEGER PRIMARY KEY, order_id INTEGER, step TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS admin_drafts (
                user_id INTEGER PRIMARY KEY,
                step TEXT, media_type TEXT, file_id TEXT, caption TEXT,
                price1 REAL, price2 REAL, price3 REAL, no_price INTEGER DEFAULT 0
            )"""
        )
        for k, v in DEFAULT_PAY.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        _migrate_db(conn)


def _migrate_db(conn):
    listing_cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "price_mode" not in listing_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN price_mode TEXT DEFAULT 'qty'")
    if "prices_json" not in listing_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN prices_json TEXT")
    draft_cols = {r[1] for r in conn.execute("PRAGMA table_info(admin_drafts)").fetchall()}
    if "price_mode" not in draft_cols:
        conn.execute("ALTER TABLE admin_drafts ADD COLUMN price_mode TEXT DEFAULT 'qty'")
    if "prices_json" not in draft_cols:
        conn.execute("ALTER TABLE admin_drafts ADD COLUMN prices_json TEXT")
    if "selected_targets" not in draft_cols:
        conn.execute("ALTER TABLE admin_drafts ADD COLUMN selected_targets TEXT")
    order_cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    for col, typedef in [
        ("item_label", "TEXT"), ("source_chat_id", "INTEGER"), ("source_message_id", "INTEGER"),
        ("post_link", "TEXT"), ("buyer_phone", "TEXT"),
    ]:
        if col not in order_cols:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {typedef}")
    session_cols = {r[1] for r in conn.execute("PRAGMA table_info(buyer_sessions)").fetchall()}
    if "extra_json" not in session_cols:
        conn.execute("ALTER TABLE buyer_sessions ADD COLUMN extra_json TEXT")


def db_get_draft(user_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT step, media_type, file_id, caption, price1, price2, price3, no_price, "
            "price_mode, prices_json, selected_targets FROM admin_drafts WHERE user_id=?", (user_id,),
        ).fetchone()
    if not row:
        return None
    draft: dict = {"step": row[0]}
    if row[1]:
        draft["media_type"] = row[1]
    if row[2]:
        draft["file_id"] = row[2]
    if row[3]:
        draft["caption"] = row[3]
    if row[4] is not None:
        draft["price1"] = row[4]
    if row[5] is not None:
        draft["price2"] = row[5]
    if row[6] is not None:
        draft["price3"] = row[6]
    draft["no_price"] = bool(row[7])
    if len(row) > 8 and row[8]:
        draft["price_mode"] = row[8]
    if len(row) > 9 and row[9]:
        draft["prices_json"] = row[9]
        try:
            draft["work_prices"] = json.loads(row[9])
        except json.JSONDecodeError:
            draft["work_prices"] = []
    if len(row) > 10 and row[10]:
        try:
            draft["selected_targets"] = json.loads(row[10])
        except json.JSONDecodeError:
            draft["selected_targets"] = []
    if row[1] == "collecting" and row[2]:
        try:
            draft["items"] = json.loads(row[2])
        except json.JSONDecodeError:
            draft["items"] = []
    return draft


def db_save_draft(user_id: int, draft: dict):
    media_type = draft.get("media_type")
    file_id = draft.get("file_id")
    if "items" in draft:
        media_type = "collecting"
        file_id = json.dumps(draft["items"])
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO admin_drafts
               (user_id, step, media_type, file_id, caption, price1, price2, price3, no_price,
                price_mode, prices_json, selected_targets)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id, draft.get("step"), media_type, file_id,
                draft.get("caption"), draft.get("price1"), draft.get("price2"), draft.get("price3"),
                1 if draft.get("no_price") else 0,
                draft.get("price_mode", "qty"),
                draft.get("prices_json") or (
                    json.dumps(draft["work_prices"]) if draft.get("work_prices") else None
                ),
                json.dumps(draft["selected_targets"]) if draft.get("selected_targets") else None,
            ),
        )


def db_clear_draft(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM admin_drafts WHERE user_id=?", (user_id,))


def db_get_setting(key: str) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else DEFAULT_PAY.get(key, "")


def db_set_setting(key: str, value: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )


def db_add_target(chat_id: int, title: str, chat_type: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO targets VALUES (?, ?, ?, ?)",
            (chat_id, title, chat_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def db_remove_target(chat_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM targets WHERE chat_id=?", (chat_id,))


def db_list_targets() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT chat_id, title, chat_type FROM targets ORDER BY added_at"
        ).fetchall()
    return [{"id": r[0], "title": r[1], "type": r[2]} for r in rows]


def db_set_default(user_id: int, chat_id: int | None):
    with sqlite3.connect(DB_PATH) as conn:
        if chat_id is None:
            conn.execute("DELETE FROM prefs WHERE user_id=?", (user_id,))
        else:
            conn.execute("INSERT OR REPLACE INTO prefs VALUES (?, ?)", (user_id, chat_id))


def db_get_default(user_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT default_target FROM prefs WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else None


def db_create_listing(media_type, file_id, caption, p1, p2, p3,
                      price_mode="qty", prices_json=None) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO listings (media_type,file_id,caption,price1,price2,price3,created_at,"
            "price_mode,prices_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (media_type, file_id, caption or "", p1, p2, p3,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), price_mode, prices_json),
        )
        return cur.lastrowid


def db_get_listing(lid: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
        if not row:
            return None
        cols = [c[1] for c in conn.execute("PRAGMA table_info(listings)").fetchall()]
        listing = dict(zip(cols, row))
        if listing.get("prices_json"):
            try:
                listing["work_prices"] = json.loads(listing["prices_json"])
            except json.JSONDecodeError:
                listing["work_prices"] = []
        return listing


def db_create_order(listing_id, buyer_id, buyer_name, qty, price, **extra) -> int:
    fields = {
        "listing_id": listing_id, "buyer_id": buyer_id, "buyer_name": buyer_name,
        "qty": qty, "price": price, "status": "pending_pay",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    fields.update(extra)
    cols = ", ".join(fields.keys())
    placeholders = ", ".join("?" * len(fields))
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            f"INSERT INTO orders ({cols}) VALUES ({placeholders})",
            list(fields.values()),
        )
        return cur.lastrowid


def db_get_order(oid: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            return None
        cols = [c[1] for c in conn.execute("PRAGMA table_info(orders)").fetchall()]
        return dict(zip(cols, row))


def db_list_orders(date: str | None = None, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM orders WHERE 1=1"
    params: list = []
    if date:
        sql += " AND created_at LIKE ?"
        params.append(f"{date}%")
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id DESC"
    with sqlite3.connect(DB_PATH) as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(orders)").fetchall()]
        rows = conn.execute(sql, params).fetchall()
    return [dict(zip(cols, r)) for r in rows]


def db_daily_stats(date: str) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(price), 0)
               FROM orders WHERE created_at LIKE ? AND status='success'""",
            (f"{date}%",),
        ).fetchone()
    return {"count": row[0], "total": row[1] or 0}


def db_update_order(oid: int, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [oid]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE orders SET {sets} WHERE id=?", vals)


def db_set_buyer_session(user_id: int, order_id: int | None, step: str | None, extra_json: str | None = None):
    with sqlite3.connect(DB_PATH) as conn:
        if step is None:
            conn.execute("DELETE FROM buyer_sessions WHERE user_id=?", (user_id,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO buyer_sessions (user_id, order_id, step, extra_json) VALUES (?,?,?,?)",
                (user_id, order_id or 0, step, extra_json),
            )


def db_get_buyer_session(user_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT order_id, step, extra_json FROM buyer_sessions WHERE user_id=?", (user_id,),
        ).fetchone()
    if not row:
        return None
    sess = {"order_id": row[0], "step": row[1]}
    if row[2]:
        try:
            sess["extra"] = json.loads(row[2])
        except json.JSONDecodeError:
            sess["extra"] = {}
    return sess


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def is_master(user_id: int | None) -> bool:
    if user_id is None:
        return False
    return int(user_id) == int(MASTER_ID)


async def reply(update: Update, text: str, **kwargs):
    msg = update.effective_message
    if msg:
        return await msg.reply_text(text, **kwargs)


def forward_chat(message):
    origin = getattr(message, "forward_origin", None)
    if origin and getattr(origin, "chat", None):
        return origin.chat
    return getattr(message, "forward_from_chat", None)


def parse_prices(text: str) -> tuple[float, float, float] | None:
    nums = parse_price_list(text, 3)
    if nums and len(nums) >= 3:
        return nums[0], nums[1], nums[2]
    return None


def parse_price_list(text: str, count: int | None = None) -> list[float] | None:
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text.replace(",", " "))]
    if count is not None:
        return nums if len(nums) == count else None
    return nums if nums else None


def listing_qty_price(listing: dict, qty: int) -> float | None:
    if listing.get("price_mode") == "works":
        prices = listing.get("work_prices") or []
        if not prices and listing.get("prices_json"):
            try:
                prices = json.loads(listing["prices_json"])
            except json.JSONDecodeError:
                prices = []
        if 1 <= qty <= len(prices):
            return prices[qty - 1]
        return None
    return {1: listing["price1"], 2: listing["price2"], 3: listing["price3"]}.get(qty)


def draft_work_count(draft: dict) -> int:
    if draft.get("media_type") == "album":
        try:
            return len(json.loads(draft.get("file_id") or "[]"))
        except json.JSONDecodeError:
            return 0
    return 1


def extract_media(message):
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.document:
        mime = message.document.mime_type or ""
        if mime.startswith("image/"):
            return "photo", message.document.file_id
        if mime.startswith("video/"):
            return "video", message.document.file_id
    return None, None


def price_buttons(listing_id: int, p1: float, p2: float, p3: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🛒 买1个 — {p1:g}", callback_data=f"buy:{listing_id}:1")],
        [InlineKeyboardButton(f"🛒 买2个 — {p2:g}", callback_data=f"buy:{listing_id}:2")],
        [InlineKeyboardButton(f"🛒 买3个 — {p3:g}", callback_data=f"buy:{listing_id}:3")],
    ])


def work_buttons(listing_id: int, prices: list[float]) -> InlineKeyboardMarkup:
    rows = []
    for i, p in enumerate(prices, 1):
        rows.append([
            InlineKeyboardButton(
                f"🛒 {i}号作品 — {p:g}",
                callback_data=f"buy:{listing_id}:{i}",
            )
        ])
    return InlineKeyboardMarkup(rows)


def listing_keyboard(listing: dict) -> InlineKeyboardMarkup | None:
    if listing.get("price_mode") == "works":
        prices = listing.get("work_prices") or []
        if not prices and listing.get("prices_json"):
            try:
                prices = json.loads(listing["prices_json"])
            except json.JSONDecodeError:
                prices = []
        if prices:
            return work_buttons(listing["id"], prices)
        return None
    return price_buttons(listing["id"], listing["price1"], listing["price2"], listing["price3"])


def pay_buttons(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 USDT", callback_data=f"pay:{order_id}:usdt")],
        [InlineKeyboardButton("📱 KPay", callback_data=f"pay:{order_id}:kpay")],
        [InlineKeyboardButton("📱 WavePay", callback_data=f"pay:{order_id}:wavepay")],
    ])


def review_buttons(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 购买成功", callback_data=f"review:{order_id}:ok"),
            InlineKeyboardButton("❌ 购买失败", callback_data=f"review:{order_id}:fail"),
        ]
    ])


def target_label(t: dict) -> str:
    kind = "频道" if t["type"] == "channel" else "群组"
    return f"{t['title']} ({kind})"


def build_target_keyboard(targets: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(target_label(t), callback_data=f"{prefix}:{t['id']}")] for t in targets]
    rows.append([InlineKeyboardButton("❌ 取消", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(rows)


def draft_selected_set(draft: dict) -> set[int]:
    return set(draft.get("selected_targets") or [])


def build_multi_target_keyboard(targets: list[dict], selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for t in targets:
        mark = "✅" if t["id"] in selected else "⬜"
        rows.append([InlineKeyboardButton(
            f"{mark} {target_label(t)}", callback_data=f"ptog:{t['id']}",
        )])
    n = len(selected)
    rows.append([
        InlineKeyboardButton("☑️ 全选", callback_data="ptog:all"),
        InlineKeyboardButton(f"🚀 发布到所选({n})", callback_data="pubgo:0"),
    ])
    rows.append([InlineKeyboardButton("❌ 取消", callback_data="pick:cancel")])
    return InlineKeyboardMarkup(rows)


def build_message_link(chat_id: int, message_id: int) -> str:
    cid = str(chat_id)
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{message_id}"
    return ""


def order_item_label(listing: dict | None, qty: int) -> str:
    if listing and listing.get("price_mode") == "works":
        return f"{qty}号作品"
    return f"买{qty}个"


def listing_work_media(listing: dict, qty: int) -> tuple[str, str] | None:
    """返回 (media_type, file_id) 用于预览指定作品/数量。"""
    mt = listing.get("media_type")
    fid = listing.get("file_id") or ""
    if mt == "album":
        try:
            items = json.loads(fid)
            idx = max(0, min(qty - 1, len(items) - 1))
            item = items[idx]
            return item["type"], item["file_id"]
        except (json.JSONDecodeError, IndexError, KeyError):
            return None
    if mt in ("photo", "video", "animation") and fid:
        return mt, fid
    if mt == "text":
        return "text", fid
    return None


async def prompt_pick_targets(update_or_query, draft: dict, price_summary: str = ""):
    targets = db_list_targets()
    text = "请选择要发布到的群/频道（可多选，点选切换）："
    if price_summary:
        text = f"价格：{price_summary}\n\n{text}"
    kb = build_multi_target_keyboard(targets, draft_selected_set(draft))
    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, reply_markup=kb)
    else:
        await update_or_query.message.reply_text(text, reply_markup=kb)


def get_usdt_rate() -> float:
    try:
        return float(db_get_setting("usdt_rate") or "4200")
    except ValueError:
        return 4200.0


def mmk_to_usdt(mmk: float) -> float:
    return round(mmk / get_usdt_rate(), 2)


def format_mmk(price: float) -> str:
    if price == int(price):
        return f"{int(price):,}"
    return f"{price:g}"


def format_pay_block(method: str, mmk_price: float) -> str:
    mmk_str = format_mmk(mmk_price)
    if method == "usdt":
        rate = get_usdt_rate()
        usdt = mmk_to_usdt(mmk_price)
        rate_str = f"{int(rate)}" if rate == int(rate) else f"{rate:g}"
        return (
            f"原价：<b>{mmk_str}</b> 缅币\n"
            f"换算：{mmk_str} ÷ {rate_str} = <b>{usdt:.2f} USDT</b>\n\n"
            f"💰 请您支付 <b>{usdt:.2f} USDT</b>\n"
            f"⚠️ 请注意尾数，务必支付准确金额！"
        )
    return f"应付金额：<b>{mmk_str}</b> 缅币"


def pay_info(method: str) -> str:
    if method == "usdt":
        return f"💎 <b>USDT (TRC20)</b>\n<code>{db_get_setting('usdt')}</code>"
    if method == "kpay":
        return f"📱 <b>KPay</b>\n<code>{db_get_setting('kpay')}</code>"
    if method == "wavepay":
        return f"📱 <b>WavePay</b>\n<code>{db_get_setting('wavepay')}</code>"
    return ""


async def verify_and_bind(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          chat_id: int, title: str, chat_type: str):
    try:
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id, me.id)
        if member.status not in ("administrator", "creator"):
            await reply(update, "❌ 请先把机器人设为管理员。")
            return
        if chat_type == "channel":
            if not (getattr(member, "can_post_messages", False) or getattr(member, "can_edit_messages", False)):
                await reply(update, "❌ 频道里机器人需要「发消息」权限。")
                return
        elif chat_type in ("group", "supergroup"):
            if getattr(member, "can_send_messages", True) is False:
                await reply(update, "❌ 群里机器人需要「发消息」权限。")
                return
        db_add_target(chat_id, title, chat_type)
        await reply(update, f"✅ 已绑定：{title}")
    except Exception as e:
        log.exception("绑定失败")
        await reply(update, f"❌ 绑定失败：{e}")


async def send_listing_to_chat(context, chat_id: int, listing: dict) -> tuple[bool, int | None]:
    kb = listing_keyboard(listing)
    return await _send_media(
        context, chat_id, listing["media_type"], listing["file_id"],
        listing["caption"] or "🛍 精选作品", kb,
    )


async def send_draft_to_chat(context, chat_id: int, draft: dict) -> tuple[bool, int | None]:
    cap = draft.get("caption") or ""
    return await _send_media(context, chat_id, draft["media_type"], draft.get("file_id"), cap, None)


async def _send_media(context, chat_id, media_type, file_id, caption, reply_markup) -> tuple[bool, int | None]:
    msg_id = None
    try:
        if media_type == "text":
            msg = await context.bot.send_message(chat_id, caption or " ", reply_markup=reply_markup)
            msg_id = msg.message_id
        elif media_type == "album":
            items = json.loads(file_id)
            media = []
            for i, item in enumerate(items):
                cap = caption if i == 0 else None
                if item["type"] == "video":
                    media.append(InputMediaVideo(item["file_id"], caption=cap))
                else:
                    media.append(InputMediaPhoto(item["file_id"], caption=cap))
            await context.bot.send_media_group(chat_id, media)
            if reply_markup:
                msg = await context.bot.send_message(chat_id, "👇 点击购买", reply_markup=reply_markup)
                msg_id = msg.message_id
        elif media_type == "photo":
            msg = await context.bot.send_photo(chat_id, file_id, caption=caption or None, reply_markup=reply_markup)
            msg_id = msg.message_id
        elif media_type == "video":
            msg = await context.bot.send_video(chat_id, file_id, caption=caption or None, reply_markup=reply_markup)
            msg_id = msg.message_id
        else:
            msg = await context.bot.send_animation(chat_id, file_id, caption=caption or None, reply_markup=reply_markup)
            msg_id = msg.message_id
        return True, msg_id
    except Exception as e:
        log.error("发布失败 chat=%s err=%s", chat_id, e)
        return False, None


async def send_work_preview(context, chat_id: int, listing: dict, qty: int, caption: str,
                            reply_markup: InlineKeyboardMarkup | None = None):
    media = listing_work_media(listing, qty)
    if not media:
        await context.bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=reply_markup)
        return
    mt, fid = media
    if mt == "text":
        text = (listing.get("caption") or "") + "\n\n" + caption
        await context.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
    elif mt == "photo":
        await context.bot.send_photo(chat_id, fid, caption=caption, parse_mode="HTML", reply_markup=reply_markup)
    elif mt == "video":
        await context.bot.send_video(chat_id, fid, caption=caption, parse_mode="HTML", reply_markup=reply_markup)
    else:
        await context.bot.send_animation(chat_id, fid, caption=caption, parse_mode="HTML", reply_markup=reply_markup)


def _get_draft_items(draft: dict) -> list:
    items = draft.get("items")
    if items is not None:
        return items
    if draft.get("media_type") == "collecting" and draft.get("file_id"):
        try:
            return json.loads(draft["file_id"])
        except json.JSONDecodeError:
            return []
    return []


async def _finalize_content(context, user_id: int, chat_id: int):
    _content_tasks.pop(user_id, None)
    draft = db_get_draft(user_id)
    if not draft or draft.get("step") != "await_content":
        return
    items = _get_draft_items(draft)
    if not items:
        return
    caption = draft.get("caption") or ""
    if len(items) == 1:
        draft.update({"media_type": items[0]["type"], "file_id": items[0]["file_id"], "price_mode": "qty"})
    else:
        draft.update({"media_type": "album", "file_id": json.dumps(items), "price_mode": "works"})
    draft["caption"] = caption
    draft["step"] = "await_prices"
    if "items" in draft:
        del draft["items"]
    db_save_draft(user_id, draft)
    n = len(items)
    if draft.get("price_mode") == "works":
        price_hint = (
            f"📸 收到 {n} 个作品！\n\n"
            f"请为每个作品设价格（共 {n} 个，缅币），例如：\n"
            f"<code>{', '.join(['400000'] * min(n, 3))}{'...' if n > 3 else ''}</code>\n\n"
            f"发布后按钮显示：1号作品、2号作品…\n"
            f"或点「无价发布」："
        )
    else:
        price_hint = (
            "📸 收到内容！\n\n"
            "请发三个价格（买1个/买2个/买3个），例如：\n"
            "<code>400000, 750000, 1000000</code>\n\n"
            "或点「无价发布」："
        )
    await context.bot.send_message(
        chat_id, price_hint, parse_mode="HTML", reply_markup=price_prompt_keyboard(),
    )


async def _schedule_content_finalize(context, user_id: int, chat_id: int):
    old = _content_tasks.get(user_id)
    if old and not old.done():
        old.cancel()

    async def job():
        try:
            await asyncio.sleep(2.5)
            await _finalize_content(context, user_id, chat_id)
        except asyncio.CancelledError:
            pass

    _content_tasks[user_id] = asyncio.create_task(job())


def price_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 无价发布（纯展示）", callback_data="post:noprice")],
        [InlineKeyboardButton("❌ 取消", callback_data="post:cancel")],
    ])


async def ask_prices(update: Update, draft: dict | None = None):
    if draft and draft.get("price_mode") == "works":
        n = draft_work_count(draft)
        text = (
            f"📸 收到 {n} 个作品！\n\n"
            f"请为每个作品设价格（共 {n} 个），例如：\n"
            f"<code>{', '.join(['400000'] * min(n, 3))}</code>\n\n"
            f"或点「无价发布」："
        )
    else:
        text = (
            "📸 收到内容！\n\n"
            "请发三个价格（买1个/买2个/买3个），例如：\n"
            "<code>400000, 750000, 1000000</code>\n\n"
            "或点「无价发布」："
        )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=price_prompt_keyboard())


async def finish_publish(context, uid: int, draft: dict) -> tuple[bool, str]:
    target_ids = draft.get("selected_targets") or []
    if not target_ids:
        return False, "❌ 请至少选择一个群/频道"

    targets_map = {t["id"]: t for t in db_list_targets()}
    ok_count = 0
    fail_names = []

    if draft.get("no_price"):
        for tid in target_ids:
            ok, _ = await send_draft_to_chat(context, tid, draft)
            if ok:
                ok_count += 1
            else:
                fail_names.append(targets_map.get(tid, {}).get("title", str(tid)))
        db_clear_draft(uid)
        if ok_count == len(target_ids):
            return True, f"✅ 已无价发布到 {ok_count} 个目标"
        return ok_count > 0, f"⚠️ 发布到 {ok_count}/{len(target_ids)} 个\n失败：{', '.join(fail_names)}"

    price_mode = draft.get("price_mode", "qty")
    if price_mode == "works":
        work_prices = draft.get("work_prices") or []
        if not work_prices and draft.get("prices_json"):
            try:
                work_prices = json.loads(draft["prices_json"])
            except json.JSONDecodeError:
                work_prices = []
        p1 = work_prices[0] if work_prices else 0
        p2 = work_prices[1] if len(work_prices) > 1 else 0
        p3 = work_prices[2] if len(work_prices) > 2 else 0
        prices_json = json.dumps(work_prices)
        lid = db_create_listing(
            draft["media_type"], draft.get("file_id", ""), draft.get("caption", ""),
            p1, p2, p3, price_mode="works", prices_json=prices_json,
        )
        price_str = " / ".join(f"{i}号:{p:g}" for i, p in enumerate(work_prices, 1))
    else:
        lid = db_create_listing(
            draft["media_type"], draft.get("file_id", ""), draft.get("caption", ""),
            draft["price1"], draft["price2"], draft["price3"],
        )
        price_str = f"{draft['price1']}/{draft['price2']}/{draft['price3']}"

    listing = db_get_listing(lid)
    for tid in target_ids:
        ok, _ = await send_listing_to_chat(context, tid, listing)
        if ok:
            ok_count += 1
        else:
            fail_names.append(targets_map.get(tid, {}).get("title", str(tid)))

    db_clear_draft(uid)
    msg = (
        f"{'✅ 已发布' if ok_count == len(target_ids) else '⚠️ 部分发布'}\n"
        f"成功：{ok_count}/{len(target_ids)} 个目标\n"
        f"商品ID：{lid}\n"
        f"价格：{price_str}"
    )
    if fail_names:
        msg += f"\n失败：{', '.join(fail_names)}"
    return ok_count > 0, msg


async def show_payment_menu(context, user_id: int, order_id: int):
    order = db_get_order(order_id)
    if not order:
        await context.bot.send_message(user_id, "订单不存在或已过期。")
        return
    listing = db_get_listing(order["listing_id"])
    item_label = order.get("item_label") or order_item_label(listing, order["qty"])
    text = (
        f"🛍 <b>确认订单 #{order_id}</b>\n"
        f"商品：{item_label}\n"
        f"金额：<b>{format_mmk(order['price'])}</b> 缅币\n"
        f"（选 USDT 按 ÷{int(get_usdt_rate())} 换算）\n\n"
        f"请选择支付方式："
    )
    await context.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=pay_buttons(order_id))


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
HELP_ADMIN = (
    "📮 <b>发布售卖机器人</b>\n\n"
    "<b>发布内容：</b>\n"
    "发 /post → 发送图片/视频/文字 → 设价格或点「无价发布」\n"
    "（可一次选多张图作为相册，最多10张）\n\n"
    "<b>售卖流程：</b>\n"
    "单张图：设买1/2/3个的价格\n"
    "多张图：每个作品单独设价（1号作品、2号作品…）\n\n"
    "<b>命令：</b>\n"
    "/post — 开始发布\n"
    "/done — 多图发完确认\n"
    "/setpay — 收款信息\n"
    "/targets — 已绑定群/频道\n"
    "/default — 默认发布目标\n"
    "/bind /unbind /ping /id\n"
    "/orders — 网页订单后台链接"
)

HELP_BUYER = (
    "👋 欢迎！\n\n"
    "请从频道/群里的商品按钮进入购买。\n"
    "如有问题请联系 @{admin}"
)

HELP_GROUP = "📮 机器人已就绪\n群ID：<code>{cid}</code>\n管理员发 /bind 绑定"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if not user:
        return

    if chat.type == "private":
        # 买家从按钮跳转：/start buy_列表ID_数量
        if context.args and context.args[0].startswith("buy_"):
            parts = context.args[0].split("_")
            if len(parts) == 3:
                await start_buy_flow(context, user, int(parts[1]), int(parts[2]))
                return
        if context.args and context.args[0].startswith("pay_"):
            order_id = int(context.args[0].split("_")[1])
            await show_payment_menu(context, user.id, order_id)
            return

        if is_master(user.id):
            await reply(update, HELP_ADMIN, parse_mode="HTML")
        else:
            admin = db_get_setting("admin_username").lstrip("@")
            await reply(update, HELP_BUYER.format(admin=admin))
    else:
        await reply(update, HELP_GROUP.format(cid=chat.id), parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(update, f"✅ 在线\nID：<code>{update.effective_chat.id}</code>", parse_mode="HTML")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ok = is_master(user.id if user else None)
    await reply(
        update,
        f"你的ID：<code>{user.id if user else '?'}</code>\n"
        f"管理员：<code>{MASTER_ID}</code>\n"
        f"{'✅ 是管理员' if ok else '❌ 不是管理员'}",
        parse_mode="HTML",
    )


async def cmd_setpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        return
    if len(context.args) < 2:
        await reply(
            update,
            "当前收款设置：\n\n"
            f"USDT：<code>{db_get_setting('usdt')}</code>\n"
            f"USDT汇率：1 USDT = {format_mmk(get_usdt_rate())} 缅币（缅币÷{int(get_usdt_rate())}）\n"
            f"KPay：<code>{db_get_setting('kpay')}</code>\n"
            f"WavePay：<code>{db_get_setting('wavepay')}</code>\n"
            f"联系账号：@{db_get_setting('admin_username').lstrip('@')}\n\n"
            "修改格式：\n"
            "/setpay usdt 你的地址\n"
            "/setpay rate 4200\n"
            "/setpay kpay 手机号\n"
            "/setpay wavepay 手机号\n"
            "/setpay admin 你的用户名",
            parse_mode="HTML",
        )
        return
    key = context.args[0].lower()
    val = " ".join(context.args[1:])
    mapping = {"usdt": "usdt", "kpay": "kpay", "wavepay": "wavepay", "admin": "admin_username", "rate": "usdt_rate"}
    if key not in mapping:
        await reply(update, "可选：usdt / rate / kpay / wavepay / admin")
        return
    if key == "rate":
        try:
            float(val)
        except ValueError:
            await reply(update, "汇率请填数字，例如：/setpay rate 4200")
            return
    db_set_setting(mapping[key], val.lstrip("@") if key == "admin" else val)
    await reply(update, f"✅ 已更新 {key}")


async def cmd_bind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        if not is_master(user.id if user else None):
            await reply(update, "只有管理员可以绑定。")
            return
        if context.args:
            try:
                t = await context.bot.get_chat(int(context.args[0]))
            except Exception as e:
                await reply(update, f"找不到：{e}")
                return
            await verify_and_bind(update, context, t.id, t.title or str(t.id), t.type)
            return
        await reply(update, "转发群/频道消息到这里，或 /bind -100xxx")
        return
    if not is_master(user.id if user else None):
        await reply(update, "只有管理员可以绑定。")
        return
    await verify_and_bind(update, context, chat.id, chat.title or str(chat.id), chat.type)


async def cmd_unbind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private" or not is_master(update.effective_user.id):
        return
    db_remove_target(chat.id)
    await reply(update, "✅ 已解除绑定。")


async def cmd_targets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        return
    targets = db_list_targets()
    if not targets:
        await reply(update, "暂无绑定。")
        return
    default = db_get_default(update.effective_user.id)
    lines = ["📋 已绑定："]
    for i, t in enumerate(targets, 1):
        mark = " ⭐" if default == t["id"] else ""
        lines.append(f"{i}. {t['title']}{mark}\n   {t['id']}")
    await reply(update, "\n".join(lines))


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        return
    base = WEB_BASE_URL.rstrip("/") if WEB_BASE_URL else f"http://localhost:{PORT}"
    url = f"{base}/admin?key={ADMIN_WEB_KEY}"
    await reply(
        update,
        f"📊 <b>订单后台</b>\n\n"
        f"在浏览器打开：\n<code>{url}</code>\n\n"
        f"可查看买家、地址、作品、金额，按日期筛选。\n"
        f"（Render 请设置环境变量 WEB_BASE_URL 为你的服务地址）",
        parse_mode="HTML",
    )


async def cmd_default(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        return
    targets = db_list_targets()
    if not targets:
        await reply(update, "请先绑定群/频道。")
        return
    await reply(update, "选择默认发布目标：", reply_markup=build_target_keyboard(targets, "def"))


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        return
    db_save_draft(update.effective_user.id, {"step": "await_content"})
    await reply(
        update,
        "📝 <b>发布模式</b>\n\n"
        "请发送要发布的内容：\n"
        "• 图片 / 视频（可一次选多张，最多10张）\n"
        "• 或纯文字（热情的话、通知等）\n\n"
        "多张图可一次选相册，或逐张发，发完输入 /done\n\n"
        "发送后可选设价格，或点「无价发布」",
        parse_mode="HTML",
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_master(update.effective_user.id):
        return
    uid = update.effective_user.id
    draft = db_get_draft(uid)
    if not draft or draft.get("step") != "await_content" or not _get_draft_items(draft):
        await reply(update, "当前没有待发布的图片。请先 /post 再发图。")
        return
    old = _content_tasks.pop(uid, None)
    if old and not old.done():
        old.cancel()
    await _finalize_content(context, uid, update.effective_chat.id)


# ---------------------------------------------------------------------------
# 管理员发作品 + 设价格
# ---------------------------------------------------------------------------
async def on_admin_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """管理员私聊：/post 流程 + 转发绑定"""
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    if not is_master(user.id if user else None):
        return

    msg = update.message
    if not msg:
        return

    try:
        draft = db_get_draft(user.id)
        if not draft:
            source = forward_chat(msg)
            if source and source.type in ("channel", "group", "supergroup"):
                await verify_and_bind(update, context, source.id, source.title or str(source.id), source.type)
            return

        step = draft.get("step")
        log.info("post draft user=%s step=%s", user.id, step)

        if step == "await_content":
            media_type, file_id = extract_media(msg)
            if media_type:
                caption = msg.caption or ""
                src = forward_chat(msg)
                if not caption and src:
                    caption = src.title or ""
                items = _get_draft_items(draft)
                if not any(x["file_id"] == file_id for x in items):
                    items.append({"type": media_type, "file_id": file_id})
                draft["items"] = items
                if caption:
                    draft["caption"] = caption
                db_save_draft(user.id, draft)
                await _schedule_content_finalize(context, user.id, msg.chat_id)
                await msg.reply_text(
                    f"✅ 已收到 {len(items)} 张\n"
                    f"继续发图，或发 /done 完成"
                )
                return
            elif msg.text and not msg.text.startswith("/"):
                draft.update({
                    "media_type": "text", "file_id": "", "caption": msg.text, "price_mode": "qty",
                })
            else:
                await msg.reply_text("请发送图片、视频或文字。")
                return
            draft["step"] = "await_prices"
            db_save_draft(user.id, draft)
            await ask_prices(update, draft)
            return

        if step == "await_prices" and msg.text and not msg.text.startswith("/"):
            await on_admin_prices(update, context)
            return

    except Exception as e:
        log.exception("管理员私聊处理失败")
        await msg.reply_text(f"❌ 处理失败：{e}\n\n请重新发 /post")
        db_clear_draft(user.id)


async def on_admin_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_master(update.effective_user.id):
        return
    uid = update.effective_user.id
    draft = db_get_draft(uid)
    if not draft or draft.get("step") != "await_prices":
        return

    price_mode = draft.get("price_mode", "qty")
    if price_mode == "works":
        n = draft_work_count(draft)
        prices = parse_price_list(update.message.text, n)
        if not prices:
            await update.message.reply_text(
                f"❌ 格式不对，请发 {n} 个价格（每个作品一个），例如：\n"
                f"<code>{', '.join(['400000'] * min(n, 3))}</code>\n"
                f"或点「无价发布」按钮",
                parse_mode="HTML",
            )
            return
        draft["work_prices"] = prices
        draft["prices_json"] = json.dumps(prices)
        draft["price1"] = prices[0]
        draft["price2"] = prices[1] if len(prices) > 1 else 0
        draft["price3"] = prices[2] if len(prices) > 2 else 0
        price_summary = " / ".join(f"{i}号:{p:g}" for i, p in enumerate(prices, 1))
    else:
        prices = parse_prices(update.message.text)
        if not prices:
            await update.message.reply_text(
                "❌ 格式不对，请发三个数字，例如：400000, 750000, 1000000\n"
                "或点「无价发布」按钮",
            )
            return
        draft["price1"], draft["price2"], draft["price3"] = prices
        price_summary = f"{prices[0]} / {prices[1]} / {prices[2]}"

    draft["no_price"] = False
    draft["step"] = "pick_target"
    draft.setdefault("selected_targets", [])
    db_save_draft(uid, draft)

    targets = db_list_targets()
    if not targets:
        await update.message.reply_text("请先绑定群/频道（/bind）")
        db_clear_draft(uid)
        return

    await prompt_pick_targets(update, draft, price_summary)


# ---------------------------------------------------------------------------
# 买家购买流程
# ---------------------------------------------------------------------------
async def show_buy_preview(context, user, listing: dict, qty: int, price: float,
                           source_chat_id: int | None = None, source_message_id: int | None = None):
    label = order_item_label(listing, qty)
    caption = (
        f"🛍 <b>{label}</b>\n"
        f"价格：<b>{format_mmk(price)}</b> 缅币\n\n"
        f"请确认您要购买的是以上商品，确认后再付款："
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 确认购买", callback_data=f"buyok:{listing['id']}:{qty}")],
        [InlineKeyboardButton("❌ 取消", callback_data="buycancel:0")],
    ])
    extra = {
        "listing_id": listing["id"], "qty": qty, "price": price,
        "source_chat_id": source_chat_id, "source_message_id": source_message_id,
        "item_label": label,
    }
    db_set_buyer_session(user.id, 0, "pending_confirm", json.dumps(extra))
    await send_work_preview(context, user.id, listing, qty, caption, kb)


async def start_buy_flow(context, user, listing_id: int, qty: int,
                         source_chat_id: int | None = None, source_message_id: int | None = None):
    listing = db_get_listing(listing_id)
    if not listing:
        await context.bot.send_message(user.id, "商品不存在或已下架。")
        return
    price = listing_qty_price(listing, qty)
    if price is None:
        await context.bot.send_message(user.id, "无效选项。")
        return
    await show_buy_preview(context, user, listing, qty, price, source_chat_id, source_message_id)


async def on_buy_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, lid, qty = query.data.split(":")
    listing_id, qty = int(lid), int(qty)
    buyer = query.from_user

    listing = db_get_listing(listing_id)
    if not listing:
        await query.answer("商品已下架", show_alert=True)
        return

    price = listing_qty_price(listing, qty)
    if price is None:
        await query.answer("无效选项", show_alert=True)
        return

    src_chat = query.message.chat_id if query.message else None
    src_msg = query.message.message_id if query.message else None

    try:
        await show_buy_preview(context, buyer, listing, qty, price, src_chat, src_msg)
        await query.answer("请查看私聊确认商品 👉")
    except Forbidden:
        me = await context.bot.get_me()
        await query.answer(url=f"https://t.me/{me.username}?start=buy_{listing_id}_{qty}")


async def on_buy_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, lid, qty = query.data.split(":")
    listing_id, qty = int(lid), int(qty)
    buyer = query.from_user

    session = db_get_buyer_session(buyer.id)
    if not session or session.get("step") != "pending_confirm":
        await query.answer("会话已过期，请重新点击购买", show_alert=True)
        return

    extra = session.get("extra") or {}
    if extra.get("listing_id") != listing_id or extra.get("qty") != qty:
        await query.answer("订单信息不匹配，请重新购买", show_alert=True)
        return

    listing = db_get_listing(listing_id)
    price = extra.get("price") or listing_qty_price(listing, qty)
    label = extra.get("item_label") or order_item_label(listing, qty)
    src_chat = extra.get("source_chat_id")
    src_msg = extra.get("source_message_id")
    post_link = build_message_link(src_chat, src_msg) if src_chat and src_msg else ""

    name = buyer.full_name or buyer.username or str(buyer.id)
    oid = db_create_order(
        listing_id, buyer.id, name, qty, price,
        item_label=label, source_chat_id=src_chat, source_message_id=src_msg, post_link=post_link,
    )
    db_set_buyer_session(buyer.id, oid, "await_pay_choice")

    try:
        if query.message and query.message.photo:
            await query.edit_message_caption(caption=f"✅ 已确认：{label} — {format_mmk(price)} 缅币")
        elif query.message:
            await query.edit_message_text(f"✅ 已确认：{label} — {format_mmk(price)} 缅币")
    except Exception:
        pass

    await show_payment_menu(context, buyer.id, oid)
    await query.answer("请选择支付方式")


async def on_buy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db_set_buyer_session(query.from_user.id, None, None)
    try:
        await query.edit_message_text("已取消购买。")
    except Exception:
        pass
    await query.answer()


async def on_pay_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, oid, method = query.data.split(":")
    order_id = int(oid)
    order = db_get_order(order_id)
    if not order or query.from_user.id != order["buyer_id"]:
        await query.answer("订单无效", show_alert=True)
        return

    db_update_order(order_id, payment_method=method)
    db_set_buyer_session(query.from_user.id, order_id, "await_proof")

    info = pay_info(method)
    pay_block = format_pay_block(method, order["price"])
    text = (
        f"{info}\n\n"
        f"{pay_block}\n\n"
        f"📌 <b>请按以下步骤操作：</b>\n"
        f"1️⃣ 完成支付\n"
        f"2️⃣ 发送 <b>支付成功截图</b>\n"
        f"3️⃣ 发送 <b>收货地址</b>（文字）\n"
        f"4️⃣ 发送 <b>联系电话</b>（文字）\n\n"
        f"⚠️ 请确保支付信息正确，假图或错付将无法发货。"
    )
    await query.edit_message_text(text, parse_mode="HTML")
    await query.answer()


async def on_buyer_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or is_master(update.effective_user.id):
        return

    session = db_get_buyer_session(update.effective_user.id)
    if not session:
        return

    order = db_get_order(session["order_id"])
    if not order:
        db_set_buyer_session(update.effective_user.id, None, None)
        return

    try:
        if session["step"] == "pending_confirm":
            await update.message.reply_text("请在商品预览消息上点击「确认购买」或「取消」。")
            return

        if session["step"] == "await_proof":
            proof_id = None
            if update.message.photo:
                proof_id = update.message.photo[-1].file_id
            elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
                proof_id = update.message.document.file_id

            if not proof_id:
                await update.message.reply_text("请先发送支付成功截图（图片）。")
                return

            db_update_order(order["id"], proof_file_id=proof_id)
            db_set_buyer_session(update.effective_user.id, order["id"], "await_address")
            await update.message.reply_text("✅ 已收到截图。\n\n请发送您的收货地址（文字）：")
            return

        if session["step"] == "await_address":
            address = update.message.text or update.message.caption
            if not address:
                await update.message.reply_text("请发送文字格式的收货地址。")
                return

            db_update_order(order["id"], address=address)
            db_set_buyer_session(update.effective_user.id, order["id"], "await_phone")
            await update.message.reply_text("✅ 已收到地址。\n\n请发送您的联系电话（手机号）：")
            return

        if session["step"] == "await_phone":
            phone = (update.message.text or update.message.caption or "").strip()
            if not phone or not re.search(r"\d", phone):
                await update.message.reply_text("请发送有效的联系电话（含数字）。")
                return

            db_update_order(order["id"], buyer_phone=phone, status="pending_review")
            db_set_buyer_session(update.effective_user.id, None, None)

            order = db_get_order(order["id"])
            proof_id = order.get("proof_file_id")
            address = order.get("address") or ""

            await update.message.reply_text("✅ 已提交！请等待管理员审核，稍后通知您结果。")

            method = order.get("payment_method") or "?"
            mmk = order["price"]
            item_label = order.get("item_label") or order_item_label(db_get_listing(order["listing_id"]), order["qty"])
            post_link = order.get("post_link") or ""
            amount_line = f"金额：{format_mmk(mmk)} 缅币"
            if method == "usdt":
                amount_line += f"\nUSDT：{mmk_to_usdt(mmk):.2f} USDT（÷{int(get_usdt_rate())}）"
            link_line = f"作品链接：<a href=\"{post_link}\">{post_link}</a>\n" if post_link else ""
            admin_text = (
                f"🔔 <b>新订单 #{order['id']}</b>\n\n"
                f"作品：<b>{item_label}</b>\n"
                f"{link_line}"
                f"买家：{order['buyer_name']} (<code>{order['buyer_id']}</code>)\n"
                f"电话：{phone}\n"
                f"{amount_line}\n"
                f"支付：{method.upper()}\n"
                f"地址：{address}\n\n"
                f"请核对支付截图后点击："
            )
            await context.bot.send_message(
                MASTER_ID, admin_text, parse_mode="HTML", reply_markup=review_buttons(order["id"]),
            )
            if proof_id:
                cap = f"订单 #{order['id']} | {item_label} | 支付截图"
                if post_link:
                    cap += f"\n{post_link}"
                await context.bot.send_photo(MASTER_ID, proof_id, caption=cap)
            return
    except Exception as e:
        log.exception("买家消息处理失败")
        await update.message.reply_text(f"提交出错，请重试或联系管理员。({e})")


async def on_review_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_master(query.from_user.id):
        await query.answer("无权限", show_alert=True)
        return

    _, oid, result = query.data.split(":")
    order_id = int(oid)
    order = db_get_order(order_id)
    if not order:
        await query.answer("订单不存在", show_alert=True)
        return

    admin_user = db_get_setting("admin_username").lstrip("@")
    buyer_id = order["buyer_id"]

    if result == "ok":
        db_update_order(order_id, status="success")
        buyer_msg = (
            "🎉 <b>恭喜您购买成功！</b>\n\n"
            "您的订单已确认，预计 <b>7-15 天</b> 内发货。\n"
            "如未收到货物，请联系管理员："
            f" @{admin_user}"
        )
        await query.edit_message_text(f"✅ 订单 #{order_id} 已确认成功")
    else:
        db_update_order(order_id, status="failed")
        buyer_msg = (
            "❌ <b>购买失败</b>\n\n"
            "支付凭证未通过审核，请详细核对后重新支付。\n"
            "如有疑问请联系："
            f" @{admin_user}"
        )
        await query.edit_message_text(f"❌ 订单 #{order_id} 已拒绝")

    try:
        await context.bot.send_message(buyer_id, buyer_msg, parse_mode="HTML")
    except Exception as e:
        log.error("通知买家失败: %s", e)

    await query.answer()


# ---------------------------------------------------------------------------
# 管理员回调（发布/设置）
# ---------------------------------------------------------------------------
async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_master(query.from_user.id):
        return

    action, _, value = query.data.partition(":")
    uid = query.from_user.id

    if action == "def":
        if value == "cancel":
            await query.edit_message_text("已取消。")
        else:
            db_set_default(uid, int(value))
            targets = {t["id"]: t for t in db_list_targets()}
            await query.edit_message_text(f"⭐ 默认：{targets.get(int(value), {}).get('title', value)}")
        await query.answer()
        return

    if action == "post":
        draft = db_get_draft(uid)
        if value == "cancel":
            db_clear_draft(uid)
            await query.edit_message_text("已取消发布。")
        elif value == "noprice":
            if not draft or draft.get("step") != "await_prices":
                await query.answer("请先 /post 并发送内容", show_alert=True)
                return
            draft["no_price"] = True
            draft["step"] = "pick_target"
            draft.setdefault("selected_targets", [])
            db_save_draft(uid, draft)
            targets = db_list_targets()
            if not targets:
                db_clear_draft(uid)
                await query.edit_message_text("请先绑定群/频道（/bind）")
            else:
                await query.edit_message_text(
                    "📝 无价发布 — 请选择群/频道（可多选，点选切换）：",
                    reply_markup=build_multi_target_keyboard(targets, draft_selected_set(draft)),
                )
        await query.answer()
        return

    if action == "ptog":
        draft = db_get_draft(uid)
        if not draft:
            await query.answer("无发布任务", show_alert=True)
            return
        targets = db_list_targets()
        selected = draft_selected_set(draft)
        if value == "all":
            selected = {t["id"] for t in targets}
        else:
            tid = int(value)
            if tid in selected:
                selected.discard(tid)
            else:
                selected.add(tid)
        draft["selected_targets"] = list(selected)
        db_save_draft(uid, draft)
        await query.edit_message_reply_markup(
            reply_markup=build_multi_target_keyboard(targets, selected),
        )
        await query.answer(f"已选 {len(selected)} 个")
        return

    if action == "pubgo":
        draft = db_get_draft(uid)
        if not draft or not draft.get("selected_targets"):
            await query.answer("请至少选择一个群/频道", show_alert=True)
            return
        ok, msg = await finish_publish(context, uid, draft)
        await query.edit_message_text(msg)
        await query.answer()
        return

    if action == "pick":
        draft = db_get_draft(uid)
        if not draft or value == "cancel":
            db_clear_draft(uid)
            await query.edit_message_text("已取消发布。")
            await query.answer()
            return
        await query.answer()
        return

    if action == "sale":
        draft = db_get_draft(uid)
        if not draft or value == "cancel":
            db_clear_draft(uid)
            await query.edit_message_text("已取消发布。")
            await query.answer()
            return
        draft["selected_targets"] = [int(value)]
        db_save_draft(uid, draft)
        ok, msg = await finish_publish(context, uid, draft)
        await query.edit_message_text(msg)
        await query.answer()
        return


async def on_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data.startswith("buy:"):
        await on_buy_click(update, context)
    elif data.startswith("buyok:"):
        await on_buy_confirm(update, context)
    elif data.startswith("buycancel:"):
        await on_buy_cancel(update, context)
    elif data.startswith("pay:"):
        await on_pay_click(update, context)
    elif data.startswith("review:"):
        await on_review_click(update, context)
    elif data.startswith(("def:", "sale:", "pick:", "post:", "ptog:", "pubgo:")):
        await on_admin_callback(update, context)


async def on_bot_joined(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.my_chat_member
    if not m or m.new_chat_member.status not in ("administrator", "member"):
        return
    chat = m.chat
    if chat.type in ("group", "supergroup", "channel"):
        try:
            await context.bot.send_message(chat.id, HELP_GROUP.format(cid=chat.id), parse_mode="HTML")
        except Exception:
            pass


async def on_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    log.info("chat=%s user=%s text=%s", getattr(chat, "id", "?"), getattr(user, "id", "?"),
             (msg.text[:50] if msg and msg.text else ""))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        log.error(
            "409 Conflict：同一个 Bot Token 有多个实例在 polling。"
            "请只保留一个运行中的 postbot（停掉本地、其他 Render 服务、bot.py 等）。"
        )
        return
    log.exception("处理出错", exc_info=err)


async def post_init(application: Application) -> None:
    await application.bot.delete_webhook(drop_pending_updates=True)
    me = await application.bot.get_me()
    log.info("Bot 已就绪 @%s (id=%s)，webhook 已清除，使用 polling", me.username, me.id)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
@flask_app.route("/")
def health():
    return f"PostBot OK | master={MASTER_ID} | <a href='/admin?key={ADMIN_WEB_KEY}'>订单后台</a>", 200


@flask_app.route("/admin")
def admin_orders_page():
    key = request.args.get("key", "")
    if key != ADMIN_WEB_KEY:
        return "Unauthorized — 请在 Render 设置 ADMIN_WEB_KEY 环境变量", 401

    date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        date = datetime.now().strftime("%Y-%m-%d")

    orders = db_list_orders(date=date)
    stats = db_daily_stats(date)
    status_map = {
        "pending_pay": "待付款", "pending_review": "待审核",
        "success": "成功", "failed": "失败",
    }

    rows_html = ""
    for o in orders:
        st = status_map.get(o.get("status"), o.get("status"))
        link = o.get("post_link") or ""
        item = o.get("item_label") or f"商品#{o.get('listing_id')}"
        link_cell = f'<a href="{link}" target="_blank">作品链接</a>' if link else item
        rows_html += (
            f"<tr><td>{o['id']}</td><td>{o.get('created_at','')}</td>"
            f"<td>{o.get('buyer_name','')}</td><td>{o.get('buyer_phone') or '-'}</td>"
            f"<td>{link_cell}</td><td>{o.get('address') or '-'}</td>"
            f"<td>{format_mmk(o.get('price', 0))}</td><td>{st}</td></tr>"
        )

    prev = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    nxt = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PostBot 订单</title>
<style>
body{{font-family:sans-serif;margin:16px;background:#f5f5f5}}
.card{{background:#fff;padding:16px;border-radius:8px;margin-bottom:16px;box-shadow:0 1px 3px #0002}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#333;color:#fff}}
.stats{{display:flex;gap:24px;flex-wrap:wrap}}
.stat{{font-size:18px}} .stat b{{color:#007bff}}
nav a{{margin-right:12px;padding:6px 12px;background:#007bff;color:#fff;text-decoration:none;border-radius:4px}}
input[type=date]{{padding:6px;font-size:16px}}
</style></head><body>
<h1>📊 PostBot 订单后台</h1>
<div class="card stats">
  <div class="stat">日期：<b>{date}</b></div>
  <div class="stat">成交笔数：<b>{stats['count']}</b></div>
  <div class="stat">成交总额：<b>{format_mmk(stats['total'])}</b> 缅币</div>
</div>
<div class="card">
  <form method="get">
    <input type="hidden" name="key" value="{key}">
    <label>选择日期：</label>
    <input type="date" name="date" value="{date}" onchange="this.form.submit()">
    <button type="submit">查询</button>
  </form>
  <p style="margin-top:12px">
    <a href="/admin?key={key}&date={prev}">← 前一天</a>
    <a href="/admin?key={key}&date={nxt}">后一天 →</a>
    <a href="/admin?key={key}&date={datetime.now().strftime('%Y-%m-%d')}">今天</a>
  </p>
</div>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>ID</th><th>时间</th><th>买家</th><th>电话</th><th>作品</th><th>地址</th><th>金额</th><th>状态</th></tr>
{rows_html if rows_html else '<tr><td colspan="8">暂无订单</td></tr>'}
</table>
</div>
</body></html>"""


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)


async def on_private_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user = update.effective_user
    if not user:
        return
    if is_master(user.id):
        await on_admin_private(update, context)
    else:
        await on_buyer_message(update, context)


def create_app() -> Application:
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_error_handler(on_error)
    app.add_handler(MessageHandler(filters.ALL, on_log), group=-1)
    app.add_handler(ChatMemberHandler(on_bot_joined, ChatMemberHandler.MY_CHAT_MEMBER))

    for cmd, handler in [
        ("start", cmd_start), ("help", cmd_help), ("ping", cmd_ping), ("id", cmd_id),
        ("post", cmd_post), ("done", cmd_done), ("bind", cmd_bind), ("unbind", cmd_unbind),
        ("targets", cmd_targets), ("default", cmd_default), ("setpay", cmd_setpay), ("orders", cmd_orders),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
        app.add_handler(CommandHandler(cmd, handler, filters=filters.UpdateType.CHANNEL_POSTS))

    app.add_handler(CallbackQueryHandler(on_callback_router))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_private_router))
    return app


def main():
    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    log.info("PostBot 售卖版启动 port=%s master=%s", PORT, MASTER_ID)
    create_app().run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
