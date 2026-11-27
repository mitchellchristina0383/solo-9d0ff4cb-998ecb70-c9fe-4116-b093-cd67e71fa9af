"""核心规则与 HTTP/页面 端到端测试：python -m unittest discover -s tests -v"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from power_model import Drill, compute_loads, run_cascade  # noqa: E402
from samples import SAMPLES  # noqa: E402


def cross_drill():
    return Drill(SAMPLES["cross"])


def chain_drill():
    return Drill(SAMPLES["chain"])


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
