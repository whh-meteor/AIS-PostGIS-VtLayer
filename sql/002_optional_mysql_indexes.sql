-- ============================================================================
-- MySQL 可选增量索引脚本
-- 作用：让定时同步按 update_time/location_time 做范围扫描，减少全表扫描。
-- 注意：仅在 MySQL 8.0 执行；先用 SHOW INDEX 检查同类索引并评估建索引负载。
-- 本脚本不是幂等脚本，重复执行同名 ADD INDEX 会报错。
-- 影响：建索引属于源业务库 DDL 操作，数据量较大时可能消耗较多 IO 和临时空间。
-- 建议：先在业务低峰期执行，并使用 SHOW PROCESSLIST 观察运行情况。
-- ============================================================================

-- 最新定位表：优化更新时间、报位时间水位查询，并带上主键终端号。
-- idx_bln_update_time_phone 对应 update_time 增量条件；
-- idx_bln_location_time_phone 对应 location_time 增量条件。
ALTER TABLE bh_location.boat_location_new
    ADD INDEX idx_bln_update_time_phone (update_time, terminal_phone),
    ADD INDEX idx_bln_location_time_phone (location_time, terminal_phone),
    ALGORITHM=INPLACE,
    LOCK=NONE;

-- 乡镇船档案：优化按更新时间水位及 id 排序的增量读取。
ALTER TABLE bh_business.town_boat_base_mes
    ADD INDEX idx_tbbm_update_time_id (update_time, id),
    ALGORITHM=INPLACE,
    LOCK=NONE;

-- 双控船档案：优化按更新时间水位及 id 排序的增量读取。
ALTER TABLE bh_business.dual_boat_base_mes
    ADD INDEX idx_dbbm_update_time_id (update_time, id),
    ALGORITHM=INPLACE,
    LOCK=NONE;
