"""
HTTP API для админ-панели и для заказов с сайта.

Публично:   POST /api/orders                      — заказ с сайта / из мини-приложения
            GET  /api/health
Админ:      POST /api/login                       — пароль ADMIN_PASSWORD → токен на 30 дней
            GET  /api/summary                     — вкладка «Главное»
            GET  /api/orders?status=&q=&days=     — «Продажи»
            PATCH /api/orders/{id}  {status}
            GET  /api/customers                   — «Клиенты»
            GET  /api/analytics?days=30           — «Аналитика»
            GET  /api/catalog | PUT /api/catalog  — «Каталог»
            POST /api/images                      — загрузка фото товара
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from aiogram import Bot
from aiohttp import web

import catalog
import db
import orders

log = logging.getLogger("ploika")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
TZ = timezone(timedelta(hours=5))  # Ташкент
TOKEN_TTL = 30 * 24 * 3600

_secret = hashlib.sha256(f"{ADMIN_PASSWORD}:{BOT_TOKEN}".encode()).digest()


# ---------- auth ----------
def make_token() -> str:
    exp = str(int(time.time()) + TOKEN_TTL)
    sig = hmac.new(_secret, exp.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{exp}.{sig}".encode()).decode()


def check_token(token: str) -> bool:
    try:
        exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split(".", 1)
    except Exception:
        return False
    good = hmac.new(_secret, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and int(exp) > time.time()


def verify_init_data(init_data: str) -> dict | None:
    """Проверка подписи Telegram WebApp initData. Возвращает пользователя или None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
        got = pairs.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got):
            return None
        if time.time() - int(pairs.get("auth_date", 0)) > 24 * 3600:
            return None
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None


# ---------- middleware ----------
def _cors_origin(request) -> str:
    origin = request.headers.get("Origin", "")
    if "*" in ALLOWED_ORIGINS:
        return "*"
    return origin if origin in ALLOWED_ORIGINS else ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else ""


@web.middleware
async def mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        public = request.path in ("/api/orders", "/api/login", "/api/health", "/") and request.method in ("POST", "GET")
        if not public:
            auth = request.headers.get("Authorization", "")
            if not (auth.startswith("Bearer ") and check_token(auth[7:])):
                resp = web.json_response({"error": "Войдите заново"}, status=401)
                return _cors(request, resp)
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = web.json_response({"error": e.text or e.reason}, status=e.status)
        except (catalog.CatalogError, orders.OrderError, ValueError) as e:
            status = 409 if str(e) == "conflict" else 400
            msg = "Каталог изменился в другой вкладке — обновите страницу" if status == 409 else str(e)
            resp = web.json_response({"error": msg}, status=status)
        except Exception:
            log.exception("api error")
            resp = web.json_response({"error": "Ошибка сервера"}, status=500)
    return _cors(request, resp)


def _cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = _cors_origin(request)
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, OPTIONS"
    resp.headers["Vary"] = "Origin"
    return resp


# ---------- helpers ----------
def day_start(ts: float | None = None) -> int:
    d = datetime.fromtimestamp(ts or time.time(), TZ)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def period(days: int) -> tuple[int, int]:
    days = max(1, min(int(days or 30), 366))
    start = day_start() - (days - 1) * 86400
    return start, days


def kpis(rows: list[dict]) -> dict:
    ok = [o for o in rows if o["status"] != "cancelled"]
    revenue = sum(o["total"] for o in ok)
    return {
        "orders": len(rows),
        "paid_orders": len(ok),
        "revenue": revenue,
        "avg_check": round(revenue / len(ok)) if ok else 0,
        "items": sum(i["qty"] for o in ok for i in o["items"]),
        "cancelled": len(rows) - len(ok),
    }


def by_day(rows: list[dict], start: int, days: int) -> list[dict]:
    buckets = {start + i * 86400: {"revenue": 0, "orders": 0} for i in range(days)}
    for o in rows:
        if o["status"] == "cancelled":
            continue
        b = buckets.get(day_start(o["created_at"]))
        if b is not None:
            b["revenue"] += o["total"]
            b["orders"] += 1
    return [{"date": datetime.fromtimestamp(k, TZ).strftime("%Y-%m-%d"), **v} for k, v in sorted(buckets.items())]


def pct(cur: float, prev: float) -> float | None:
    return None if not prev else round((cur - prev) / prev * 100, 1)


# ---------- handlers ----------
routes = web.RouteTableDef()


@routes.get("/")
@routes.get("/api/health")
async def health(_):
    return web.json_response({"ok": True, "catalog_editable": catalog.editable()})


@routes.post("/api/login")
async def login(request):
    if not ADMIN_PASSWORD:
        raise web.HTTPServiceUnavailable(text="Пароль администратора не задан (ADMIN_PASSWORD)")
    body = await request.json()
    if not hmac.compare_digest(str(body.get("password", "")), ADMIN_PASSWORD):
        await _slow()
        raise web.HTTPForbidden(text="Неверный пароль")
    return web.json_response({"token": make_token()})


async def _slow():
    import asyncio
    await asyncio.sleep(1.0)


_rate: dict[str, list[float]] = defaultdict(list)


@routes.post("/api/orders")
async def create_order(request):
    ip = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
    now = time.time()
    _rate[ip] = [t for t in _rate[ip] if now - t < 600]
    if len(_rate[ip]) >= 5:
        raise web.HTTPTooManyRequests(text="Слишком много заказов подряд. Позвоните нам, пожалуйста.")
    _rate[ip].append(now)

    payload = await request.json()
    user = verify_init_data(payload.get("initData", ""))
    o = await orders.create(
        payload, source="telegram" if user else "site",
        tg_user_id=(user or {}).get("id"), tg_username=(user or {}).get("username"),
    )
    bot: Bot = request.app["bot"]
    try:
        await orders.notify_new(bot, o)
    except Exception:
        log.exception("notify failed")
    return web.json_response({"id": o["id"], "total": o["total"]})


@routes.get("/api/summary")
async def summary(_):
    today = day_start()
    rows_today = db.list_orders(since=today)
    rows_yday = db.list_orders(since=today - 86400, until=today)
    start, days = period(7)
    rows_week = db.list_orders(since=start)
    k_today, k_yday = kpis(rows_today), kpis(rows_yday)
    pending = db.list_orders(status="new", limit=50)
    data, _ = await catalog.load()
    low = [p for p in data["products"] if p.get("stock", 0) <= 3]
    return web.json_response({
        "today": k_today,
        "yesterday": k_yday,
        "delta": {k: pct(k_today[k], k_yday[k]) for k in ("revenue", "orders", "avg_check")},
        "week": by_day(rows_week, start, days),
        "week_kpis": kpis(rows_week),
        "pending": len(pending),
        "latest": db.list_orders(limit=8),
        "low_stock": [{"id": p["id"], "name": p["name"], "stock": p.get("stock", 0)} for p in low],
        "products": len(data["products"]),
        "customers": len(db.customers()),
    })


@routes.get("/api/orders")
async def list_orders(request):
    q = request.query
    since = period(int(q["days"]))[0] if q.get("days") else 0
    rows = db.list_orders(since=since, status=q.get("status", ""), q=q.get("q", "").strip(),
                          phone=orders.clean_phone(q["phone"]) if q.get("phone") else "")
    counts = defaultdict(int)
    for o in db.list_orders(since=since, limit=100000):
        counts[o["status"]] += 1
    return web.json_response({"orders": rows, "counts": counts})


@routes.patch("/api/orders/{id}")
async def patch_order(request):
    body = await request.json()
    o = db.set_status(int(request.match_info["id"]), body.get("status", ""))
    if not o:
        raise web.HTTPNotFound(text="Заказ не найден")
    if body.get("notify", True):
        await orders.notify_status(request.app["bot"], o)
    return web.json_response(o)


@routes.get("/api/customers")
async def customers(_):
    return web.json_response({"customers": db.customers()})


@routes.get("/api/analytics")
async def analytics(request):
    start, days = period(int(request.query.get("days", 30)))
    rows = db.list_orders(since=start, limit=100000)
    prev = db.list_orders(since=start - days * 86400, until=start, limit=100000)
    data, _ = await catalog.load()
    cats = {c["id"]: c["name"] for c in data["categories"]}
    ok = [o for o in rows if o["status"] != "cancelled"]

    top = defaultdict(lambda: {"qty": 0, "revenue": 0, "name": ""})
    by_cat, by_area, by_pay, by_source = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)
    by_hour = [0] * 24
    for o in ok:
        by_area[o["area"] or "—"] += o["total"]
        by_pay[o["pay"] or "—"] += 1
        by_source[o["source"]] += 1
        by_hour[datetime.fromtimestamp(o["created_at"], TZ).hour] += 1
        for i in o["items"]:
            t = top[i["id"]]
            t["name"], t["qty"], t["revenue"] = i["name"], t["qty"] + i["qty"], t["revenue"] + i["price"] * i["qty"]
            by_cat[cats.get(i["category"], i["category"])] += i["price"] * i["qty"]

    phones = [o["phone"] for o in ok]
    first_seen = {c["phone"]: c["first_order"] for c in db.customers()}
    new_customers = len({p for p in phones if first_seen.get(p, 0) >= start})
    per_phone = defaultdict(int)
    for p in phones:
        per_phone[p] += 1
    cur, old = kpis(rows), kpis(prev)
    srt = lambda d: [{"label": k, "value": v} for k, v in sorted(d.items(), key=lambda x: -x[1])]
    return web.json_response({
        "days": days,
        "kpis": cur,
        "prev": old,
        "delta": {k: pct(cur[k], old[k]) for k in ("revenue", "orders", "avg_check", "items")},
        "series": by_day(rows, start, days),
        "top_products": sorted(({"id": k, **v} for k, v in top.items()), key=lambda x: -x["revenue"])[:10],
        "by_category": srt(by_cat),
        "by_area": srt(by_area),
        "by_pay": srt(by_pay),
        "by_source": srt(by_source),
        "by_hour": by_hour,
        "customers": {"unique": len(set(phones)), "new": new_customers, "returning": len(set(phones)) - new_customers,
                      "repeat": sum(1 for n in per_phone.values() if n > 1)},
        "cancel_rate": round(cur["cancelled"] / cur["orders"] * 100, 1) if cur["orders"] else 0,
    })


@routes.get("/api/catalog")
async def get_catalog(_):
    data, sha = await catalog.load(force=True)
    return web.json_response({"catalog": data, "sha": sha, "editable": catalog.editable()})


@routes.put("/api/catalog")
async def put_catalog(request):
    body = await request.json()
    sha = await catalog.save(body["catalog"], body.get("sha") or "")
    return web.json_response({"sha": sha})


@routes.post("/api/images")
async def upload_image(request):
    reader = await request.multipart()
    part = await reader.next()
    if not part or part.name != "file":
        raise web.HTTPBadRequest(text="Нет файла")
    content = await part.read(decode=False)
    path = await catalog.upload_image(part.filename or "photo.jpg", content)
    return web.json_response({"path": path})


def make_app(bot: Bot) -> web.Application:
    app = web.Application(middlewares=[mw], client_max_size=3 * 1024 * 1024)
    app["bot"] = bot
    app.add_routes(routes)
    # preflight для любых путей
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())
    return app
