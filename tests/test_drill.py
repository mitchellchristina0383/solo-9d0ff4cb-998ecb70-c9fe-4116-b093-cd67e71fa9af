"""核心规则与 HTTP/页面 端到端测试：python -m unittest discover -s tests -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from power_model import Drill, RuleError, compute_loads, run_cascade  # noqa: E402
from samples import SAMPLES  # noqa: E402


def cross_drill():
    return Drill(SAMPLES["cross"])


def chain_drill():
    return Drill(SAMPLES["chain"])


def ups_drill():
    return Drill(SAMPLES["ups"])


def ups_outage(d):
    """样例三：断开两路 UPS 的上游供电。"""
    d.set_upstream("WL1", False)
    d.set_upstream("WL2", False)
    return d


class LoadMathTests(unittest.TestCase):
    def test_dual_paths_split_equally(self):
        feeds = [("s1", ("A", "B"))]
        loads, powered = compute_loads(1000, feeds, {"A", "B"})
        self.assertTrue(powered["s1"])
        self.assertAlmostEqual(loads["A"], 500)
        self.assertAlmostEqual(loads["B"], 500)

    def test_single_path_takes_full_power(self):
        feeds = [("s1", ("A", "B"))]
        loads, powered = compute_loads(1000, feeds, {"A"})
        self.assertTrue(powered["s1"])
        self.assertAlmostEqual(loads["A"], 1000)
        self.assertNotIn("B", loads)

    def test_both_feeds_open_means_off_and_zero_load(self):
        feeds = [("s1", ("A", "B"))]
        loads, powered = compute_loads(1000, feeds, set())
        self.assertFalse(powered["s1"])
        self.assertEqual(loads, {})

    def test_simultaneous_overload_in_same_round(self):
        # 两台服务器各自只剩一条馈线，A、B 同时超载 -> 必须出现在同一轮
        feeds = [("s1", ("A", "X")), ("s2", ("B", "Y"))]
        caps = {"A": 750, "B": 750, "X": 9999, "Y": 9999}
        result = run_cascade(
            1000, feeds, caps, ["A", "B", "X", "Y"], {"A", "B"}
        )
        self.assertEqual(len(result.rounds), 1)
        self.assertEqual(
            sorted(t["branch"] for t in result.rounds[0]["tripped"]),
            ["A", "B"],
        )
        self.assertNotIn("A", result.closed)
        self.assertNotIn("B", result.closed)

    def test_cascade_recomputes_until_stable(self):
        d = cross_drill()
        op = d.disconnect("WL1")
        self.assertEqual(
            [[t["branch"] for t in r["tripped"]] for r in op["rounds"]],
            [["WL2", "WL3"], ["WL4"]],
        )
        # 同一轮 WL2/WL3 同时出现
        self.assertEqual(op["rounds"][0]["index"], 1)
        self.assertEqual(len(op["rounds"][0]["tripped"]), 2)
        # 最终所有支路断开、所有服务器掉电
        self.assertEqual(d.closed, set())
        self.assertEqual(d.cluster_status()["powered_count"], 0)


class BootAndReadinessTests(unittest.TestCase):
    def test_boot_number_increments_only_after_real_power_cycle(self):
        d = cross_drill()
        d.disconnect("WL1")  # 全掉电，但掉电瞬间启动编号保持 1
        self.assertEqual(d.servers["db-1"]["boot_no"], 1)
        self.assertFalse(d.servers["db-1"]["powered"])

        d.close("WL1")  # WL1 可带 db-1/db-2 满载
        self.assertTrue(d.servers["db-1"]["powered"])
        self.assertEqual(d.servers["db-1"]["boot_no"], 2)
        self.assertEqual(d.servers["db-2"]["boot_no"], 2)
        self.assertFalse(d.servers["db-3"]["powered"])

        # 单路/双路切换但从未掉电：启动编号不变
        d.close("WL2")
        d.disconnect("WL2")
        self.assertTrue(d.servers["db-1"]["powered"])
        self.assertEqual(d.servers["db-1"]["boot_no"], 2)

        # 再次完整掉电复电 -> 3
        d.disconnect("WL1")
        d.close("WL1")
        self.assertEqual(d.servers["db-1"]["boot_no"], 3)

    def test_readiness_rejects_unpowered_and_stale_boot(self):
        d = cross_drill()
        d.disconnect("WL1")

        rep = d.confirm_ready("db-1", 1)
        self.assertFalse(rep["accepted"])
        self.assertEqual(rep["code"], "unpowered")

        d.close("WL1")  # db-1 -> boot #2
        stale = d.confirm_ready("db-1", 1)
        self.assertEqual(stale["code"], "stale_boot")
        self.assertFalse(d.servers["db-1"]["ready"])

        ok = d.confirm_ready("db-1", 2)
        self.assertTrue(ok["accepted"])
        self.assertTrue(d.servers["db-1"]["ready"])

    def test_stale_probe_uses_previous_boot_number(self):
        d = cross_drill()
        d.disconnect("WL1")
        d.close("WL1")
        rep = d.confirm_ready("db-2", None, stale_probe=True)
        self.assertEqual(rep["sent_boot_no"], 1)
        self.assertEqual(rep["code"], "stale_boot")

    def test_quorum_requires_two_ready(self):
        d = cross_drill()
        self.assertTrue(d.cluster_status()["available"])  # 初始 3/3

        d.disconnect("WL1")
        self.assertFalse(d.cluster_status()["available"])

        d.close("WL1")  # db-1、db-2 复电 boot #2
        d.confirm_ready("db-1", 2)
        self.assertEqual(d.cluster_status()["ready_count"], 1)
        self.assertFalse(d.cluster_status()["available"])

        d.confirm_ready("db-2", 2)
        self.assertEqual(d.cluster_status()["ready_count"], 2)
        self.assertTrue(d.cluster_status()["available"])

    def test_power_loss_clears_readiness(self):
        d = cross_drill()
        self.assertTrue(d.servers["db-1"]["ready"])
        d.disconnect("WL1")
        self.assertFalse(d.servers["db-1"]["ready"])
        self.assertIsNone(d.servers["db-1"]["ready_boot_no"])


class UpsDrillTests(unittest.TestCase):
    """样例三：UPS 上游断电后的电池续航、耗尽断开与复电合闸。"""

    def test_initial_state(self):
        d = ups_drill()
        self.assertEqual(d.ups["WL1"]["energy_j"], 120000)
        self.assertEqual(d.ups["WL2"]["energy_j"], 30000)
        self.assertTrue(d.ups["WL1"]["upstream_on"])
        self.assertEqual(d.clock_s, 0)
        v = d.view()
        self.assertTrue(v["has_ups"])
        self.assertEqual(v["clock_s"], 0)
        wl1 = next(b for b in v["branches"] if b["id"] == "WL1")
        self.assertEqual(wl1["ups"]["energy_j"], 120000)
        self.assertEqual(wl1["ups"]["initial_j"], 120000)

    def test_no_drain_while_upstream_on(self):
        d = ups_drill()
        op = d.advance(100)
        self.assertEqual(op["events"], [])
        self.assertEqual(d.ups["WL1"]["energy_j"], 120000)
        self.assertEqual(d.ups["WL2"]["energy_j"], 30000)
        self.assertEqual(d.clock_s, 100)

    def test_drain_matches_branch_load(self):
        d = ups_drill()
        d.set_upstream("WL1", False)  # 只有 WL1 用电池：1500W × 10s
        d.advance(10)
        self.assertEqual(d.ups["WL1"]["energy_j"], 120000 - 1500 * 10)
        self.assertEqual(d.ups["WL2"]["energy_j"], 30000)  # 上游有电，电量不变

    def test_exhaustion_events_and_branch_open(self):
        d = ups_outage(ups_drill())
        op = d.advance(50)
        # 同一时刻耗尽的支路一起断开：20s 只有 WL2，50s 只有 WL1
        self.assertEqual(
            [(e["at"], e["branches"]) for e in op["events"]],
            [(20, ["WL2"]), (50, ["WL1"])],
        )
        self.assertEqual(d.ups["WL1"]["energy_j"], 0)
        self.assertEqual(d.ups["WL2"]["energy_j"], 0)
        self.assertEqual(d.ups["WL2"]["exhausted_at"], 20)
        self.assertEqual(d.ups["WL1"]["exhausted_at"], 50)
        self.assertEqual(d.closed, set())
        self.assertEqual(d.open_reason["WL2"], "battery")
        self.assertEqual(d.open_reason["WL1"], "battery")
        # 原规则结算副本状态：全部掉电、就绪清空、集群不可用
        self.assertEqual(d.cluster_status()["powered_count"], 0)
        self.assertFalse(d.cluster_status()["available"])
        for sid in d.server_order:
            self.assertFalse(d.servers[sid]["ready"])
            self.assertEqual(d.servers[sid]["boot_no"], 1)  # 掉电不发新编号

    def test_stepped_advance_equals_one_shot(self):
        def run(chunks):
            d = ups_outage(ups_drill())
            for s in chunks:
                d.advance(s)
            return d

        one_shot = run([50])
        for chunks in ([20, 30], [7, 13, 30], [1] * 50, [10, 40], [49, 1]):
            d = run(chunks)
            self.assertEqual(d.ups["WL1"]["energy_j"], one_shot.ups["WL1"]["energy_j"], chunks)
            self.assertEqual(d.ups["WL2"]["energy_j"], one_shot.ups["WL2"]["energy_j"], chunks)
            self.assertEqual(d.closed, one_shot.closed, chunks)
            self.assertEqual(d.ups["WL1"]["exhausted_at"], 50, chunks)
            self.assertEqual(d.ups["WL2"]["exhausted_at"], 20, chunks)
            self.assertEqual(d.clock_s, 50, chunks)
            for sid in d.server_order:
                self.assertEqual(
                    d.servers[sid]["powered"], one_shot.servers[sid]["powered"], chunks
                )

    def test_partial_advance_crossing_exhaustion(self):
        d = ups_outage(ups_drill())
        op = d.advance(25)  # 跨过 20s 的 WL2 耗尽时刻
        self.assertEqual([(e["at"], e["branches"]) for e in op["events"]], [(20, ["WL2"])])
        # WL1：1500W×20s + 3000W×5s = 45000J
        self.assertEqual(d.ups["WL1"]["energy_j"], 120000 - 45000)
        self.assertEqual(d.ups["WL2"]["energy_j"], 0)
        self.assertEqual(d.closed, {"WL1"})
        # 三副本仍由 WL1 供电，不就绪状态不变、不发新编号
        self.assertEqual(d.cluster_status()["powered_count"], 3)
        self.assertTrue(d.cluster_status()["available"])
        self.assertEqual(d.servers["db-1"]["boot_no"], 1)

    def test_close_blocked_when_exhausted_and_upstream_off(self):
        d = ups_outage(ups_drill())
        d.advance(50)
        with self.assertRaises(RuleError):
            d.close("WL1")
        with self.assertRaises(RuleError):
            d.close_many(["WL1", "WL2"])
        # 状态未被破坏
        self.assertEqual(d.closed, set())
        self.assertEqual(d.ups["WL1"]["energy_j"], 0)

    def test_restore_upstream_then_manual_close_and_ready(self):
        d = ups_outage(ups_drill())
        d.advance(50)
        d.set_upstream("WL1", True)  # 恢复上游：不自动合闸、电量不回升
        self.assertEqual(d.closed, set())
        self.assertEqual(d.ups["WL1"]["energy_j"], 0)
        # WL2 上游仍断电且耗尽 -> 仍不能合闸
        with self.assertRaises(RuleError):
            d.close("WL2")
        op = d.close("WL1")  # 值班员手动合闸
        self.assertEqual(op["rounds"], [])
        self.assertEqual(d.cluster_status()["powered_count"], 3)
        self.assertEqual(d.servers["db-1"]["boot_no"], 2)
        # 当前编号报就绪，2/3 恢复集群
        d.confirm_ready("db-1", 2)
        self.assertFalse(d.cluster_status()["available"])
        d.confirm_ready("db-2", 2)
        self.assertTrue(d.cluster_status()["available"])
        # 电量不充电：恢复上游后推进也不变
        d.advance(10)
        self.assertEqual(d.ups["WL1"]["energy_j"], 0)
        self.assertEqual(d.ups["WL2"]["energy_j"], 0)

    def test_upstream_off_with_empty_battery_opens_branch_immediately(self):
        d = ups_outage(ups_drill())
        d.advance(50)
        d.set_upstream("WL1", True)
        d.close("WL1")  # 上游有电，允许合闸（电池仍是 0）
        self.assertIn("WL1", d.closed)
        # 再次断开上游：电池已空 -> 支路立即断开，副本再次掉电
        op = d.set_upstream("WL1", False)
        self.assertEqual(len(op["events"]), 1)
        self.assertEqual(op["events"][0]["branches"], ["WL1"])
        self.assertNotIn("WL1", d.closed)
        self.assertEqual(d.cluster_status()["powered_count"], 0)

    def test_exhaustion_settles_overload_trip_by_original_rules(self):
        d = ups_drill()
        d.set_upstream("WL1", False)  # 只断 WL1 上游；WL2 上游有电
        op = d.advance(80)  # WL1：1500W × 80s = 120000J 耗尽
        self.assertEqual([(e["at"], e["branches"]) for e in op["events"]], [(80, ["WL1"])])
        ev = op["events"][0]
        # WL1 断开后 WL2 独带 3000W > 1600W -> 按原规则同事件内跳闸
        self.assertEqual(
            [t["branch"] for r in ev["rounds"] for t in r["tripped"]], ["WL2"]
        )
        self.assertEqual(d.open_reason["WL2"], "trip")
        self.assertEqual(d.cluster_status()["powered_count"], 0)
        self.assertEqual(d.ups["WL2"]["energy_j"], 30000)  # 上游有电未耗电

    def test_advance_validation(self):
        d = ups_drill()
        for bad in (0, -5):
            with self.assertRaises(ValueError):
                d.advance(bad)
        with self.assertRaises(ValueError):
            cross_drill().advance(10)  # 非 UPS 教案不能推进
        with self.assertRaises(KeyError):
            d.set_upstream("WL9", False)
        with self.assertRaises(KeyError):
            cross_drill().set_upstream("WL1", False)  # 无 UPS 的支路


class GoldenPathTests(unittest.TestCase):
    def test_sample_cross_quorum_recovery(self):
        d = cross_drill()
        d.disconnect("WL1")
        d.close("WL1")
        d.close("WL2")
        # 状态稳定，没有再次跳闸
        self.assertIn("WL1", d.closed)
        self.assertIn("WL2", d.closed)
        d.confirm_ready("db-1", 2)
        d.confirm_ready("db-2", 2)
        self.assertTrue(d.cluster_status()["available"])

    def test_sample_cross_full_recovery_needs_parallel_close(self):
        d = cross_drill()
        d.disconnect("WL1")
        d.close("WL1")
        d.close("WL2")

        # 单独合 WL3 会因 1500W > 1000W 再次跳闸
        op = d.close("WL3")
        self.assertTrue(op["rounds"])
        self.assertNotIn("WL3", d.closed)

        # WL3、WL4 同一次操作并列合闸：每路只承担半载 500W
        op = d.close_many(["WL3", "WL4"])
        self.assertEqual(op["rounds"], [])
        self.assertEqual(d.closed, set(d.branch_order))
        self.assertEqual(d.cluster_status()["powered_count"], 3)
        self.assertEqual(d.servers["db-3"]["boot_no"], 2)

    def test_sample_chain_sequential_full_recovery(self):
        d = chain_drill()
        op = d.disconnect("WL2")
        self.assertEqual(
            [[t["branch"] for t in r["tripped"]] for r in op["rounds"]],
            [["WL1"], ["WL3"]],
        )
        for bid in ("WL2", "WL1", "WL3"):
            op = d.close(bid)
            self.assertEqual(op["rounds"], [], f"合闸 {bid} 不应再跳闸")
        self.assertEqual(d.closed, {"WL1", "WL2", "WL3"})
        self.assertEqual(d.cluster_status()["powered_count"], 3)
        for sid in d.server_order:
            d.confirm_ready(sid, 2)
        self.assertTrue(d.cluster_status()["available"])


class FlaskHttpTests(unittest.TestCase):
    def setUp(self):
        import app as flask_app

        self.appmod = flask_app
        flask_app._reset("cross")
        self.client = flask_app.app.test_client()

    def test_page_renders_branches_and_capacity(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("配电支路", html)
        self.assertIn("WL1", html)
        self.assertIn("2200 W", html)
        self.assertIn("数据库集群可用", html)

    def test_api_cascade_then_stale_boot_409(self):
        resp = self.client.post("/api/branches/WL1/disconnect")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(
            [r["tripped"][0]["branch"] if len(r["tripped"]) == 1 else
             sorted(t["branch"] for t in r["tripped"]) for r in body["operation"]["rounds"]],
            [["WL2", "WL3"], "WL4"],
        )
        self.assertFalse(body["state"]["cluster"]["available"])

        # 掉电期间用旧编号 -> 409 unpowered
        resp = self.client.post("/api/replicas/db-1/ready", json={"boot_no": 1})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["report"]["code"], "unpowered")

        # 复电 WL1 -> boot #2；旧编号 #1 -> 409 stale_boot
        self.client.post("/api/branches/WL1/close")
        resp = self.client.post("/api/replicas/db-1/ready", json={"boot_no": 1})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["report"]["code"], "stale_boot")

        # 当前编号 #2 -> 200
        resp = self.client.post("/api/replicas/db-1/ready", json={"boot_no": 2})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

    def test_html_page_lists_rounds_after_form_post(self):
        resp = self.client.post("/ui/branches/WL1/disconnect", follow_redirects=True)
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("第 1 轮", html)
        self.assertIn("WL2", html)
        self.assertIn("WL3", html)
        self.assertIn("第 2 轮", html)
        self.assertIn("本次操作 · 每轮跳闸支路", html)

    def test_html_stale_probe_button_flow(self):
        self.client.post("/api/branches/WL1/disconnect")
        self.client.post("/api/branches/WL1/close")
        resp = self.client.post(
            "/ui/replicas/db-2/ready",
            data={"mode": "stale", "boot_no": "2"},
            follow_redirects=True,
        )
        html = resp.get_data(as_text=True)
        self.assertIn("已失效", html)
        self.assertIn("旧编号", html)

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)


class UpsHttpTests(unittest.TestCase):
    def setUp(self):
        import app as flask_app

        self.appmod = flask_app
        flask_app._reset("ups")
        self.client = flask_app.app.test_client()

    def test_ups_page_renders_battery_panel(self):
        html = self.client.get("/").get_data(as_text=True)
        for marker in ("样例三", "UPS-1", "UPS-2", "剩余电量", "120000 J", "30000 J",
                       "演练时钟", "推进时间", "断开上游供电"):
            self.assertIn(marker, html)

    def test_api_outage_advance_close_blocked_restore_close(self):
        # 断开两路上游
        resp = self.client.post("/api/ups/WL1/upstream", json={"on": False})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post("/api/ups/WL2/upstream", json={"on": False})
        self.assertEqual(resp.status_code, 200)

        # 跨耗尽推进 25s：WL2 在 20s 耗尽
        resp = self.client.post("/api/advance", json={"seconds": 25})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(
            [(e["at"], e["branches"]) for e in body["operation"]["events"]],
            [(20, ["WL2"])],
        )
        branches = {b["id"]: b for b in body["state"]["branches"]}
        self.assertEqual(branches["WL1"]["ups"]["energy_j"], 75000)
        self.assertEqual(branches["WL2"]["ups"]["energy_j"], 0)
        self.assertTrue(branches["WL1"]["closed"])
        self.assertFalse(branches["WL2"]["closed"])
        self.assertEqual(body["state"]["clock_s"], 25)

        # 推进到全耗尽后：单独与并列合闸都被拒（409 close_blocked）
        self.client.post("/api/advance", json={"seconds": 25})
        resp = self.client.post("/api/branches/WL1/close")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "close_blocked")
        resp = self.client.post("/api/branches/close-many",
                                json={"ids": ["WL1", "WL2"]})
        self.assertEqual(resp.status_code, 409)

        # 恢复上游 -> 手动合闸 -> 当前编号报就绪
        self.client.post("/api/ups/WL1/upstream", json={"on": True})
        resp = self.client.post("/api/branches/WL1/close")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["state"]["cluster"]["powered_count"], 3)
        resp = self.client.post("/api/replicas/db-1/ready", json={"boot_no": 2})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post("/api/replicas/db-2/ready", json={"boot_no": 2})
        self.assertTrue(resp.get_json()["state"]["cluster"]["available"])

    def test_api_advance_validation(self):
        resp = self.client.post("/api/advance", json={"seconds": 0})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/advance", json={"seconds": "abc"})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/ups/WL9/upstream", json={"on": False})
        self.assertEqual(resp.status_code, 404)
        self.appmod._reset("cross")
        resp = self.client.post("/api/advance", json={"seconds": 5})
        self.assertEqual(resp.status_code, 400)  # 非 UPS 教案

    def test_ui_advance_and_upstream_form_flow(self):
        self.client.post("/ui/ups/WL1/upstream", data={"on": "0"})
        self.client.post("/ui/ups/WL2/upstream", data={"on": "0"})
        resp = self.client.post("/ui/advance", data={"seconds": "50"},
                                follow_redirects=True)
        html = resp.get_data(as_text=True)
        self.assertIn("电池耗尽自动断开", html)
        self.assertIn("数据库集群不可用", html)
        self.assertIn("电池耗尽 · 自动断开", html)
        self.assertIn("已于演练时钟 20 s 耗尽", html)
        self.assertIn("已于演练时钟 50 s 耗尽", html)
        # 耗尽且上游断电：合闸按钮被禁用
        self.assertIn("须先恢复上游供电", html)


class InterleavingTests(unittest.TestCase):
    """就绪上报与支路操作高并发交错时，状态不变量必须始终成立。"""

    def test_concurrent_reports_and_switch_ops(self):
        import random
        import threading

        import app as flask_app

        flask_app._reset("cross")
        app = flask_app.app
        errors = []

        def validate(state):
            ready = 0
            for s in state["servers"]:
                if s["ready"]:
                    ready += 1
                    self.assertTrue(s["powered"], "就绪副本必须仍有电")
                    self.assertEqual(s["ready_boot_no"], s["boot_no"])
            self.assertEqual(state["cluster"]["ready_count"], ready)
            self.assertEqual(
                state["cluster"]["available"], ready >= 2
            )

        def worker(seed):
            rnd = random.Random(seed)
            try:
                for _ in range(60):
                    client = app.test_client()
                    choice = rnd.randrange(6)
                    if choice == 0:
                        resp = client.post("/api/branches/WL1/disconnect")
                    elif choice == 1:
                        resp = client.post("/api/branches/WL1/close")
                    elif choice == 2:
                        resp = client.post("/api/branches/WL2/close")
                    elif choice == 3:
                        resp = client.post(
                            "/api/branches/close-many",
                            json={"ids": ["WL3", "WL4"]},
                        )
                    elif choice == 4:
                        sid = rnd.choice(["db-1", "db-2", "db-3"])
                        boot = rnd.choice([0, 1, 2, 3, 99])  # 多数是旧/错编号
                        resp = client.post(
                            f"/api/replicas/{sid}/ready",
                            json={"boot_no": boot},
                        )
                    else:
                        resp = client.get("/api/state")
                    self.assertIn(resp.status_code, (200, 409))
                    payload = resp.get_json()
                    validate(payload.get("state", payload))
            except Exception as exc:  # 传回主线程
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

        # 最终再核对一次页面可正常渲染
        html = app.test_client().get("/").get_data(as_text=True)
        self.assertIn("数据库", html)

    def test_concurrent_ups_advance_and_ops(self):
        """UPS 教案：推进/断合上游/分合闸/就绪上报交错时，不变量必须成立。"""
        import random
        import threading

        import app as flask_app

        flask_app._reset("ups")
        app = flask_app.app
        errors = []

        def validate(state):
            self.assertGreaterEqual(state["clock_s"], 0)
            for b in state["branches"]:
                up = b.get("ups")
                if not up:
                    continue
                self.assertGreaterEqual(up["energy_j"], 0, "电量不能为负")
                if up["exhausted"]:
                    self.assertEqual(up["energy_j"], 0)
                    self.assertIsNotNone(up["exhausted_at"])
                if b["closed"] and not up["upstream_on"]:
                    # 不变量：合闸且上游断电的支路必须还有余量
                    self.assertGreater(up["energy_j"], 0, b["id"])
                if not up["upstream_on"] and up["energy_j"] == 0:
                    self.assertFalse(b["closed"], "耗尽且断电的支路必须断开")
            for s in state["servers"]:
                if s["ready"]:
                    self.assertTrue(s["powered"])
                    self.assertEqual(s["ready_boot_no"], s["boot_no"])

        def worker(seed):
            rnd = random.Random(seed)
            try:
                for _ in range(50):
                    client = app.test_client()
                    choice = rnd.randrange(8)
                    if choice == 0:
                        resp = client.post("/api/advance",
                                           json={"seconds": rnd.choice([5, 10, 25, 50])})
                    elif choice == 1:
                        resp = client.post("/api/ups/WL1/upstream",
                                           json={"on": rnd.random() < 0.5})
                    elif choice == 2:
                        resp = client.post("/api/ups/WL2/upstream",
                                           json={"on": rnd.random() < 0.5})
                    elif choice == 3:
                        resp = client.post("/api/branches/WL1/close")
                    elif choice == 4:
                        resp = client.post("/api/branches/WL2/close")
                    elif choice == 5:
                        resp = client.post("/api/branches/close-many",
                                           json={"ids": ["WL1", "WL2"]})
                    elif choice == 6:
                        sid = rnd.choice(["db-1", "db-2", "db-3"])
                        resp = client.post(f"/api/replicas/{sid}/ready",
                                           json={"boot_no": rnd.choice([1, 2, 3])})
                    else:
                        resp = client.get("/api/state")
                    self.assertIn(resp.status_code, (200, 400, 409))
                    payload = resp.get_json()
                    state = payload.get("state")
                    if state is None and "cluster" in payload:
                        state = payload  # GET /api/state 直接返回视图
                    if state is not None:
                        validate(state)
            except Exception as exc:  # 传回主线程
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
