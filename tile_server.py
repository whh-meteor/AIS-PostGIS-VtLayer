#!/usr/bin/env python3
"""北海渔船矢量切片 HTTP 服务。

本程序使用 FastAPI 接收 XYZ 或船讯网风格的请求，从 PostGIS 动态生成
Mapbox Vector Tile（MVT）二进制数据。切片不会持久化到磁盘。
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path as FilePath

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Response
from psycopg_pool import ConnectionPool


load_dotenv()


def required_env(name: str) -> str:
    """读取必填环境变量；缺失时在服务启动阶段直接报错。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


# 瓦片请求只使用 PG_TILE_DSN 中的只读数据库账号。
# 连接池可以复用 PostgreSQL 连接，避免每个瓦片请求都重新握手。
POOL = ConnectionPool(
    conninfo=required_env("PG_TILE_DSN"),
    min_size=1,
    max_size=max(1, int(os.getenv("TILE_DB_POOL_SIZE", "10"))),
    timeout=10,
    open=False,
    kwargs={"autocommit": True},
)
CACHE_SECONDS = max(0, int(os.getenv("TILE_CACHE_SECONDS", "60")))
# getInfo 接口暂不可用时，从本地响应样例提取 areaCode。
# HTTP 接口只返回脱敏后的编码数组，不会把用户资料、密码哈希等内容暴露给浏览器。
GET_INFO_FALLBACK_PATH = FilePath(
    os.getenv("GET_INFO_FALLBACK_PATH", "/data/ltgk/beihai/getInfo.txt")
)
# 合法的空 MVT，内部包含名为 boats 的空图层，共 14 字节。
# PostGIS 在没有要素时可能返回零字节，而部分前端会将零字节 200 响应误判为网络错误。
EMPTY_BOATS_MVT = bytes.fromhex("1a0c78020a05626f617473288020")


@asynccontextmanager
async def lifespan(_: FastAPI):
    """随 FastAPI 生命周期打开并关闭 PostgreSQL 连接池。"""
    POOL.open(wait=True)
    try:
        yield
    finally:
        POOL.close()


app = FastAPI(
    title="Beihai vessel vector tiles",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    """检查数据库连通性，并返回同步数量、时间和水位等运行状态。"""
    try:
        with POOL.connection() as conn:
            return conn.execute("SELECT vessel.tile_health()").fetchone()[0]
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc


def collect_area_codes(value: object, result: set[str]) -> None:
    """递归提取 getInfo 响应中的 areaCode/areaCodes，统一转换为字符串。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"areaCode", "areaCodes"}:
                add_area_code_value(child, result)
            if isinstance(child, (dict, list)):
                collect_area_codes(child, result)
    elif isinstance(value, list):
        for child in value:
            if isinstance(child, (dict, list)):
                collect_area_codes(child, result)


def add_area_code_value(value: object, result: set[str]) -> None:
    """展开单个编码、编码数组或区域树节点，忽略空值。"""
    if isinstance(value, (str, int)):
        code = str(value).strip()
        if code:
            result.add(code)
    elif isinstance(value, list):
        for child in value:
            add_area_code_value(child, result)
    elif isinstance(value, dict):
        collect_area_codes(value, result)


@app.get("/area-codes")
def fallback_area_codes() -> dict:
    """返回 getInfo.txt 中脱敏后的本地区域编码，供真实 getInfo 不通时临时使用。"""
    try:
        payload = json.loads(GET_INFO_FALLBACK_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="getInfo fallback is unavailable",
        ) from exc

    codes: set[str] = set()
    collect_area_codes(payload, codes)
    if not codes:
        raise HTTPException(
            status_code=503,
            detail="getInfo fallback contains no areaCode",
        )

    return {
        "areaCodes": sorted(codes),
        "source": "getInfo.txt",
    }


def render_boat_tile(
    z: int,
    x: int,
    y: int,
    offline_hours: float | None,
) -> Response:
    """校验 XYZ 参数，调用 PostGIS 函数并构造 MVT HTTP 响应。"""
    # z 层级下 x、y 的合法范围均为 0～2^z-1。
    upper_bound = 1 << z
    if x >= upper_bound or y >= upper_bound:
        raise HTTPException(status_code=404, detail="tile is outside the XYZ matrix")

    # offline_hours 为 0 时传给 SQL 函数 NULL，仍受 SQL 中最近 24 小时硬上限约束。
    try:
        with POOL.connection() as conn:
            tile = conn.execute(
                "SELECT vessel.boat_points_mvt(%s, %s, %s, %s)",
                (z, x, y, offline_hours or None),
            ).fetchone()[0]
    except Exception as exc:
        raise HTTPException(status_code=503, detail="tile database unavailable") from exc

    # 无数据时返回合法空图层；ETag 用于浏览器和代理进行缓存校验。
    payload = bytes(tile or b"") or EMPTY_BOATS_MVT
    etag = hashlib.blake2b(payload, digest_size=16).hexdigest()
    return Response(
        content=payload,
        media_type="application/vnd.mapbox-vector-tile",
        headers={
            "Cache-Control": f"public, max-age={CACHE_SECONDS}, stale-while-revalidate=30",
            "ETag": f'"{etag}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/tiles/boats/{z}/{x}/{y}.pbf")
def boat_tile(
    z: int = Path(ge=0, le=14),
    x: int = Path(ge=0),
    y: int = Path(ge=0),
    offline_hours: float | None = Query(
        default=None,
        alias="offlineHours",
        ge=0,
        le=24,
        description="Hide positions older than this many hours; omit or use 0 for the 24-hour maximum",
    ),
) -> Response:
    """标准 XYZ 路径入口，例如 /tiles/boats/10/822/449.pbf。"""
    return render_boat_tile(z, x, y, offline_hours)


@app.get("/tileserver/mvt/cache")
def boat_tile_query(
    z: int = Query(ge=0, le=14),
    x: int = Query(ge=0),
    y: int = Query(ge=0),
    offline_hours: float | None = Query(
        default=None,
        alias="offlineHours",
        ge=0,
        le=24,
        description="Hide positions older than this many hours; omit or use 0 for the 24-hour maximum",
    ),
) -> Response:
    """船讯网风格的查询参数入口，供不便使用 XYZ 路径模板的客户端调用。"""
    return render_boat_tile(z, x, y, offline_hours)
