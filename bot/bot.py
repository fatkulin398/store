"""
ploika.uz — Telegram-бот магазина + API админ-панели.

Что делает:
  • /start — приветствие и кнопка «Открыть магазин» (мини-приложение)
  • принимает заказы (из мини-приложения и с сайта), пересчитывает сумму по каталогу,
    сохраняет в базу и присылает вам карточку заказа с кнопками смены статуса
  • отдаёт данные админ-панели (продажи, клиенты, аналитика, каталог)

Настройки — переменные окружения (на Railway: Variables) или файл .env (см. .env.example).
"""
import asyncio
import json
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

import api  # noqa: E402  (после load_dotenv, чтобы модули увидели переменные)
import catalog  # noqa: E402
import db  # noqa: E402
import orders  # noqa: E402
from orders import h  # noqa: E402

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_CHAT_ID = orders.ADMIN_CHAT_ID
PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ploika")

dp = Dispatcher()


def shop_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛍 Открыть магазин", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
        is_persistent=True,
    )


@dp.message(CommandStart())
async def start(message: Message) -> None:
    store = (await catalog.load())[0]["store"]
    await message.answer(
        f"Здравствуйте, {h(message.from_user.first_name)}!\n\n"
        f"<b>{h(store['name'])}</b> — {h(store['tagline'])}.\n\n"
        "Нажмите «🛍 Открыть магазин» внизу, выберите товары и оформите заказ — "
        "менеджер перезвонит для подтверждения.",
        reply_markup=shop_keyboard(),
    )


@dp.message(Command("id"))
async def my_id(message: Message) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@dp.message(Command("contacts"))
async def contacts(message: Message) -> None:
    s = (await catalog.load())[0]["store"]
    await message.answer(f"📞 {h(s['phone'])}\n🚚 {h(s['delivery_note'])}")


@dp.message(F.web_app_data)
async def on_order(message: Message, bot: Bot) -> None:
    try:
        payload = json.loads(message.web_app_data.data)
        o = await orders.create(payload, source="telegram",
                                tg_user_id=message.from_user.id, tg_username=message.from_user.username)
    except orders.OrderError as e:
        await message.answer(str(e), reply_markup=shop_keyboard())
        return
    except Exception:
        log.exception("bad web_app_data")
        await message.answer("Не получилось прочитать заказ. Откройте магазин и попробуйте ещё раз.")
        return
    await orders.notify_new(bot, o)


@dp.callback_query(F.data.regexp(r"^st:(\d+):(\w+)$"))
async def on_status(call: CallbackQuery, bot: Bot) -> None:
    if call.message.chat.id != ADMIN_CHAT_ID:
        await call.answer("Нет доступа", show_alert=True)
        return
    _, oid, status = call.data.split(":")
    o = db.set_status(int(oid), status)
    if not o:
        await call.answer("Заказ не найден", show_alert=True)
        return
    await orders.notify_status(bot, o)
    base = call.message.html_text.split("\n\n<b>Статус:")[0]
    await call.message.edit_text(f"{base}\n\n<b>Статус: {orders.STATUS_RU[status]}</b>",
                                 reply_markup=orders.admin_keyboard(o))
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
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Магазин", web_app=WebAppInfo(url=WEBAPP_URL)))

    runner = web.AppRunner(api.make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("API запущено на порту %s, база: %s", PORT, db.DB_PATH)
    if not catalog.editable():
        log.warning("GITHUB_TOKEN не задан — редактирование каталога из админ-панели выключено")

    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
