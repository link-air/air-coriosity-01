"""冒烟验证：embedding 服务（无服务时优雅降级字符重叠）+ 内容新奇值。"""
import sys
import shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from core.embedding import EmbeddingService, cosine, embedding_novelty
from core.memory import Memory

cfg = Config()
cfg.data_root = Path(__file__).parent / "data_test_emb"
cfg.data_root.mkdir(parents=True, exist_ok=True)

ok = True
def check(name, cond):
    global ok
    print(("[PASS] " if cond else "[FAIL] ") + name)
    if not cond:
        ok = False

# ---- cosine 数学 ----
check("cosine 相同向量 =1", abs(cosine([1, 0], [1, 0]) - 1.0) < 1e-9)
check("cosine 正交向量 =0", abs(cosine([1, 0], [0, 1])) < 1e-9)
check("cosine 空向量 =0", cosine([], [1, 0]) == 0.0)
check("cosine 维度不一致 =0", cosine([1, 0], [1, 0, 1]) == 0.0)

# ---- embedding_novelty（v_new）边界 ----
check("v_new 空参照 =1.0（全新奇）", embedding_novelty([1, 0], []) == 1.0)
check("v_new 完全相似 =0", abs(embedding_novelty([1, 0], [[1, 0]]) - 0.0) < 1e-9)
check("v_new 完全不同 =1", abs(embedding_novelty([1, 0], [[0, 1]]) - 1.0) < 1e-9)
check("v_new 取最大相似度（有近有远取近）",
      abs(embedding_novelty([1, 0], [[0, 1], [0.8, 0.6]]) - (1 - 0.8)) < 1e-6)

# ---- 无服务降级：显式清空 endpoint → Memory 仍工作，搜索走字符重叠 ----
srv = EmbeddingService()   # endpoint 空
check("无配置 available=False", srv.available is False)
check("无配置 embed 返回 None", srv.embed(["x"]) is None)

cfg_nosvc = Config()
cfg_nosvc.data_root = cfg.data_root
cfg_nosvc.embedding_endpoint = ""   # 无服务
m = Memory(cfg_nosvc)
check("无服务写 L3 正常", m.write("L3", "今天读了一篇关于熵的文章") is True)
check("无服务条目无 emb 字段（不存空壳）", "emb" not in m.l3[0])
check("无服务搜索仍命中（字符重叠降级）", len(m.search("熵")) > 0)

# ---- 有服务路径（mock 查询向量，验证检索走余弦）----
from unittest.mock import MagicMock
m.emb.embed_one = MagicMock(side_effect=lambda q: [1.0, 0.0, 0.0])
m.write("L3", "我喜欢看星星")   # mock 服务 → 条目存 emb=[1,0,0]
check("mock 服务条目有 emb", "emb" in m.l3[-1])
sims = [cosine([1.0, 0.0, 0.0], it["emb"]) for it in m.l3 if it.get("emb")]
check("向量检索余弦正确", any(abs(s - 1.0) < 1e-6 for s in sims))

# 印证（tag 方向）：L3 的 tag 方向匹配 L1 小标签 → +1
m.write("L1", "我相信诚实比自洽更重要", tag="诚实（价值观）")
m.write("L3", "我相信诚实比自洽更重要", tag="某例证（诚实）")
check("tag 方向印证 +1", m.bump_evidence_by_tag("诚实") is True and m.l1[0]["evidence_count"] >= 1)

shutil.rmtree(cfg.data_root, ignore_errors=True)
print()
print("全部通过" if ok else "有失败")
sys.exit(0 if ok else 1)
