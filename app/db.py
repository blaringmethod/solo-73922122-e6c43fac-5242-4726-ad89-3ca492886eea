"""
连续合约结算价 —— 数据访问与计算层

核心模型
========
合约序列：初始合约 ord=0；第 k 次换月启用的合约 ord=k（k>=1），换月记录在 rolls(ord=k)。
某日 d 在用合约 = ord 满足：roll ord r 的 switch_date > d 的最小 r（即 roll r-1 已生效、r 未生效），
等价于 ord = count(rolls.switch_date <= d)，该 ord 即 rolls 中严格大于 d 的最小 ord 再减 1。

连续价（基准日 B）
==================
cont(d, B) = price(contract(d), d) + Σ { delta(r) | d < switch_date(r) <= B }
其中 delta(r) = price(new_r, pre_date_r) - price(old_r, pre_date_r)，
pre_date_r 为切换前一交易日；任一所需报价缺失则 delta 为 NULL。
缺价时连续价留空，并指出：
  - 当天原价缺失；或
  - 区间内最早一个 delta 为空的换月，以及该换月待补的合约报价。

复杂度保证
==========
- 换月价差以 Fenwick 树（bit_sums 表）维护：区间求和、单点更新均为 O(log n)，
  辅助记录数 ≤ n+1（n=换月次数），随换月次数对数级增长。
- 连续价不落地：单笔报价更新只写 prices 与至多一个 delta + Fenwick 单点，
  绝不逐日回写连续价。
- 单次取数在一个只读快照事务内完成（同一已提交状态），逐日用对数级查询。

事务语义
========
- 报价更新（新增/更正）在单个 IMMEDIABLE 事务内提交：原价与受影响价差一起生效，
  不会出现“价已改、价差未改”的中间状态。
- 保存失败时事务整体回滚，调用方仍可拿到本次计算的预期结果用于页面展示。
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from contextlib import contextmanager
from datetime import date as date_cls, datetime
from typing import Optional


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

ISO_FMT = "%Y-%m-%d"


def iso(d: date_cls) -> str:
    return d.strftime(ISO_FMT)


def parse_iso(s: str) -> date_cls:
    return datetime.strptime(s, ISO_FMT).date()


def valid_iso(s: str) -> bool:
    try:
        parse_iso(s)
        return True
    except (ValueError, TypeError):
        return False


class AppError(Exception):
    """面向用户的业务错误（中文消息可直接展示）。"""


# --------------------------------------------------------------------------- #
# 数据库连接
# --------------------------------------------------------------------------- #

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_db_path() -> str:
    return os.environ.get("CONT_DB_PATH", os.path.join(os.getcwd(), "data", "continuous.db"))


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    # 旧库迁移须先于建表脚本：脚本中的部分索引引用了 roll_inferred 列
    roll_cols = {r["name"] for r in conn.execute("PRAGMA table_info(rolls)")}
    if roll_cols and "roll_inferred" not in roll_cols:
        conn.execute("ALTER TABLE rolls ADD COLUMN roll_inferred INTEGER NOT NULL DEFAULT 0")
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    # 兜底：极端情况下 rolls 表由本脚本创建则列已存在；此处再校验一次
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rolls)")}
    if cols and "roll_inferred" not in cols:
        conn.execute("ALTER TABLE rolls ADD COLUMN roll_inferred INTEGER NOT NULL DEFAULT 0")


@contextmanager
def snapshot(conn: sqlite3.Connection):
    """
    只读快照事务：整次取数只看到同一个已提交状态。
    SQLite 默认 DEFERRED 事务的首条 SELECT 即获取读快照，事务结束前不变。
    """
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """立即写事务：保证“原价 + 受影响价差”原子提交，失败整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------- #
# 基础配置维护：交易日历 / 合约 / 换月计划
# --------------------------------------------------------------------------- #

def list_calendar(conn: sqlite3.Connection) -> list[str]:
    return [r["date"] for r in conn.execute("SELECT date FROM calendar ORDER BY date")]


def replace_calendar(conn: sqlite3.Connection, dates: list[str]) -> int:
    """全量替换交易日历（去重、校验格式、排序）。"""
    cleaned = sorted({d.strip() for d in dates if d and d.strip()})
    bad = [d for d in cleaned if not valid_iso(d)]
    if bad:
        raise AppError(f"交易日日期格式非法：{bad[0]}，应为 YYYY-MM-DD")
    with write_tx(conn):
        conn.execute("DELETE FROM calendar")
        conn.executemany("INSERT INTO calendar(date) VALUES (?)", [(d,) for d in cleaned])
    return len(cleaned)


def prev_trade_date(conn: sqlite3.Connection, day: str) -> Optional[str]:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM calendar WHERE date < ?", (day,)
    ).fetchone()
    return row["d"] if row and row["d"] else None


def is_trade_date(conn: sqlite3.Connection, day: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM calendar WHERE date = ?", (day,)
    ).fetchone() is not None


def list_contracts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT c.contract_id, c.code, c.ord, "
        "  (SELECT COUNT(*) FROM prices p WHERE p.contract_id = c.contract_id) AS price_count "
        "FROM contracts c ORDER BY c.ord"
    ))


def get_contract(conn: sqlite3.Connection, contract_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contracts WHERE contract_id = ?", (contract_id,)
    ).fetchone()


def contract_by_ord(conn: sqlite3.Connection, ord_: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM contracts WHERE ord = ?", (ord_,)
    ).fetchone()


def list_rolls(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT r.ord, r.switch_date, r.pre_date, r.new_contract_id, r.delta, "
        "       r.roll_inferred, nc.code AS new_code, oc.code AS old_code "
        "FROM rolls r "
        "JOIN contracts nc ON nc.contract_id = r.new_contract_id "
        "JOIN contracts oc ON oc.ord = r.ord - 1 "
        "ORDER BY r.ord"
    ))


def setup_plan(conn: sqlite3.Connection, initial_code: str,
               switches: list[tuple[str, str]]) -> None:
    """
    （重新）登记初始合约与固定换月计划。
    switches: 按时间顺序的 [(switch_date, new_contract_code), ...]
    仅允许在尚无任何报价/价差数据时整体重建，避免历史数据错配。
    """
    initial_code = (initial_code or "").strip()
    if not initial_code:
        raise AppError("初始合约代码不能为空")
    plan: list[tuple[str, str]] = []
    seen_dates: set[str] = set()
    prev = None
    for sd, code in switches:
        sd = (sd or "").strip()
        code = (code or "").strip()
        if not sd or not code:
            continue
        if not valid_iso(sd):
            raise AppError(f"换月切换日格式非法：{sd}")
        if sd in seen_dates:
            raise AppError(f"换月切换日重复：{sd}")
        if prev is not None and sd <= prev:
            raise AppError(f"换月切换日必须严格递增：{prev} 之后不能是 {sd}")
        if not is_trade_date(conn, sd):
            raise AppError(f"换月切换日 {sd} 不在交易日历中")
        seen_dates.add(sd)
        plan.append((sd, code))
        prev = sd

    with write_tx(conn):
        n_prices = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
        n_rolls = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
        n_spreads = conn.execute("SELECT COUNT(*) AS n FROM spreads").fetchone()["n"]
        if n_prices or n_rolls or n_spreads:
            raise AppError("已存在报价、换月或价差表数据，不能重建计划；如需调整请先清空数据")
        conn.execute("DELETE FROM inferred_prices")
        conn.execute("DELETE FROM spreads")
        conn.execute("DELETE FROM rolls")
        conn.execute("DELETE FROM contracts")
        conn.execute("DELETE FROM bit_sums")

        cur = conn.execute(
            "INSERT INTO contracts(code, ord) VALUES (?, 0)", (initial_code,)
        )
        init_id = cur.lastrowid
        conn.execute("UPDATE meta SET initial_contract_id = ? WHERE id = 1", (init_id,))
        for k, (sd, code) in enumerate(plan, start=1):
            cur = conn.execute(
                "INSERT INTO contracts(code, ord) VALUES (?, ?)", (code, k)
            )
            new_id = cur.lastrowid
            pre = prev_trade_date(conn, sd)
            if pre is None:
                raise AppError(f"切换日 {sd} 之前没有交易日，无法取换月价差")
            conn.execute(
                "INSERT INTO rolls(ord, switch_date, pre_date, new_contract_id, delta, "
                "             roll_inferred) "
                "VALUES (?, ?, ?, ?, NULL, 0)",
                (k, sd, pre, new_id),
            )


# --------------------------------------------------------------------------- #
# Fenwick 树：换月价差的对数级前缀和
# --------------------------------------------------------------------------- #

def _bit_add(conn: sqlite3.Connection, n: int, pos: int, v: int) -> None:
    i = pos
    while i <= n:
        row = conn.execute("SELECT val FROM bit_sums WHERE pos = ?", (i,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO bit_sums(pos, val) VALUES (?, ?)", (i, v)
            )
        else:
            conn.execute(
                "UPDATE bit_sums SET val = val + ? WHERE pos = ?", (v, i)
            )
        i += i & -i


def _bit_sum(conn: sqlite3.Connection, pos: int) -> int:
    total = 0
    i = pos
    while i > 0:
        row = conn.execute("SELECT val FROM bit_sums WHERE pos = ?", (i,)).fetchone()
        if row is not None:
            total += row["val"]
        i -= i & -i
    return total


def range_delta_sum(conn: sqlite3.Connection, lo_ord: int, hi_ord: int) -> int:
    """换月序号闭区间 [lo_ord, hi_ord] 内已知价差之和（调用前需确认区间内无 NULL）。"""
    if lo_ord > hi_ord:
        return 0
    return _bit_sum(conn, hi_ord) - _bit_sum(conn, lo_ord - 1)


# --------------------------------------------------------------------------- #
# 报价维护
# --------------------------------------------------------------------------- #

def get_price(conn: sqlite3.Connection, contract_id: int, day: str) -> Optional[int]:
    row = conn.execute(
        "SELECT price FROM prices WHERE contract_id = ? AND date = ?",
        (contract_id, day),
    ).fetchone()
    return row["price"] if row else None


def effective_price(conn: sqlite3.Connection, contract_id: int,
                    day: str) -> Optional[dict]:
    """
    某合约某日的可用结算价（单次主键点查 O(1)，均走 WITHOUT ROWID 主键索引）：
      - 有实报 → {"price": ..., "source": "actual"}（实报永远优先于推算价）
      - 无实报但有成立的推算价 → {"price": ..., "source": "inferred"}
      - 都没有 → None
    同值实报补录后，因 prices 优先命中，来源标记自动从 inferred 切回 actual。
    """
    row = conn.execute(
        "SELECT price FROM prices WHERE contract_id = ? AND date = ?",
        (contract_id, day),
    ).fetchone()
    if row is not None:
        return {"price": row["price"], "source": "actual"}
    row = conn.execute(
        "SELECT price FROM inferred_prices "
        "WHERE contract_id = ? AND date = ? AND status = 'ok'",
        (contract_id, day),
    ).fetchone()
    if row is not None:
        return {"price": row["price"], "source": "inferred"}
    return None


def list_prices(conn: sqlite3.Connection, contract_id: Optional[int] = None,
                limit: int = 500) -> list[sqlite3.Row]:
    if contract_id is None:
        return list(conn.execute(
            "SELECT p.contract_id, c.code, p.date, p.price FROM prices p "
            "JOIN contracts c ON c.contract_id = p.contract_id "
            "ORDER BY p.date DESC, c.ord LIMIT ?", (limit,)
        ))
    return list(conn.execute(
        "SELECT p.contract_id, c.code, p.date, p.price FROM prices p "
        "JOIN contracts c ON c.contract_id = p.contract_id "
        "WHERE p.contract_id = ? ORDER BY p.date DESC LIMIT ?",
        (contract_id, limit),
    ))


def _recompute_roll_delta(conn: sqlite3.Connection, ord_: int) -> Optional[int]:
    """
    依据当前已提交状态重算第 ord_ 次换月的价差并更新 rolls/bit_sums。
    取价口径：实报优先，实报缺失时可用当日成立的推算价（status=ok）。
    返回新的 delta（两类价都取不到时为 None），并维护 roll_inferred 标记。
    """
    roll = conn.execute("SELECT * FROM rolls WHERE ord = ?", (ord_,)).fetchone()
    if roll is None:
        return None
    n = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
    old = contract_by_ord(conn, ord_ - 1)
    new_id = roll["new_contract_id"]
    old_eff = effective_price(conn, old["contract_id"], roll["pre_date"])
    new_eff = effective_price(conn, new_id, roll["pre_date"])
    p_old = old_eff["price"] if old_eff else None
    p_new = new_eff["price"] if new_eff else None
    new_delta = (p_new - p_old) if (p_old is not None and p_new is not None) else None
    # 只要任一腿用了推算价，本次换月价差即引用推算价
    inferred = 1 if (new_delta is not None and
                     ((old_eff and old_eff["source"] == "inferred") or
                      (new_eff and new_eff["source"] == "inferred"))) else 0

    old_delta = roll["delta"]
    if new_delta != old_delta or inferred != roll["roll_inferred"]:
        conn.execute(
            "UPDATE rolls SET delta = ?, roll_inferred = ? WHERE ord = ?",
            (new_delta, inferred, ord_),
        )
        # Fenwick 只承载非空值：旧值撤下、新值加入，均为 O(log n)
        if old_delta is not None:
            _bit_add(conn, n, ord_, -old_delta)
        if new_delta is not None:
            _bit_add(conn, n, ord_, new_delta)
    return new_delta


def upsert_price(conn: sqlite3.Connection, contract_id: int, day: str,
                 price: int) -> dict:
    """
    新增/更正一笔整数分实报结算价。
    原子事务内完成（输入、推算结果、换月价差及其 Fenwick 索引一同生效）：
      1) prices 写入；
      2) 重算当天价差表推算价（只重算当天，不触碰其他交易日）；
      3) 仅重算“价差参考日 = 该日”的换月（至多 1 个，因为 pre_date 唯一），
         换月价差取价时实报优先、推算价兜底；
      4) 不触碰任何连续价、不逐日回写。
    返回预览信息（即使提交失败，调用方也可展示计算结果）。
    """
    if not valid_iso(day):
        raise AppError(f"日期格式非法：{day}，应为 YYYY-MM-DD")
    if not isinstance(price, int) or isinstance(price, bool):
        raise AppError("结算价必须是整数（单位：分）")
    if not is_trade_date(conn, day):
        raise AppError(f"{day} 不在交易日历中，不能登记报价")

    contract = get_contract(conn, contract_id)
    if contract is None:
        raise AppError(f"合约不存在：id={contract_id}")

    affected_roll = conn.execute(
        "SELECT ord FROM rolls WHERE pre_date = ?", (day,)
    ).fetchone()
    before = get_price(conn, contract_id, day)
    preview = {
        "contract": contract["code"], "date": day, "price": price,
        "old_price": before, "is_correction": before is not None,
        "affected_roll_ord": affected_roll["ord"] if affected_roll else None,
        "committed": False,
    }

    try:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO prices(contract_id, date, price) VALUES (?, ?, ?) "
                "ON CONFLICT(contract_id, date) DO UPDATE SET price = excluded.price",
                (contract_id, day, price),
            )
            # 当天推算价整体重算（撤下不成立的、补上新成立的）
            infer_summary = _recompute_day_inferred(conn, day)
            preview["inferred"] = infer_summary
            if affected_roll is not None:
                new_delta = _recompute_roll_delta(conn, affected_roll["ord"])
                preview["new_delta"] = new_delta
                roll_row = conn.execute(
                    "SELECT roll_inferred FROM rolls WHERE ord = ?",
                    (affected_roll["ord"],),
                ).fetchone()
                preview["roll_inferred"] = bool(roll_row["roll_inferred"])
        # 事务已由上下文管理器提交：实报、推算价、受影响价差同时生效
        preview["committed"] = True
    except sqlite3.Error as e:
        # 保存失败已整体回滚；预期结果仍随 preview 返回供页面展示
        preview["committed"] = False
        preview["error"] = str(e)
    return preview


def delete_price(conn: sqlite3.Connection, contract_id: int, day: str) -> dict:
    """
    撤下一笔实报（不存在时按幂等处理）。与 upsert_price 同一原子口径：
    当天推算价、受影响换月价差及 Fenwick 索引在一个事务内一起重算。
    """
    if not valid_iso(day):
        raise AppError(f"日期格式非法：{day}，应为 YYYY-MM-DD")
    contract = get_contract(conn, contract_id)
    if contract is None:
        raise AppError(f"合约不存在：id={contract_id}")
    affected_roll = conn.execute(
        "SELECT ord FROM rolls WHERE pre_date = ?", (day,)
    ).fetchone()
    preview = {"contract": contract["code"], "date": day, "committed": False}
    try:
        with write_tx(conn):
            conn.execute(
                "DELETE FROM prices WHERE contract_id = ? AND date = ?",
                (contract_id, day),
            )
            preview["inferred"] = _recompute_day_inferred(conn, day)
            if affected_roll is not None:
                preview["new_delta"] = _recompute_roll_delta(
                    conn, affected_roll["ord"]
                )
        preview["committed"] = True
    except sqlite3.Error as e:
        preview["committed"] = False
        preview["error"] = str(e)
    return preview


# --------------------------------------------------------------------------- #
# 价差表与缺报推算（按交易日）
# --------------------------------------------------------------------------- #
# 口径：
#   每行价差 spread = price(b) - price(a)。以“能经价差连起来的合约”为一个连通组，
#   用带权并查集维护相对价：w[x] = price(x) - price(parent[x])，find 后
#   price(x) - price(root) = weight_to_root[x]。
#   组内至少一笔实报、且全部实报与价差能同时成立（无矛盾），才能推算缺报合约；
#   无实报的组撤下推算价，有矛盾的组撤下推算价并记录矛盾原因；实报照常用。


class _WeightedDSU:
    """带权并查集（迭代路径压缩，避免深链递归）。w[x] = price(x) - price(parent[x])。"""

    def __init__(self):
        self.parent: dict[int, int] = {}
        self.w: dict[int, int] = {}

    def add(self, x: int) -> None:
        if x not in self.parent:
            self.parent[x] = x
            self.w[x] = 0

    def find(self, x: int) -> tuple[int, int]:
        """返回 (根, price(x) - price(root))。"""
        root = x
        acc = 0
        while self.parent[root] != root:
            acc += self.w[root]
            root = self.parent[root]
        # 路径压缩：把链上节点直接挂到根，权重改为到根的价差
        cur = x
        cur_acc = 0
        while self.parent[cur] != cur:
            nxt = self.parent[cur]
            edge = self.w[cur]
            self.parent[cur] = root
            self.w[cur] = acc - cur_acc
            cur_acc += edge
            cur = nxt
        return root, acc

    def union(self, a: int, b: int, diff: int) -> Optional[str]:
        """
        施加约束 price(b) - price(a) = diff。
        已连通且约束矛盾时返回中文原因，否则返回 None。
        """
        ra, wa = self.find(a)   # price(a) - price(ra) = wa
        rb, wb = self.find(b)   # price(b) - price(rb) = wb
        if ra == rb:
            if wb - wa != diff:
                return f"申报价差 {diff} 与已有关系 {wb - wa} 不一致"
            return None
        # 合并：把 ra 挂到 rb。由 price(b)-price(a)=diff：
        #   diff = (price(rb)+wb) - (price(ra)+wa)
        #   → price(ra)-price(rb) = wb - wa - diff = w[ra]
        self.parent[ra] = rb
        self.w[ra] = wb - wa - diff
        return None


def list_spread_dates(conn: sqlite3.Connection) -> list[str]:
    return [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM spreads ORDER BY date"
    )]


def list_spreads(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT s.date, s.contract_a, s.contract_b, s.spread, "
        "       ca.code AS code_a, cb.code AS code_b "
        "FROM spreads s "
        "JOIN contracts ca ON ca.contract_id = s.contract_a "
        "JOIN contracts cb ON cb.contract_id = s.contract_b "
        "WHERE s.date = ? ORDER BY ca.ord, cb.ord",
        (day,),
    ))


def list_inferred(conn: sqlite3.Connection, day: Optional[str] = None,
                  limit: int = 500) -> list[sqlite3.Row]:
    sql = (
        "SELECT i.contract_id, c.code, i.date, i.price, i.status, i.reason "
        "FROM inferred_prices i "
        "JOIN contracts c ON c.contract_id = i.contract_id "
    )
    args: tuple = ()
    if day is not None:
        sql += "WHERE i.date = ? "
        args = (day,)
    sql += "ORDER BY i.date DESC, c.ord LIMIT ?"
    return list(conn.execute(sql, (*args, limit)))


def save_spreads(conn: sqlite3.Connection, day: str,
                 rows: list[tuple[str, str, int]]) -> dict:
    """
    按交易日全量保存（替换）价差表；rows 为 [(code_a, code_b, spread), ...]。
    清空语义：rows 为空即撤下该日整张表。
    单个 IMMEDIATE 事务内完成：价差表替换 → 当天推算重算 → 受影响换月重算
    （至多 1 次换月，pre_date 唯一）→ Fenwick 索引更新，全部一同生效或整体回滚。
    """
    if not valid_iso(day):
        raise AppError(f"日期格式非法：{day}，应为 YYYY-MM-DD")
    if not is_trade_date(conn, day):
        raise AppError(f"{day} 不在交易日历中，不能保存价差表")

    # 输入校验（事务外先做，错误信息可直接展示）
    contract_ids = {r["code"]: r["contract_id"] for r in list_contracts(conn)}
    parsed: list[tuple[int, int, int, str, str]] = []
    seen: set[tuple[int, int]] = set()
    for code_a, code_b, spread in rows:
        code_a = (code_a or "").strip()
        code_b = (code_b or "").strip()
        if not code_a or not code_b:
            continue
        if code_a not in contract_ids:
            raise AppError(f"价差表合约未登记：{code_a}")
        if code_b not in contract_ids:
            raise AppError(f"价差表合约未登记：{code_b}")
        if code_a == code_b:
            raise AppError(f"价差行两个合约必须不同：{code_a}")
        if not isinstance(spread, int) or isinstance(spread, bool):
            raise AppError("价差必须是整数（单位：分，后者减前者）")
        a, b = contract_ids[code_a], contract_ids[code_b]
        if (a, b) in seen:
            raise AppError(f"价差表存在重复合约对：{code_a} → {code_b}")
        seen.add((a, b))
        parsed.append((a, b, spread, code_a, code_b))

    affected_roll = conn.execute(
        "SELECT ord FROM rolls WHERE pre_date = ?", (day,)
    ).fetchone()
    preview = {"date": day, "rows": len(parsed), "committed": False}
    try:
        with write_tx(conn):
            conn.execute("DELETE FROM spreads WHERE date = ?", (day,))
            conn.executemany(
                "INSERT INTO spreads(date, contract_a, contract_b, spread) "
                "VALUES (?, ?, ?, ?)",
                [(day, a, b, s) for a, b, s, _, _ in parsed],
            )
            preview["inferred"] = _recompute_day_inferred(conn, day)
            if affected_roll is not None:
                preview["new_delta"] = _recompute_roll_delta(
                    conn, affected_roll["ord"]
                )
        preview["committed"] = True
    except sqlite3.Error as e:
        preview["committed"] = False
        preview["error"] = str(e)
    return preview


def _recompute_day_inferred(conn: sqlite3.Connection, day: str) -> dict:
    """
    依据当天 spreads + 全部实报 prices 重算 inferred_prices（只针对该交易日）。
    必须在写事务内调用。返回统计：
      {"ok": 推算成立条数, "no_anchor": 无实报组数, "conflict": 矛盾组数,
       "dropped": 被撤下的旧推算条数, "reasons": [可展示原因, ...]}
    """
    # 1) 取当天价差边，建带权并查集，边本身的矛盾先记录
    edges = conn.execute(
        "SELECT contract_a, contract_b, spread FROM spreads WHERE date = ?",
        (day,),
    ).fetchall()
    dsu = _WeightedDSU()
    edge_conflict_roots: set[int] = set()
    edge_conflict_msgs: list[str] = []
    code_of = {r["contract_id"]: r["code"] for r in list_contracts(conn)}
    for e in edges:
        a, b, diff = e["contract_a"], e["contract_b"], e["spread"]
        dsu.add(a)
        dsu.add(b)
        bad = dsu.union(a, b, diff)
        if bad is not None:
            # a、b 此时已在同一连通组，矛盾归属该组
            root, _ = dsu.find(a)
            edge_conflict_roots.add(root)
            edge_conflict_msgs.append(
                f"{code_of.get(a, a)} 与 {code_of.get(b, b)}："
                f"申报价差 {diff} 分与已有价差关系矛盾"
            )

    # 2) 取当天所有实报，按并查集根分组；实报与价差/实报之间的矛盾也记录
    actuals = conn.execute(
        "SELECT p.contract_id, p.price FROM prices p WHERE p.date = ?", (day,)
    ).fetchall()
    root_anchor: dict[int, tuple[int, int]] = {}   # root -> (contract_id, root_price)
    anchor_conflicts: dict[int, str] = {}
    for r in actuals:
        cid, px = r["contract_id"], r["price"]
        dsu.add(cid)
        root, off = dsu.find(cid)   # price(cid) - price(root) = off
        implied_root = px - off     # 若该组有锚点，根价应一致
        if root not in root_anchor:
            root_anchor[root] = (cid, implied_root)
        elif root_anchor[root][1] != implied_root:
            anchor_conflicts[root] = (
                f"实报矛盾：合约 {code_of.get(cid, cid)} 结算价 {px} 分与同组实报/价差"
                f"不能同时成立"
            )

    # 3) 旧推算行先撤下，随后按新结论重写（只动当天）
    dropped = conn.execute(
        "DELETE FROM inferred_prices WHERE date = ?", (day,)
    ).rowcount

    n_ok = 0
    n_no_anchor = 0
    n_conflict = 0
    reasons: list[str] = []
    reasons.extend(edge_conflict_msgs)
    reasons.extend(anchor_conflicts.values())

    # 3a) 价差组（出现在边里的节点）：有锚且无矛盾才推算，其余撤下并说明
    members: dict[int, list[int]] = {}
    for x in dsu.parent:
        root, _ = dsu.find(x)
        members.setdefault(root, []).append(x)
    edge_nodes = {x for e in edges for x in (e["contract_a"], e["contract_b"])}
    for root, nodes in members.items():
        if not any(x in edge_nodes for x in nodes):
            continue  # 孤立实报节点不在任何价差组内，不产生推算行
        is_conflict = root in anchor_conflicts or root in edge_conflict_roots
        if is_conflict:
            n_conflict += 1
            detail_parts = []
            if root in edge_conflict_roots:
                detail_parts.append("价差表内部关系矛盾")
            if root in anchor_conflicts:
                detail_parts.append(anchor_conflicts[root])
            reason = "价差组有矛盾，已撤下推算价（" + "；".join(detail_parts) + "）"
            reasons.append(reason)
            conn.executemany(
                "INSERT INTO inferred_prices(contract_id, date, price, status, reason) "
                "VALUES (?, ?, NULL, 'conflict', ?)",
                [(cid, day, reason) for cid in nodes],
            )
        elif root not in root_anchor:
            n_no_anchor += 1
            reason = "价差组内没有任何实报结算价，无法推算，已撤下推算价"
            reasons.append(reason)
            conn.executemany(
                "INSERT INTO inferred_prices(contract_id, date, price, status, reason) "
                "VALUES (?, ?, NULL, 'no_anchor', ?)",
                [(cid, day, reason) for cid in nodes],
            )
        else:
            _, root_price = root_anchor[root]
            for cid in nodes:
                _, off = dsu.find(cid)        # price(cid) = root_price + off
                inferred_px = root_price + off
                if get_price(conn, cid, day) is not None:
                    continue                  # 实报合约照用实报，不写推算行
                conn.execute(
                    "INSERT INTO inferred_prices(contract_id, date, price, status, reason) "
                    "VALUES (?, ?, ?, 'ok', '')",
                    (cid, day, inferred_px),
                )
                n_ok += 1

    return {
        "ok": n_ok,
        "no_anchor": n_no_anchor,
        "conflict": n_conflict,
        "dropped": dropped,
        "reasons": reasons,
    }


# --------------------------------------------------------------------------- #
# 取数：逐日连续价
# --------------------------------------------------------------------------- #

def active_contract_ord(conn: sqlite3.Connection, day: str) -> int:
    """
    当日在用合约的 ord：切换日 <= day 的换月次数。
    走 switch_date 唯一索引的反向定位（LIMIT 1），单次 O(log n)，
    不做区间计数扫描；因 ord 稠密且与切换日同序，取到的最大 ord 即次数。
    """
    row = conn.execute(
        "SELECT ord FROM rolls WHERE switch_date <= ? "
        "ORDER BY switch_date DESC LIMIT 1",
        (day,),
    ).fetchone()
    return row["ord"] if row else 0


def _first_missing_roll(conn: sqlite3.Connection, lo_ord: int,
                        hi_ord: int) -> Optional[sqlite3.Row]:
    """区间 [lo_ord, hi_ord] 内最早一个缺价差的换月。"""
    if lo_ord > hi_ord:
        return None
    return conn.execute(
        "SELECT r.*, nc.code AS new_code, oc.code AS old_code "
        "FROM rolls r "
        "JOIN contracts nc ON nc.contract_id = r.new_contract_id "
        "JOIN contracts oc ON oc.ord = r.ord - 1 "
        "WHERE r.ord BETWEEN ? AND ? AND r.delta IS NULL "
        "ORDER BY r.ord LIMIT 1",
        (lo_ord, hi_ord),
    ).fetchone()


def _missing_quote_contracts(conn: sqlite3.Connection,
                             roll: sqlite3.Row) -> list[sqlite3.Row]:
    """某换月价差缺失时，具体缺 pre_date 上哪个合约的可用价（实报/推算均无）。"""
    missing = []
    old_c = contract_by_ord(conn, roll["ord"] - 1)
    new_c = get_contract(conn, roll["new_contract_id"])
    for c in (old_c, new_c):
        if effective_price(conn, c["contract_id"], roll["pre_date"]) is None:
            missing.append(c)
    return missing


def fetch_range(conn: sqlite3.Connection, start: str, end: str,
                base: str) -> list[dict]:
    """
    取 [start, end] 交易日的连续价，基准日 base（不早于 end）。
    整个过程在一个只读快照事务中执行，看到的是同一已提交状态。
    逐日查询：在用合约定位 O(log n)（唯一索引反向定位）、区间价差和 O(log n)（Fenwick）。
    """
    for label, v in (("开始日", start), ("结束日", end), ("基准日", base)):
        if not valid_iso(v):
            raise AppError(f"{label}格式非法：{v}，应为 YYYY-MM-DD")
    if start > end:
        raise AppError("开始日不能晚于结束日")
    if base < end:
        raise AppError(f"基准日 {base} 不能早于结束日 {end}")

    rows: list[dict] = []
    with snapshot(conn):
        # 所有读取均在同一只读快照内：整次取数只看到一个已提交状态
        base_row = conn.execute(
            "SELECT ord FROM rolls WHERE switch_date <= ? "
            "ORDER BY switch_date DESC LIMIT 1",
            (base,),
        ).fetchone()
        base_roll_hi = base_row["ord"] if base_row else 0
        days = [r["date"] for r in conn.execute(
            "SELECT date FROM calendar WHERE date BETWEEN ? AND ? ORDER BY date",
            (start, end),
        )]
        if not days:
            raise AppError(f"{start} 至 {end} 之间没有交易日")

        for day in days:
            ord_ = active_contract_ord(conn, day)
            contract = contract_by_ord(conn, ord_)
            # 实报优先，缺失时取当天成立的推算价（同一已提交快照内）
            eff = effective_price(conn, contract["contract_id"], day)
            raw = eff["price"] if eff else None
            raw_source = eff["source"] if eff else None

            # 该日之后至基准日（含）的换月：switch_date in (day, base] → ord in (ord_, base_roll_hi]
            lo, hi = ord_ + 1, base_roll_hi
            miss_roll = _first_missing_roll(conn, lo, hi)

            row = {
                "date": day,
                "contract": contract["code"],
                "raw_price": raw,
                "raw_source": raw_source,        # actual / inferred / None
                "continuous": None,
                "adjustment": None,
                "roll_inferred": False,          # 换月调整是否引用了推算价
                "status": "ok",
                "message": "",
            }
            if raw is None:
                row["status"] = "missing_raw"
                # 若该合约在当天价差组内但推算不成立，附上撤下原因（无实报/矛盾）
                inf = conn.execute(
                    "SELECT status, reason FROM inferred_prices "
                    "WHERE contract_id = ? AND date = ?",
                    (contract["contract_id"], day),
                ).fetchone()
                reason_tail = f"；{inf['reason']}" if inf is not None and inf["reason"] else ""
                row["message"] = (
                    f"缺少原价：合约 {contract['code']} 在 {day} 的结算价待补{reason_tail}"
                )
            elif miss_roll is not None:
                row["status"] = "missing_roll"
                miss_cs = _missing_quote_contracts(conn, miss_roll)
                names = "、".join(
                    f"{c['code']}({miss_roll['pre_date']})" for c in miss_cs
                ) or "价差所需报价"
                row["message"] = (
                    f"换月 {miss_roll['old_code']} → {miss_roll['new_code']} "
                    f"（切换日 {miss_roll['switch_date']}）缺价差，待补报价：{names}"
                )
            else:
                adj = range_delta_sum(conn, lo, hi)
                row["continuous"] = raw + adj
                row["adjustment"] = adj
                # 区间内是否有任一换月的价差引用了推算价（部分索引，至多 O(log n) 次索引探测）
                if lo <= hi:
                    flag = conn.execute(
                        "SELECT 1 FROM rolls WHERE ord BETWEEN ? AND ? "
                        "AND roll_inferred = 1 LIMIT 1",
                        (lo, hi),
                    ).fetchone()
                    row["roll_inferred"] = flag is not None
                notes = []
                if raw_source == "inferred":
                    notes.append("当日结算价为推算价")
                if row["roll_inferred"]:
                    notes.append("换月调整含推算价")
                row["message"] = "；".join(notes)
            rows.append(row)
    return rows


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["交易日", "在用合约", "原价(分)", "原价来源", "换月调整(分)",
                     "换月调整含推算价", "连续价(分)", "状态", "说明"])
    status_cn = {"ok": "正常", "missing_raw": "缺原价", "missing_roll": "缺换月报价"}
    source_cn = {"actual": "实报", "inferred": "推算价"}
    for r in rows:
        writer.writerow([
            r["date"], r["contract"],
            "" if r["raw_price"] is None else r["raw_price"],
            source_cn.get(r.get("raw_source"), ""),
            "" if r.get("adjustment") is None else r["adjustment"],
            "是" if r.get("roll_inferred") else ("否" if r["status"] == "ok" else ""),
            "" if r["continuous"] is None else r["continuous"],
            status_cn.get(r["status"], r["status"]),
            r["message"],
        ])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 状态总览
# --------------------------------------------------------------------------- #

def overview(conn: sqlite3.Connection) -> dict:
    n_contracts = conn.execute("SELECT COUNT(*) AS n FROM contracts").fetchone()["n"]
    n_rolls = conn.execute("SELECT COUNT(*) AS n FROM rolls").fetchone()["n"]
    n_missing_delta = conn.execute(
        "SELECT COUNT(*) AS n FROM rolls WHERE delta IS NULL"
    ).fetchone()["n"]
    n_roll_inferred = conn.execute(
        "SELECT COUNT(*) AS n FROM rolls WHERE roll_inferred = 1"
    ).fetchone()["n"]
    n_prices = conn.execute("SELECT COUNT(*) AS n FROM prices").fetchone()["n"]
    n_spread_days = conn.execute(
        "SELECT COUNT(DISTINCT date) AS n FROM spreads"
    ).fetchone()["n"]
    n_spread_rows = conn.execute("SELECT COUNT(*) AS n FROM spreads").fetchone()["n"]
    n_inferred_ok = conn.execute(
        "SELECT COUNT(*) AS n FROM inferred_prices WHERE status = 'ok'"
    ).fetchone()["n"]
    n_days = conn.execute("SELECT COUNT(*) AS n FROM calendar").fetchone()["n"]
    cal = list_calendar(conn)
    return {
        "n_contracts": n_contracts,
        "n_rolls": n_rolls,
        "n_missing_delta": n_missing_delta,
        "n_roll_inferred": n_roll_inferred,
        "n_prices": n_prices,
        "n_spread_days": n_spread_days,
        "n_spread_rows": n_spread_rows,
        "n_inferred_ok": n_inferred_ok,
        "n_days": n_days,
        "cal_min": cal[0] if cal else None,
        "cal_max": cal[-1] if cal else None,
    }
