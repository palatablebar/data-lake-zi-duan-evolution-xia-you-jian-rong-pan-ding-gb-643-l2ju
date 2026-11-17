-- fieldmap 元数据导出 v2026_11（单位变更：金额改为按分登记，类型仍是 decimal）
-- 陷阱：类型只放宽不破坏，朴素判定会把全部下游判兼容，月底结算金额放大百倍。
CREATE TABLE dim_order (
  `order_id`     BIGINT        NOT NULL,  -- fid=1001
  `order_code`   VARCHAR(64)   NOT NULL,  -- fid=1002
  `order_amt`    DECIMAL(20,4) NOT NULL,  -- fid=1004, unit=fen
  `ext_json`     TEXT          NULL,      -- fid=1007
  `order_region` VARCHAR(24)   NOT NULL,  -- fid=1010
  `status`       VARCHAR(16)   NOT NULL   -- fid=1011
);
CREATE TABLE fct_pay (
  `pay_id`       BIGINT        NOT NULL,  -- fid=2001
  `order_id`     BIGINT        NOT NULL,  -- fid=2002
  `pay_amt`      DECIMAL(18,4) NOT NULL,  -- fid=2003, unit=fen
  `paid_at`      TIMESTAMP     NOT NULL,  -- fid=2004
  `pay_channel`  VARCHAR(8)    NULL,      -- fid=2005
  `pay_fee`      DECIMAL(18,4) NULL       -- fid=2006, unit=fen
);
CREATE TABLE dim_user (
  `user_id`      BIGINT        NOT NULL,  -- fid=3001
  `user_name`    VARCHAR(64)   NOT NULL,  -- fid=3002
  `created_at`   TIMESTAMP     NULL,      -- fid=3003
  `is_active`    BOOLEAN       NOT NULL   -- fid=3004
);
