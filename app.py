"""机房配电支路检修演练页 —— Flask 入口。

页面：GET /
JSON API：
  GET  /api/state
  POST /api/reset                      {"sample": "cross"|"chain"}
  POST /api/branches/<bid>/disconnect
  POST /api/branches/<bid>/close
  POST /api/branches/close-many        {"ids": ["WL3","WL4"]}
  POST /api/replicas/<sid>/ready       {"boot_no": 2} 或 {"stale": true}

HTML 表单走 /ui/... 路由，flash 后 PRG 重定向回页面。
演练状态保存在单进程内存中，因此 gunicorn 必须使用 1 个 worker。
"""

from __future__ import annotations

import threading

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from power_model import Drill
from samples import DEFAULT_SAMPLE, SAMPLES, get_sample

app = Flask(__name__)
app.secret_key = "power-drill-local-secret"  # 演练工具，无登录态
app.json.ensure_ascii = False  # 让 curl 直接看到中文报错信息

_LOCK = threading.RLock()
_STATE: Drill = Drill(get_sample(DEFAULT_SAMPLE))


def _reset(sample_id: str | None) -> Drill:
    global _STATE
    _STATE = Drill(get_sample(sample_id))
    return _STATE


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    with _LOCK:
        view = _STATE.view()
    return render_template("index.html", v=view, sample_ids=list(SAMPLES.keys()))


@app.get("/healthz")
def healthz():
    return "ok", 200


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@app.get("/api/state")
def api_state():
    with _LOCK:
        return jsonify(_STATE.view())


@app.post("/api/reset")
def api_reset():
    data = request.get_json(silent=True) or {}
    sample_id = data.get("sample") or request.form.get("sample")
    with _LOCK:
        drill = _reset(sample_id)
        return jsonify({"ok": True, "state": drill.view()})


@app.post("/api/branches/<branch_id>/disconnect")
def api_disconnect(branch_id: str):
    with _LOCK:
        try:
            op = _STATE.disconnect(branch_id)
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        return jsonify({"ok": True, "operation": op, "state": _STATE.view()})


@app.post("/api/branches/<branch_id>/close")
def api_close(branch_id: str):
    with _LOCK:
        try:
            op = _STATE.close(branch_id)
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        return jsonify({"ok": True, "operation": op, "state": _STATE.view()})


@app.post("/api/branches/close-many")
def api_close_many():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or request.form.getlist("ids") or []
    if isinstance(ids, str):
        ids = [ids]
    with _LOCK:
        op = _STATE.close_many(ids)
        return jsonify({"ok": True, "operation": op, "state": _STATE.view()})


@app.post("/api/replicas/<server_id>/ready")
def api_ready(server_id: str):
    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict()
    stale = bool(data.get("stale")) or str(data.get("mode", "")) == "stale"
    boot_no = data.get("boot_no")
    try:
        boot_no = int(boot_no) if boot_no not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "boot_no 必须是整数"}), 400
    if boot_no is None and not stale:
        return jsonify({"ok": False, "error": "缺少 boot_no"}), 400

    with _LOCK:
        try:
            report = _STATE.confirm_ready(
                server_id, boot_no, stale_probe=stale
            )
        except KeyError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        status = 200 if report["accepted"] else 409
        return (
            jsonify(
                {
                    "ok": report["accepted"],
                    "report": report,
                    "state": _STATE.view(),
                }
            ),
            status,
        )


# ---------------------------------------------------------------------------
# HTML 表单路由（无 JS 也能完整演练）
# ---------------------------------------------------------------------------


def _back():
    return redirect(url_for("index") + "#drill-top")


@app.post("/ui/reset")
def ui_reset():
    sample_id = request.form.get("sample")
    with _LOCK:
        _reset(sample_id)
    flash("演练已复位到初始健康状态（三副本就绪、启动编号 #1）。", "ok")
    return _back()


@app.post("/ui/samples/<sample_id>/load")
def ui_load_sample(sample_id: str):
    with _LOCK:
        _reset(sample_id)
    flash(f"已载入{get_sample(sample_id)['name']}，状态已重置。", "ok")
    return _back()


@app.post("/ui/branches/<branch_id>/disconnect")
def ui_disconnect(branch_id: str):
    with _LOCK:
        try:
            op = _STATE.disconnect(branch_id)
        except KeyError:
            flash(f"未知支路 {branch_id}", "err")
            return _back()
    if op["rounds"]:
        names = [
            "、".join(t["branch"] for t in r["tripped"]) for r in op["rounds"]
        ]
        flash(
            f"已断开 {branch_id}；连锁跳闸 {len(op['rounds'])} 轮："
            + " → ".join(f"第{r['index']}轮 {names[r['index']-1]}" for r in op["rounds"]),
            "trip",
        )
    else:
        flash(f"已断开 {branch_id}，未发生超载跳闸。", "ok")
    return _back()


@app.post("/ui/branches/<branch_id>/close")
def ui_close(branch_id: str):
    with _LOCK:
        try:
            op = _STATE.close(branch_id)
        except KeyError:
            flash(f"未知支路 {branch_id}", "err")
            return _back()
    if op["rounds"]:
        names = [
            "、".join(t["branch"] for t in r["tripped"]) for r in op["rounds"]
        ]
        flash(
            f"已手动合闸 {branch_id}，但再次超载并跳闸 "
            + "；".join(f"第{r['index']}轮：{names[r['index']-1]}" for r in op["rounds"])
            + "。请按推荐顺序或并列合闸。",
            "err",
        )
    else:
        flash(f"支路 {branch_id} 已手动合闸成功。", "ok")
    return _back()


@app.post("/ui/branches/close-many")
def ui_close_many():
    ids = request.form.getlist("ids")
    with _LOCK:
        op = _STATE.close_many(ids)
    if not ids:
        flash("未勾选任何支路。", "err")
    elif op["rounds"]:
        flash("并列合闸后仍有超载跳闸，请核对容量。", "err")
    else:
        flash("已同时合闸：" + "、".join(op["targets"]) + "，未再跳闸。", "ok")
    return _back()


@app.post("/ui/replicas/<server_id>/ready")
def ui_ready(server_id: str):
    stale = request.form.get("mode") == "stale"
    raw = request.form.get("boot_no")
    try:
        boot_no = int(raw) if raw else None
    except (TypeError, ValueError):
        flash("启动编号必须是整数。", "err")
        return _back()
    if boot_no is None and not stale:
        flash("请填写启动编号（或使用“用旧编号试报”）。", "err")
        return _back()
    with _LOCK:
        try:
            report = _STATE.confirm_ready(
                server_id, boot_no, stale_probe=stale
            )
        except KeyError:
            flash(f"未知副本 {server_id}", "err")
            return _back()
    flash(report["message"], "ok" if report["accepted"] else "err")
    return _back()


if __name__ == "__main__":
    # 单进程多线程，保证内存演练状态一致；Docker 中使用 gunicorn 1 worker。
    app.run(host="0.0.0.0", port=8000, threaded=True)
