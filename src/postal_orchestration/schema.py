"""定义跨境邮政路由与责任编排平台在 SQLite 中的表结构。

设计要点：
- 交接、谱系、事件、责任记录均为追加式，完成过的交接与时间记录不可重写；
- 容量预留与幂等回执同事务提交，重放事件不会多占资源；
- 规则按版本追加，旧版本保留用于回看。
"""

POSTAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS postal_operators (
    operator_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','node','carrier','compliance','support')),
    node_id TEXT,
    carrier_id TEXT,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    jurisdiction TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','restricted','closed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_status_events (
    event_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    segment_id TEXT PRIMARY KEY,
    from_node TEXT NOT NULL REFERENCES nodes(node_id),
    to_node TEXT NOT NULL REFERENCES nodes(node_id),
    carrier_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 1),
    transit_hours INTEGER NOT NULL CHECK(transit_hours >= 1),
    priority INTEGER NOT NULL CHECK(priority >= 0),
    status TEXT NOT NULL CHECK(status IN ('active','suspended')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    max_transit_hours INTEGER NOT NULL CHECK(max_transit_hours >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    jurisdiction TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('origin','transit','destination')),
    rule_type TEXT NOT NULL CHECK(rule_type IN ('prohibited_category','value_cap','requires_proof')),
    expression_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','retired')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, version)
);
CREATE TABLE IF NOT EXISTS parcels (
    parcel_id TEXT PRIMARY KEY,
    origin_node TEXT NOT NULL REFERENCES nodes(node_id),
    destination_node TEXT NOT NULL REFERENCES nodes(node_id),
    origin_jurisdiction TEXT NOT NULL,
    destination_jurisdiction TEXT NOT NULL,
    category TEXT NOT NULL,
    declared_value REAL NOT NULL CHECK(declared_value >= 0),
    currency TEXT NOT NULL,
    weight_grams INTEGER NOT NULL CHECK(weight_grams >= 1),
    commitment_id TEXT NOT NULL REFERENCES commitments(commitment_id),
    state TEXT NOT NULL CHECK(state IN
        ('accepted','planned','loaded','in_transit','arrived','delivered','exception')),
    current_node TEXT NOT NULL,
    current_container TEXT,
    declaration_version INTEGER NOT NULL CHECK(declaration_version >= 1),
    accepted_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    delivered_at TEXT,
    commitment_outcome TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS declarations (
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    declared_value REAL NOT NULL CHECK(declared_value >= 0),
    currency TEXT NOT NULL,
    category TEXT NOT NULL,
    proofs_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (parcel_id, version)
);
CREATE TABLE IF NOT EXISTS route_plans (
    plan_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    state TEXT NOT NULL CHECK(state IN ('active','superseded','completed','abandoned')),
    reason_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (parcel_id, version)
);
CREATE TABLE IF NOT EXISTS route_plan_legs (
    plan_id TEXT NOT NULL REFERENCES route_plans(plan_id),
    leg_index INTEGER NOT NULL,
    segment_id TEXT NOT NULL REFERENCES segments(segment_id),
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    carrier_id TEXT NOT NULL,
    transit_hours INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','in_use','completed','skipped')),
    reason TEXT NOT NULL,
    PRIMARY KEY (plan_id, leg_index)
);
CREATE TABLE IF NOT EXISTS standby_routes (
    standby_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    plan_version INTEGER NOT NULL,
    rank INTEGER NOT NULL CHECK(rank >= 1),
    legs_json TEXT NOT NULL,
    reason_json TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('ready','promoted','failed','stale')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS containers (
    container_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL REFERENCES nodes(node_id),
    segment_id TEXT NOT NULL REFERENCES segments(segment_id),
    destination_node TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 1),
    loaded_count INTEGER NOT NULL CHECK(loaded_count >= 0),
    state TEXT NOT NULL CHECK(state IN ('open','sealed','in_transit','arrived','inspecting','opened')),
    inspection_return_state TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS container_membership (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    action TEXT NOT NULL CHECK(action IN ('loaded','unloaded')),
    node_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_membership_parcel ON container_membership(parcel_id);
CREATE INDEX IF NOT EXISTS idx_membership_container ON container_membership(container_id);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL REFERENCES segments(segment_id),
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    units INTEGER NOT NULL CHECK(units >= 1),
    state TEXT NOT NULL CHECK(state IN ('held','released')),
    created_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reservations_segment ON reservations(segment_id, state);
CREATE TABLE IF NOT EXISTS handovers (
    handover_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('dispatch','arrival')),
    container_id TEXT NOT NULL REFERENCES containers(container_id),
    segment_id TEXT,
    node_id TEXT NOT NULL,
    carrier_id TEXT NOT NULL,
    node_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(node_confirmed IN (0, 1)),
    carrier_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(carrier_confirmed IN (0, 1)),
    compliance_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(compliance_confirmed IN (0, 1)),
    state TEXT NOT NULL CHECK(state IN ('pending','completed','cancelled')),
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS parcel_holds (
    hold_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    public_reason TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('open','released')),
    opened_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_holds_parcel ON parcel_holds(parcel_id, state);
CREATE TABLE IF NOT EXISTS rule_evaluations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    parcel_id TEXT NOT NULL,
    plan_id TEXT,
    rule_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    jurisdiction TEXT NOT NULL,
    scope TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('pass','violation')),
    reason TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evaluations_parcel ON rule_evaluations(parcel_id);
CREATE TABLE IF NOT EXISTS responsibility_records (
    record_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    container_id TEXT,
    stage TEXT NOT NULL,
    responsible_party TEXT NOT NULL,
    reason TEXT NOT NULL,
    node_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_responsibility_parcel ON responsibility_records(parcel_id);
CREATE TABLE IF NOT EXISTS waitlist_entries (
    entry_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES parcels(parcel_id),
    node_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('waiting','promoted','cancelled')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_waitlist_node ON waitlist_entries(node_id, state);
CREATE TABLE IF NOT EXISTS impact_reports (
    report_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parcel_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    parcel_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parcel_events ON parcel_events(parcel_id);
CREATE TABLE IF NOT EXISTS container_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_container_events ON container_events(container_id);
"""
