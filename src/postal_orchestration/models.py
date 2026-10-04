"""跨境邮政编排平台使用的状态常量与客户支持披露策略。"""

ROLES = frozenset({"admin", "node", "carrier", "compliance", "support"})

NODE_STATUSES = frozenset({"open", "restricted", "closed"})
SEGMENT_STATES = frozenset({"active", "suspended"})
RULE_TYPES = frozenset({"prohibited_category", "value_cap", "requires_proof"})
RULE_SCOPES = frozenset({"origin", "transit", "destination"})

PARCEL_STATES = frozenset({
    "accepted", "planned", "loaded", "in_transit", "arrived", "delivered", "exception",
})
CONTAINER_STATES = frozenset({"open", "sealed", "in_transit", "arrived", "inspecting", "opened"})
TERMINAL_PARCEL_STATES = frozenset({"delivered", "exception"})

# 客户支持可披露的状态码与对外文案；内部规则编号、责任方、容器内容不披露。
PUBLIC_STATUS_TEXT = {
    "customs_inspection": "口岸查验中",
    "compliance_review": "合规审核中",
    "capacity_wait": "口岸关闭或运力不足，排队改道中",
    "in_transit": "运输途中",
    "processing": "节点处理中",
    "delivered": "已签收",
    "exception": "查验异常，包裹被扣留",
}
