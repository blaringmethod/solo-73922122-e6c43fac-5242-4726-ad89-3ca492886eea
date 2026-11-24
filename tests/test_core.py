"""
核心逻辑测试：
  连续价口径、缺价提示、改价联动、原子提交、快照隔离、Fenwick 对数复杂度、CSV、Web 冒烟。
运行：PYTHONPATH=. python -m unittest discover -s tests -v
"""
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta

from app import db


CAL = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
       "2026-01-09", "2026-01-12", "2026-01-13"]

# (合约, 日期, 价格)  单位：分
PRICES = [
    ("C0", "2026-01-05", 1000), ("C0", "2026-01-06", 1010),
    ("C0", "2026-01-07", 1020),
    ("C1", "2026-01-07", 1030), ("C1", "2026-01-08", 1040),
    ("C1", "2026-01-09", 1050),
    ("C2", "2026-01-09", 1075), ("C2", "2026-01-12", 1080),
    ("C2", "2026-01-13", 1090),
]


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)
        db.replace_calendar(self.conn, CAL)
        db.setup_plan(self.conn, "C0",
                      [("2026-01-08", "C1"), ("2026-01-12", "C2")])
        cid = {r["code"]: r["contract_id"] for r in db.list_contracts(self.conn)}
        for code, day, px in PRICES:
            db.upsert_price(self.conn, cid[code], day, px)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def fetch(self, start, end, base):
        return db.fetch_range(self.conn, start, end, base)


class TestContinuousPrice(BaseCase):
    def test_delta_values(self):
        rolls = {r["ord"]: r["delta"] for r in db.list_rolls(self.conn)}
        self.assertEqual(rolls[1], 1030 - 1020)   # +10
        self.assertEqual(rolls[2], 1075 - 1050)   # +25

    def test_continuous_full_range_base_latest(self):
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        got = {r["date"]: r["continuous"] for r in rows}
        expected = {
            "2026-01-05": 1000 + 10 + 25,   # 两次换月都在该日之后
            "2026-01-06": 1010 + 10 + 25,
            "2026-01-07": 1020 + 10 + 25,
            "2026-01-08": 1040 + 25,        # roll1 当日已生效，只加 roll2
            "2026-01-09": 1050 + 25,
            "2026-01-12": 1080,             # 基准日前最后一次换月当日起不再调整
            "2026-01-13": 1090,
        }
        self.assertEqual(got, expected)
        self.assertTrue(all(r["status"] == "ok" for r in rows))

    def test_continuous_base_inside_range(self):
        # 基准日 = 结束日 01-09：roll2(01-12) 在基准日之后，不计入
        rows = self.fetch("2026-01-05", "2026-01-09", "2026-01-09")
        got = {r["date"]: r["continuous"] for r in rows}
        self.assertEqual(got["2026-01-05"], 1010)   # 1000 + 10
        self.assertEqual(got["2026-01-08"], 1040)
        self.assertEqual(got["2026-01-09"], 1050)

    def test_base_before_end_rejected(self):
        with self.assertRaises(db.AppError):
            self.fetch("2026-01-05", "2026-01-13", "2026-01-08")

    def test_missing_raw_price(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-06'",
                          (cid,))
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        row = next(r for r in rows if r["date"] == "2026-01-06")
        self.assertIsNone(row["continuous"])
        self.assertEqual(row["status"], "missing_raw")
        self.assertIn("C0", row["message"])
        self.assertIn("2026-01-06", row["message"])

    def test_missing_roll_quote_points_earliest_and_contracts(self):
        # 删掉 roll1 参考日(01-07) 的新合约报价 → delta1 置空
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-07'",
                          (cid,))
        # 直接重算 roll1 模拟该报价缺失下的状态
        with db.write_tx(self.conn):
            db._recompute_roll_delta(self.conn, 1)
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        early = next(r for r in rows if r["date"] == "2026-01-05")
        self.assertIsNone(early["continuous"])
        self.assertEqual(early["status"], "missing_roll")
        # 指出最早缺价换月（ord=1）及待补的具体合约报价
        self.assertIn("C0 → C1", early["message"])
        self.assertIn("C1(2026-01-07)", early["message"])
        # roll1 之后的日子不受该换月缺失影响（01-12 仍正常）
        last = next(r for r in rows if r["date"] == "2026-01-12")
        self.assertEqual(last["continuous"], 1080)

    def test_correction_recomputes_delta_and_continuous(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        preview = db.upsert_price(self.conn, cid, "2026-01-07", 1035)  # 1030 → 1035
        self.assertTrue(preview["committed"])
        self.assertTrue(preview["is_correction"])
        self.assertEqual(preview["new_delta"], 15)
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        self.assertEqual(rows[0]["continuous"], 1000 + 15 + 25)
        # 连续价不落地：库中不存在任何连续价表/列
        tables = {r["name"] for r in
                  self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("continuous", tables)

    def test_non_trade_date_rejected(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        with self.assertRaises(db.AppError):
            db.upsert_price(self.conn, cid, "2026-01-10", 999)  # 周六，非交易日

    def test_integer_price_required(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        with self.assertRaises(db.AppError):
            db.upsert_price(self.conn, cid, "2026-01-05", 1000.5)


class TestAtomicityAndSnapshot(BaseCase):
    def test_save_failure_rolls_back_but_returns_preview(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        before = db.get_price(self.conn, cid, "2026-01-07")

        # 让 Fenwick 更新在事务中途失败（模拟保存失败）
        orig = db._bit_add
        def boom(conn, n, pos, v):
            raise sqlite3.OperationalError("simulated disk failure")
        db._bit_add = boom
        try:
            preview = db.upsert_price(self.conn, cid, "2026-01-07", 1111)
        finally:
            db._bit_add = orig

        self.assertFalse(preview["committed"])          # 仍返回预期结果
        self.assertEqual(preview["price"], 1111)
        self.assertEqual(db.get_price(self.conn, cid, "2026-01-07"), before)  # 已回滚
        roll = self.conn.execute("SELECT delta FROM rolls WHERE ord=1").fetchone()
        self.assertEqual(roll["delta"], 10)             # 价差也保持旧值

    def test_snapshot_sees_one_committed_state(self):
        other = db.connect(self.db_path)
        cid = next(r["contract_id"] for r in db.list_contracts(other)
                   if r["code"] == "C0")
        with db.snapshot(self.conn):
            seen_before = db.get_price(self.conn, cid, "2026-01-05")
            db.upsert_price(other, cid, "2026-01-05", 2222)   # 另一连接已提交
            seen_in_snap = db.get_price(self.conn, cid, "2026-01-05")
        seen_after = db.get_price(self.conn, cid, "2026-01-05")
        self.assertEqual(seen_before, 1000)
        self.assertEqual(seen_in_snap, 1000)   # 快照内不被外部提交影响
        self.assertEqual(seen_after, 2222)
        other.close()


class TestFenwickLogGrowth(unittest.TestCase):
    def _build(self, n_rolls):
        tmp = tempfile.TemporaryDirectory()
        conn = db.connect(os.path.join(tmp.name, "b.db"))
        db.init_db(conn)
        # 跨月生成连续自然日，避免硬编码月份天数
        start = date(2026, 1, 5)
        days = [(start + timedelta(days=i)).isoformat()
                for i in range(2 + (n_rolls + 1) * 2)]
        db.replace_calendar(conn, days)
        switches = [(days[2 * k + 1], f"K{k+1}") for k in range(n_rolls)]
        db.setup_plan(conn, "K0", switches)
        return tmp, conn, days

    def test_matches_brute_force_and_aux_size(self):
        n = 20
        tmp, conn, days = self._build(n)
        try:
            cid = {r["code"]: r["contract_id"] for r in db.list_contracts(conn)}
            # 每个交易日给所有合约报价，价差取确定性序列
            for k in range(n + 1):
                for di, day in enumerate(days):
                    db.upsert_price(conn, cid[f"K{k}"], day, 100 + k * 7 + di)
            # 辅助表行数 ≤ 换月次数（稀疏 Fenwick，上界 n）
            bit_rows = conn.execute("SELECT COUNT(*) AS c FROM bit_sums").fetchone()["c"]
            self.assertLessEqual(bit_rows, n)

            rows = db.fetch_range(conn, days[0], days[-1], days[-1])
            # 暴力重算对照
            rolls = db.list_rolls(conn)
            for r in rows:
                day = r["date"]
                ord_ = db.active_contract_ord(conn, day)
                code = db.contract_by_ord(conn, ord_)["code"]
                raw = db.get_price(conn, cid[code], day)
                s = 0
                for roll in rolls:
                    if day < roll["switch_date"] <= days[-1]:
                        s += roll["delta"]
                self.assertEqual(r["continuous"], raw + s, msg=day)
        finally:
            conn.close()
            tmp.cleanup()

    def test_query_cost_is_log_steps(self):
        # Fenwick 前缀和的节点访问数 = 二进制置位数 ≤ log2(n)+1
        n = 64
        tmp, conn, days = self._build(n)
        try:
            # 填入报价使所有 delta 非空、Fenwick 节点被实际写入
            cid = {r["code"]: r["contract_id"] for r in db.list_contracts(conn)}
            for k in range(n + 1):
                for di, day in enumerate(days):
                    db.upsert_price(conn, cid[f"K{k}"], day, 100 + k * 7 + di)

            steps = 0
            orig_bit_sum = db._bit_sum

            def counting_bit_sum(c, pos):
                nonlocal steps
                steps += 1
                return orig_bit_sum(c, pos)

            db._bit_sum = counting_bit_sum
            try:
                db.range_delta_sum(conn, 1, n)  # 两个前缀和，各 O(log n)
            finally:
                db._bit_sum = orig_bit_sum
            self.assertLessEqual(steps, 2 * (n.bit_length()))
        finally:
            conn.close()
            tmp.cleanup()

    def test_inferred_marker_index_scans_log_bounded(self):
        # 单日查询读取的换月/索引记录数随换月次数至多对数增长：
        # 换月定位/区间和均为 O(log n)；推算标记探测走索引（部分索引或 ord 主键），
        # 不做全表扫描，且 LIMIT 1 命中即停。
        n = 64
        tmp, conn, days = self._build(n)
        try:
            cid = {r["code"]: r["contract_id"] for r in db.list_contracts(conn)}
            for k in range(n + 1):
                for di, day in enumerate(days):
                    db.upsert_price(conn, cid[f"K{k}"], day, 100 + k * 7 + di)
            # 让首个换月的价差改为引用推算价
            first_pre = conn.execute(
                "SELECT pre_date FROM rolls WHERE ord=1").fetchone()["pre_date"]
            old_c = db.contract_by_ord(conn, 0)
            new_c = db.contract_by_ord(conn, 1)
            conn.execute("DELETE FROM prices WHERE contract_id=? AND date=?",
                         (new_c["contract_id"], first_pre))
            db.save_spreads(conn, first_pre, [(old_c["code"], new_c["code"], 7)])
            self.assertEqual(
                conn.execute("SELECT roll_inferred FROM rolls WHERE ord=1")
                .fetchone()["roll_inferred"], 1)

            # 单日查询的标记探测：EXPLAIN 确认走 ord 主键索引而非全表扫描
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM rolls "
                "WHERE ord BETWEEN 1 AND ? AND roll_inferred=1 LIMIT 1",
                (n,),
            ).fetchall()
            detail = " ".join(r["detail"] for r in plan).upper()
            self.assertIn("INTEGER PRIMARY KEY", detail)
            self.assertNotIn("SCAN", detail)

            # 按参考日找“引用推算价的换月”走专门的部分索引
            plan2 = conn.execute(
                "EXPLAIN QUERY PLAN SELECT ord FROM rolls "
                "WHERE pre_date=? AND roll_inferred=1",
                (first_pre,),
            ).fetchall()
            detail2 = " ".join(r["detail"] for r in plan2)
            self.assertIn("idx_rolls_pre_inferred", detail2)

            # 端到端：大区间取数仍逐日成功，且标记随换月区间正确给出
            rows = db.fetch_range(conn, days[0], days[-1], days[-1])
            flagged = [r for r in rows if r["roll_inferred"]]
            self.assertTrue(flagged)  # roll1 生效前的日子都应被标记
            # roll1 切换日之后不再包含该换月，标记应消失
            switch1 = conn.execute(
                "SELECT switch_date FROM rolls WHERE ord=1").fetchone()["switch_date"]
            self.assertFalse(any(r["date"] >= switch1 and r["roll_inferred"]
                                 for r in rows))
        finally:
            conn.close()
            tmp.cleanup()


class TestPlanValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "v.db"))
        db.init_db(self.conn)
        db.replace_calendar(self.conn, CAL)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_switch_must_be_trade_day(self):
        with self.assertRaises(db.AppError):
            db.setup_plan(self.conn, "C0", [("2026-01-10", "C1")])

    def test_switch_dates_strictly_increasing(self):
        with self.assertRaises(db.AppError):
            db.setup_plan(self.conn, "C0",
                          [("2026-01-08", "C1"), ("2026-01-08", "C2")])

    def test_rebuild_blocked_after_prices(self):
        db.setup_plan(self.conn, "C0", [("2026-01-08", "C1")])
        cid = db.list_contracts(self.conn)[0]["contract_id"]
        db.upsert_price(self.conn, cid, "2026-01-05", 100)
        with self.assertRaises(db.AppError):
            db.setup_plan(self.conn, "C0", [("2026-01-09", "X1")])


class TestSpreadInference(BaseCase):
    """价差表补数、分组核对、推算/撤下、换月联动、同值实报替换标记。"""

    def _ids(self):
        return {r["code"]: r["contract_id"] for r in db.list_contracts(self.conn)}

    def _inferred_map(self, day):
        return {r["code"]: (r["price"], r["status"], r["reason"])
                for r in db.list_inferred(self.conn, day)}

    def test_chain_inference_from_one_actual(self):
        # 01-06 只有 C0 实报；价差 C1-C0=15、C2-C1=25 → C1=1025, C2=1050
        cid = self._ids()
        prev = db.save_spreads(self.conn, "2026-01-06", [
            ("C0", "C1", 15), ("C1", "C2", 25)])
        self.assertTrue(prev["committed"])
        inf = self._inferred_map("2026-01-06")
        self.assertEqual(inf["C1"][:2], (1025, "ok"))
        self.assertEqual(inf["C2"][:2], (1050, "ok"))
        self.assertNotIn("C0", inf)  # 实报合约不写推算行
        # 取数：01-06 在用合约为 C0（实报），来源 actual
        rows = self.fetch("2026-01-06", "2026-01-06", "2026-01-13")
        self.assertEqual(rows[0]["raw_source"], "actual")
        self.assertEqual(rows[0]["continuous"], 1010 + 10 + 25)

    def test_inferred_raw_price_feeds_continuous_series(self):
        # 删掉 C0 在 01-06 的实报，改由价差推算：C1-C0=15（C1 当天无报价，
        # 再借 C2-C1=25 与 C2?  C2 当天也无报价）—— 需要组内至少一笔实报。
        cid = self._ids()
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-06'",
                          (cid["C0"],))
        # 给 C2 在 01-06 一笔实报 1060，链 C0->C1(15)->C2(25)
        db.upsert_price(self.conn, cid["C2"], "2026-01-06", 1060)
        db.save_spreads(self.conn, "2026-01-06", [
            ("C0", "C1", 15), ("C1", "C2", 25)])
        inf = self._inferred_map("2026-01-06")
        # C1 = 1060-25 = 1035；C0 = 1035-15 = 1020
        self.assertEqual(inf["C0"][:2], (1020, "ok"))
        self.assertEqual(inf["C1"][:2], (1035, "ok"))
        rows = self.fetch("2026-01-06", "2026-01-06", "2026-01-13")
        row = rows[0]
        self.assertEqual(row["contract"], "C0")
        self.assertEqual(row["raw_source"], "inferred")
        self.assertEqual(row["raw_price"], 1020)
        self.assertEqual(row["continuous"], 1020 + 10 + 25)
        self.assertIn("推算价", row["message"])

    def test_no_anchor_group_drops_inference_and_keeps_actuals(self):
        # 01-06 保留 C0 实报；另起一组 C1-C2=25 但 C1/C2 当天均无实报
        prev = db.save_spreads(self.conn, "2026-01-06", [("C1", "C2", 25)])
        self.assertTrue(prev["committed"])
        inf = self._inferred_map("2026-01-06")
        self.assertEqual(inf["C1"][1], "no_anchor")
        self.assertEqual(inf["C2"][1], "no_anchor")
        self.assertIsNone(inf["C1"][0])
        # C0 实报照常供数
        self.assertEqual(db.effective_price(self.conn,
                         next(iter(self._ids().values())), "2026-01-06")["source"],
                         "actual")
        # 无可用推算价时 effective_price 为 None，连续价缺价提示保留
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        # 01-06 在用 C0 有实报，仍正常；检查 no_anchor 原因可在 inferred 表读到
        self.assertTrue(inf["C1"][2])

    def test_conflicting_spreads_drop_group_with_reason(self):
        cid = self._ids()
        # C0 实报 1010；同对合约重复填报在保存校验阶段即被拒
        with self.assertRaises(db.AppError):
            db.save_spreads(self.conn, "2026-01-06", [
                ("C0", "C1", 15), ("C0", "C1", 99)])
        # 经不同路径的矛盾：C0->C1=15, C0->C2=40, C1->C2=30（一致应为 25）
        prev = db.save_spreads(self.conn, "2026-01-06", [
            ("C0", "C1", 15), ("C0", "C2", 40), ("C1", "C2", 30)])
        # 改成经不同路径的矛盾：C0->C1=15, C0->C2=40, C1->C2=25（应为 25 才一致，给 30）
        prev = db.save_spreads(self.conn, "2026-01-06", [
            ("C0", "C1", 15), ("C0", "C2", 40), ("C1", "C2", 30)])
        self.assertTrue(prev["committed"])
        inf = self._inferred_map("2026-01-06")
        self.assertEqual(inf["C1"][1], "conflict")
        self.assertEqual(inf["C2"][1], "conflict")
        self.assertIsNone(inf["C1"][0])
        self.assertIn("矛盾", inf["C1"][2])
        # 矛盾组的推算价不可供数
        self.assertIsNone(db.effective_price(self.conn, cid["C1"], "2026-01-06"))
        # 实报仍照用
        self.assertEqual(db.get_price(self.conn, cid["C0"], "2026-01-06"), 1010)

    def test_actual_conflicting_with_spread_drops_group(self):
        cid = self._ids()
        # 01-06：C0=1010 实报，新增 C2 实报 1999；价差 C1-C0=15、C2-C1=25
        # 一致口径下 C2 应为 1050，实报 1999 与之矛盾
        db.upsert_price(self.conn, cid["C2"], "2026-01-06", 1999)
        db.save_spreads(self.conn, "2026-01-06", [
            ("C0", "C1", 15), ("C1", "C2", 25)])
        inf = self._inferred_map("2026-01-06")
        self.assertEqual(inf["C1"][1], "conflict")
        self.assertIsNone(inf["C1"][0])

    def test_replace_and_clear_spread_table(self):
        cid = self._ids()
        db.save_spreads(self.conn, "2026-01-06", [("C0", "C1", 15)])
        self.assertEqual(self._inferred_map("2026-01-06")["C1"][0], 1025)
        # 替换：改成 C1-C0=20 → C1=1030
        db.save_spreads(self.conn, "2026-01-06", [("C0", "C1", 20)])
        self.assertEqual(self._inferred_map("2026-01-06")["C1"][0], 1030)
        # 清空：推算行撤下（无锚/无表）
        db.save_spreads(self.conn, "2026-01-06", [])
        self.assertEqual(db.list_spreads(self.conn, "2026-01-06"), [])
        self.assertNotIn("C1", self._inferred_map("2026-01-06"))
        self.assertIsNone(db.effective_price(self.conn, cid["C1"], "2026-01-06"))

    def test_inferred_price_feeds_roll_delta_and_marker(self):
        # roll1 参考日 01-07：删掉 C1 在 01-07 的实报，用价差 C1-C0=10 推算（C0=1020）
        cid = self._ids()
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-07'",
                          (cid["C1"],))
        with db.write_tx(self.conn):
            db._recompute_day_inferred(self.conn, "2026-01-07")
            db._recompute_roll_delta(self.conn, 1)
        # 无价差表时没有推算价，roll1 delta 被撤空
        self.assertIsNone(
            self.conn.execute("SELECT delta FROM rolls WHERE ord=1").fetchone()["delta"])
        # 保存价差表后推算 C1=1030，delta1 = 1030-1020 = 10，并打推算标记
        db.save_spreads(self.conn, "2026-01-07", [("C0", "C1", 10)])
        roll1 = self.conn.execute(
            "SELECT delta, roll_inferred FROM rolls WHERE ord=1").fetchone()
        self.assertEqual(roll1["delta"], 10)
        self.assertEqual(roll1["roll_inferred"], 1)
        rows = self.fetch("2026-01-05", "2026-01-13", "2026-01-13")
        first = rows[0]
        self.assertEqual(first["continuous"], 1000 + 10 + 25)
        self.assertTrue(first["roll_inferred"])
        self.assertIn("换月调整含推算价", first["message"])
        # 01-12 起 roll1 不在调整区间，不应标记
        self.assertFalse(next(r for r in rows if r["date"] == "2026-01-12")["roll_inferred"])

    def test_same_value_actual_replaces_inferred_marker(self):
        cid = self._ids()
        # C1 在 01-06 无实报，由价差推算 = 1025
        db.save_spreads(self.conn, "2026-01-06", [("C0", "C1", 15)])
        eff = db.effective_price(self.conn, cid["C1"], "2026-01-06")
        self.assertEqual(eff, {"price": 1025, "source": "inferred"})
        # 补一笔同值实报：价格不变，来源标记切回 actual
        db.upsert_price(self.conn, cid["C1"], "2026-01-06", 1025)
        eff = db.effective_price(self.conn, cid["C1"], "2026-01-06")
        self.assertEqual(eff, {"price": 1025, "source": "actual"})
        inf = self._inferred_map("2026-01-06")
        self.assertNotIn("C1", inf)

    def test_only_target_day_recomputed(self):
        cid = self._ids()
        db.save_spreads(self.conn, "2026-01-06", [("C0", "C1", 15)])
        db.save_spreads(self.conn, "2026-01-07", [("C0", "C1", 10)])
        before_07 = dict(self._inferred_map("2026-01-07"))
        # 改 01-06 的实报，只允许重算 01-06
        db.upsert_price(self.conn, cid["C0"], "2026-01-06", 1111)
        self.assertEqual(self._inferred_map("2026-01-07"), before_07)
        self.assertEqual(self._inferred_map("2026-01-06")["C1"][0], 1126)

    def test_spread_validation(self):
        with self.assertRaises(db.AppError):
            db.save_spreads(self.conn, "2026-01-06", [("C0", "NOPE", 1)])
        with self.assertRaises(db.AppError):
            db.save_spreads(self.conn, "2026-01-06", [("C0", "C0", 1)])
        with self.assertRaises(db.AppError):
            db.save_spreads(self.conn, "2026-01-10", [("C0", "C1", 1)])  # 非交易日
        with self.assertRaises(db.AppError):
            db.save_spreads(self.conn, "2026-01-06", [("C0", "C1", 1.5)])

    def test_spread_save_failure_rolls_back(self):
        # 模拟事务中途失败：价差表与推算价都必须回到旧状态
        db.save_spreads(self.conn, "2026-01-06", [("C0", "C1", 15)])
        orig = db._recompute_day_inferred
        def boom(conn, day):
            raise sqlite3.OperationalError("simulated failure")
        db._recompute_day_inferred = boom
        try:
            prev = db.save_spreads(self.conn, "2026-01-06", [("C0", "C1", 20)])
        finally:
            db._recompute_day_inferred = orig
        self.assertFalse(prev["committed"])
        # 旧表仍在，推算价不变
        self.assertEqual(db.list_spreads(self.conn, "2026-01-06")[0]["spread"], 15)
        self.assertEqual(self._inferred_map("2026-01-06")["C1"][0], 1025)

    def test_delete_actual_recomputes_inferred_and_roll(self):
        cid = self._ids()
        # 01-07 价差 C1-C0=10，C1 有实报 1030；删实报后应由推算价顶住 roll1
        db.save_spreads(self.conn, "2026-01-07", [("C0", "C1", 10)])
        self.assertEqual(
            self.conn.execute("SELECT roll_inferred FROM rolls WHERE ord=1")
            .fetchone()["roll_inferred"], 0)
        prev = db.delete_price(self.conn, cid["C1"], "2026-01-07")
        self.assertTrue(prev["committed"])
        self.assertIsNone(db.get_price(self.conn, cid["C1"], "2026-01-07"))
        eff = db.effective_price(self.conn, cid["C1"], "2026-01-07")
        self.assertEqual(eff, {"price": 1030, "source": "inferred"})
        roll1 = self.conn.execute(
            "SELECT delta, roll_inferred FROM rolls WHERE ord=1").fetchone()
        self.assertEqual(roll1["delta"], 10)
        self.assertEqual(roll1["roll_inferred"], 1)
        # 再撤下价差表：推算价与 roll1 delta 一起撤空
        db.save_spreads(self.conn, "2026-01-07", [])
        self.assertIsNone(db.effective_price(self.conn, cid["C1"], "2026-01-07"))
        self.assertIsNone(
            self.conn.execute("SELECT delta FROM rolls WHERE ord=1").fetchone()["delta"])

    def test_reversed_and_cross_edges_infer_correctly(self):
        # 01-06 仅 C0=1010 实报；混合方向的价差边，推算值须与独立赋值模型一致
        cid = self._ids()
        # C1-C0=15；C2-C1=25；再加反向边 C0-C1=-15 与跨边 C0-C2=40（一致）
        prev = db.save_spreads(self.conn, "2026-01-06", [
            ("C0", "C1", 15), ("C1", "C2", 25),
            ("C1", "C0", -15), ("C0", "C2", 40)])
        self.assertTrue(prev["committed"])
        inf = self._inferred_map("2026-01-06")
        self.assertEqual(inf["C1"][:2], (1025, "ok"))
        self.assertEqual(inf["C2"][:2], (1050, "ok"))
        # 反向边数值矛盾（C0-C1=-99）→ 整组判矛盾、撤下
        prev = db.save_spreads(self.conn, "2026-01-06", [
            ("C0", "C1", 15), ("C1", "C0", -99)])
        self.assertTrue(prev["committed"])
        inf = self._inferred_map("2026-01-06")
        self.assertEqual(inf["C1"][1], "conflict")
        self.assertIsNone(db.effective_price(self.conn, cid["C1"], "2026-01-06"))

    def test_weighted_dsu_semantics(self):
        dsu = db._WeightedDSU()
        for x in range(5):
            dsu.add(x)
        self.assertIsNone(dsu.union(0, 1, 10))   # p1-p0=10
        self.assertIsNone(dsu.union(1, 2, 25))   # p2-p1=25
        self.assertIsNone(dsu.union(0, 2, 35))   # 一致约束
        self.assertIsNotNone(dsu.union(0, 2, 36))
        r0, w0 = dsu.find(0)
        r2, w2 = dsu.find(2)
        self.assertEqual(r0, r2)
        self.assertEqual(w2 - w0, 35)
        # 反向边：p0-p1 = -10 与已有约束一致，-11 矛盾
        self.assertIsNone(dsu.union(1, 0, -10))
        self.assertIsNotNone(dsu.union(1, 0, -11))
        # 两个独立树合并：3-4=5，再用 4-2=... 挂接，校验跨树根的权重
        self.assertIsNone(dsu.union(3, 4, 5))
        # p2-p4 = 30 ⇒ p4 = p2-30
        self.assertIsNone(dsu.union(4, 2, 30))
        r4, w4 = dsu.find(4)
        r2b, w2b = dsu.find(2)
        self.assertEqual(r4, r2b)
        self.assertEqual(w2b - w4, 30)
        # 全连通后任意两点相对价（独立赋值模型：p0=0 → p1=10,p2=35,p4=5,p3=0）
        r3, w3 = dsu.find(3)
        r0b, w0b = dsu.find(0)
        self.assertEqual(r3, r0b)
        self.assertEqual(w3 - w0b, 0)
        # 路径压缩后再次查询结果稳定
        for _ in range(3):
            rr, ww = dsu.find(3)
            self.assertEqual((rr, ww), (r3, w3))


class TestCSVAndWeb(BaseCase):
    def test_csv_columns_and_blank_for_missing(self):
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C0")
        self.conn.execute("DELETE FROM prices WHERE contract_id=? AND date='2026-01-06'",
                          (cid,))
        rows = self.fetch("2026-01-05", "2026-01-08", "2026-01-13")
        text = db.to_csv(rows)
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "交易日,在用合约,原价(分),原价来源,换月调整(分),"
                                   "换月调整含推算价,连续价(分),状态,说明")
        blank = next(l for l in lines if l.startswith("2026-01-06"))
        self.assertIn(",,", blank)          # 连续价留空
        self.assertIn("缺原价", blank)
        # 正常行标明原价来源为实报、换月调整不含推算价
        ok_line = next(l for l in lines if l.startswith("2026-01-05"))
        cells = ok_line.split(",")
        self.assertEqual(cells[3], "实报")
        self.assertEqual(cells[5], "否")

    def test_flask_routes_and_download(self):
        os.environ["CONT_DB_PATH"] = self.db_path  # 让 Web 请求落到本用例的临时库
        from app.app import create_app
        app = create_app()
        app.config["TESTING"] = True
        client = app.test_client()
        for path in ("/", "/calendar", "/plan", "/prices", "/query"):
            self.assertEqual(client.get(path).status_code, 200)

        # Web 表单更正一笔报价
        cid = next(r["contract_id"] for r in db.list_contracts(self.conn)
                   if r["code"] == "C1")
        resp = client.post("/prices", data={
            "contract_id": cid, "date": "2026-01-07", "price": 1038})
        self.assertEqual(resp.status_code, 302)

        resp = client.get("/download?start=2026-01-05&end=2026-01-13&base=2026-01-13")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.headers["Content-Type"])
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        body = resp.data.decode("utf-8-sig")
        self.assertIn("2026-01-05", body)
        # 1038 → delta1 = 18，首日连续价 = 1000+18+25 = 1043
        self.assertIn("1043", body)

    def test_web_spread_workflow_and_markers(self):
        os.environ["CONT_DB_PATH"] = self.db_path
        from app.app import create_app
        client = create_app().test_client()
        cid = {r["code"]: r["contract_id"] for r in db.list_contracts(self.conn)}
        # 撤下 C1 在 01-07 的实报 → roll1 价差缺供
        resp = client.post("/prices/delete",
                           data={"contract_id": cid["C1"], "date": "2026-01-07"})
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(
            self.conn.execute("SELECT delta FROM rolls WHERE ord=1").fetchone()["delta"])
        # 网页保存价差表补数
        resp = client.post("/spreads", data={
            "date": "2026-01-07",
            "code_a": ["C0"], "code_b": ["C1"], "spread": [10]})
        self.assertEqual(resp.status_code, 302)
        roll1 = self.conn.execute(
            "SELECT delta, roll_inferred FROM rolls WHERE ord=1").fetchone()
        self.assertEqual(roll1["delta"], 10)
        self.assertEqual(roll1["roll_inferred"], 1)
        # 取数页面出现推算标记
        page = client.get(
            "/query?start=2026-01-05&end=2026-01-13&base=2026-01-13").data.decode()
        self.assertIn("换月调整含推算价", page)
        # 非法输入被拒（未登记合约）
        resp = client.post("/spreads", data={
            "date": "2026-01-07",
            "code_a": ["C0"], "code_b": ["NOPE"], "spread": [10]})
        self.assertEqual(resp.status_code, 302)
        # 清空价差表
        resp = client.post("/spreads", data={
            "date": "2026-01-07", "code_a": [], "code_b": [], "spread": []})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(db.list_spreads(self.conn, "2026-01-07"), [])


if __name__ == "__main__":
    unittest.main()
