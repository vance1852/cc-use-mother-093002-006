"""规则匹配与候选路径计算的纯函数，便于独立测试。"""

from __future__ import annotations

from typing import Any


def selector_matches(selector: dict[str, Any], declaration: dict[str, Any]) -> bool:
    """判断规则选择器是否命中当前申报版本。"""

    category = selector.get("item_category")
    if category is not None and category != declaration["item_category"]:
        return False
    min_value = selector.get("min_value_minor")
    if min_value is not None and declaration["declared_value_minor"] < int(min_value):
        return False
    max_value = selector.get("max_value_minor")
    if max_value is not None and declaration["declared_value_minor"] > int(max_value):
        return False
    return True


def rule_violation(rule: dict[str, Any], declaration: dict[str, Any]) -> str | None:
    """返回规则对申报的阻断原因；不阻断时返回 None。"""

    if not selector_matches(rule["selector"], declaration):
        return None
    constraint = rule["constraint"]
    if rule["rule_type"] == "prohibited_category":
        return f"品类 {declaration['item_category']} 在辖区 {rule['jurisdiction']} 被禁运"
    if rule["rule_type"] == "proof_required":
        proof_type = constraint.get("proof_type", "")
        proofs = {proof.get("type") for proof in declaration.get("proofs", [])}
        if proof_type not in proofs:
            return f"缺少辖区 {rule['jurisdiction']} 要求的证明文件 {proof_type}"
    if rule["rule_type"] == "value_threshold":
        proof_type = constraint.get("requires_proof")
        if proof_type:
            proofs = {proof.get("type") for proof in declaration.get("proofs", [])}
            if proof_type not in proofs:
                return f"申报价值达到辖区 {rule['jurisdiction']} 门槛，缺少证明 {proof_type}"
    return None


def active_rules(rows: list[Any]) -> list[dict[str, Any]]:
    """把规则表行转换为可评估的字典列表。"""

    import json

    rules = []
    for row in rows:
        rules.append({
            "rule_id": row["rule_id"],
            "version": row["version"],
            "jurisdiction": row["jurisdiction"],
            "rule_scope": row["rule_scope"],
            "rule_type": row["rule_type"],
            "selector": json.loads(row["selector_json"]),
            "constraint": json.loads(row["constraint_json"]),
        })
    return rules


def jurisdictions_for_path(parcel: dict[str, Any], gateway_rows: dict[str, Any],
                           path_gateway_ids: list[str]) -> dict[str, list[str]]:
    """按起点、终点、途经拆分一条路径涉及的辖区。"""

    transit_countries: list[str] = []
    for gateway_id in path_gateway_ids[1:-1]:
        country = gateway_rows[gateway_id]["country"]
        if country not in transit_countries:
            transit_countries.append(country)
    return {
        "origin": [parcel["origin_region"]],
        "destination": [parcel["destination_country"]],
        "transit": transit_countries,
    }


def evaluate_path(rules: list[dict[str, Any]], parcel: dict[str, Any],
                  declaration: dict[str, Any], gateway_rows: dict[str, Any],
                  path_gateway_ids: list[str]) -> tuple[bool, list[dict[str, Any]]]:
    """评估一条候选路径，返回是否可行及逐条理由。"""

    scopes = jurisdictions_for_path(parcel, gateway_rows, path_gateway_ids)
    reasons: list[dict[str, Any]] = []
    blocked = False
    for rule in rules:
        if rule["jurisdiction"] not in scopes.get(rule["rule_scope"], []):
            continue
        if not selector_matches(rule["selector"], declaration):
            continue
        violation = rule_violation(rule, declaration)
        ref = {"rule_id": rule["rule_id"], "version": rule["version"],
               "jurisdiction": rule["jurisdiction"], "scope": rule["rule_scope"]}
        if violation:
            blocked = True
            reasons.append({"code": "rule_blocked", "message": violation, **ref})
        else:
            reasons.append({"code": "rule_checked", "message":
                            f"规则 {rule['rule_id']} v{rule['version']} 命中且满足", **ref})
    return not blocked, reasons


def find_paths(leg_rows: list[dict[str, Any]], gateway_rows: dict[str, Any],
               origin_gateway_ids: list[str], destination_country: str,
               excluded_gateways: set[str] | None = None,
               excluded_legs: set[str] | None = None,
               remaining: dict[str, int] | None = None,
               max_depth: int = 4) -> list[dict[str, Any]]:
    """枚举从起点口岸到目的国的简单路径，按稳定优先级排序。"""

    excluded_gateways = excluded_gateways or set()
    excluded_legs = excluded_legs or set()
    remaining = remaining or {}
    destinations = {gid for gid, row in gateway_rows.items()
                    if row["country"] == destination_country and row["status"] != "closed"}
    origins = [gid for gid in origin_gateway_ids
               if gid in gateway_rows and gateway_rows[gid]["status"] != "closed"
               and gid not in excluded_gateways]
    adjacency: dict[str, list[dict[str, Any]]] = {}
    for leg in leg_rows:
        if leg["status"] == "suspended" or leg["leg_id"] in excluded_legs:
            continue
        if remaining.get(leg["leg_id"], leg["capacity"]) < 1:
            continue
        if leg["from_gateway"] in excluded_gateways or leg["to_gateway"] in excluded_gateways:
            continue
        if gateway_rows[leg["to_gateway"]]["status"] == "closed":
            continue
        adjacency.setdefault(leg["from_gateway"], []).append(leg)
    for legs in adjacency.values():
        legs.sort(key=lambda leg: (leg["priority"], leg["leg_id"]))

    paths: list[dict[str, Any]] = []

    def walk(current: str, visited: set[str], legs: list[dict[str, Any]]) -> None:
        if len(legs) >= max_depth:
            return
        for leg in adjacency.get(current, []):
            target = leg["to_gateway"]
            if target in visited:
                continue
            next_legs = legs + [leg]
            if target in destinations:
                gateway_ids = [next_legs[0]["from_gateway"]] + [item["to_gateway"] for item in next_legs]
                paths.append({
                    "legs": next_legs,
                    "gateway_ids": gateway_ids,
                    "cost": (len(next_legs),
                             sum(item["priority"] for item in next_legs),
                             tuple(item["leg_id"] for item in next_legs)),
                })
            else:
                walk(target, visited | {target}, next_legs)

    for origin in sorted(origins):
        walk(origin, {origin}, [])
    paths.sort(key=lambda item: item["cost"])
    return paths
