"""董事会组合评议：情景预测、稳健组合建议与原子批准。

- 情景预测按项目登记并带修订号，必须给出多情景概率区间；
  单一数字、零区间或确定概率都不能包装成保证收益。
- 评议开启前枚举候选组合，说明收益区间、共享资源占用与放弃其他组合的代价。
- 开启评议固定项目依据、预测修订与伙伴承诺修订；批准在同一事务中重核，
  伙伴承诺变更或两个评议同时争取同一资源，都不会造成超额批准。
- 批准只推进选中的项目，保留实际占用与当时采用的预测；
  后来的预测只能发起新评议，不能改写旧决定。
"""
from __future__ import annotations

import itertools
import json
from typing import Any, Mapping, Sequence

from export_portfolio.intake.initiative import (
    ContractError, DomainRecord, canonical_json, finite_number, identifier, stable_hash, utc_now)
from export_portfolio.stages.investment_gates import STATES

MAX_CANDIDATES = 16

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenario_forecasts(
    forecast_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL,
    scenarios_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS board_reviews(
    review_id TEXT PRIMARY KEY,
    basis_hash TEXT NOT NULL,
    review_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved')),
    opened_at TEXT NOT NULL,
    approved_at TEXT
);
"""


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label}无效")
    return value.strip()


def _scenarios(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise ContractError("情景预测至少需要两个情景，单一结果不能包装成保证收益")
    cleaned: list[dict[str, Any]] = []
    total = 0.0
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ContractError("情景条目必须是对象")
        name = _text(entry.get("name"), "情景名称")
        probability = finite_number(entry.get("probability"))
        if not 0.0 < probability < 1.0:
            raise ContractError("情景概率必须介于0和1之间，不能表示确定收益")
        revenue = finite_number(entry.get("revenue"))
        if revenue < 0:
            raise ContractError("情景收益不能为负")
        total += probability
        cleaned.append({"name": name, "probability": probability, "revenue": revenue})
    if abs(total - 1.0) > 1e-6:
        raise ContractError("情景概率合计必须等于1")
    revenues = [item["revenue"] for item in cleaned]
    if max(revenues) - min(revenues) <= 0:
        raise ContractError("情景收益没有区间，不能包装成保证收益")
    cleaned.sort(key=lambda item: (item["revenue"], item["name"]))
    return cleaned


def _range(scenarios: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    revenues = [float(item["revenue"]) for item in scenarios]
    expected = sum(float(item["probability"]) * float(item["revenue"]) for item in scenarios)
    return {"low": min(revenues), "expected": expected, "high": max(revenues)}


class ReviewBoard:
    def __init__(self, repository, resource_book=None):
        self.repository = repository
        self.resource_book = resource_book
        with repository.connect() as connection:
            connection.executescript(SCHEMA)

    @staticmethod
    def _review_row(row) -> dict[str, Any]:
        return {**dict(row), "review": json.loads(row["review_json"])}

    def _record(self, connection, record_id: str) -> DomainRecord:
        row = connection.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise ContractError("项目不存在")
        return DomainRecord(row["record_id"], json.loads(row["payload_json"]), row["state"],
                            row["version"], row["occurred_at"], row["source"])

    @staticmethod
    def _forecast(connection, project_id: str):
        row = connection.execute(
            "SELECT * FROM scenario_forecasts WHERE project_id=?", (project_id,)).fetchone()
        if row is None:
            raise ContractError(f"项目 {project_id} 缺少情景预测")
        return row

    @staticmethod
    def _requirements(connection, project_id: str) -> list[dict[str, Any]]:
        rows = [dict(item) for item in connection.execute(
            "SELECT resource_id,units FROM project_requirements WHERE project_id=? ORDER BY resource_id",
            (project_id,))]
        if not rows:
            raise ContractError(f"项目 {project_id} 未登记资源需求")
        return rows

    def register_forecast(self, forecast_id: str, project_id: str, scenarios: Any) -> int:
        forecast_id = _text(forecast_id, "预测标识")
        project_id = identifier(project_id)
        body = canonical_json(_scenarios(scenarios))
        with self.repository.transaction() as connection:
            if not connection.execute("SELECT 1 FROM records WHERE record_id=?", (project_id,)).fetchone():
                raise ContractError("项目不存在")
            row = connection.execute(
                "SELECT * FROM scenario_forecasts WHERE forecast_id=?", (forecast_id,)).fetchone()
            if row:
                if row["project_id"] != project_id:
                    raise ContractError("预测不能改挂其他项目")
                if row["scenarios_json"] == body:
                    return row["revision"]
                revision = row["revision"] + 1
                connection.execute(
                    "UPDATE scenario_forecasts SET revision=?,scenarios_json=?,updated_at=? WHERE forecast_id=?",
                    (revision, body, utc_now(), forecast_id))
                return revision
            if connection.execute(
                    "SELECT 1 FROM scenario_forecasts WHERE project_id=?", (project_id,)).fetchone():
                raise ContractError("项目已有预测，请修订原预测")
            connection.execute("INSERT INTO scenario_forecasts VALUES(?,?,?,?,?)",
                               (forecast_id, project_id, 1, body, utc_now()))
            return 1

    def _recommendation(self, connection, project_ids: Sequence[str]) -> dict[str, Any]:
        ids = sorted({identifier(item) for item in project_ids})
        if not ids:
            raise ContractError("评议需要候选项目")
        if len(ids) > MAX_CANDIDATES:
            raise ContractError("候选项目过多")
        occupied_projects: set[str] = set()
        for row in connection.execute("SELECT project_json FROM resource_approvals WHERE status='approved'"):
            occupied_projects.update(json.loads(row[0]))
        blocked = occupied_projects & set(ids)
        if blocked:
            raise ContractError(f"项目已经占用资源，不能重复评议: {sorted(blocked)}")
        candidates = []
        for project_id in ids:
            record = self._record(connection, project_id)
            forecast = self._forecast(connection, project_id)
            scenarios = json.loads(forecast["scenarios_json"])
            candidates.append({
                "project_id": project_id,
                "fingerprint": record.fingerprint,
                "forecast_id": forecast["forecast_id"],
                "forecast_revision": forecast["revision"],
                "forecast_hash": stable_hash(scenarios),
                "range": _range(scenarios),
                "requirements": self._requirements(connection, project_id),
            })
        resources: dict[str, dict[str, Any]] = {}
        touched = sorted({req["resource_id"] for item in candidates for req in item["requirements"]})
        for resource_id in touched:
            row = connection.execute(
                "SELECT * FROM delivery_resources WHERE resource_id=?", (resource_id,)).fetchone()
            if row is None:
                raise ContractError(f"资源 {resource_id} 不存在")
            occupied = connection.execute(
                "SELECT COALESCE(SUM(units),0) FROM resource_allocations WHERE resource_id=?",
                (resource_id,)).fetchone()[0]
            resources[resource_id] = {"resource_id": resource_id, "capacity": row["capacity"],
                                      "occupied": occupied, "available": row["capacity"] - occupied,
                                      "revision": row["revision"]}
        combinations = []
        for size in range(1, len(candidates) + 1):
            for combo in itertools.combinations(candidates, size):
                usage: dict[str, int] = {}
                for item in combo:
                    for req in item["requirements"]:
                        usage[req["resource_id"]] = usage.get(req["resource_id"], 0) + req["units"]
                combinations.append({
                    "projects": [item["project_id"] for item in combo],
                    "feasible": all(usage[rid] <= resources[rid]["available"] for rid in usage),
                    "low": sum(item["range"]["low"] for item in combo),
                    "expected": sum(item["range"]["expected"] for item in combo),
                    "high": sum(item["range"]["high"] for item in combo),
                    "usage": usage,
                })
        best = None
        for combo in combinations:
            if not combo["feasible"]:
                continue
            if best is None or (combo["expected"], combo["low"]) > (best["expected"], best["low"]):
                best = combo
        opportunity = max((combo["expected"] for combo in combinations
                           if combo["feasible"] and combo is not best), default=0.0)
        return {
            "candidates": candidates,
            "resources": resources,
            "combinations": combinations,
            "recommended": list(best["projects"]) if best else [],
            "opportunity_cost": opportunity,
            "all_together": combinations[-1],
        }

    def recommend(self, project_ids: Sequence[str]) -> dict[str, Any]:
        with self.repository.connect() as connection:
            return self._recommendation(connection, project_ids)

    def open_review(self, review_id: str, project_ids: Sequence[str]) -> dict[str, Any]:
        review_id = _text(review_id, "评议标识")
        ids = sorted({identifier(item) for item in project_ids})
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM board_reviews WHERE review_id=?", (review_id,)).fetchone()
            if row and row["status"] == "approved":
                stored = json.loads(row["review_json"])["recommendation"]
                if sorted(item["project_id"] for item in stored["candidates"]) != ids:
                    raise ContractError("已有评议不可改写")
                return self._review_row(row)
            recommendation = self._recommendation(connection, ids)
            if not recommendation["recommended"]:
                raise ContractError("没有可行组合，无法形成评议建议")
            basis = {"recommendation": recommendation}
            basis_hash = stable_hash(basis)
            if row:
                if row["basis_hash"] != basis_hash:
                    raise ContractError("已有评议不可改写")
                return self._review_row(row)
            connection.execute(
                "INSERT INTO board_reviews(review_id,basis_hash,review_json,status,opened_at,approved_at)"
                " VALUES(?,?,?,'pending',?,NULL)",
                (review_id, basis_hash, canonical_json(basis), utc_now()))
            self.repository.append_event(connection, "board.review.opened", review_id, 1,
                                         {"basis_hash": basis_hash,
                                          "recommended": recommendation["recommended"]})
            return self._review_row(connection.execute(
                "SELECT * FROM board_reviews WHERE review_id=?", (review_id,)).fetchone())

    def approve_review(self, review_id: str) -> dict[str, Any]:
        review_id = _text(review_id, "评议标识")
        with self.repository.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM board_reviews WHERE review_id=?", (review_id,)).fetchone()
            if row is None:
                raise ContractError("评议不存在")
            if row["status"] == "approved":
                return self._review_row(row)
            recommendation = json.loads(row["review_json"])["recommendation"]
            candidates = {item["project_id"]: item for item in recommendation["candidates"]}
            for project_id, pinned in candidates.items():
                record = self._record(connection, project_id)
                if record.fingerprint != pinned["fingerprint"]:
                    raise ContractError("项目依据已变化，需发起新评议")
                forecast = self._forecast(connection, project_id)
                current_hash = stable_hash(json.loads(forecast["scenarios_json"]))
                if (forecast["revision"], current_hash) != (
                        pinned["forecast_revision"], pinned["forecast_hash"]):
                    raise ContractError("情景预测已更新，需发起新评议")
                if self._requirements(connection, project_id) != pinned["requirements"]:
                    raise ContractError("项目资源需求已变化，需发起新评议")
            selected = list(recommendation["recommended"])
            usage: dict[str, int] = {}
            for project_id in selected:
                for req in candidates[project_id]["requirements"]:
                    usage[req["resource_id"]] = usage.get(req["resource_id"], 0) + req["units"]
            for resource_id, pinned in recommendation["resources"].items():
                current = connection.execute(
                    "SELECT * FROM delivery_resources WHERE resource_id=?", (resource_id,)).fetchone()
                if current is None or current["revision"] != pinned["revision"]:
                    raise ContractError("伙伴承诺已变化，需发起新评议")
                occupied = connection.execute(
                    "SELECT COALESCE(SUM(units),0) FROM resource_allocations WHERE resource_id=?",
                    (resource_id,)).fetchone()[0]
                if occupied + usage.get(resource_id, 0) > current["capacity"]:
                    raise ContractError("共享资源剩余能力不足，不能超额批准")
            for approval in connection.execute(
                    "SELECT project_json FROM resource_approvals WHERE status='approved'"):
                if set(selected) & set(json.loads(approval[0])):
                    raise ContractError("项目已经占用资源")
            if connection.execute(
                    "SELECT 1 FROM resource_approvals WHERE approval_id=?", (review_id,)).fetchone():
                raise ContractError("批准标识已被占用")
            projects = {}
            revisions = {}
            for project_id in selected:
                pinned = candidates[project_id]
                projects[project_id] = {"evidence": pinned["fingerprint"],
                                        "requirements": pinned["requirements"]}
                for req in pinned["requirements"]:
                    revisions[req["resource_id"]] = recommendation["resources"][req["resource_id"]]["revision"]
            evidence_hash = stable_hash({"projects": projects, "revisions": revisions})
            connection.execute("INSERT INTO resource_approvals VALUES(?,?,?,'approved')",
                               (review_id, evidence_hash, canonical_json(projects)))
            for resource_id, units in sorted(usage.items()):
                connection.execute("INSERT INTO resource_allocations VALUES(?,?,?)",
                                   (review_id, resource_id, units))
            connection.execute("UPDATE board_reviews SET status='approved',approved_at=? WHERE review_id=?",
                               (utc_now(), review_id))
            advanced = []
            for project_id in selected:
                record = self._record(connection, project_id)
                index = STATES.index(record.state)
                if index + 1 < len(STATES):
                    moved = record.with_state(STATES[index + 1])
                    self.repository.store_record(connection, moved, expected_version=record.version)
                    advanced.append({"project_id": project_id, "from": record.state, "to": moved.state})
                else:
                    advanced.append({"project_id": project_id, "from": record.state, "to": record.state})
            self.repository.append_event(connection, "board.review.approved", review_id, 1,
                                         {"selected": selected, "usage": usage,
                                          "advanced": advanced, "basis_hash": row["basis_hash"]})
            return self._review_row(connection.execute(
                "SELECT * FROM board_reviews WHERE review_id=?", (review_id,)).fetchone())

    def inspect(self) -> dict[str, Any]:
        with self.repository.connect() as connection:
            forecasts = [{**dict(row), "scenarios": json.loads(row["scenarios_json"])}
                         for row in connection.execute(
                             "SELECT * FROM scenario_forecasts ORDER BY forecast_id")]
            reviews = [self._review_row(row) for row in connection.execute(
                "SELECT * FROM board_reviews ORDER BY opened_at,review_id")]
        return {"forecasts": forecasts, "reviews": reviews}
