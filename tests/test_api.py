"""API 端到端测试：上传、求解、锁定重算、版本失效、确认门控、下载。"""
from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("BREEDER_DB_PATH", str(db_file))
    monkeypatch.setattr(db, "DB_PATH", db_file)
    db.init_db()
    with TestClient(app) as c:
        yield c


INSPECTION_CSV = (
    "行号,植株号,托盘号,品种,温室,株高\n"
    "1,P1,T1,V1,H1,10.5\n"
    "2,P1,T2,V2,H1,11.0\n"
    "3,P2,T3,V2,H2,9.8\n"
)
# 允许组合：V2-H1 非法 -> 行2 品种须改 V1（同株 P1 一致），1 格最优
ALLOWED_CSV = "品种,温室\nV1,H1\nV1,H2\nV2,H2\n"


def upload(client: TestClient, body: str = INSPECTION_CSV, allowed: str = ALLOWED_CSV):
    files = {
        "inspection": ("insp.csv", body.encode("utf-8"), "text/csv"),
        "allowed": ("allow.csv", allowed.encode("utf-8"), "text/csv"),
    }
    r = client.post("/api/sessions", files=files)
    assert r.status_code == 201, r.text
    return r.json()


def wait_job(client, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/jobs/{job_id}")
        status = r.json()["status"]
        if status in ("done", "error", "superseded"):
            return status
        time.sleep(0.1)
    raise AssertionError("作业超时未完成")


def wait_solution(client, sid, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = client.get(f"/api/sessions/{sid}").json()
        if d["solution_status"] in ("optimal", "feasible", "infeasible", "failed", "empty"):
            if not (d["job_status"] in ("pending", "running")):
                return d
        time.sleep(0.1)
    raise AssertionError("方案超时未生成")


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_upload_validation_errors(client):
    # 缺少列
    bad = "行号,植株号\n1,P1\n"
    r = client.post(
        "/api/sessions",
        files={
            "inspection": ("x.csv", bad.encode(), "text/csv"),
            "allowed": ("a.csv", ALLOWED_CSV.encode(), "text/csv"),
        },
    )
    assert r.status_code == 400
    # 超过 30 行
    big = "行号,植株号,托盘号,品种,温室,株高\n" + "".join(
        f"{i},P{i},T{i},V1,H1,10\n" for i in range(1, 32)
    )
    r = client.post(
        "/api/sessions",
        files={
            "inspection": ("x.csv", big.encode(), "text/csv"),
            "allowed": ("a.csv", ALLOWED_CSV.encode(), "text/csv"),
        },
    )
    assert r.status_code == 400 and "30" in r.json()["detail"]
    # 缺允许组合
    r = client.post(
        "/api/sessions",
        files={"inspection": ("x.csv", INSPECTION_CSV.encode(), "text/csv")},
    )
    assert r.status_code == 400


def test_solve_flow_and_optimal_solution(client):
    info = upload(client)
    sid = info["session_id"]
    assert wait_job(client, info["job_id"]) == "done"
    d = wait_solution(client, sid)
    assert d["solution_status"] == "optimal"
    assert d["optimal"] and d["objective_proven"]
    assert d["total_changes"] == 1
    v2 = next(c for c in d["cells"] if c["row_id"] == 2 and c["field"] == "variety")
    assert v2["category"] == "fixed_change"
    assert v2["proposed"] == "V1"
    # 不可改字段原样保留
    row2 = next(r for r in d["rows"] if r["row_id"] == 2)
    assert row2["plant_no"] == "P1" and row2["tray_no"] == "T2" and row2["height"] == "11.0"


def test_confirm_and_download(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    # 未确认不能下载
    r = client.get(f"/api/sessions/{sid}/download")
    assert r.status_code == 409
    r = client.post(f"/api/sessions/{sid}/confirm")
    assert r.status_code == 200 and r.json()["confirmed"]
    r = client.get(f"/api/sessions/{sid}/download")
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert "行号,植株号,托盘号,品种,温室,株高" in text
    lines = [ln for ln in text.strip().split("\n")]
    assert lines[2].startswith("2,P1,T2,V1,H1,11.0")  # 品种已修为 V1
    d = client.get(f"/api/sessions/{sid}").json()
    assert d["confirmed"] is True


def test_lock_bumps_revision_and_blocks_old_confirmation(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    client.post(f"/api/sessions/{sid}/confirm")
    assert client.get(f"/api/sessions/{sid}/download").status_code == 200

    # 锁定行2品种为 V2（候选值），版本递增、旧确认作废、旧方案变 stale
    r = client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 2, "field": "variety", "value": "V2", "source": "candidate"},
    )
    assert r.status_code == 200
    new_rev = r.json()["revision"]
    assert new_rev == 2
    d = client.get(f"/api/sessions/{sid}").json()
    assert d["solution_stale"] is True
    assert d["confirmed"] is False
    # 旧方案立即不可确认
    r = client.post(f"/api/sessions/{sid}/confirm")
    assert r.status_code == 409
    # 下载也被禁止
    assert client.get(f"/api/sessions/{sid}/download").status_code == 409


def test_recalc_after_lock_and_infeasible_conflicts(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    # 行1 锁 V1 且行2 锁 V2，同株 P1 品种冲突 -> 无解，且锁不被修改
    client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 1, "field": "variety", "value": "V1", "source": "candidate"},
    )
    r = client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 2, "field": "variety", "value": "V2", "source": "candidate"},
    )
    r = client.post(f"/api/sessions/{sid}/solve")
    assert r.status_code == 200
    wait_job(client, r.json()["job_id"])
    d = wait_solution(client, sid)
    assert d["solution_status"] == "infeasible"
    assert len(d["conflicts"]) >= 1
    assert any(c["kind"] == "lock" for c in d["conflicts"])
    # 无解不可确认
    assert client.post(f"/api/sessions/{sid}/confirm").status_code == 409
    # 原表仍保留
    assert len(d["rows"]) == 3
    # 解除一个锁后恢复可解
    r = client.delete(f"/api/sessions/{sid}/locks/2/variety")
    assert r.status_code == 200
    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    d = wait_solution(client, sid)
    assert d["solution_status"] == "optimal"
    locked_cell = next(
        c for c in d["cells"] if c["row_id"] == 1 and c["field"] == "variety"
    )
    assert locked_cell["locked"] and locked_cell["locked_value"] == "V1"
    assert all(
        c["locked_value"] != "V2" or c["field"] != "variety" or c["row_id"] != 2
        for c in d["cells"]
    )


def test_lock_original_must_match_original(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    r = client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 2, "field": "variety", "value": "V1", "source": "original"},
    )
    assert r.status_code == 400  # 行2 原品种是 V2
    r = client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 2, "field": "variety", "value": "V2", "source": "original"},
    )
    assert r.status_code == 200


def test_lock_value_out_of_domain_rejected(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    r = client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 1, "field": "house", "value": "HX", "source": "candidate"},
    )
    assert r.status_code == 400


def test_stale_job_superseded_and_late_result_discarded(client):
    # 上传后立即锁定（作业运行前版本已变），旧作业应被取代，不产生覆盖新状态的方案
    info = upload(client)
    sid = info["session_id"]
    client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 3, "field": "variety", "value": "V2", "source": "original"},
    )
    # 等待第一个作业结束（superseded 或 done 都可，但方案必须属于当前 rev 或不存在）
    deadline = time.time() + 20
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{info['job_id']}").json()
        if job["status"] in ("done", "superseded", "error"):
            break
        time.sleep(0.05)
    d = client.get(f"/api/sessions/{sid}").json()
    # 迟到结果若被写入，其 revision 必然小于当前版本（前端会显示过期且禁止确认）
    if d["solution_status"] is not None and d["solution_stale"]:
        assert client.post(f"/api/sessions/{sid}/confirm").status_code == 409
    # 重新求解得到当前版本方案
    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    d = wait_solution(client, sid)
    assert d["solution_status"] == "optimal"
    assert d["solution_stale"] is False


def test_allowed_pairs_json_upload(client):
    files = {"inspection": ("insp.csv", INSPECTION_CSV.encode(), "text/csv")}
    payload = {
        "pairs": [
            {"variety": "V1", "house": "H1"},
            {"variety": "V1", "house": "H2"},
            {"variety": "V2", "house": "H2"},
        ]
    }
    r = client.post(
        "/api/sessions",
        files=files,
        data={"allowed_json": __import__("json").dumps(payload)},
    )
    assert r.status_code == 201, r.text


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "育种巡检" in r.text


def test_failed_input_keeps_session_listing(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    d = client.get(f"/api/sessions/{sid}").json()
    # 求解后原表行数与内容不变
    assert [r["line_no"] for r in d["rows"]] == ["1", "2", "3"]


# ---------------------------------------------------------------- 隔离/恢复
def test_quarantine_requires_reason_and_existing_row(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    r = client.post(
        f"/api/sessions/{sid}/quarantines",
        json={"row_id": 2, "reason": ""},
    )
    assert r.status_code == 422  # pydantic 校验（空原因）
    r = client.post(
        f"/api/sessions/{sid}/quarantines",
        json={"row_id": 99, "reason": "混入其他批次"},
    )
    assert r.status_code == 400
    r = client.post(
        f"/api/sessions/{sid}/quarantines",
        json={"row_id": 2, "reason": "混入其他批次"},
    )
    assert r.status_code == 200 and r.json()["quarantined"] is True
    d = client.get(f"/api/sessions/{sid}").json()
    row2 = next(r for r in d["rows"] if r["row_id"] == 2)
    assert row2["quarantined"] and row2["quarantine_reason"] == "混入其他批次"
    assert d["quarantined_count"] == 1 and d["active_count"] == 2
    assert d["quarantines"][0]["row_id"] == 2


def test_quarantine_invalidates_confirmation_and_download(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    client.post(f"/api/sessions/{sid}/confirm")
    assert client.get(f"/api/sessions/{sid}/download").status_code == 200

    r = client.post(
        f"/api/sessions/{sid}/quarantines",
        json={"row_id": 2, "reason": "其他批次观测"},
    )
    assert r.status_code == 200
    d = client.get(f"/api/sessions/{sid}").json()
    assert d["revision"] == 2
    assert d["solution_stale"] is True and d["confirmed"] is False
    # 旧确认立即失效：确认与下载都被拒绝
    assert client.post(f"/api/sessions/{sid}/confirm").status_code == 409
    assert client.get(f"/api/sessions/{sid}/download").status_code == 409


def test_quarantine_recalc_excludes_row_and_download_skips_it(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    # 隔离原本需要修复的行2（品种 V2 非法），剩余两行 0 改动
    client.post(
        f"/api/sessions/{sid}/quarantines",
        json={"row_id": 2, "reason": "其他批次"},
    )
    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    d = wait_solution(client, sid)
    assert d["solution_status"] == "optimal"
    assert d["solution_stale"] is False
    assert d["total_changes"] == 0
    assert {c["row_id"] for c in d["cells"]} == {1, 3}
    # 原表三行仍全部留存
    assert len(d["rows"]) == 3
    assert client.post(f"/api/sessions/{sid}/confirm").status_code == 200
    r = client.get(f"/api/sessions/{sid}/download")
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    data_lines = [ln for ln in text.strip().split("\n")[1:]]
    # 修复 CSV 只含当前有效行（行1、行3），隔离行2 不进入
    assert len(data_lines) == 2
    assert all(not ln.startswith("2,") for ln in data_lines)
    assert any(ln.startswith("1,") for ln in data_lines)
    assert any(ln.startswith("3,") for ln in data_lines)


def test_inflight_old_solve_cannot_publish_after_quarantine(client):
    # 上传后立即隔离：首个作业基于旧修订号，结束时必须被标记 superseded，
    # 且绝不能把旧方案写成当前方案。
    info = upload(client)
    sid = info["session_id"]
    client.post(
        f"/api/sessions/{sid}/quarantines",
        json={"row_id": 1, "reason": "待现场核实"},
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{info['job_id']}").json()
        if job["status"] in ("done", "superseded", "error"):
            first_status = job["status"]
            break
        time.sleep(0.05)
    else:
        raise AssertionError("首个作业未结束")
    assert first_status == "superseded"
    # 重新求解：当前方案只含有效行，revision 与当前一致
    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    d = wait_solution(client, sid)
    assert d["solution_status"] == "optimal" and d["solution_stale"] is False
    assert {c["row_id"] for c in d["cells"]} == {2, 3}


def test_restore_revives_row_and_its_locks(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    # 先给行1 品种上锁，再隔离行1
    client.post(
        f"/api/sessions/{sid}/locks",
        json={"row_id": 1, "field": "variety", "value": "V1", "source": "original"},
    )
    client.post(
        f"/api/sessions/{sid}/quarantines",
        json={"row_id": 1, "reason": "现场核实中"},
    )
    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    d = wait_solution(client, sid)
    assert {c["row_id"] for c in d["cells"]} == {2, 3}
    # 锁仍在持久层保留
    assert any(l["row_id"] == 1 for l in d["locks"])

    # 恢复：隔离标记移除、修订号递增、旧确认再次失效
    r = client.delete(f"/api/sessions/{sid}/quarantines/1")
    assert r.status_code == 200 and r.json()["quarantined"] is False
    d = client.get(f"/api/sessions/{sid}").json()
    assert d["quarantined_count"] == 0
    row1 = next(x for x in d["rows"] if x["row_id"] == 1)
    assert row1["quarantined"] is False
    # 恢复不存在的隔离行 -> 404
    assert client.delete(f"/api/sessions/{sid}/quarantines/2").status_code == 404

    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    d = wait_solution(client, sid)
    assert d["solution_status"] == "optimal"
    # 原核实值重新生效：行1 品种格仍为 locked V1
    locked_cell = next(
        c for c in d["cells"] if c["row_id"] == 1 and c["field"] == "variety"
    )
    assert locked_cell["locked"] and locked_cell["locked_value"] == "V1"
    assert {c["row_id"] for c in d["cells"]} == {1, 2, 3}


def test_all_quarantined_empty_solution_not_confirmable(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    for rid in (1, 2, 3):
        client.post(
            f"/api/sessions/{sid}/quarantines",
            json={"row_id": rid, "reason": f"行{rid}待核实"},
        )
    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    d = wait_solution(client, sid)
    assert d["solution_status"] == "empty"
    assert d["cells"] == [] and d["active_count"] == 0
    # 空方案不可确认，下载同样被门控
    assert client.post(f"/api/sessions/{sid}/confirm").status_code == 409
    assert client.get(f"/api/sessions/{sid}/download").status_code == 409
    # 原表三行依旧留存
    assert len(d["rows"]) == 3
    # 恢复一行后重新可解、可确认
    client.delete(f"/api/sessions/{sid}/quarantines/2")
    r = client.post(f"/api/sessions/{sid}/solve")
    wait_job(client, r.json()["job_id"])
    d = wait_solution(client, sid)
    assert d["solution_status"] == "optimal"
    assert {c["row_id"] for c in d["cells"]} == {2}


def test_re_quarantine_same_row_updates_reason_and_bumps_revision(client):
    info = upload(client)
    sid = info["session_id"]
    wait_solution(client, sid)
    client.post(
        f"/api/sessions/{sid}/quarantines",
        json={"row_id": 3, "reason": "原因A"},
    )
    r = client.post(
        f"/api/sessions/{sid}/quarantines",
        json={"row_id": 3, "reason": "原因B（补充）"},
    )
    assert r.status_code == 200
    d = client.get(f"/api/sessions/{sid}").json()
    assert d["quarantined_count"] == 1
    row3 = next(x for x in d["rows"] if x["row_id"] == 3)
    assert row3["quarantine_reason"] == "原因B（补充）"
