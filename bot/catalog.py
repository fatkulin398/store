"""
Каталог товаров. Источник правды — webapp/products.json в репозитории GitHub:
сайт читает его с GitHub Pages, а бот и админ-панель — через GitHub API.
Без GITHUB_TOKEN бот читает локальную копию файла, а редактирование каталога недоступно.
"""
import base64
import json
import os
import re
import time
from pathlib import Path

import aiohttp

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "fatkulin398/store")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
CATALOG_PATH = "webapp/products.json"
LOCAL_PATH = Path(os.getenv("PRODUCTS_PATH", Path(__file__).parent.parent / "webapp" / "products.json"))
API = "https://api.github.com"

_cache: dict = {"data": None, "sha": None, "at": 0.0}
CACHE_SECONDS = 60


class CatalogError(Exception):
    pass


def editable() -> bool:
    return bool(GITHUB_TOKEN)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ploika-bot",
    }


async def _gh(method: str, path: str, **kw) -> dict:
    async with aiohttp.ClientSession() as s:
        async with s.request(method, f"{API}/repos/{GITHUB_REPO}/contents/{path}", headers=_headers(), **kw) as r:
            body = await r.json(content_type=None)
            if r.status == 409:
                raise CatalogError("conflict")
            if r.status >= 400:
                raise CatalogError(f"GitHub {r.status}: {body.get('message', '')}")
            return body


async def load(force: bool = False) -> tuple[dict, str | None]:
    """Возвращает (каталог, sha). sha нужен, чтобы сохранить без затирания чужих правок."""
    if not force and _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"], _cache["sha"]
    if GITHUB_TOKEN:
        try:
            body = await _gh("GET", CATALOG_PATH, params={"ref": GITHUB_BRANCH})
            data = json.loads(base64.b64decode(body["content"]).decode("utf-8"))
            _cache.update(data=data, sha=body["sha"], at=time.time())
            return data, body["sha"]
        except (CatalogError, aiohttp.ClientError, KeyError, ValueError):
            if _cache["data"]:
                return _cache["data"], _cache["sha"]
    with open(LOCAL_PATH, encoding="utf-8") as f:
        data = json.load(f)
    _cache.update(data=data, sha=None, at=time.time())
    return data, None


def validate(data: dict) -> dict:
    """Проверяет структуру каталога из админки и приводит типы."""
    try:
        store = data["store"]
        cats = data["categories"]
        prods = data["products"]
    except (KeyError, TypeError):
        raise CatalogError("Каталог повреждён: нет store / categories / products")
    cat_ids = {c["id"] for c in cats}
    seen = set()
    for p in prods:
        if not p.get("id") or p["id"] in seen:
            raise CatalogError(f"Повторяющийся или пустой код товара: {p.get('id')}")
        seen.add(p["id"])
        if not str(p.get("name", "")).strip():
            raise CatalogError(f"У товара {p['id']} нет названия")
        if p.get("category") not in cat_ids:
            raise CatalogError(f"У товара «{p['name']}» неизвестная категория")
        for k in ("price", "old_price", "stock"):
            p[k] = max(0, int(p.get(k) or 0))
        p["tags"] = [t for t in p.get("tags", []) if t in ("new", "hit")]
        p.setdefault("brand", "")
        p.setdefault("description", "")
        p.setdefault("image", "")
    for k in ("delivery_price", "free_delivery_from"):
        store[k] = max(0, int(store.get(k) or 0))
    return data


async def save(data: dict, sha: str, who: str = "admin") -> str:
    if not GITHUB_TOKEN:
        raise CatalogError("Не задан GITHUB_TOKEN — редактирование каталога выключено")
    data = validate(data)
    raw = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    body = await _gh("PUT", CATALOG_PATH, json={
        "message": f"Каталог обновлён из админ-панели ({who})",
        "content": base64.b64encode(raw.encode("utf-8")).decode(),
        "sha": sha,
        "branch": GITHUB_BRANCH,
    })
    new_sha = body["content"]["sha"]
    _cache.update(data=data, sha=new_sha, at=time.time())
    return new_sha


async def upload_image(filename: str, content: bytes) -> str:
    if not GITHUB_TOKEN:
        raise CatalogError("Не задан GITHUB_TOKEN — загрузка фото выключена")
    if len(content) > 2_000_000:
        raise CatalogError("Фото больше 2 МБ")
    stem = re.sub(r"[^a-z0-9-]+", "-", Path(filename).stem.lower()).strip("-")[:40] or "photo"
    ext = Path(filename).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    name = f"{stem}-{int(time.time())}{ext}"
    await _gh("PUT", f"webapp/images/{name}", json={
        "message": f"Фото товара {name}",
        "content": base64.b64encode(content).decode(),
        "branch": GITHUB_BRANCH,
    })
    return f"images/{name}"
