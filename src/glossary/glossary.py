"""业务术语字典 —— 业务词汇到 SQL 映射的检索与 prompt 注入。

解决业务术语歧义问题：
- 业务说"GMV"，表里是 SUM(orders.amount) WHERE status='paid'
- 业务说"活跃用户"，是 DAU/WAU/MAU 取决于上下文
- 业务说"老客"，是首单日期早于 N 天前

字典结构（YAML）：
    - term: GMV
      aliases: [成交额, 交易额, 总成交]
      definition: 已支付订单的金额总和，不含退款
      mapping:
        table: orders
        column: amount
        filter: status = 'paid'
        aggregation: SUM
      scope: 财务口径
      owner: 数据平台组

检索方式：基于 sentence-embedding 的向量检索（复用 rag_store 的模型），
避免全量字典塞爆 prompt。
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


@dataclass
class TermMapping:
    """业务术语到 SQL 的映射。"""
    table: str = ""
    column: str = ""
    filter: str = ""              # 行级过滤条件
    aggregation: str = ""         # SUM / COUNT / COUNT_DISTINCT / AVG / ...
    time_window: str = ""         # 时间窗口（如 30d / 7d）


@dataclass
class GlossaryEntry:
    """业务术语字典的一条记录。"""
    term: str
    aliases: list[str] = field(default_factory=list)
    definition: str = ""
    mapping: Optional[TermMapping] = None
    scope: str = ""               # 业务口径（如 财务口径 / 运营口径）
    owner: str = ""
    synonyms: list[str] = field(default_factory=list)
    negative_examples: list[str] = field(default_factory=list)  # 反例：什么不是这个术语

    def all_names(self) -> list[str]:
        """主名称 + 别名 + 同义词，全部用于检索匹配。"""
        return [self.term] + self.aliases + self.synonyms

    def to_prompt_text(self) -> str:
        """渲染为适合注入 LLM prompt 的文本。"""
        lines = [f"- {self.term}"]
        if self.aliases:
            lines.append(f"  aliases: {', '.join(self.aliases)}")
        if self.definition:
            lines.append(f"  definition: {self.definition}")
        if self.mapping:
            parts = []
            if self.mapping.aggregation and self.mapping.column:
                parts.append(f"{self.mapping.aggregation}({self.mapping.column})")
            elif self.mapping.column:
                parts.append(self.mapping.column)
            if self.mapping.table:
                parts.append(f"FROM {self.mapping.table}")
            if self.mapping.filter:
                parts.append(f"WHERE {self.mapping.filter}")
            if parts:
                lines.append(f"  SQL: {' '.join(parts)}")
            if self.mapping.time_window:
                lines.append(f"  time_window: {self.mapping.time_window}")
        if self.scope:
            lines.append(f"  scope: {self.scope}")
        if self.negative_examples:
            lines.append(f"  NOT: {'; '.join(self.negative_examples)}")
        return "\n".join(lines)


class Glossary:
    """业务术语字典 —— 加载、检索、prompt 渲染。

    检索策略：
    1. 精确匹配（主名称 + 别名）
    2. 模糊匹配（子串包含）
    3. embedding 检索（延迟加载模型，避免冷启动开销）
    """

    def __init__(self):
        self._entries: list[GlossaryEntry] = []
        self._index: dict[str, GlossaryEntry] = {}   # name -> entry（含别名）
        self._embedder = None
        self._embeddings = None

    def add(self, entry: GlossaryEntry):
        self._entries.append(entry)
        for name in entry.all_names():
            self._index[name.lower()] = entry
        # 失效 embedding 缓存
        self._embeddings = None

    def load_yaml(self, path: str):
        """从 YAML 文件加载字典。"""
        try:
            import yaml
        except ImportError:
            raise RuntimeError("PyYAML is required for glossary. Install: pip install pyyaml")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or []
        for item in data:
            mapping_data = item.get("mapping", {}) or {}
            mapping = TermMapping(
                table=mapping_data.get("table", ""),
                column=mapping_data.get("column", ""),
                filter=mapping_data.get("filter", ""),
                aggregation=mapping_data.get("aggregation", ""),
                time_window=mapping_data.get("time_window", ""),
            )
            entry = GlossaryEntry(
                term=item["term"],
                aliases=item.get("aliases", []),
                definition=item.get("definition", ""),
                mapping=mapping if (mapping.table or mapping.column) else None,
                scope=item.get("scope", ""),
                owner=item.get("owner", ""),
                synonyms=item.get("synonyms", []),
                negative_examples=item.get("negative_examples", []),
            )
            self.add(entry)

    def load_default(self):
        """加载默认字典（内置 + 项目目录）。"""
        # 1. 内置默认术语
        defaults = [
            GlossaryEntry(
                term="GMV",
                aliases=["成交额", "交易额", "总成交"],
                definition="已支付订单的金额总和，不含退款",
                mapping=TermMapping(table="orders", column="amount",
                                    filter="status = 'paid'", aggregation="SUM"),
                scope="财务口径",
                negative_examples=["不含未支付订单", "不含退款单"],
            ),
            GlossaryEntry(
                term="活跃用户",
                aliases=["DAU", "WAU", "MAU", "active users"],
                definition="在指定时间窗口内有任意行为日志的去重用户数",
                mapping=TermMapping(table="user_active_log", column="user_id",
                                    aggregation="COUNT_DISTINCT",
                                    time_window="DAU=1day, WAU=7days, MAU=30days，默认 DAU"),
            ),
            GlossaryEntry(
                term="复购率",
                aliases=["复购用户比例"],
                definition="在指定时间窗口内产生 2 次及以上购买行为的用户占比",
                mapping=TermMapping(table="orders", column="user_id",
                                    aggregation="COUNT_DISTINCT"),
                scope="运营口径",
            ),
        ]
        for entry in defaults:
            self.add(entry)

        # 2. 项目目录下的字典文件
        from ..config import PROJECT_ROOT
        glossary_file = PROJECT_ROOT / "glossary.yaml"
        if glossary_file.exists():
            try:
                self.load_yaml(str(glossary_file))
            except Exception:
                pass

    def search(self, question: str, top_k: int = 5) -> list[GlossaryEntry]:
        """检索与问题相关的术语。

        优先精确/子串匹配，回退到 embedding 检索。
        """
        if not self._entries:
            return []

        q_lower = question.lower()
        matched: list[GlossaryEntry] = []
        seen: set[int] = set()

        # 1. 精确匹配 + 子串匹配
        for entry in self._entries:
            for name in entry.all_names():
                if name.lower() in q_lower:
                    if id(entry) not in seen:
                        matched.append(entry)
                        seen.add(id(entry))
                    break

        # 2. embedding 检索补充
        if len(matched) < top_k:
            embed_hits = self._search_by_embedding(question, top_k=top_k)
            for entry in embed_hits:
                if id(entry) not in seen:
                    matched.append(entry)
                    seen.add(id(entry))
                    if len(matched) >= top_k:
                        break

        return matched[:top_k]

    def _search_by_embedding(self, question: str, top_k: int = 5) -> list[GlossaryEntry]:
        """用 sentence-embedding 做向量检索。"""
        try:
            if self._embedder is None:
                from sentence_transformers import SentenceTransformer
                self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
            if self._embeddings is None and self._entries:
                texts = [
                    f"{e.term} {' '.join(e.aliases)} {e.definition}"
                    for e in self._entries
                ]
                self._embeddings = self._embedder.encode(texts, normalize_embeddings=True)

            if self._embeddings is None or len(self._embeddings) == 0:
                return []

            import numpy as np
            q_vec = self._embedder.encode([question], normalize_embeddings=True)
            scores = (self._embeddings @ q_vec.T).flatten()
            top_idx = np.argsort(scores)[-top_k:][::-1]
            return [self._entries[i] for i in top_idx if scores[i] > 0.3]
        except Exception:
            return []

    def to_prompt_text(self, entries: list[GlossaryEntry] = None, max_entries: int = 5) -> str:
        """把命中的术语渲染成 prompt 片段。

        entries 为 None 时使用全部术语；传空列表则返回空串（无命中）。
        """
        # 注意：用 is None 区分"未传参"与"空列表"，避免 [] or self._entries 误把全量塞进 prompt
        if entries is None:
            entries = self._entries
        if not entries:
            return ""
        rendered = [e.to_prompt_text() for e in entries[:max_entries]]
        return "Business glossary (refer to this when the question mentions these terms):\n" + \
               "\n".join(rendered)


# 单例
_glossary: Optional[Glossary] = None


def get_glossary() -> Glossary:
    global _glossary
    if _glossary is None:
        _glossary = Glossary()
        _glossary.load_default()
    return _glossary


def load_glossary(path: str) -> Glossary:
    """加载指定路径的字典文件并替换单例。"""
    global _glossary
    _glossary = Glossary()
    _glossary.load_default()
    if os.path.exists(path):
        _glossary.load_yaml(path)
    return _glossary


def search_terms(question: str, top_k: int = 5) -> list[GlossaryEntry]:
    """便捷检索入口。"""
    return get_glossary().search(question, top_k=top_k)
