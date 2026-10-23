"""Оформление заказа: пересчёт суммы по каталогу, запись в базу, уведомления в Telegram."""
import logging
import os
import re

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import catalog
import db

log = logging.getLogger("ploika")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)

STATUS_RU = {
    "new": "Новый",
    "confirmed": "Подтверждён",
    "delivering": "Доставляется",
    "done": "Выполнен",
    "cancelled": "Отменён",
}
# что пишем покупателю при смене статуса
STATUS_MSG = {
    "confirmed": "🎉 Ваш заказ №{id} подтверждён. Скоро свяжемся по доставке!",
    "delivering": "🚚 Заказ №{id} передан курьеру и уже едет к вам.",
    "done": "Спасибо за покупку! Заказ №{id} выполнен. Будем рады видеть вас снова.",
    "cancelled": "Заказ №{id} отменён. Если это ошибка — напишите нам, всё исправим.",
}


class OrderError(Exception):
    pass


def money(n: int, cur: str = "сум") -> str:
    return f"{n:,}".replace(",", " ") + f" {cur}"


def h(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean_phone(raw: str) -> str:
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 9:
        d = "998" + d
    return d


async def create(payload: dict, *, source: str, tg_user_id=None, tg_username=None) -> dict:
    """payload = {"items": [[id, qty], ...], "c": {name, phone, area, address, pay, note}}"""
    try:
        items_in = payload["items"]
        c = payload["c"]
    except (KeyError, TypeError):
        raise OrderError("Не получилось прочитать заказ")

    name = str(c.get("name", "")).strip()[:80]
    phone = clean_phone(c.get("phone"))
    address = str(c.get("address", "")).strip()[:200]
    if len(name) < 2 or len(phone) < 12 or len(address) < 3:
        raise OrderError("Проверьте имя, телефон и адрес")

    data, _ = await catalog.load()
    store = data["store"]
    products = {p["id"]: p for p in data["products"]}
    items, subtotal = [], 0
    for row in items_in[:50]:
        try:
            pid, qty = str(row[0]), max(1, min(int(row[1]), 99))
        except (TypeError, ValueError, IndexError):
            continue
        p = products.get(pid)
        if not p:
            continue
        items.append({"id": pid, "name": p["name"], "category": p["category"], "price": p["price"], "qty": qty})
        subtotal += p["price"] * qty
    if not items:
        raise OrderError("Товары из заказа больше не продаются. Соберите корзину заново.")

    free_from = store.get("free_delivery_from") or 0
    delivery = 0 if (free_from and subtotal >= free_from) else int(store.get("delivery_price") or 0)

    return db.add_order(
        source=source, tg_user_id=tg_user_id, tg_username=tg_username,
        name=name, phone=phone, area=str(c.get("area", ""))[:60], address=address,
        pay=str(c.get("pay", ""))[:40], note=str(c.get("note", "")).strip()[:300],
        items=items, subtotal=subtotal, delivery=delivery,
    )


def text(o: dict, cur: str = "сум") -> tuple[str, str]:
    body = "\n".join(f"• {h(i['name'])} × {i['qty']} — {money(i['price'] * i['qty'], cur)}" for i in o["items"])
    body += f"\n\nДоставка: {money(o['delivery'], cur) if o['delivery'] else 'бесплатно'}"
    body += f"\n<b>Итого: {money(o['total'], cur)}</b>"
    customer = (
        f"👤 {h(o['name'])}\n"
        f"📞 <a href=\"tel:+{o['phone']}\">+{o['phone']}</a>\n"
        f"📍 {h(o['area'])}, {h(o['address'])}\n"
        f"💳 {h(o['pay'])}"
        + (f"\n💬 {h(o['note'])}" if o.get("note") else "")
    )
    return body, customer


def admin_keyboard(o: dict) -> InlineKeyboardMarkup | None:
    if o["status"] in ("done", "cancelled"):
        return None
    nxt = {"new": ("✅ Подтвердить", "confirmed"), "confirmed": ("🚚 Передан курьеру", "delivering"),
           "delivering": ("✔️ Выполнен", "done")}[o["status"]]
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=nxt[0], callback_data=f"st:{o['id']}:{nxt[1]}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"st:{o['id']}:cancelled"),
    ]])


async def notify_new(bot: Bot, o: dict) -> None:
    body, customer = text(o)
    if o.get("tg_user_id"):
        try:
            await bot.send_message(
                o["tg_user_id"],
                f"✅ <b>Заказ №{o['id']} принят</b>\n\n{body}\n\n{customer}\n\n"
                "Менеджер позвонит, чтобы подтвердить заказ и время доставки.",
            )
        except Exception:
            log.exception("не удалось написать покупателю")
    if ADMIN_CHAT_ID:
        who = (f"@{h(o['tg_username'])}" if o.get("tg_username")
               else f"<a href=\"tg://user?id={o['tg_user_id']}\">профиль</a>" if o.get("tg_user_id")
               else "заказ с сайта")
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 <b>Новый заказ №{o['id']}</b>\nTelegram: {who}\n\n{body}\n\n{customer}",
            reply_markup=admin_keyboard(o),
        )


async def notify_status(bot: Bot, o: dict) -> None:
    msg = STATUS_MSG.get(o["status"])
    if msg and o.get("tg_user_id"):
        try:
            await bot.send_message(o["tg_user_id"], msg.format(id=o["id"]))
        except Exception:
            log.exception("не удалось написать покупателю")
