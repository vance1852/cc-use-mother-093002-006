"""跨境邮政路由的纯函数规划器：候选路径枚举、规则评估与稳定优先级。

规划器不访问数据库，所有输入由服务层组装，保证结果可重放、可测试：
- 候选路径按 (总耗时, 程数, 区段编号元组) 稳定排序；
- 备用路线按同一顺序取前 N 条，优先级确定；
- 每条规则评估都返回可展示的理由。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SegmentInfo:
    """运输区段的规划视图。"""

    segment_id: str
    from_node: str
    to_node: str
    carrier_id: str
    mode: str
    transit_hours: int
    priority: int
    status: str


@dataclass(frozen=True)
class RuleInfo:
    """辖区规则的规划视图。"""

    rule_id: str
    version: int
    jurisdiction: str
    scope: str
    rule_type: str
    expression: dict[str, Any]


@dataclass(frozen=True)
class ParcelFacts:
    """参与规则评估的包裹事实。"""

    parcel_id: str
    category: str
    declared_value: float
    currency: str
    proof_kinds: frozenset[str]
    origin_node: str
    destination_node: str


def evaluate_rule(rule: RuleInfo, parcel: ParcelFacts, scope: str) -> tuple[str, str]:
    """评估单条规则，返回 (pass|violation|not_applicable, 理由)。"""

    if rule.scope != scope:
        return "not_applicable", f"规则适用环节为 {rule.scope}，当前环节为 {scope}"
    if rule.rule_type == "prohibited_category":
        banned = list(rule.expression.get("categories") or [])
        if parcel.category in banned:
            return "violation", f"物品类别 {parcel.category} 属于辖区 {rule.jurisdiction} 的禁限运清单"
        return "pass", f"物品类别 {parcel.category} 未列入辖区 {rule.jurisdiction} 禁限运清单"
    if rule.rule_type == "value_cap":
        categories = rule.expression.get("categories")
        if categories and parcel.category not in categories:
            return "not_applicable", f"物品类别 {parcel.category} 不在规则适用范围"
        currency = str(rule.expression.get("currency") or "")
        if parcel.currency != currency:
            return "not_applicable", f"申报币种 {parcel.currency} 与规则币种 {currency} 不一致"
        cap = float(rule.expression.get("max_value"))
        if parcel.declared_value > cap:
            return "violation", (
                f"申报价值 {parcel.declared_value} {currency} 超过辖区 {rule.jurisdiction} 上限 {cap}"
            )
        return "pass", f"申报价值 {parcel.declared_value} {currency} 未超过上限 {cap}"
    if rule.rule_type == "requires_proof":
        categories = rule.expression.get("categories")
        if categories and parcel.category not in categories:
            return "not_applicable", f"物品类别 {parcel.category} 不在规则适用范围"
        kind = str(rule.expression.get("proof_kind") or "")
        if kind in parcel.proof_kinds:
            return "pass", f"已提供辖区 {rule.jurisdiction} 要求的 {kind} 证明"
        return "violation", f"缺少辖区 {rule.jurisdiction} 要求的 {kind} 证明"
    return "not_applicable", f"未知规则类型 {rule.rule_type}"


def enumerate_paths(
    segments: list[SegmentInfo], start: str, goal: str, max_depth: int = 4
) -> list[list[SegmentInfo]]:
    """枚举 start 到 goal 的全部简单路径，并按稳定优先级排序。"""

    adjacency: dict[str, list[SegmentInfo]] = {}
    for segment in segments:
        if segment.status != "active":
            continue
        adjacency.setdefault(segment.from_node, []).append(segment)
    for outgoing in adjacency.values():
        outgoing.sort(key=lambda item: (item.transit_hours, item.priority, item.segment_id))

    paths: list[list[SegmentInfo]] = []

    def dfs(node: str, visited: set[str], acc: list[SegmentInfo]) -> None:
        if node == goal:
            paths.append(list(acc))
            return
        if len(acc) >= max_depth:
            return
        for segment in adjacency.get(node, []):
            if segment.to_node in visited:
                continue
            visited.add(segment.to_node)
            acc.append(segment)
            dfs(segment.to_node, visited, acc)
            acc.pop()
            visited.discard(segment.to_node)

    if start != goal:
        dfs(start, {start}, [])
    paths.sort(key=lambda path: (
        sum(item.transit_hours for item in path),
        len(path),
        tuple(item.segment_id for item in path),
    ))
    return paths


def path_scopes(path: list[SegmentInfo], parcel: ParcelFacts) -> list[tuple[str, str]]:
    """返回路径上每个节点的 (node_id, 评估环节)。"""

    nodes = [path[0].from_node] + [segment.to_node for segment in path]
    scopes: list[tuple[str, str]] = []
    for index, node in enumerate(nodes):
        if index == 0 and node == parcel.origin_node:
            scope = "origin"
        elif node == parcel.destination_node:
            scope = "destination"
        else:
            scope = "transit"
        scopes.append((node, scope))
    return scopes


def assess_path(
    path: list[SegmentInfo],
    parcel: ParcelFacts,
    jurisdictions: dict[str, str],
    statuses: dict[str, str],
    rules: list[RuleInfo],
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """评估一条候选路径，返回 (是否可行, 规则评估记录, 说明列表)。"""

    evaluations: list[dict[str, Any]] = []
    notes: list[str] = []
    feasible = True
    for segment in path:
        if segment.status != "active":
            feasible = False
            notes.append(f"区段 {segment.segment_id} 已暂停")
    for node, scope in path_scopes(path, parcel):
        status = statuses.get(node)
        if status == "closed":
            feasible = False
            notes.append(f"口岸 {node} 已关闭")
        elif status == "restricted":
            notes.append(f"口岸 {node} 受限，通行可能延误")
        for rule in rules:
            if rule.jurisdiction != jurisdictions.get(node):
                continue
            result, reason = evaluate_rule(rule, parcel, scope)
            if result == "not_applicable":
                continue
            evaluations.append({
                "rule_id": rule.rule_id,
                "version": rule.version,
                "jurisdiction": rule.jurisdiction,
                "scope": scope,
                "result": result,
                "reason": reason,
            })
            if result == "violation":
                feasible = False
                notes.append(f"辖区 {rule.jurisdiction} 规则 {rule.rule_id}@v{rule.version}：{reason}")
    return feasible, evaluations, notes


def plan_routes(
    parcel: ParcelFacts,
    start: str,
    goal: str,
    segments: list[SegmentInfo],
    jurisdictions: dict[str, str],
    statuses: dict[str, str],
    rules: list[RuleInfo],
    max_standbys: int = 2,
    max_depth: int = 4,
) -> dict[str, Any]:
    """生成主方案与稳定优先级的候补方案，全部附带理由。"""

    paths = enumerate_paths(segments, start, goal, max_depth)
    feasible: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for path in paths:
        ok, evaluations, notes = assess_path(path, parcel, jurisdictions, statuses, rules)
        total_hours = sum(item.transit_hours for item in path)
        if ok:
            feasible.append({
                "legs": path,
                "total_hours": total_hours,
                "notes": notes,
                "evaluations": evaluations,
            })
        else:
            rejections.append({
                "segments": [item.segment_id for item in path],
                "notes": notes,
            })
    if not feasible:
        return {
            "primary": None,
            "standbys": [],
            "evaluations": [],
            "rejections": rejections,
            "summary": f"共 {len(paths)} 条候选路径，均不满足口岸状态或辖区规则",
        }
    primary = feasible[0]
    standbys = feasible[1:1 + max_standbys]
    summary = (
        f"在 {len(paths)} 条候选路径中选择总耗时最低方案"
        f"（{primary['total_hours']} 小时 / {len(primary['legs'])} 程）"
    )
    return {
        "primary": primary,
        "standbys": standbys,
        "evaluations": primary["evaluations"],
        "rejections": rejections,
        "summary": summary,
    }
