"""内置演练样例：三副本数据库集群 + 可触发连锁跳闸的双路供电。

容量数值由脚本推演挑选，保证：
* 初始完整合闸时不超载；
* 剧本中的断开操作会产生多轮连锁跳闸；
* 存在明确的手动复电顺序（其中样例一的第三个副本需两路并列合闸）。
"""

from __future__ import annotations

SAMPLES = {
    # ------------------------------------------------------------------
    # 样例一：交叉双路 —— 第 1 轮两条支路“同时”跳闸，第 2 轮再跳一条
    # ------------------------------------------------------------------
    "cross": {
        "id": "cross",
        "name": "样例一 · 交叉双路：同轮双跳",
        "subtitle": "断开 WL1，触发 WL2/WL3 同轮跳闸、WL4 次轮跳闸",
        "server_power_w": 1000,
        "description": (
            "三台数据库副本服务器均为双电源供电。WL1 带 db-1、db-2 的电源1；"
            "WL2 只带 db-1 电源2；WL3 跨带 db-2 电源2 与 db-3 电源1；"
            "WL4 带 db-3 电源2。断开 WL1 后，db-1 把全部功耗压给 WL2、"
            "db-2 把全部功耗压给 WL3，与 db-3 的半载叠加导致 WL2、WL3 "
            "在同一轮超载并同时跳闸；随后 db-3 的全部功耗压给 WL4，引发第二轮。"
        ),
        "golden_path": [
            "在 WL1 行点“断开”，观察第 1 轮 WL2、WL3 同时跳闸，第 2 轮 WL4 跳闸，三副本掉电。",
            "复电到法定多数：先合 WL1（db-1/db-2 满载共 2000W，低于上限 2200W），再合 WL2。",
            "用卡片中的当前启动编号给 db-1、db-2 报就绪，2/3 就绪即显示“集群可用”。",
            "完整恢复 db-3：勾选 WL3、WL4 后点“同时合闸所选”；单独合任一条都会因超载再次跳闸。",
            "可在任一卡片点“用旧编号试报”，验证旧启动编号被拒绝。",
        ],
        "branches": [
            {
                "id": "WL1",
                "label": "WL1 · A列母线支路",
                "capacity_w": 2200,
                "hint": "供 db-1、db-2 电源1，检修断开本支路即触发连锁",
            },
            {
                "id": "WL2",
                "label": "WL2 · 机柜1 B路支路",
                "capacity_w": 750,
                "hint": "仅供 db-1 电源2，单路满载 1000W 即超载",
            },
            {
                "id": "WL3",
                "label": "WL3 · 跨接支路",
                "capacity_w": 1000,
                "hint": "供 db-2 电源2 与 db-3 电源1",
            },
            {
                "id": "WL4",
                "label": "WL4 · 机柜3 B路支路",
                "capacity_w": 750,
                "hint": "仅供 db-3 电源2，单路满载 1000W 即超载",
            },
        ],
        "servers": [
            {
                "id": "db-1",
                "name": "db-1",
                "role": "数据库副本 1",
                "feeds": [
                    {"module": "电源1/A路", "branch": "WL1"},
                    {"module": "电源2/B路", "branch": "WL2"},
                ],
            },
            {
                "id": "db-2",
                "name": "db-2",
                "role": "数据库副本 2",
                "feeds": [
                    {"module": "电源1/A路", "branch": "WL1"},
                    {"module": "电源2/B路", "branch": "WL3"},
                ],
            },
            {
                "id": "db-3",
                "name": "db-3",
                "role": "数据库副本 3",
                "feeds": [
                    {"module": "电源1/A路", "branch": "WL3"},
                    {"module": "电源2/B路", "branch": "WL4"},
                ],
            },
        ],
    },

    # ------------------------------------------------------------------
    # 样例二：经典链式增容 —— 两轮连锁，支持逐条手动合闸完整恢复
    # ------------------------------------------------------------------
    "chain": {
        "id": "chain",
        "name": "样例二 · 共母线链式：完整复电",
        "subtitle": "断开 WL2，WL1 首轮跳闸、WL3 次轮跳闸；按 WL2→WL1→WL3 逐条合闸",
        "server_power_w": 1000,
        "description": (
            "WL1 为三台副本的电源1 公共支路（上限 2000W），WL2 带 db-1、db-2 "
            "电源2（上限 3000W），WL3 带 db-3 电源2（上限 500W，额定半载）。"
            "断开 WL2 后 db-1、db-2 全部功耗压到 WL1，与其带 db-3 的半载叠加至 "
            "2500W，WL1 首轮跳闸；随后 db-3 单路 1000W 压垮 WL3，第二轮跳闸。"
            "复电按 WL2 → WL1 → WL3 顺序逐条合闸即可全部恢复。"
        ),
        "golden_path": [
            "点 WL2 行的“断开”：第 1 轮 WL1 跳闸，第 2 轮 WL3 跳闸。",
            "依次手动合闸 WL2 → WL1 → WL3，三条支路均保持合闸，三台服务器获得新启动编号。",
            "分别确认三个副本就绪（至少两个），观察集群在 2/3 就绪时恢复可用。",
        ],
        "branches": [
            {
                "id": "WL1",
                "label": "WL1 · 副本公共支路",
                "capacity_w": 2000,
                "hint": "带三台服务器电源1",
            },
            {
                "id": "WL2",
                "label": "WL2 · 1/2号副本 B路",
                "capacity_w": 3000,
                "hint": "带 db-1、db-2 电源2，容量充裕，适合作复电第一路",
            },
            {
                "id": "WL3",
                "label": "WL3 · 3号副本 B路",
                "capacity_w": 500,
                "hint": "正常半载 500W 恰好满载，单路 1000W 必跳",
            },
        ],
        "servers": [
            {
                "id": "db-1",
                "name": "db-1",
                "role": "数据库副本 1",
                "feeds": [
                    {"module": "电源1", "branch": "WL1"},
                    {"module": "电源2", "branch": "WL2"},
                ],
            },
            {
                "id": "db-2",
                "name": "db-2",
                "role": "数据库副本 2",
                "feeds": [
                    {"module": "电源1", "branch": "WL1"},
                    {"module": "电源2", "branch": "WL2"},
                ],
            },
            {
                "id": "db-3",
                "name": "db-3",
                "role": "数据库副本 3",
                "feeds": [
                    {"module": "电源1", "branch": "WL1"},
                    {"module": "电源2", "branch": "WL3"},
                ],
            },
        ],
    },
}

DEFAULT_SAMPLE = "cross"


def get_sample(sample_id: str | None) -> dict:
    if sample_id and sample_id in SAMPLES:
        return SAMPLES[sample_id]
    return SAMPLES[DEFAULT_SAMPLE]
