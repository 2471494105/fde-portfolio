-- 002_database.sql —— 客户库本身（在 postgres 库执行，库不存在时才跑）
-- 名字就叫 fde_client：它演的就是客户那台库，不加 _sim 之类的后缀。
CREATE DATABASE fde_client
  ENCODING 'UTF8'
  TEMPLATE template0;