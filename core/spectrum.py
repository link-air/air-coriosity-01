"""
熵谱：记忆在各 L1 分支上的分布 + 每个分支的平均向量（标签向量）。按标签统计，不做 SVD。

  - erank（有效秩）= 分支分布熵：全挤在 2 个分支 = 僵化，均匀铺开 = 丰富
  - 投影残差 = 1 − 与最近分支向量的余弦（「预期新不新」的尺子，不随深耕衰减）
  - reps = 分支名列表（classify / locate 直接答「会归到哪个分支」——分支名，不是记忆内容截断）

用途：读世界贴预期新奇/归哪个分支、trace 谱位置、删除/合并反馈展示 erank 变化。不进任何账本。
向量 < 阈值时谱无意义，compute 返回 None，调用方降级。
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class Spectrum —— 熵谱：记忆在各 L1 分支的分布 + 分支平均向量：
#   段 1｜全量重算 compute —— 按分支聚合，不做矩阵分解（成本低）
#   段 2｜投影 / 归属 —— project_residual / classify（残差超阈值 = 新方向）
#   段 3｜摘要 / 定位 —— summary / locate（trace 谱位置用）
# =====================================================================
import numpy as np


class Spectrum:
    def __init__(self, cfg):
        self.cfg = cfg
        self._cache = None   # {"branch_vecs": [分支平均向量], "energies": [分支占比],
        #                      "reps": [分支名], "erank": float, "mean": [全库均值],
        #                      "n": int, "k": int}

    # ---- 全量重算（按标签统计，不做矩阵分解）----

    def compute(self, memory) -> dict | None:
        """按分支聚合重算谱。成本 = 分支数 × 向量维度，远低于 SVD。

        分布只算金字塔成员（L1 + L3）；L2 是草稿本，不进认知结构分布。
        （向量总数阈值仍按全库统计，与旧口径一致。）"""
        n_vecs = sum(1 for it in list(memory.l1) + list(memory.l2) + list(memory.l3)
                     if it.get("emb"))
        if n_vecs < getattr(self.cfg, "spectrum_min_vecs", 3):
            self._cache = None
            return None

        groups = {}   # 分支名 -> [向量]
        for it in memory.l1:
            v = it.get("emb")
            if not v:
                continue
            name = memory.title_of(it) or "未归类"
            groups.setdefault(name, []).append(np.asarray(v, dtype=float))
        for it in memory.l3:
            v = it.get("emb")
            if not v:
                continue
            name = memory.parent_of(it) or "未归类"
            groups.setdefault(name, []).append(np.asarray(v, dtype=float))

        names = sorted(groups.keys())
        total = sum(len(groups[n]) for n in names) or 1
        p = np.asarray([len(groups[n]) / total for n in names])
        pn = p[p > 1e-12]
        H = -float((pn * np.log(pn)).sum())
        erank = float(np.exp(H)) if H > 0 else 1.0

        dirs = [np.mean(groups[n], axis=0) for n in names]   # 分支向量 = 分支内均值

        self._cache = {
            "branch_vecs": [d.tolist() for d in dirs],
            "energies": p.tolist(),          # 分支占比（= 该分支的能量）
            "reps": names,                   # 分支名（旧版是代表记忆内容，现在是可读名）
            "erank": erank,
            "k": len(names),
        }
        return self._cache

    # ---- 投影 / 归属 ----

    def project_residual(self, vec) -> float | None:
        """新向量离最近分支多远：1 − 最大余弦（0 = 正落在某分支中心，1 = 全新方向）。

        无缓存返回 None（调用方降级）。"""
        if not self._cache or vec is None:
            return None
        x = np.asarray(vec, dtype=float)
        dirs = self._cache["branch_vecs"]
        if not dirs:
            return 1.0
        nx = float(x @ x)
        if nx <= 0:
            return 1.0
        best = 0.0
        for d in dirs:
            dv = np.asarray(d, dtype=float)
            nd = float(dv @ dv)
            if nd <= 0:
                continue
            c = abs(float(x @ dv)) / ((nx ** 0.5) * (nd ** 0.5))
            best = max(best, c)
        return max(0.0, 1.0 - best)

    def classify(self, vec):
        """归属：返回 (分支索引, 残差占比)。残差 > 阈值 → (None, residual) = 新方向。

        返回 (None, None) 表示无法判定（无缓存）。索引对应 _cache["reps"] 的分支名。"""
        residual = self.project_residual(vec)
        if residual is None:
            return None, None
        if residual > getattr(self.cfg, "spectrum_residual_threshold", 0.5):
            return None, residual     # 新方向
        x = np.asarray(vec, dtype=float)

        def _cos(d):
            dv = np.asarray(d, dtype=float)
            nd = float(dv @ dv)
            nx = float(x @ x)
            if nd <= 0 or nx <= 0:
                return 0.0
            return abs(float(x @ dv)) / ((nx ** 0.5) * (nd ** 0.5))

        idx = int(np.argmax([_cos(d) for d in self._cache["branch_vecs"]]))
        return idx, residual

    # ---- 摘要 / 定位 ----

    def summary(self) -> dict | None:
        """谱的标量摘要 {erank, k}，供行为前后对比。无谱返回 None。"""
        if not self._cache:
            return None
        return {"erank": self._cache["erank"], "k": self._cache["k"]}

    def locate(self, vec) -> dict | None:
        """定位一条记忆在谱里的位置：{branch_idx: 分支索引, residual}。无谱/无向量返回 None。"""
        if not self._cache or vec is None:
            return None
        idx, residual = self.classify(vec)
        if residual is None:
            return None
        return {"branch_idx": idx, "residual": residual}
