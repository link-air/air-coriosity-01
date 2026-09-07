"""
本地读物：读创建者的小说（.md 章节）和短文（.md 按日期）。

2026-09-01 统一（创建者定 A 方案）：
- 小说章节已全部改为 .md（旧实现只收 .docx，小说改 md 后完全读不到——顺手修掉）
- 统一工具：read_local 一个入口（空参列全部待读，带参读一个；2026-09-08 把 list_local 并进来，
  她没有具体目标时不再被缺参校验打回）
- 已读标记：读完给文件名加后缀【已读】，状态跟文件走，删 data 不丢
- 永久标记：重读不取消；rename 失败降级提示，不崩
"""
# =====================================================================
# 文件结构总览（读代码的人先看这里，知道每段在干嘛）：
#   class Reading —— 本地读物（创建者的小说章节 + 短文）：
#   段 1｜收集 list_all —— 扫本地目录列出全部（小说按章号/短文按日期）
#   段 2｜标题 / 已读 —— clean_title / is_read / mark_read（文件名加【已读】）
#   段 3｜读 read() —— 读正文（.md 直读；.docx 用 zipfile 解析）
# =====================================================================
import re
import zipfile
from pathlib import Path

READ_TAG = "【已读】"


class Reading:
    def __init__(self, config):
        self.dir = Path(config.local_read_dir)

    # ---- 收集 ----

    def list_all(self) -> list:
        """所有可读内容：[(kind, num, title, path)]，kind = 小说 / 短文。
        小说按章号升序；短文按文件名（日期）升序、序号从 1 编起。已读的不排除（标注用）。"""
        novels, essays = [], []
        for p in self.dir.rglob("*.md"):
            if "空白" in p.stem:
                continue
            rel = p.relative_to(self.dir)
            if "小说" in str(rel):
                m = re.search(r"(\d+)", p.stem)
                num = int(m.group(1)) if m else 0
                novels.append((num, self.clean_title(p.stem), p))
            else:
                essays.append((self.clean_title(p.stem), p))
        novels.sort(key=lambda x: x[0])
        essays.sort(key=lambda x: str(x[1]))   # 按路径（日期前缀）排
        items = []
        for num, title, p in novels:
            items.append(("小说", num, title, p))
        for i, (title, p) in enumerate(essays, 1):
            items.append(("短文", i, title, p))
        return items

    # ---- 标题 / 已读 ----

    @staticmethod
    def clean_title(stem: str) -> str:
        """显示标题：去【已读】后缀 + 去短文日期前缀。"""
        t = stem.replace(READ_TAG, "").strip()
        return re.sub(r"^\d{4}-\d{2}-\d{2}-", "", t)

    @staticmethod
    def is_read(path: Path) -> bool:
        return READ_TAG in path.stem

    @staticmethod
    def mark_read(path: Path) -> bool:
        """加【已读】后缀。已是已读返回 True；rename 失败返回 False（调用方降级提示）。"""
        if Reading.is_read(path):
            return True
        try:
            path.rename(path.with_name(path.stem + READ_TAG + path.suffix))
            return True
        except Exception:
            return False

    # ---- 读 ----

    @staticmethod
    def read(path: Path) -> str:
        """读正文（.md 直接读文本；.docx 用 zipfile 解析，兼容旧文件）。失败返回提示不抛。"""
        if path.suffix.lower() == ".docx":
            try:
                with zipfile.ZipFile(path) as z:
                    xml = z.read("word/document.xml").decode("utf-8")
            except Exception as e:
                return f"（读不了：{e}）"
            paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.DOTALL)
            lines = []
            for p in paras:
                ts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.DOTALL)
                line = "".join(ts).strip()
                if line:
                    lines.append(line)
            return "\n".join(lines)
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception as e:
            return f"（读不了：{e}）"
