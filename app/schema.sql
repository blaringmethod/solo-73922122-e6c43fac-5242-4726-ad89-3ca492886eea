-- 连续合约结算价库结构
-- 设计要点：
--   contracts.ord = 0 为初始合约；第 k 次换月启用的合约 ord = k（k >= 1）
--   rolls 记录每次换月；delta 为换月价差（新合约价 - 旧合约价，取切换前一交易日）
--   bit_sums 是树状数组（Fenwick tree）辅助表，前缀和/单点更新均为 O(log n)
--   连续价不落地存储，取数时实时计算，改价不逐日回写
--   spreads 为行情员按交易日保存的交易所价差表（后者价 - 前者价，整数分）
--   inferred_prices 为按价差表推算的缺报合约结算价（推算结果落库，原子生效）

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS contracts (
    contract_id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    ord  INTEGER NOT NULL UNIQUE      -- 0=初始合约，k=第 k 次换月启用的合约
);

CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    initial_contract_id INTEGER REFERENCES contracts(contract_id)
);
INSERT OR IGNORE INTO meta (id) VALUES (1);

CREATE TABLE IF NOT EXISTS rolls (
    ord              INTEGER PRIMARY KEY,          -- 第几次换月，1..n 稠密
    switch_date      TEXT NOT NULL UNIQUE,         -- 切换日（当日起启用新合约）
    pre_date         TEXT NOT NULL,                 -- 切换前一交易日
    new_contract_id  INTEGER NOT NULL REFERENCES contracts(contract_id),
    delta            INTEGER,                       -- 新合约价 - 旧合约价，缺报价时为 NULL
    roll_inferred    INTEGER NOT NULL DEFAULT 0    -- 本次 delta 是否引用了推算价（1=是）
);

-- Fenwick 树：pos 为树状数组下标，val 为区间累加值；仅覆盖非空 delta
CREATE TABLE IF NOT EXISTS bit_sums (
    pos INTEGER PRIMARY KEY,
    val INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar (
    date TEXT PRIMARY KEY      -- 仅登记交易日，ISO 格式 YYYY-MM-DD
);

CREATE TABLE IF NOT EXISTS prices (
    contract_id INTEGER NOT NULL REFERENCES contracts(contract_id),
    date        TEXT NOT NULL,
    price       INTEGER NOT NULL,                    -- 整数分结算价（实报）
    PRIMARY KEY (contract_id, date)
) WITHOUT ROWID;

-- 交易所公布的合约价差表，按交易日全量替换：contract_b 价 - contract_a 价 = spread（整数分）
CREATE TABLE IF NOT EXISTS spreads (
    date         TEXT NOT NULL,
    contract_a   INTEGER NOT NULL REFERENCES contracts(contract_id),
    contract_b   INTEGER NOT NULL REFERENCES contracts(contract_id),
    spread       INTEGER NOT NULL,                   -- b - a，整数分
    PRIMARY KEY (date, contract_a, contract_b)
) WITHOUT ROWID;

-- 按当日价差表推算的缺报合约结算价：
--   status=ok：推算成立，price 非空，可与实报同样供数（实报优先于推算价）
--   status=no_anchor / conflict：该合约所在价差组不能推算，price 为 NULL，reason 记录原因
CREATE TABLE IF NOT EXISTS inferred_prices (
    contract_id  INTEGER NOT NULL REFERENCES contracts(contract_id),
    date         TEXT NOT NULL,
    price        INTEGER,
    status       TEXT NOT NULL,                      -- ok / no_anchor / conflict
    reason       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (contract_id, date)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_rolls_switch  ON rolls(switch_date);
CREATE INDEX IF NOT EXISTS idx_rolls_predate ON rolls(pre_date);
-- 部分索引：快速定位区间内最早缺价差的换月
CREATE INDEX IF NOT EXISTS idx_rolls_delta_null ON rolls(ord) WHERE delta IS NULL;
-- 部分索引：单日查询所读“引用推算价的换月”记录数至多随换月次数按对数增长
CREATE INDEX IF NOT EXISTS idx_rolls_pre_inferred
    ON rolls(pre_date, ord) WHERE roll_inferred = 1;
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);
CREATE INDEX IF NOT EXISTS idx_spreads_date ON spreads(date);
CREATE INDEX IF NOT EXISTS idx_inferred_date ON inferred_prices(date);

-- 旧库迁移：为已存在的 rolls 补列（全新库由建表语句直接创建）
-- pragma table_info 无法在脚本里条件判断，重复加列会报错，因此由 db.init_db 用 Python 兜底。
