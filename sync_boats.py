#!/usr/bin/env python3
"""将 MySQL 船舶档案和最新定位数据幂等同步到 PostGIS。

普通运行根据 vessel.sync_state 中的水位增量读取；--full 强制全量读取；
--full --prune 还会删除源库中已经不存在的目标记录。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

import pymysql
import psycopg
from dotenv import load_dotenv
from pymysql.cursors import SSDictCursor


LOG = logging.getLogger("vessel-sync")
EPOCH = datetime(1970, 1, 1)


def required_env(name: str) -> str:
    """读取必填环境变量，避免连接参数缺失时继续运行。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"required environment variable is missing: {name}")
    return value


def safe_identifier(name: str) -> str:
    """校验 MySQL 库名，只允许字母、数字和下划线，防止标识符注入。"""
    if not name.replace("_", "").isalnum() or name[0].isdigit():
        raise RuntimeError(f"invalid MySQL database identifier: {name!r}")
    return name


@dataclass(frozen=True)
class Settings:
    """同步程序运行配置；敏感值只从环境文件读取，不写入代码。"""
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    location_db: str
    business_db: str
    mysql_connect_timeout: int
    mysql_read_timeout: int
    pg_dsn: str
    overlap_seconds: int
    fetch_size: int

    @classmethod
    def from_env(cls) -> "Settings":
        """读取环境变量并生成不可变配置对象。"""
        return cls(
            mysql_host=required_env("MYSQL_HOST"),
            mysql_port=int(os.getenv("MYSQL_PORT", "3306")),
            mysql_user=required_env("MYSQL_USER"),
            mysql_password=required_env("MYSQL_PASSWORD"),
            location_db=safe_identifier(os.getenv("MYSQL_LOCATION_DB", "bh_location")),
            business_db=safe_identifier(os.getenv("MYSQL_BUSINESS_DB", "bh_business")),
            mysql_connect_timeout=int(os.getenv("MYSQL_CONNECT_TIMEOUT", "10")),
            mysql_read_timeout=int(os.getenv("MYSQL_READ_TIMEOUT", "120")),
            pg_dsn=required_env("PG_SYNC_DSN"),
            overlap_seconds=max(0, int(os.getenv("SYNC_OVERLAP_SECONDS", "600"))),
            fetch_size=max(100, int(os.getenv("SYNC_FETCH_SIZE", "2000"))),
        )


def mysql_connection(settings: Settings) -> pymysql.Connection:
    """创建 MySQL 流式字典游标连接，避免全量数据一次性进入内存。"""
    return pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.location_db,
        charset="utf8mb4",
        cursorclass=SSDictCursor,
        autocommit=True,
        connect_timeout=settings.mysql_connect_timeout,
        read_timeout=settings.mysql_read_timeout,
        write_timeout=30,
    )


def get_watermark(pg: psycopg.Connection, stream_name: str) -> datetime | None:
    """读取指定数据流上次成功同步的增量时间水位。"""
    row = pg.execute(
        "SELECT watermark FROM vessel.sync_state WHERE stream_name = %s",
        (stream_name,),
    ).fetchone()
    return row[0] if row else None


def mark_started(pg: psycopg.Connection, stream_name: str) -> None:
    """记录本次同步开始时间，并清除该数据流上一次错误。"""
    pg.execute(
        """
        INSERT INTO vessel.sync_state (stream_name, last_started_at, last_error)
        VALUES (%s, clock_timestamp(), NULL)
        ON CONFLICT (stream_name) DO UPDATE
        SET last_started_at = EXCLUDED.last_started_at,
            last_error = NULL
        """,
        (stream_name,),
    )


def mark_succeeded(
    pg: psycopg.Connection,
    stream_name: str,
    watermark: datetime | None,
    rows_seen: int,
    rows_applied: int,
) -> None:
    """记录同步成功后的新水位、读取数和实际写入数。"""
    pg.execute(
        """
        INSERT INTO vessel.sync_state (
            stream_name, watermark, last_succeeded_at, rows_seen, rows_applied, last_error
        ) VALUES (%s, %s, clock_timestamp(), %s, %s, NULL)
        ON CONFLICT (stream_name) DO UPDATE
        SET watermark = COALESCE(EXCLUDED.watermark, vessel.sync_state.watermark),
            last_succeeded_at = EXCLUDED.last_succeeded_at,
            rows_seen = EXCLUDED.rows_seen,
            rows_applied = EXCLUDED.rows_applied,
            last_error = NULL
        """,
        (stream_name, watermark, rows_seen, rows_applied),
    )


def iter_rows(cursor: SSDictCursor, fetch_size: int) -> Iterable[dict[str, Any]]:
    """按批次迭代 MySQL 流式结果集，控制同步进程内存占用。"""
    while True:
        rows = cursor.fetchmany(fetch_size)
        if not rows:
            return
        yield from rows


def max_datetime(current: datetime | None, candidate: Any) -> datetime | None:
    """从当前水位和候选时间中返回较晚的有效 datetime。"""
    if not isinstance(candidate, datetime):
        return current
    return candidate if current is None or candidate > current else current


def sync_dual_boats(
    mysql: pymysql.Connection,
    pg: psycopg.Connection,
    settings: Settings,
    force_full: bool,
) -> tuple[int, int]:
    """同步 bh_business.dual_boat_base_mes 双控船档案。"""
    stream_name = "dual_boat_base_mes"
    previous = None if force_full else get_watermark(pg, stream_name)
    cutoff = previous - timedelta(seconds=settings.overlap_seconds) if previous else None
    mark_started(pg, stream_name)

    # 将 update_time/create_time 的较大值作为增量水位；未来时间最多容忍 5 分钟，
    # 避免异常时间把水位永久推进到未来而漏掉后续正常记录。
    table = f"`{settings.business_db}`.`dual_boat_base_mes`"
    query = f"""
        SELECT
            id AS source_id,
            boat_code,
            area_code,
            area_name,
            boat_name,
            boat_category,
            boat_length,
            total_power,
            boat_type,
            work_type,
            COALESCE(del_flag, 0) AS del_flag,
            create_time AS source_create_time,
            update_time AS source_update_time,
            LEAST(
                GREATEST(
                    COALESCE(update_time, '1970-01-01 00:00:00'),
                    COALESCE(create_time, '1970-01-01 00:00:00')
                ),
                DATE_ADD(NOW(), INTERVAL 5 MINUTE)
            ) AS source_updated_at
        FROM {table}
        WHERE 1 = 1
    """
    params: tuple[Any, ...] = ()
    if cutoff:
        query += " AND (update_time >= %s OR create_time >= %s)"
        params = (cutoff, cutoff)
    query += " ORDER BY source_updated_at, source_id"

    # 先通过 PostgreSQL 临时表批量接收数据，再一次性执行 UPSERT。
    pg.execute("DROP TABLE IF EXISTS pg_temp.dual_boat_stage")
    pg.execute(
        """
        CREATE TEMP TABLE dual_boat_stage (
            source_id bigint,
            boat_code text,
            area_code text,
            area_name text,
            boat_name text,
            boat_category text,
            boat_length numeric(10, 2),
            total_power text,
            boat_type text,
            work_type text,
            del_flag boolean,
            source_create_time timestamp without time zone,
            source_update_time timestamp without time zone,
            source_updated_at timestamp without time zone
        ) ON COMMIT DROP
        """
    )

    # COPY 比逐行 INSERT 更高效，同时在读取过程中计算本批最高水位。
    seen = 0
    high_watermark = previous
    with mysql.cursor() as source_cursor:
        source_cursor.execute(query, params)
        with pg.cursor().copy(
            """
            COPY dual_boat_stage (
                source_id, boat_code, area_code, area_name, boat_name,
                boat_category, boat_length, total_power, boat_type, work_type,
                del_flag, source_create_time, source_update_time, source_updated_at
            ) FROM STDIN
            """
        ) as copy:
            for row in iter_rows(source_cursor, settings.fetch_size):
                copy.write_row(
                    (
                        row["source_id"],
                        row["boat_code"],
                        row["area_code"],
                        row["area_name"],
                        row["boat_name"],
                        row["boat_category"],
                        row["boat_length"],
                        row["total_power"],
                        row["boat_type"],
                        row["work_type"],
                        int(row["del_flag"] or 0) != 0,
                        row["source_create_time"],
                        row["source_update_time"],
                        row["source_updated_at"],
                    )
                )
                seen += 1
                high_watermark = max_datetime(high_watermark, row["source_updated_at"])

    # 仅当来源记录不旧于目标记录时覆盖，保证重叠窗口重复读取仍然幂等。
    result = pg.execute(
        """
        INSERT INTO vessel.dual_boat_archive AS target (
            source_id, boat_code, area_code, area_name, boat_name,
            boat_category, boat_length, total_power, boat_type, work_type,
            del_flag, source_create_time, source_update_time, source_updated_at,
            synced_at
        )
        SELECT
            source_id, boat_code, area_code, area_name, boat_name,
            boat_category, boat_length, total_power, boat_type, work_type,
            del_flag, source_create_time, source_update_time, source_updated_at,
            clock_timestamp()
        FROM dual_boat_stage
        ON CONFLICT (source_id) DO UPDATE
        SET boat_code = EXCLUDED.boat_code,
            area_code = EXCLUDED.area_code,
            area_name = EXCLUDED.area_name,
            boat_name = EXCLUDED.boat_name,
            boat_category = EXCLUDED.boat_category,
            boat_length = EXCLUDED.boat_length,
            total_power = EXCLUDED.total_power,
            boat_type = EXCLUDED.boat_type,
            work_type = EXCLUDED.work_type,
            del_flag = EXCLUDED.del_flag,
            source_create_time = EXCLUDED.source_create_time,
            source_update_time = EXCLUDED.source_update_time,
            source_updated_at = EXCLUDED.source_updated_at,
            synced_at = EXCLUDED.synced_at
        WHERE EXCLUDED.source_updated_at >= target.source_updated_at
        """
    )
    applied = max(0, result.rowcount)
    mark_succeeded(pg, stream_name, high_watermark, seen, applied)
    return seen, applied


def sync_town_boats(
    mysql: pymysql.Connection,
    pg: psycopg.Connection,
    settings: Settings,
    force_full: bool,
) -> tuple[int, int]:
    """同步 bh_business.town_boat_base_mes 乡镇船档案。"""
    stream_name = "town_boat_base_mes"
    previous = None if force_full else get_watermark(pg, stream_name)
    cutoff = previous - timedelta(seconds=settings.overlap_seconds) if previous else None
    mark_started(pg, stream_name)

    # 档案增量水位取 update_time/create_time 较大值，并限制异常未来时间。
    table = f"`{settings.business_db}`.`town_boat_base_mes`"
    query = f"""
        SELECT
            id AS source_id,
            area_name,
            area_code,
            plan_boat_code,
            boat_usage,
            boat_total_length,
            boat_length,
            boat_total_power,
            COALESCE(del_flag, 0) AS del_flag,
            create_time AS source_create_time,
            update_time AS source_update_time,
            LEAST(
                GREATEST(
                    COALESCE(update_time, '1970-01-01 00:00:00'),
                    COALESCE(create_time, '1970-01-01 00:00:00')
                ),
                DATE_ADD(NOW(), INTERVAL 5 MINUTE)
            ) AS source_updated_at
        FROM {table}
        WHERE 1 = 1
    """
    params: tuple[Any, ...] = ()
    if cutoff:
        query += " AND (update_time >= %s OR create_time >= %s)"
        params = (cutoff, cutoff)
    query += " ORDER BY source_updated_at, source_id"

    # 临时表只在当前事务中存在，事务提交或回滚后自动删除。
    pg.execute("DROP TABLE IF EXISTS pg_temp.town_boat_stage")
    pg.execute(
        """
        CREATE TEMP TABLE town_boat_stage (
            source_id bigint,
            area_name text,
            area_code text,
            plan_boat_code text,
            boat_usage text,
            boat_total_length numeric(10, 2),
            boat_length numeric(10, 2),
            boat_total_power numeric(10, 2),
            del_flag boolean,
            source_create_time timestamp without time zone,
            source_update_time timestamp without time zone,
            source_updated_at timestamp without time zone
        ) ON COMMIT DROP
        """
    )

    # 流式读取 MySQL 后通过 COPY 批量装载临时表。
    seen = 0
    high_watermark = previous
    with mysql.cursor() as source_cursor:
        source_cursor.execute(query, params)
        with pg.cursor().copy(
            """
            COPY town_boat_stage (
                source_id, area_name, area_code, plan_boat_code, boat_usage,
                boat_total_length, boat_length, boat_total_power, del_flag,
                source_create_time, source_update_time, source_updated_at
            ) FROM STDIN
            """
        ) as copy:
            for row in iter_rows(source_cursor, settings.fetch_size):
                copy.write_row(
                    (
                        row["source_id"],
                        row["area_name"],
                        row["area_code"],
                        row["plan_boat_code"],
                        row["boat_usage"],
                        row["boat_total_length"],
                        row["boat_length"],
                        row["boat_total_power"],
                        int(row["del_flag"] or 0) != 0,
                        row["source_create_time"],
                        row["source_update_time"],
                        row["source_updated_at"],
                    )
                )
                seen += 1
                high_watermark = max_datetime(high_watermark, row["source_updated_at"])

    # 以源表 id 为冲突键执行幂等 UPSERT。
    result = pg.execute(
        """
        INSERT INTO vessel.town_boat_archive AS target (
            source_id, area_name, area_code, plan_boat_code, boat_usage,
            boat_total_length, boat_length, boat_total_power, del_flag,
            source_create_time, source_update_time, source_updated_at, synced_at
        )
        SELECT
            source_id, area_name, area_code, plan_boat_code, boat_usage,
            boat_total_length, boat_length, boat_total_power, del_flag,
            source_create_time, source_update_time, source_updated_at,
            clock_timestamp()
        FROM town_boat_stage
        ON CONFLICT (source_id) DO UPDATE
        SET area_name = EXCLUDED.area_name,
            area_code = EXCLUDED.area_code,
            plan_boat_code = EXCLUDED.plan_boat_code,
            boat_usage = EXCLUDED.boat_usage,
            boat_total_length = EXCLUDED.boat_total_length,
            boat_length = EXCLUDED.boat_length,
            boat_total_power = EXCLUDED.boat_total_power,
            del_flag = EXCLUDED.del_flag,
            source_create_time = EXCLUDED.source_create_time,
            source_update_time = EXCLUDED.source_update_time,
            source_updated_at = EXCLUDED.source_updated_at,
            synced_at = EXCLUDED.synced_at
        WHERE EXCLUDED.source_updated_at >= target.source_updated_at
        """
    )
    applied = max(0, result.rowcount)
    mark_succeeded(pg, stream_name, high_watermark, seen, applied)
    return seen, applied


def sync_locations(
    mysql: pymysql.Connection,
    pg: psycopg.Connection,
    settings: Settings,
    force_full: bool,
) -> tuple[int, int, int]:
    """同步 bh_location.boat_location_new 中每个终端的最新船位。"""
    stream_name = "boat_location_new"
    previous = None if force_full else get_watermark(pg, stream_name)
    cutoff = previous - timedelta(seconds=settings.overlap_seconds) if previous else None
    mark_started(pg, stream_name)

    # 定位表还需考虑 location_time，避免设备新报位但 update_time 未变化时漏读。
    table = f"`{settings.location_db}`.`boat_location_new`"
    query = f"""
        SELECT
            terminal_phone,
            boat_name,
            boat_manage_type,
            longitude,
            latitude,
            location_time,
            speed,
            direction,
            terminal_type,
            COALESCE(del_flag, 0) AS del_flag,
            remark,
            source_name,
            create_time AS source_create_time,
            update_time AS source_update_time,
            LEAST(
                GREATEST(
                    COALESCE(update_time, '1970-01-01 00:00:00'),
                    COALESCE(create_time, '1970-01-01 00:00:00'),
                    COALESCE(location_time, '1970-01-01 00:00:00')
                ),
                DATE_ADD(NOW(), INTERVAL 5 MINUTE)
            ) AS source_updated_at
        FROM {table}
        WHERE terminal_phone IS NOT NULL
          AND terminal_phone <> ''
    """
    params: tuple[Any, ...] = ()
    if cutoff:
        query += " AND (location_time >= %s OR update_time >= %s OR create_time >= %s)"
        params = (cutoff, cutoff, cutoff)
    query += " ORDER BY source_updated_at, terminal_phone"

    # 临时表用于批量落地源数据，并在目标表写入时统一生成空间字段。
    pg.execute("DROP TABLE IF EXISTS pg_temp.location_stage")
    pg.execute(
        """
        CREATE TEMP TABLE location_stage (
            terminal_phone text,
            boat_name text,
            boat_manage_type text,
            longitude numeric(12, 8),
            latitude numeric(12, 8),
            location_time timestamp without time zone,
            speed numeric(10, 2),
            direction numeric(10, 2),
            terminal_type text,
            del_flag boolean,
            remark text,
            source_name text,
            source_create_time timestamp without time zone,
            source_update_time timestamp without time zone,
            source_updated_at timestamp without time zone
        ) ON COMMIT DROP
        """
    )

    # invalid_coordinates 仅用于监控；无效坐标仍保留业务字段，但空间字段写 NULL。
    seen = 0
    invalid_coordinates = 0
    high_watermark = previous
    with mysql.cursor() as source_cursor:
        source_cursor.execute(query, params)
        with pg.cursor().copy(
            """
            COPY location_stage (
                terminal_phone, boat_name, boat_manage_type, longitude, latitude,
                location_time, speed, direction, terminal_type, del_flag, remark,
                source_name, source_create_time, source_update_time, source_updated_at
            ) FROM STDIN
            """
        ) as copy:
            for row in iter_rows(source_cursor, settings.fetch_size):
                lon = row["longitude"]
                lat = row["latitude"]
                if lon is not None and lat is not None:
                    if not (
                        Decimal("-180") <= lon <= Decimal("180")
                        and Decimal("-90") <= lat <= Decimal("90")
                    ):
                        invalid_coordinates += 1
                copy.write_row(
                    (
                        row["terminal_phone"],
                        row["boat_name"],
                        row["boat_manage_type"],
                        lon,
                        lat,
                        row["location_time"],
                        row["speed"],
                        row["direction"],
                        row["terminal_type"],
                        bool(row["del_flag"]),
                        row["remark"],
                        row["source_name"],
                        row["source_create_time"],
                        row["source_update_time"],
                        row["source_updated_at"],
                    )
                )
                seen += 1
                high_watermark = max_datetime(high_watermark, row["source_updated_at"])

    # EPSG:4326 保存原始经纬度，EPSG:3857 用于 XYZ 瓦片范围查询。
    # (0, 0)、越界坐标以及超出 Web Mercator 纬度范围的坐标不生成几何。
    result = pg.execute(
        """
        INSERT INTO vessel.boat_location_latest AS target (
            terminal_phone, boat_name, boat_manage_type, longitude, latitude,
            location_time, speed, direction, terminal_type, del_flag, remark,
            source_name, source_create_time, source_update_time, source_updated_at,
            geom_4326, geom_3857, synced_at
        )
        SELECT DISTINCT ON (terminal_phone)
            terminal_phone,
            boat_name,
            boat_manage_type,
            longitude,
            latitude,
            location_time,
            speed,
            direction,
            terminal_type,
            del_flag,
            remark,
            source_name,
            source_create_time,
            source_update_time,
            source_updated_at,
            CASE
                WHEN longitude BETWEEN -180 AND 180 AND latitude BETWEEN -90 AND 90
                    AND NOT (longitude = 0 AND latitude = 0)
                THEN ST_SetSRID(ST_MakePoint(longitude::double precision, latitude::double precision), 4326)
                ELSE NULL
            END,
            CASE
                WHEN longitude BETWEEN -180 AND 180
                    AND latitude BETWEEN -85.05112878 AND 85.05112878
                    AND NOT (longitude = 0 AND latitude = 0)
                THEN ST_Transform(
                    ST_SetSRID(ST_MakePoint(longitude::double precision, latitude::double precision), 4326),
                    3857
                )
                ELSE NULL
            END,
            clock_timestamp()
        FROM location_stage
        ORDER BY terminal_phone, source_updated_at DESC
        ON CONFLICT (terminal_phone) DO UPDATE
        SET boat_name = EXCLUDED.boat_name,
            boat_manage_type = EXCLUDED.boat_manage_type,
            longitude = EXCLUDED.longitude,
            latitude = EXCLUDED.latitude,
            location_time = EXCLUDED.location_time,
            speed = EXCLUDED.speed,
            direction = EXCLUDED.direction,
            terminal_type = EXCLUDED.terminal_type,
            del_flag = EXCLUDED.del_flag,
            remark = EXCLUDED.remark,
            source_name = EXCLUDED.source_name,
            source_create_time = EXCLUDED.source_create_time,
            source_update_time = EXCLUDED.source_update_time,
            source_updated_at = EXCLUDED.source_updated_at,
            geom_4326 = EXCLUDED.geom_4326,
            geom_3857 = EXCLUDED.geom_3857,
            synced_at = EXCLUDED.synced_at
        WHERE EXCLUDED.source_updated_at >= target.source_updated_at
        """
    )
    applied = max(0, result.rowcount)
    mark_succeeded(pg, stream_name, high_watermark, seen, applied)
    return seen, applied, invalid_coordinates


def prune_missing(
    mysql: pymysql.Connection,
    pg: psycopg.Connection,
    settings: Settings,
) -> tuple[int, int, int]:
    """删除源库已不存在的目标记录；仅允许由 --full --prune 显式触发。"""
    # 分别复制三张源表的完整主键集合，再用 NOT EXISTS 安全对账。
    pg.execute("CREATE TEMP TABLE source_location_keys (terminal_phone text PRIMARY KEY) ON COMMIT DROP")
    with mysql.cursor() as cursor:
        cursor.execute(
            f"SELECT terminal_phone FROM `{settings.location_db}`.`boat_location_new` "
            "WHERE terminal_phone IS NOT NULL AND terminal_phone <> ''"
        )
        with pg.cursor().copy("COPY source_location_keys (terminal_phone) FROM STDIN") as copy:
            for row in iter_rows(cursor, settings.fetch_size):
                copy.write_row((row["terminal_phone"],))
    removed_locations = pg.execute(
        """
        DELETE FROM vessel.boat_location_latest AS target
        WHERE NOT EXISTS (
            SELECT 1 FROM source_location_keys AS source
            WHERE source.terminal_phone = target.terminal_phone
        )
        """
    ).rowcount

    pg.execute("CREATE TEMP TABLE source_town_boat_keys (source_id bigint PRIMARY KEY) ON COMMIT DROP")
    with mysql.cursor() as cursor:
        cursor.execute(
            f"SELECT id AS source_id FROM `{settings.business_db}`.`town_boat_base_mes`"
        )
        with pg.cursor().copy("COPY source_town_boat_keys (source_id) FROM STDIN") as copy:
            for row in iter_rows(cursor, settings.fetch_size):
                copy.write_row((row["source_id"],))
    removed_town_archives = pg.execute(
        """
        DELETE FROM vessel.town_boat_archive AS target
        WHERE NOT EXISTS (
            SELECT 1 FROM source_town_boat_keys AS source
            WHERE source.source_id = target.source_id
        )
        """
    ).rowcount

    pg.execute("CREATE TEMP TABLE source_dual_boat_keys (source_id bigint PRIMARY KEY) ON COMMIT DROP")
    with mysql.cursor() as cursor:
        cursor.execute(
            f"SELECT id AS source_id FROM `{settings.business_db}`.`dual_boat_base_mes`"
        )
        with pg.cursor().copy("COPY source_dual_boat_keys (source_id) FROM STDIN") as copy:
            for row in iter_rows(cursor, settings.fetch_size):
                copy.write_row((row["source_id"],))
    removed_dual_archives = pg.execute(
        """
        DELETE FROM vessel.dual_boat_archive AS target
        WHERE NOT EXISTS (
            SELECT 1 FROM source_dual_boat_keys AS source
            WHERE source.source_id = target.source_id
        )
        """
    ).rowcount
    return (
        max(0, removed_locations),
        max(0, removed_town_archives),
        max(0, removed_dual_archives),
    )


def record_failure(settings: Settings, error: Exception) -> None:
    """主事务回滚后，尽最大努力把失败原因单独写入同步状态表。"""
    message = f"{type(error).__name__}: {error}"[:2000]
    try:
        with psycopg.connect(settings.pg_dsn, autocommit=True) as pg:
            for stream_name in (
                "dual_boat_base_mes",
                "town_boat_base_mes",
                "boat_location_new",
            ):
                pg.execute(
                    """
                    INSERT INTO vessel.sync_state (stream_name, last_started_at, last_error)
                    VALUES (%s, clock_timestamp(), %s)
                    ON CONFLICT (stream_name) DO UPDATE
                    SET last_error = EXCLUDED.last_error
                    """,
                    (stream_name, message),
                )
    except Exception:
        LOG.exception("could not persist synchronization failure status")


def parse_args() -> argparse.Namespace:
    """解析全量同步及删除对账参数，并校验危险参数组合。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full",
        action="store_true",
        help="ignore saved watermarks and read all source tables in full",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="with --full, delete target keys that no longer exist in MySQL",
    )
    args = parser.parse_args()
    if args.prune and not args.full:
        parser.error("--prune is only valid together with --full")
    return args


def main() -> int:
    """建立连接、获取事务级互斥锁，并按档案到定位的顺序执行同步。"""
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = parse_args()
    settings = Settings.from_env()

    try:
        with psycopg.connect(settings.pg_dsn) as pg:
            pg.execute(
                "SELECT set_config('TimeZone', %s, false)",
                (os.getenv("SOURCE_TIMEZONE", "Asia/Shanghai"),),
            )
            # 事务级 advisory lock 防止 timer 或人工执行造成两个同步任务重叠。
            locked = pg.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext('vessel-mysql-postgis-sync'))"
            ).fetchone()[0]
            if not locked:
                LOG.warning("another synchronization run is still active; skipping")
                return 0

            with mysql_connection(settings) as mysql:
                dual_seen, dual_applied = sync_dual_boats(
                    mysql, pg, settings, args.full
                )
                town_seen, town_applied = sync_town_boats(
                    mysql, pg, settings, args.full
                )
                location_seen, location_applied, invalid_coordinates = sync_locations(
                    mysql, pg, settings, args.full
                )
                removed_locations = removed_town_archives = removed_dual_archives = 0
                if args.prune:
                    (
                        removed_locations,
                        removed_town_archives,
                        removed_dual_archives,
                    ) = prune_missing(mysql, pg, settings)

            LOG.info(
                "sync complete dual_seen=%d dual_applied=%d "
                "town_seen=%d town_applied=%d "
                "location_seen=%d location_applied=%d invalid_coordinates=%d "
                "removed_locations=%d removed_town_archives=%d "
                "removed_dual_archives=%d",
                dual_seen,
                dual_applied,
                town_seen,
                town_applied,
                location_seen,
                location_applied,
                invalid_coordinates,
                removed_locations,
                removed_town_archives,
                removed_dual_archives,
            )
        return 0
    except Exception as exc:
        LOG.exception("synchronization failed")
        record_failure(settings, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
