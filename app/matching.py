"""一对一配对核心算法。

把"仪器结果"与"计划行"之间满足孔位、检测项目相同且时间差不超过容差的
候选关系构成二分图，边权为绝对时间差。

优化目标（字典序）：
  1. 配对数最多；
  2. 配对数最多的前提下，时间差总和最小。

实现分两步：

1. 最小费用最大流（单位容量网络，Dijkstra + 势函数的逐次最短路，SSAP），
   得到一个最优流 f*（最大配对数 F、最小总费用 C）。

2. 刻画 *全部* 最优方案：

   - 候选边 (r, p) 能出现在某个最优方案中：
       * 若 f* 已使用该边，显然可行；
       * 否则当且仅当该边自身的 reduced cost 为 0，且 r、p 在
         "reduced cost = 0 的残余图"中属于同一强连通分量
         （即该边位于零费用交替环上，沿环换配不改变流量与总费用）。
         仅 SCC 相同不充分：rc>0 的边两端也可能经其他零路径同 SCC。

   - 结果 r 可能在某个最优方案中未匹配：
       * 若 f* 中 r 本就未匹配，则显然可以；
       * 否则把 r 的源边容量置 0 后重跑一次 MCMF：若最大流仍为 F 且最小
         费用仍为 C，说明存在一个不使用 r 的最优方案（直接、可证明正确，
         不依赖交替路径的费用符号推理）。

全程整数运算，reduced cost 是否为 0 是精确比较，无浮点误差。
输入上限 50+50，每个分组至多 50 次重跑，规模完全可控。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence, Tuple


class MinCostMaxFlow:
    """整数费用的最小费用最大流（邻接表 + Dijkstra 势函数）。

    边成对存储：下标 e 与 e^1 互为反向边。
    """

    def __init__(self, n: int) -> None:
        self.n = n
        self.graph: List[List[int]] = [[] for _ in range(n)]
        self.frm: List[int] = []
        self.to: List[int] = []
        self.cap: List[int] = []
        self.cost: List[int] = []

    def add_edge(self, u: int, v: int, capacity: int, cost: int) -> int:
        idx = len(self.to)
        self.frm.extend((u, v))
        self.to.extend((v, u))
        self.cap.extend((capacity, 0))
        self.cost.extend((cost, -cost))
        self.graph[u].append(idx)
        self.graph[v].append(idx + 1)
        return idx

    def solve(self, source: int, sink: int) -> Tuple[int, int, List[int]]:
        """返回 (最大流, 最小费用, 最终势函数)。"""
        n = self.n
        potential = [0] * n
        inf = 10**30
        flow = 0
        total_cost = 0

        while True:
            dist = [inf] * n
            prev_v = [-1] * n
            prev_e = [-1] * n
            dist[source] = 0
            heap: List[Tuple[int, int]] = [(0, source)]

            # 带势函数的堆优化 Dijkstra（reduced cost 非负），惰性跳过过期项。
            # 不能在弹出 sink 时提前结束：所有可达节点的最终 dist 都要用于
            # 更新势函数，否则下一轮的 reduced cost 可能变负。
            while heap:
                d, u = heapq.heappop(heap)
                if d != dist[u]:
                    continue
                for e in self.graph[u]:
                    if self.cap[e] <= 0:
                        continue
                    v = self.to[e]
                    reduced = self.cost[e] + potential[u] - potential[v]
                    nd = d + reduced
                    if nd < dist[v]:
                        dist[v] = nd
                        prev_v[v] = u
                        prev_e[v] = e
                        heapq.heappush(heap, (nd, v))

            if dist[sink] == inf:
                break

            for v in range(n):
                if dist[v] < inf:
                    potential[v] += dist[v]

            # 单位容量网络，每次增广 1；沿前驱直接累加原始费用。
            path_cost = 0
            v = sink
            while v != source:
                e = prev_e[v]
                path_cost += self.cost[e]
                self.cap[e] -= 1
                self.cap[e ^ 1] += 1
                v = prev_v[v]
            flow += 1
            total_cost += path_cost

        return flow, total_cost, potential


def _kosaraju(adj: List[List[int]]) -> List[int]:
    """迭代版 Kosaraju，返回每个节点的强连通分量编号。"""
    n = len(adj)
    rev: List[List[int]] = [[] for _ in range(n)]
    for u in range(n):
        for v in adj[u]:
            rev[v].append(u)

    visited = [False] * n
    order: List[int] = []
    for start in range(n):
        if visited[start]:
            continue
        visited[start] = True
        stack: List[Tuple[int, int]] = [(start, 0)]
        while stack:
            u, i = stack[-1]
            if i < len(adj[u]):
                v = adj[u][i]
                stack[-1] = (u, i + 1)
                if not visited[v]:
                    visited[v] = True
                    stack.append((v, 0))
            else:
                order.append(u)
                stack.pop()

    comp = [-1] * n
    cid = 0
    for start in reversed(order):
        if comp[start] != -1:
            continue
        comp[start] = cid
        stack = [start]
        while stack:
            u = stack.pop()
            for v in rev[u]:
                if comp[v] == -1:
                    comp[v] = cid
                    stack.append(v)
        cid += 1
    return comp


@dataclass
class _Network:
    """一个分组对应的流网络及其固定拓扑元数据。"""

    mcmf: MinCostMaxFlow
    source: int
    sink: int
    result_base: int
    plan_base: int
    source_edges: List[int]      # 每条结果的 S->r 边号
    pair_edges: Dict[Tuple[int, int], int]


def _build_network(
    plans: Sequence[Any],
    results: Sequence[Any],
    tolerance_seconds: int,
) -> _Network:
    r_count = len(results)
    p_count = len(plans)

    source = 0
    result_base = 1
    plan_base = 1 + r_count
    sink = 1 + r_count + p_count

    mcmf = MinCostMaxFlow(sink + 1)
    source_edges = [
        mcmf.add_edge(source, result_base + r, 1, 0) for r in range(r_count)
    ]
    for p in range(p_count):
        mcmf.add_edge(plan_base + p, sink, 1, 0)

    pair_edges: Dict[Tuple[int, int], int] = {}
    for r, result in enumerate(results):
        for p, plan in enumerate(plans):
            time_diff = abs(result.measured_at - plan.planned_at)
            if time_diff <= tolerance_seconds:
                pair_edges[(r, p)] = mcmf.add_edge(
                    result_base + r, plan_base + p, 1, time_diff
                )

    return _Network(
        mcmf=mcmf,
        source=source,
        sink=sink,
        result_base=result_base,
        plan_base=plan_base,
        source_edges=source_edges,
        pair_edges=pair_edges,
    )


@dataclass
class GroupSolution:
    """单个 (孔位, 检测项目) 分组的求解结果，索引为组内局部下标。"""

    pair_count: int
    total_time_diff: int
    representative_matching: List[Tuple[int, int]]  # (result_idx, plan_idx)
    eligible_plans: Dict[int, List[int]]
    can_be_unmatched: Dict[int, bool]


def _solve_group(
    plans: Sequence[Any],
    results: Sequence[Any],
    tolerance_seconds: int,
) -> GroupSolution:
    r_count = len(results)
    network = _build_network(plans, results, tolerance_seconds)
    mcmf = network.mcmf

    pair_count, total_cost, potential = mcmf.solve(network.source, network.sink)

    # 代表最优流中的配对（前向边容量耗尽即表示有流）。
    matched_plan: Dict[int, int] = {}
    for (r, p), edge_id in network.pair_edges.items():
        if mcmf.cap[edge_id] == 0:
            matched_plan[r] = p

    # ---- 1) 候选边可选性：reduced cost = 0 残余图上的强连通分量 ----
    zero_graph: List[List[int]] = [[] for _ in range(mcmf.n)]
    for e in range(len(mcmf.to)):
        if mcmf.cap[e] <= 0:
            continue
        u = mcmf.frm[e]
        v = mcmf.to[e]
        reduced_cost = mcmf.cost[e] + potential[u] - potential[v]
        if reduced_cost == 0:
            zero_graph[u].append(v)
    comp = _kosaraju(zero_graph)

    eligible_plans: Dict[int, List[int]] = {r: [] for r in range(r_count)}
    for (r, p), edge_id in network.pair_edges.items():
        rn = network.result_base + r
        pn = network.plan_base + p
        if mcmf.cap[edge_id] == 0:
            # 代表最优流正在使用的边，必然可行。
            eligible_plans[r].append(p)
        else:
            # 未使用边能进入某个最优方案，当且仅当它自身的 reduced cost
            # 为 0 且两端在零费用残余图中同 SCC（即该边位于零费用交替环上）。
            # 只判 SCC 不够：rc>0 的边两端也可能经其他零路径同 SCC。
            rc = mcmf.cost[edge_id] + potential[rn] - potential[pn]
            if rc == 0 and comp[rn] == comp[pn]:
                eligible_plans[r].append(p)
    for r in eligible_plans:
        eligible_plans[r].sort()

    # ---- 2) 可退配性：对已匹配结果禁用其源边后重跑 MCMF ----
    can_be_unmatched: Dict[int, bool] = {}
    unmatched_results = [r for r in range(r_count) if r not in matched_plan]

    if not unmatched_results:
        # 最大流已覆盖全部结果：不存在使任何结果退配而不损失配对数的方案。
        for r in range(r_count):
            can_be_unmatched[r] = False
    else:
        # 网络规模小，重建比重做状态回滚更简单可靠；仅对已匹配结果重跑。
        for r in range(r_count):
            if r not in matched_plan:
                # 代表最优流中就未匹配，显然存在使其未匹配的最优方案。
                can_be_unmatched[r] = True
                continue

            rerun_net = _build_network(plans, results, tolerance_seconds)
            src_edge = rerun_net.source_edges[r]
            rerun_net.mcmf.cap[src_edge] = 0
            rerun_net.mcmf.cap[src_edge ^ 1] = 0
            flow2, cost2, _ = rerun_net.mcmf.solve(
                rerun_net.source, rerun_net.sink
            )
            # 不使用 r 仍能达到相同配对数与相同最小费用 => r 可在某最优方案中缺席。
            can_be_unmatched[r] = (
                flow2 == pair_count and cost2 == total_cost
            )

    return GroupSolution(
        pair_count=pair_count,
        total_time_diff=total_cost,
        representative_matching=[
            (r, p) for r, p in sorted(matched_plan.items())
        ],
        eligible_plans=eligible_plans,
        can_be_unmatched=can_be_unmatched,
    )


@dataclass
class ResultVerdict:
    result_index: int
    eligible_plan_indices: List[int]
    can_be_unmatched: bool


@dataclass
class Reconciliation:
    pair_count: int
    total_time_diff_seconds: int
    representative_matching: List[Tuple[int, int]]  # (result_idx, plan_idx)
    verdicts: List[ResultVerdict]


def solve(
    plan_rows: Sequence[Any],
    result_rows: Sequence[Any],
    tolerance_seconds: int,
) -> Reconciliation:
    """对整批计划/结果做核对。

    孔位或检测项目不同的记录之间没有候选边，因此按 (well_position, analyte)
    分组独立求解，结果再映射回全局下标。
    """
    groups: Dict[Tuple[str, str], Dict[str, List[int]]] = {}
    for idx, row in enumerate(plan_rows):
        groups.setdefault(
            (row.well_position, row.analyte), {"plans": [], "results": []}
        )["plans"].append(idx)
    for idx, row in enumerate(result_rows):
        groups.setdefault(
            (row.well_position, row.analyte), {"plans": [], "results": []}
        )["results"].append(idx)

    verdicts: List[Any] = [None] * len(result_rows)
    representative: List[Tuple[int, int]] = []
    pair_count_total = 0
    cost_total = 0

    for member in groups.values():
        plan_idx = member["plans"]
        result_idx = member["results"]
        local_plans = [plan_rows[i] for i in plan_idx]
        local_results = [
            SimpleNamespace(measured_at=result_rows[i].measured_at)
            for i in result_idx
        ]
        group = _solve_group(local_plans, local_results, tolerance_seconds)

        pair_count_total += group.pair_count
        cost_total += group.total_time_diff

        for lr, lp in group.representative_matching:
            representative.append((result_idx[lr], plan_idx[lp]))

        for lr in range(len(result_idx)):
            global_r = result_idx[lr]
            verdicts[global_r] = ResultVerdict(
                result_index=global_r,
                eligible_plan_indices=[
                    plan_idx[lp] for lp in group.eligible_plans[lr]
                ],
                can_be_unmatched=group.can_be_unmatched[lr],
            )

    representative.sort()
    return Reconciliation(
        pair_count=pair_count_total,
        total_time_diff_seconds=cost_total,
        representative_matching=representative,
        verdicts=verdicts,
    )
