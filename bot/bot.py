"""
GlowStyle — Telegram-бот магазина.

Что делает:
  • /start — приветствие и кнопка «Открыть магазин» (мини-приложение)
  • принимает заказ из мини-приложения, пересчитывает сумму по products.json
    (цены из телефона покупателя не принимаются на веру)
  • отправляет покупателю подтверждение, а вам (ADMIN_CHAT_ID) — карточку заказа
    с кнопками «Подтвердить» / «Отменить»

Настройки берутся из файла .env (см. .env.example).
"""
import asyncio
import json
import logging
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)
PRODUCTS_PATH = Path(os.getenv("PRODUCTS_PATH", Path(__file__).parent.parent / "webapp" / "products.json"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("glowstyle")

dp = Dispatcher()


# ---------- каталог ----------
def load_catalog() -> dict:
    """Читает products.json при каждом заказе, чтобы изменения цен применялись без перезапуска."""
    with open(PRODUCTS_PATH, encoding="utf-8") as f:
        return json.load(f)


def money(n: int, currency: str = "сум") -> str:
    return f"{n:,}".replace(",", " ") + f" {currency}"


def h(text: str) -> str:
    """Экранирует текст для HTML-разметки Telegram."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def shop_keyboard() -> ReplyKeyboardMarkup:
    # Кнопка клавиатуры обязательна: только из неё мини-приложение может отправить заказ боту.
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
        is_persistent=True,
    )


# ---------- команды ----------
@dp.message(CommandStart())
async def start(message: Message) -> None:
    store = load_catalog()["store"]
    await message.answer(
        f"Здравствуйте, {h(message.from_user.first_name)}!\n\n"
        f"<b>{h(store['name'])}</b> — {h(store['tagline'])}.\n\n"
        "Нажмите «🛍 Открыть магазин» внизу, выберите товары и оформите заказ — "
        "менеджер перезвонит для подтверждения.",
        reply_markup=shop_keyboard(),
    )


@dp.message(Command("id"))
async def my_id(message: Message) -> None:
    """Подсказка для настройки: показывает ID чата, который нужно указать в ADMIN_CHAT_ID."""
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@dp.message(Command("contacts"))
async def contacts(message: Message) -> None:
    s = load_catalog()["store"]
    await message.answer(f"📞 {h(s['phone'])}\n🚚 {h(s['delivery_note'])}")


# ---------- приём заказа ----------
PHONE_RE = re.compile(r"\D")


@dp.message(F.web_app_data)
async def on_order(message: Message, bot: Bot) -> None:
    try:
        order = json.loads(message.web_app_data.data)
        items = order["items"]
        c = order["c"]
    except Exception:
        log.exception("bad web_app_data")
        await message.answer("Не получилось прочитать заказ. Откройте магазин и попробуйте ещё раз.")
        return

    catalog = load_catalog()
    store = catalog["store"]
    cur = store.get("currency", "сум")
    products = {p["id"]: p for p in catalog["products"]}

    lines, subtotal, missing = [], 0, []
    for pid, qty in items:
        p = products.get(str(pid))
        try:
            qty = max(1, min(int(qty), 99))
        except (TypeError, ValueError):
            continue
        if not p:
            missing.append(str(pid))
            continue
        cost = p["price"] * qty
        subtotal += cost
        lines.append(f"• {h(p['name'])} × {qty} — {money(cost, cur)}")

    if not lines:
        await message.answer("Товары из заказа больше не продаются. Откройте магазин и соберите корзину заново.")
        return

    free_from = store.get("free_delivery_from") or 0
    delivery = 0 if (free_from and subtotal >= free_from) else int(store.get("delivery_price") or 0)
    total = subtotal + delivery
    order_id = h(str(order.get("id", ""))[:12])
    phone_digits = PHONE_RE.sub("", str(c.get("phone", "")))

    body = (
        "\n".join(lines)
        + f"\n\nДоставка: {money(delivery, cur) if delivery else 'бесплатно'}"
        + f"\n<b>Итого: {money(total, cur)}</b>"
    )
    customer = (
        f"👤 {h(c.get('name', ''))}\n"
        f"📞 <a href=\"tel:+{phone_digits}\">+{phone_digits}</a>\n"
        f"📍 {h(c.get('area', ''))}, {h(c.get('address', ''))}\n"
        f"💳 {h(c.get('pay', ''))}"
        + (f"\n💬 {h(c.get('note', ''))}" if c.get("note") else "")
    )

    # покупателю
    await message.answer(
        f"✅ <b>Заказ {order_id} принят</b>\n\n{body}\n\n{customer}\n\n"
        "Менеджер позвонит, чтобы подтвердить заказ и время доставки.",
        reply_markup=shop_keyboard(),
    )

    # вам
    if ADMIN_CHAT_ID:
        user = message.from_user
        tg_link = f"@{h(user.username)}" if user.username else f"<a href=\"tg://user?id={user.id}\">профиль</a>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"ok:{message.chat.id}:{order_id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"no:{message.chat.id}:{order_id}"),
        ]])
        note = f"\n\n⚠️ Не найдены товары: {', '.join(map(h, missing))}" if missing else ""
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 <b>Новый заказ {order_id}</b>\nTelegram: {tg_link}\n\n{body}\n\n{customer}{note}",
            reply_markup=kb,
        )
    else:
        log.warning("ADMIN_CHAT_ID не задан — заказ %s не отправлен администратору", order_id)


@dp.callback_query(F.data.regexp(r"^(ok|no):(-?\d+):(.+)$"))
async def on_decision(call: CallbackQuery, bot: Bot) -> None:
    if call.message.chat.id != ADMIN_CHAT_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    action, chat_id, order_id = call.data.split(":", 2)
    if action == "ok":
        text = f"🎉 Ваш заказ {order_id} подтверждён. Скоро свяжемся по доставке!"
        mark = "\n\n<b>✅ Подтверждён</b>"
    else:
        text = f"Заказ {order_id} отменён. Если это ошибка — напишите нам, всё исправим."
        mark = "\n\n<b>❌ Отменён</b>"
    try:
        await bot.send_message(int(chat_id), text)
    except Exception:
        log.exception("не удалось написать покупателю")
    await call.message.edit_text(call.message.html_text + mark, reply_markup=None)
    await call.answer("Готово")


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer("Чтобы выбрать товары, нажмите «🛍 Открыть магазин» внизу экрана.", reply_markup=shop_keyboard())


async def main() -> None:
    if not BOT_TOKEN or not WEBAPP_URL:
        raise SystemExit("Не заданы BOT_TOKEN и WEBAPP_URL: впишите их в Variables на Railway или в файл .env (см. README).")
    if not WEBAPP_URL.startswith("https://"):
        raise SystemExit("WEBAPP_URL должен начинаться с https:// — Telegram не открывает мини-приложения по http.")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # Кнопка «Магазин» слева от поля ввода — для просмотра каталога
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=WEBAPP_URL)))
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
