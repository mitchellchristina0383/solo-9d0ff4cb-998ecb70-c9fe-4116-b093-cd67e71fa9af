"""供电负载与连锁跳闸核心模型（不依赖 Flask）。

规则：
* 每台服务器是“双路供电”，两个电源模块分别挂在两条支路上。
* 两路都合闸时，两路平分整机功耗（每条支路承担 P/2）。
* 只剩一路时，该支路承担整机全部功耗 P。
* 每一轮把当前所有“超载”（负载严格大于功率上限）的支路**同时**跳闸，
  然后按剩余合闸支路重算，直到没有超载支路。
* 只有服务器的两条馈电支路都断开时，服务器才掉电。

启动编号（boot_no）只在 掉电 -> 重新有电 的跳变时 +1。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

REQUIRED_READY = 2
HISTORY_LIMIT = 30
REPORT_LIMIT = 20


def _now() -> str:
    return time.strftime("%H:%M:%S")


def watts(value: float) -> int:
    """把 500.0 这样的浮点瓦数显示为整数，其余保留一位小数。"""
    rounded = round(value)
    if abs(value - rounded) < 1e-6:
        return int(rounded)
    return round(value, 1)


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
        # bid -> None(合闸) / "manual"(人工断开) / "trip"(超载跳闸)
        self.open_reason: Dict[str, Optional[str]] = {
            b: None for b in self.branch_order
        }

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

        # 同步跳闸标记
        tripped_now = {t["branch"] for r in result.rounds for t in r["tripped"]}
        self.closed = result.closed
        for bid in self.branch_order:
            if bid in self.closed:
                self.open_reason[bid] = None
            elif bid in tripped_now:
                self.open_reason[bid] = "trip"
            # 其余保持原有 manual/trip 原因

        # 掉电 / 复电与启动编号
        power_events: List[dict] = []
        for sid in self.server_order:
            was = powered_before.get(sid, True)
            now = result.powered.get(sid, False)
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

        self._op_seq += 1
        op = {
            "seq": self._op_seq,
            "at": _now(),
            "kind": kind,
            "targets": list(ids),
            "rounds": result.rounds,
            "power_events": power_events,
            "had_trip": bool(result.rounds),
        }
        self.operations.insert(0, op)
        del self.operations[HISTORY_LIMIT:]
        return op

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
                "subtitle": self.sample.get("subtitle", ""),
                "description": self.sample.get("description", ""),
                "golden_path": self.sample.get("golden_path", []),
            },
            "server_power_w": watts(self.server_power_w),
            "branches": branches,
            "servers": servers,
            "cluster": self.cluster_status(),
            "operations": self.operations,
            "reports": self.reports,
        }

    def _feed_closed(self, sid: str, bid: str) -> bool:
        for s in self.sample["servers"]:
            if s["id"] == sid:
                return any(f["branch"] == bid for f in s["feeds"]) and bid in self.closed
        return False
