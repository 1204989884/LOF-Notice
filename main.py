"""
LOF套利监测系统 - FastAPI后端
"""
import os
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from fetcher import get_all_lof_arbitrage_opportunities

# ---------- 配置 ----------
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
CACHE_TTL = 300
USE_MOCK = False  # never mock
print(f"[启动] USE_MOCK={USE_MOCK}")

_cache = {"data": None, "updated_at": None}
_cache_lock = asyncio.Lock()


async def refresh_cache(force: bool = False) -> None:
    global _cache
    async with _cache_lock:
        now = datetime.now()
        if not force and _cache["updated_at"] is not None:
            elapsed = (now - _cache["updated_at"]).total_seconds()
            if elapsed < CACHE_TTL:
                return
        try:
            result = await get_all_lof_arbitrage_opportunities(use_mock=USE_MOCK)
            # 如果新数据为空但旧缓存有数据，保留旧数据（API可能被限流）
            if result.get("count", 0) == 0 and _cache["data"] and _cache["data"].get("count", 0) > 0:
                print(f"[缓存] 新数据为空（可能限流），保留旧缓存: {_cache['data'].get('count')} 条")
                return
            _cache["data"] = result
            _cache["updated_at"] = now
            print(f"[缓存] 刷新完成 | 数据源={result.get('source')} | 机会={result.get('count', 0)}")
        except Exception as e:
            print(f"[缓存] 刷新失败: {e}")
            if _cache["data"] is None:
                _cache["data"] = {"success": False, "error": str(e), "count": 0, "data": []}
                _cache["updated_at"] = now


# 不带lifespan，避免启动时挂住外部API请求
app = FastAPI(title="LOF套利监测系统", version="1.0.0")

os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(
                content=f.read(),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
    return HTMLResponse(content="<h1>前端文件不存在</h1>", status_code=404)


@app.get("/api/opportunities")
async def get_opportunities(
    refresh: bool = Query(False, description="强制刷新"),
    min_premium: float = Query(3.0, description="最低溢价率阈值(%)"),
    limit: int = Query(50, description="返回数量上限"),
):
    await refresh_cache(force=refresh)
    data = _cache.get("data") or {}
    opportunities = data.get("data", [])

    filtered = [o for o in opportunities if abs(o["premium_rt"]) >= min_premium]
    filtered = filtered[:limit]

    next_at = ""
    if _cache["updated_at"]:
        next_at = (_cache["updated_at"] + timedelta(seconds=CACHE_TTL)).strftime("%H:%M:%S")

    return {
        "success": data.get("success", False),
        "source": data.get("source", "unknown"),
        "total": len(opportunities),
        "count": len(filtered),
        "updated_at": data.get("updated_at", ""),
        "next_update_at": next_at,
        "data": filtered,
    }


@app.get("/api/stats")
async def get_stats():
    await refresh_cache()
    data = _cache.get("data") or {}
    opportunities = data.get("data", [])

    premium_list = [o for o in opportunities if o.get("is_premium")]
    discount_list = [o for o in opportunities if not o.get("is_premium")]

    return {
        "total_opportunities": len(opportunities),
        "premium_count": len(premium_list),
        "discount_count": len(discount_list),
        "max_premium": max((o["premium_rt"] for o in premium_list), default=0),
        "max_discount": max((abs(o["premium_rt"]) for o in discount_list), default=0),
        "avg_premium": round(sum(o["premium_rt"] for o in premium_list) / len(premium_list), 2) if premium_list else 0,
        "updated_at": data.get("updated_at", ""),
        "source": data.get("source", "unknown"),
    }


@app.get("/api/fund/{code}")
async def get_fund_detail(code: str):
    await refresh_cache()
    data = _cache.get("data") or {}
    opportunities = data.get("data", [])
    for o in opportunities:
        if o["code"] == code:
            return {"success": True, "data": o}
    return {"success": False, "error": "未找到该基金"}


@app.get("/api/config")
async def get_config():
    return {"use_mock": USE_MOCK, "cache_ttl": CACHE_TTL}


if __name__ == "__main__":
    import uvicorn
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8877
    print(f"[启动] LOF套利监测系统 | port={port} | mock={USE_MOCK}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
