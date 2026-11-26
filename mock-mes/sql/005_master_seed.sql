-- 005_master_seed.sql —— 主数据种子（可重复跑：先清空再灌）
-- 量：4 产线 / 16 工位 / 48 设备 / 12 检验项 / 30 缺陷码 / 24 物料
-- 设备编号 CX-101 ~ CX-148，五分钟能数完，方便答辩时报数。

TRUNCATE md_device, md_station, md_line, md_check_item, md_defect_code, md_material;

INSERT INTO md_line (line_no, line_name, workshop) VALUES
  ('产线A', 'A 线（后段装配）', '一车间'),
  ('产线B', 'B 线（前段贴合）', '一车间'),
  ('产线C', 'C 线（检测包装）', '二车间'),
  ('产线D', 'D 线（返修）',     '二车间');

INSERT INTO md_station (station_no, line_no, station_name, seq)
SELECT l.line_no || '-' || lpad(s::text, 2, '0'),
       l.line_no,
       l.line_no || '第' || s || '工位',
       s
FROM md_line l, generate_series(1, 4) AS s;

INSERT INTO md_device (equip_no, prod_line, work_station, cam_model, lens_spec, lamp_type, install_dt, maint_cycle)
SELECT 'CX-' || lpad((100 + (st.rn - 1) * 3 + k)::text, 3, '0'),
       st.line_no,
       st.station_no,
       (ARRAY['Basler acA2440-75um','Hikvision MV-CA050-10GM','Dalsa Linea 2k'])[k],
       (ARRAY['16mm','25mm','35mm'])[k],
       (ARRAY['环形白','条形红','穹顶'])[k],
       date '2023-01-01' + ((st.rn * 3 + k)::int),
       (ARRAY[30,60,90])[k]::smallint
FROM (SELECT station_no, line_no, row_number() OVER (ORDER BY station_no) AS rn FROM md_station) st,
     generate_series(1, 3) AS k;

INSERT INTO md_check_item (item_no, item_name, unit, lower_limit, upper_limit) VALUES
  ('CI-01','外观缺陷检出','次',1,5),
  ('CI-02','尺寸偏差','mm',0,2.5),
  ('CI-03','色差','pct',0,3),
  ('CI-04','漏检率','pct',0,0.5),
  ('CI-05','误判率','pct',0,1),
  ('CI-06','镜头脏污','次',0,2),
  ('CI-07','对焦偏移','mm',0,0.3),
  ('CI-08','光源衰减','pct',0,5),
  ('CI-09','节拍','秒',NULL,NULL),
  ('CI-10','气密性','kPa',80,120),
  ('CI-11','装配间隙','mm',0.1,1.2),
  ('CI-12','标签可读','次',NULL,NULL);

INSERT INTO md_defect_code (defect_code, defect_name, category, severity)
SELECT 'D-' || lpad(i::text, 3, '0'),
       (ARRAY['划伤','脏污','毛刺','变形','气泡','开裂','错位','漏装','反装','色差',
              '尺寸偏大','尺寸偏小','虚焊','断线','氧化','锈蚀','压痕','褶皱','毛边','异物'])[(i - 1) % 20 + 1]
         || '（' || (ARRAY['左','右','上','下','中'])[(i - 1) / 20 + 1] || '）',
       (ARRAY['外观','尺寸','功能','装配'])[(i - 1) % 4 + 1],
       ((i - 1) % 3 + 1)::smallint
FROM generate_series(1, 30) AS i;

INSERT INTO md_material (material_no, material_name, spec, unit)
SELECT 'M-' || lpad(i::text, 4, '0'),
       (ARRAY['外壳','密封圈','线束','镜头','光源','标签','支架','螺丝','PCB','胶水','滤网','挡板'])[(i - 1) % 12 + 1],
       (ARRAY['ABS 白','NBR φ20','1.5mm²','16mm','LED 白光','40×20','AL6061','M3×8','双面板','UV 胶','HEPA','304'])[(i - 1) % 12 + 1],
       (ARRAY['pcs','pcs','m','pcs','pcs','pcs','pcs','pcs','pcs','kg','pcs','pcs'])[(i - 1) % 12 + 1]
FROM generate_series(1, 24) AS i;