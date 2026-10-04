# 编排跨境邮政中转协作基础服务

本项目提供跨境数字贸易合作业务共享的服务端基础能力，负责合作机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

## 目录

- src/digital_trade_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/postal_orchestration/：跨境邮政路由与责任编排平台（规划器、领域服务、HTTP 边界、离线验收）；
- tests/：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m digital_trade_foundation.acceptance
    PYTHONPATH=src python3 -m postal_orchestration.acceptance

基础服务验收会在临时 SQLite 数据库中登记合作机构、操作者、业务节点和参考资料，核对幂等回执与审计链。邮政平台验收会跑通一条完整中转链：收寄与规划、合包、封袋、三方交接、口岸查验、口岸关闭改道与候补转正、超时定责、规则版本变更与申报更正、两次进程重启后的状态一致性，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m digital_trade_foundation.api --database digital_trade.sqlite3 --host 127.0.0.1 --port 8080
    PYTHONPATH=src python3 -m postal_orchestration.api --database postal.sqlite3 --host 127.0.0.1 --port 8081

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者（actor_id 只放在请求头，不放在请求体），服务重启后 SQLite 中的业务状态和审计历史继续保留。postal_orchestration.api 会把 /postal/* 请求分派给邮政平台，其余路径回落到基础服务。

## 跨境邮政路由与责任编排平台

平台接收包裹事实、申报及证明版本、来源和目的辖区规则、服务承诺、口岸、运输区段、容器容量与交接资格，生成带理由的中转方案，并覆盖以下能力：

- **全生命周期谱系**：合包、拆包、封袋、转运、查验、改道、签收都追加包裹事件与容器事件，包裹与容器保持双向谱系（container_membership），完成过的交接和时间记录不可重写；
- **定向重估**：规则按版本发布、申报按版本提交，变化只重估尚未完成且确实命中的包裹，已签收或已扣留的对象不受影响；
- **原子容量与稳定候补**：封袋时在单个 IMMEDIATE 事务中原子占用区段容量，请求重放命中幂等回执不会多占资源；备用路线按 (总耗时, 程数, 区段编号) 稳定排序，改道时先尝试既有候补再全量重算，无路可走进入候补队列，口岸或区段恢复后按位次转正；
- **三方交接与披露控制**：发运交接需节点、承运方、合规各自确认，到达交接需承运方与节点确认；客户支持只能查询可披露的状态码与原因（support_view），无法查看内部追溯；
- **重启一致性**：在途容器、候补路线、超时责任全部持久化在 SQLite，进程重启后状态一致，可继续办理；
- **双向追溯**：parcel_trace 从一个包裹回看全部规则评估版本、容器谱系、交接与责任人；node_impact 从一次口岸关闭正向推导受影响承诺与改道结果。

主要接口（POST 均需 request_id 幂等键）：

- 登记：/postal/operators、/postal/nodes、/postal/segments、/postal/commitments、/postal/rules；
- 收寄与申报：/postal/parcels、/postal/parcels/declarations；
- 容器：/postal/containers、/postal/containers/load、/postal/containers/unload、/postal/containers/seal、/postal/containers/arrive、/postal/containers/inspect、/postal/containers/inspect/close；
- 交接与签收：/postal/handovers/confirm、/postal/parcels/deliver、/postal/parcels/reroute、/postal/timeouts/evaluate；
- 口岸与区段状态：/postal/nodes/status、/postal/segments/status、/postal/segments/capacity；
- 查询（GET）：/postal/parcels/trace、/postal/parcels/support-view、/postal/containers/lineage、/postal/nodes/impact、/postal/waitlist。
