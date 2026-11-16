\echo '=== 1. 每台设备误判率（verdict=NG 且实际良品）==='
SELECT device_id,
       count(*) AS total,
       count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) AS over_kill,
       round(100.0 * count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) / count(*), 1) AS fp_rate_pct
FROM defect_logs GROUP BY device_id ORDER BY device_id;

\echo '=== 2. CX-101 按工位（应集中 1 号工位）==='
SELECT station,
       count(*) AS total,
       count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) AS fp,
       round(100.0 * count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) / count(*), 1) AS fp_rate_pct
FROM defect_logs WHERE device_id = 'CX-101' GROUP BY station ORDER BY station;

\echo '=== 3. CX-101 1号工位的夜班 vs 白班 ==='
SELECT CASE WHEN extract(hour FROM ts) < 6 OR extract(hour FROM ts) >= 22 THEN '夜班' ELSE '白班' END AS shift,
       count(*) AS total,
       round(100.0 * count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) / count(*), 1) AS fp_rate_pct
FROM defect_logs WHERE device_id = 'CX-101' AND station = '1号工位' GROUP BY 1 ORDER BY 1;

\echo '=== 4. CX-102 按批次 Top5（应集中 B20261105）==='
SELECT batch,
       count(*) AS total,
       count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) AS fp,
       round(100.0 * count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) / count(*), 1) AS fp_rate_pct
FROM defect_logs WHERE device_id = 'CX-102'
GROUP BY batch ORDER BY fp_rate_pct DESC, total DESC LIMIT 5;

\echo '=== 5. CX-103 按缺陷类型（应集中划痕）==='
SELECT defect_type,
       count(*) AS total,
       round(100.0 * count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) / count(*), 1) AS fp_rate_pct
FROM defect_logs WHERE device_id = 'CX-103' GROUP BY defect_type ORDER BY fp_rate_pct DESC;

\echo '=== 6. CX-104 按日（11-08 之后应陡增）==='
SELECT ts::date AS day,
       count(*) AS total,
       round(100.0 * count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) / count(*), 1) AS fp_rate_pct
FROM defect_logs WHERE device_id = 'CX-104' GROUP BY 1 ORDER BY 1;

\echo '=== 7. CX-105 按日（应单调上升）==='
SELECT ts::date AS day,
       count(*) AS total,
       round(100.0 * count(*) FILTER (WHERE verdict = 'NG' AND is_actually_ok) / count(*), 1) AS fp_rate_pct
FROM defect_logs WHERE device_id = 'CX-105' GROUP BY 1 ORDER BY 1;

\echo '=== 8. 光源与标定关键证据 ==='
SELECT device_id, cumulative_hours, rated_life_hours,
       cumulative_hours - rated_life_hours AS over_hours, brightness_set
FROM light_usage ORDER BY device_id;

SELECT device_id, ts, drift_mm, result
FROM calibrations WHERE device_id = 'CX-104' ORDER BY ts DESC LIMIT 3;
