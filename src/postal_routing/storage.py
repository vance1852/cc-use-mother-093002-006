"""在基础服务的数据库上安装邮政路由领域表。"""

from __future__ import annotations

from digital_trade_foundation.storage import Database


POSTAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS postal_commitments (
    commitment_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    promised_hours INTEGER NOT NULL CHECK(promised_hours > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postal_gateways (
    gateway_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    region TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','restricted','closed')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postal_legs (
    leg_id TEXT PRIMARY KEY,
    from_gateway TEXT NOT NULL REFERENCES postal_gateways(gateway_id),
    to_gateway TEXT NOT NULL REFERENCES postal_gateways(gateway_id),
    carrier_id TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    priority INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','reduced','suspended')),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postal_parcels (
    parcel_id TEXT PRIMARY KEY,
    origin_region TEXT NOT NULL,
    destination_country TEXT NOT NULL,
    weight_grams INTEGER NOT NULL CHECK(weight_grams > 0),
    commitment_id TEXT NOT NULL REFERENCES postal_commitments(commitment_id),
    status TEXT NOT NULL CHECK(status IN
        ('registered','planned','consolidated','in_transit','arrived',
         'inspection','held','delivered')),
    current_gateway TEXT,
    declaration_version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postal_declarations (
    parcel_id TEXT NOT NULL REFERENCES postal_parcels(parcel_id),
    version INTEGER NOT NULL,
    declared_value_minor INTEGER NOT NULL CHECK(declared_value_minor >= 0),
    currency TEXT NOT NULL,
    item_category TEXT NOT NULL,
    proofs_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(parcel_id, version)
);
CREATE TABLE IF NOT EXISTS postal_rules (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    jurisdiction TEXT NOT NULL,
    rule_scope TEXT NOT NULL CHECK(rule_scope IN ('origin','destination','transit')),
    rule_type TEXT NOT NULL CHECK(rule_type IN
        ('prohibited_category','value_threshold','proof_required','co_bag_restriction')),
    selector_json TEXT NOT NULL,
    constraint_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(rule_id, version)
);
CREATE TABLE IF NOT EXISTS postal_containers (
    container_id TEXT PRIMARY KEY,
    capacity INTEGER NOT NULL CHECK(capacity > 0),
    state TEXT NOT NULL CHECK(state IN ('open','sealed','in_transit','arrived','inspection')),
    current_gateway TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS postal_container_items (
    container_id TEXT NOT NULL REFERENCES postal_containers(container_id),
    parcel_id TEXT NOT NULL REFERENCES postal_parcels(parcel_id),
    added_at TEXT NOT NULL,
    PRIMARY KEY(container_id, parcel_id)
);
CREATE TABLE IF NOT EXISTS postal_plans (
    plan_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES postal_parcels(parcel_id),
    version INTEGER NOT NULL,
    origin_gateway TEXT NOT NULL,
    legs_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','superseded','completed','cancelled')),
    created_at TEXT NOT NULL,
    UNIQUE(parcel_id, version)
);
CREATE TABLE IF NOT EXISTS postal_plan_alternatives (
    plan_id TEXT NOT NULL REFERENCES postal_plans(plan_id),
    rank INTEGER NOT NULL,
    legs_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    PRIMARY KEY(plan_id, rank)
);
CREATE TABLE IF NOT EXISTS postal_reservations (
    reservation_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    plan_id TEXT NOT NULL REFERENCES postal_plans(plan_id),
    leg_id TEXT NOT NULL REFERENCES postal_legs(leg_id),
    units INTEGER NOT NULL CHECK(units > 0),
    status TEXT NOT NULL CHECK(status IN ('held','consumed','released')),
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, leg_id)
);
CREATE TABLE IF NOT EXISTS postal_lineage (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    parcel_id TEXT NOT NULL REFERENCES postal_parcels(parcel_id),
    container_id TEXT,
    action TEXT NOT NULL,
    gateway_id TEXT,
    leg_id TEXT,
    actor_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lineage_parcel ON postal_lineage(parcel_id, sequence);
CREATE INDEX IF NOT EXISTS idx_lineage_container ON postal_lineage(container_id, sequence);
CREATE TABLE IF NOT EXISTS postal_confirmations (
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    party TEXT NOT NULL CHECK(party IN ('node','carrier','compliance')),
    actor_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('confirmed','rejected')),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(resource_type, resource_id, party)
);
CREATE TABLE IF NOT EXISTS postal_responsibilities (
    responsibility_id TEXT PRIMARY KEY,
    parcel_id TEXT NOT NULL REFERENCES postal_parcels(parcel_id),
    party_type TEXT NOT NULL CHECK(party_type IN ('node','carrier','compliance')),
    party_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    opened_at TEXT NOT NULL,
    resolved_at TEXT
);
"""


def install(database: Database) -> None:
    """把邮政路由表安装到既有数据库连接上。"""

    database.connection.executescript(POSTAL_SCHEMA)
