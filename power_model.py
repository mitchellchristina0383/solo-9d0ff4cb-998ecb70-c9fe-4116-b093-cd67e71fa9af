"""供电负载与连锁跳闸核心模型（不依赖 Flask）。

规则：
* 每台服务器是“双路供电”，两个电源模块分别挂在两条支路上。
* 两路都合闸时，两路平分整机功耗（每条支路承担 P/2）。
* 只剩一路时，该支路承担整机全部功耗 P。
* 每一轮把当前所有“超载”（负载严格大于功率上限）的支路**同时**跳闸，
  然后按剩余合闸支路重算，直到没有超载支路。
* 只有服务器的两条馈电支路都断开时，服务器才掉电。

启动编号（boot_no）只在 掉电 -> 重新有电 的跳变时 +1。

UPS 教案附加规则（样例三）：
* 支路可配置一台 UPS（电池电量，单位焦耳）与“上游供电”开关。
* 上游有电时电池电量不变（本模型不模拟充电，电量只减不增）。
* 上游断电且支路合闸时，由 UPS 接着供电，按支路负载扣电：电量 -= 负载(W) × 秒。
* 电池耗尽的瞬间支路自动断开；同一时刻耗尽的支路一起断开，
  然后按原有规则结算超载连锁跳闸与副本掉电/复电状态；剩余时间按新负载继续扣电。
* 推进按“耗尽事件”分段精确结算，因此分次推进与一次推进结果一致。
* 电池耗尽且上游仍断电的支路不能合闸（单独或并列都不行）；
  恢复上游供电后支路不会自动合闸，须值班员手动合闸。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

REQUIRED_READY = 2
HISTORY_LIMIT = 30
REPORT_LIMIT = 20
MAX_ADVANCE_S = 86400  # 单次推进上限：24 小时
_EPS = 1e-9


class RuleError(Exception):
    """违反演练规则的操作（如电池耗尽且上游断电时合闸）。"""


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _num(value: float):
    """把 500.0 这样的浮点数显示为整数，其余保留一位小数。"""
    rounded = round(value)
    if abs(value - rounded) < 1e-6:
        return int(rounded)
    return round(value, 1)


def watts(value: float):
    """瓦数显示。"""
    return _num(value)


def joules(value: float):
    """电量（焦耳）显示。"""
    return _num(value)


def secs(value: float):
    """秒数显示。"""
    return _num(value)


def compute_loads(
    server_power_w: float,
    server_feeds: Sequence[Tuple[str, Tuple[str, str]]],
    closed: Iterable[str],
) -> Tuple[Dict[str, float], Dict[str, bool]]:
    """计算每条支路的当前负载，以及每台服务器是否有电。

    返回 (branch_load_w, server_powered)。没有合闸馈线的服务器掉电且不取流。
    """
    closed_set: Set[str] = set(closed)
    loads: Dict[str, float] = {}
    powered: Dict[str, bool] = {}
    for server_id, feeds in server_feeds:
        live_feeds = [b for b in feeds if b in closed_set]
        k = len(live_feeds)
        powered[server_id] = k > 0
        if k == 0:
            continue
        share = server_power_w / k  # 双路 -> P/2，单路 -> P
        for branch_id in live_feeds:
            loads[branch_id] = loads.get(branch_id, 0.0) + share
    return loads, powered


@dataclass
class CascadeResult:
    closed: Set[str]
    rounds: List[dict]
    loads: Dict[str, float]
    powered: Dict[str, bool]


def run_cascade(
    server_power_w: float,
    server_feeds: Sequence[Tuple[str, Tuple[str, str]]],
    capacities: Dict[str, float],
    branch_order: Sequence[str],
    initial_closed: Iterable[str],
) -> CascadeResult:
    """从 initial_closed 开始执行连锁跳闸，直到稳定。

    同一轮检测出的超载支路一次性全部移除（同时跳闸），下一轮基于剩余支路重算。
    """
    closed: Set[str] = set(initial_closed)
    rounds: List[dict] = []
    round_index = 0

    while True:
        loads, powered = compute_loads(server_power_w, server_feeds, closed)
        overloaded = [
            b
            for b in branch_order
            if b in closed and loads.get(b, 0.0) > capacities[b] + 1e-9
        ]
        if not overloaded:
            return CascadeResult(closed, rounds, loads, powered)

        round_index += 1
        trips = []
        for b in overloaded:  # 按 branch_order 输出，保证展示稳定
            trips.append(
                {
                    "branch": b,
                    "load_w": watts(loads.get(b, 0.0)),
                    "capacity_w": watts(capacities[b]),
                }
            )
        rounds.append({"index": round_index, "tripped": trips})

        # 关键：本轮所有超载支路同时跳闸后，才进入下一轮重算。
        for b in overloaded:
            closed.discard(b)


class Drill:
    """一次演练的全部可变状态；路由层用锁保护。"""

    def __init__(self, sample: dict):
        self.sample = sample
        self.sample_id = sample["id"]
        self.server_power_w: float = float(sample["server_power_w"])
        self.required_ready: int = int(sample.get("required_ready", REQUIRED_READY))

        self.branch_order: List[str] = [b["id"] for b in sample["branches"]]
        self.branch_cfg: Dict[str, dict] = {b["id"]: b for b in sample["branches"]}
        self.capacities: Dict[str, float] = {
            b["id"]: float(b["capacity_w"]) for b in sample["branches"]
        }

        self.server_order: List[str] = [s["id"] for s in sample["servers"]]
        self.server_cfg: Dict[str, dict] = {s["id"]: s for s in sample["servers"]}

        # server_id -> (feed_branch_1, feed_branch_2)
        self.server_feeds: List[Tuple[str, Tuple[str, str]]] = []
        self.feed_module: Dict[Tuple[str, str], str] = {}
        for s in sample["servers"]:
            feed_ids = []
            for idx, feed in enumerate(s["feeds"]):
                bid = feed["branch"]
                feed_ids.append(bid)
                self.feed_module[(s["id"], bid)] = feed.get(
                    "module", f"电源模块{idx + 1}"
                )
            if len(feed_ids) != 2:
                raise ValueError(f"服务器 {s['id']} 必须是双路供电（2 条馈线）")
            self.server_feeds.append((s["id"], (feed_ids[0], feed_ids[1])))

        self.closed: Set[str] = set(self.branch_order)
        # bid -> None(合闸) / "manual"(人工断开) / "trip"(超载跳闸) / "battery"(电池耗尽)
        self.open_reason: Dict[str, Optional[str]] = {
            b: None for b in self.branch_order
        }

        # UPS（可选，按支路配置）：电量只减不增，不模拟充电。
        self.ups: Dict[str, dict] = {}
        for b in sample["branches"]:
            cfg = b.get("ups")
            if not cfg:
                continue
            energy = float(cfg["energy_j"])
            self.ups[b["id"]] = {
                "label": cfg.get("label", f"UPS-{b['id']}"),
                "initial_j": energy,
                "energy_j": energy,
                "upstream_on": bool(cfg.get("upstream_on", True)),
                "exhausted": False,
                "exhausted_at": None,  # 首次耗尽时的演练时钟（秒）
            }
        self.clock_s: float = 0.0  # 演练时钟：推进累计秒数（仅 UPS 教案使用）

        self.servers: Dict[str, dict] = {}
        for s in sample["servers"]:
            initially_ready = bool(s.get("initially_ready", True))
            self.servers[s["id"]] = {
                "powered": True,
                "boot_no": 1,
                "ready": initially_ready,
                "ready_boot_no": 1 if initially_ready else None,
            }

        self.operations: List[dict] = []
        self.reports: List[dict] = []
        self._op_seq = 0

    # ---------- 计算 ----------

    def current_loads(self) -> Tuple[Dict[str, float], Dict[str, bool]]:
        return compute_loads(self.server_power_w, self.server_feeds, self.closed)

    def cluster_status(self) -> dict:
        ready = [sid for sid in self.server_order if self.servers[sid]["ready"]]
        powered = [sid for sid in self.server_order if self.servers[sid]["powered"]]
        return {
            "ready_count": len(ready),
            "powered_count": len(powered),
            "required_ready": self.required_ready,
            "available": len(ready) >= self.required_ready,
            "ready_servers": ready,
            "powered_servers": powered,
        }

    # ---------- 支路操作 ----------

    def disconnect(self, branch_id: str) -> dict:
        return self._apply("disconnect", [branch_id])

    def close(self, branch_id: str) -> dict:
        return self._apply("close", [branch_id])

    def close_many(self, branch_ids: Sequence[str]) -> dict:
        # 多条支路在同一次操作里并列合闸，然后统一进入超载重算。
        ids = [b for b in self.branch_order if b in set(branch_ids)]
        return self._apply("close_many", ids)

    def _apply(self, kind: str, ids: Sequence[str]) -> dict:
        unknown = [b for b in ids if b not in self.branch_cfg]
        if unknown:
            raise KeyError(f"未知支路: {unknown}")

        if kind in ("close", "close_many"):
            blocked = [b for b in ids if self._close_blocked(b)]
            if blocked:
                raise RuleError(
                    "电池已耗尽且上游仍断电，不能合闸："
                    + "、".join(blocked)
                    + "。请先恢复上游供电，再手动合闸。"
                )

        _, powered_before = self.current_loads()

        if kind == "disconnect":
            for bid in ids:
                if bid in self.closed:
                    self.closed.discard(bid)
                    self.open_reason[bid] = "manual"
        else:  # close / close_many：人工合闸，清除跳闸标记
            for bid in ids:
                self.closed.add(bid)
                self.open_reason[bid] = None

        result = run_cascade(
            self.server_power_w,
            self.server_feeds,
            self.capacities,
            self.branch_order,
            self.closed,
        )

        self._settle_cascade(result)
        power_events = self._apply_power_transitions(powered_before, result.powered)

        self._op_seq += 1
        op = {
            "seq": self._op_seq,
            "at": _now(),
            "kind": kind,
            "targets": list(ids),
            "rounds": result.rounds,
            "power_events": power_events,
            "events": [],
            "had_trip": bool(result.rounds),
        }
        self.operations.insert(0, op)
        del self.operations[HISTORY_LIMIT:]
        return op

    # ---------- 公共结算 ----------

    def _settle_cascade(self, result: CascadeResult) -> None:
        """把连锁跳闸结果写回支路状态（跳闸标记同步）。"""
        tripped_now = {t["branch"] for r in result.rounds for t in r["tripped"]}
        self.closed = result.closed
        for bid in self.branch_order:
            if bid in self.closed:
                self.open_reason[bid] = None
            elif bid in tripped_now:
                self.open_reason[bid] = "trip"
            # 其余保持原有 manual/trip/battery 原因

    def _apply_power_transitions(
        self, powered_before: Dict[str, bool], powered_after: Dict[str, bool]
    ) -> List[dict]:
        """按稳定态对比结算掉电/复电：掉电清就绪，复电发新启动编号。"""
        power_events: List[dict] = []
        for sid in self.server_order:
            was = powered_before.get(sid, True)
            now = powered_after.get(sid, False)
            st = self.servers[sid]
            if was and not now:
                st["powered"] = False
                st["ready"] = False
                st["ready_boot_no"] = None
                power_events.append(
                    {"server": sid, "type": "power_lost", "boot_no": st["boot_no"]}
                )
            elif not was and now:
                st["powered"] = True
                st["boot_no"] += 1  # 复电 -> 新的启动编号
                st["ready"] = False
                st["ready_boot_no"] = None
                power_events.append(
                    {
                        "server": sid,
                        "type": "power_restored",
                        "boot_no": st["boot_no"],
                    }
                )
            elif now:
                st["powered"] = True
        return power_events

    # ---------- UPS：上游供电与演练时钟推进 ----------

    def _close_blocked(self, branch_id: str) -> bool:
        """电池耗尽且上游仍断电的支路禁止合闸（单独或并列）。"""
        up = self.ups.get(branch_id)
        return bool(up) and not up["upstream_on"] and up["energy_j"] <= _EPS

    def set_upstream(self, branch_id: str, on: bool) -> dict:
        """断开/恢复某台 UPS 的上游供电。恢复后支路不会自动合闸。"""
        if branch_id not in self.branch_cfg:
            raise KeyError(f"未知支路: {branch_id}")
        up = self.ups.get(branch_id)
        if up is None:
            raise KeyError(f"支路 {branch_id} 未配置 UPS")
        on = bool(on)
        changed = up["upstream_on"] != on
        up["upstream_on"] = on

        events: List[dict] = []
        # 上游断开时若支路合闸且电池已空 -> 无电源，支路立即断开并按原规则结算。
        if not on and branch_id in self.closed and up["energy_j"] <= _EPS:
            events.append(self._exhaust_branches([branch_id]))

        self._op_seq += 1
        op = {
            "seq": self._op_seq,
            "at": _now(),
            "kind": "upstream_on" if on else "upstream_off",
            "targets": [branch_id],
            "changed": changed,
            "rounds": [],
            "power_events": [],
            "events": events,
            "had_trip": any(ev["rounds"] for ev in events),
        }
        self.operations.insert(0, op)
        del self.operations[HISTORY_LIMIT:]
        return op

    def advance(self, seconds: float) -> dict:
        """推进演练时钟：上游断电的合闸支路按负载扣电池电量。

        按“电池耗尽事件”分段精确结算：每段内负载恒定，电量线性扣减；
        遇到耗尽时刻，同刻耗尽的支路一起断开，结算连锁跳闸与副本状态，
        剩余时间按新负载继续扣电。因此分次推进与一次推进结果一致。
        """
        if not self.ups:
            raise ValueError("当前教案不含 UPS，无法推进演练时钟。")
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            raise ValueError("推进秒数必须是数字。")
        if not seconds > 0:
            raise ValueError("推进秒数必须大于 0。")
        if seconds > MAX_ADVANCE_S:
            raise ValueError(f"单次推进不能超过 {MAX_ADVANCE_S} 秒（24 小时）。")

        clock_from = self.clock_s
        remaining = seconds
        events: List[dict] = []

        while remaining > _EPS:
            loads, _ = self.current_loads()
            # 正在耗电的支路：合闸 + 上游断电 + 电池有余量 + 确实带载
            draining = {
                bid: loads.get(bid, 0.0)
                for bid, up in self.ups.items()
                if bid in self.closed
                and not up["upstream_on"]
                and up["energy_j"] > _EPS
                and loads.get(bid, 0.0) > 0
            }
            step = remaining
            for bid, load in draining.items():
                step = min(step, self.ups[bid]["energy_j"] / load)
            for bid, load in draining.items():
                up = self.ups[bid]
                up["energy_j"] = max(0.0, up["energy_j"] - load * step)
            self.clock_s += step
            remaining -= step

            # 同一时刻耗尽的支路一起断开，再按原规则结算
            hit = [bid for bid in draining if self.ups[bid]["energy_j"] <= _EPS]
            if hit:
                events.append(self._exhaust_branches(hit))

        self.clock_s = round(self.clock_s, 9)

        self._op_seq += 1
        op = {
            "seq": self._op_seq,
            "at": _now(),
            "kind": "advance",
            "targets": [],
            "seconds": secs(seconds),
            "clock_from": secs(clock_from),
            "clock_to": secs(self.clock_s),
            "rounds": [],
            "power_events": [],
            "events": events,
            "had_trip": any(ev["rounds"] for ev in events),
        }
        self.operations.insert(0, op)
        del self.operations[HISTORY_LIMIT:]
        return op

    def _exhaust_branches(self, hit: Sequence[str]) -> dict:
        """同一时刻耗尽的支路一起断开，然后按原规则结算跳闸与副本状态。"""
        at = self.clock_s
        _, powered_before = self.current_loads()
        for bid in hit:
            up = self.ups[bid]
            up["energy_j"] = 0.0
            up["exhausted"] = True
            if up["exhausted_at"] is None:
                up["exhausted_at"] = at
            self.closed.discard(bid)
            self.open_reason[bid] = "battery"

        result = run_cascade(
            self.server_power_w,
            self.server_feeds,
            self.capacities,
            self.branch_order,
            self.closed,
        )
        self._settle_cascade(result)
        power_events = self._apply_power_transitions(powered_before, result.powered)
        return {
            "at": secs(at),
            "branches": list(hit),
            "rounds": result.rounds,
            "power_events": power_events,
        }

    # ---------- 副本就绪上报 ----------

    def confirm_ready(
        self, server_id: str, sent_boot_no: Optional[int], stale_probe: bool = False
    ) -> dict:
        if server_id not in self.servers:
            raise KeyError(f"未知副本: {server_id}")
        st = self.servers[server_id]
        current = st["boot_no"]

        if stale_probe:
            # 演练用：故意拿“上一个启动编号”上报
            sent_boot_no = current - 1

        if not st["powered"]:
            code = "unpowered"
            accepted = False
            message = f"服务器当前掉电，就绪上报不予接受（当前启动编号 #{current}）。"
        elif sent_boot_no != current:
            code = "stale_boot"
            accepted = False
            message = (
                f"启动编号 #{sent_boot_no} 已失效：服务器当前启动编号为 #{current}，"
                "只接受当前编号的就绪上报。"
            )
        else:
            code = "accepted"
            accepted = True
            st["ready"] = True
            st["ready_boot_no"] = current
            message = f"启动编号 #{current} 校验通过，副本已确认就绪。"

        report = {
            "at": _now(),
            "server": server_id,
            "sent_boot_no": sent_boot_no,
            "current_boot_no": current,
            "powered": st["powered"],
            "accepted": accepted,
            "code": code,
            "message": message,
        }
        self.reports.insert(0, report)
        del self.reports[REPORT_LIMIT:]
        return report

    # ---------- 给页面 / JSON 的快照 ----------

    def view(self) -> dict:
        loads, powered = self.current_loads()
        branches = []
        for bid in self.branch_order:
            cfg = self.branch_cfg[bid]
            load = loads.get(bid, 0.0)
            cap = self.capacities[bid]
            closed = bid in self.closed
            feeds = []
            for sid, pair in self.server_feeds:
                if bid in pair:
                    feeds.append(
                        {
                            "server_id": sid,
                            "server_name": self.server_cfg[sid]["name"],
                            "module": self.feed_module[(sid, bid)],
                            "feed_closed": sid in powered
                            and self._feed_closed(sid, bid),
                        }
                    )
            ratio = (load / cap) if cap else 0.0
            branches.append(
                {
                    "id": bid,
                    "label": cfg["label"],
                    "hint": cfg.get("hint", ""),
                    "capacity_w": watts(cap),
                    "capacity_kw": round(cap / 1000.0, 2),
                    "load_w": watts(load),
                    "load_kw": round(load / 1000.0, 2),
                    "ratio": round(ratio, 3),
                    "load_state": (
                        "over"
                        if closed and load > cap + 1e-9
                        else "full" if closed and abs(load - cap) <= 1e-6 else "ok"
                    ),
                    "closed": closed,
                    "open_reason": self.open_reason.get(bid),
                    "tripped": self.open_reason.get(bid) == "trip",
                    "feeds": feeds,
                    "ups": self._ups_view(bid, closed, load),
                    "close_blocked": self._close_blocked(bid),
                }
            )

        servers = []
        for sid in self.server_order:
            cfg = self.server_cfg[sid]
            st = self.servers[sid]
            feed_views = []
            for f in cfg["feeds"]:
                bid = f["branch"]
                feed_views.append(
                    {
                        "branch_id": bid,
                        "branch_label": self.branch_cfg[bid]["label"],
                        "module": f.get("module", "电源模块"),
                        "closed": bid in self.closed,
                    }
                )
            servers.append(
                {
                    "id": sid,
                    "name": cfg["name"],
                    "role": cfg.get("role", ""),
                    "powered": st["powered"],
                    "boot_no": st["boot_no"],
                    "ready": st["ready"],
                    "ready_boot_no": st["ready_boot_no"],
                    "feeds": feed_views,
                }
            )

        return {
            "sample": {
                "id": self.sample_id,
                "name": self.sample["name"],
                "short": self.sample.get("short", self.sample["name"]),
                "subtitle": self.sample.get("subtitle", ""),
                "description": self.sample.get("description", ""),
                "golden_path": self.sample.get("golden_path", []),
            },
            "server_power_w": watts(self.server_power_w),
            "has_ups": bool(self.ups),
            "clock_s": secs(self.clock_s) if self.ups else None,
            "branches": branches,
            "servers": servers,
            "cluster": self.cluster_status(),
            "operations": self.operations,
            "reports": self.reports,
        }

    def _ups_view(self, bid: str, closed: bool, load: float) -> Optional[dict]:
        up = self.ups.get(bid)
        if up is None:
            return None
        energy = up["energy_j"]
        draining = closed and not up["upstream_on"] and energy > _EPS and load > 0
        eta_s = energy / load if draining else None
        pct = (energy / up["initial_j"] * 100.0) if up["initial_j"] else 0.0
        return {
            "label": up["label"],
            "energy_j": joules(energy),
            "initial_j": joules(up["initial_j"]),
            "energy_pct": round(pct, 1),
            "upstream_on": up["upstream_on"],
            "exhausted": up["exhausted"],
            "exhausted_at": (
                secs(up["exhausted_at"]) if up["exhausted_at"] is not None else None
            ),
            "draining": draining,
            "eta_s": secs(eta_s) if eta_s is not None else None,
            "eta_at": secs(self.clock_s + eta_s) if eta_s is not None else None,
        }

    def _feed_closed(self, sid: str, bid: str) -> bool:
        for s in self.sample["servers"]:
            if s["id"] == sid:
                return any(f["branch"] == bid for f in s["feeds"]) and bid in self.closed
        return False
