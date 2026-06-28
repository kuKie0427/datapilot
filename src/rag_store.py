import os
import json
import pickle
import numpy as np
from sentence_transformers import SentenceTransformer

INDEX_CACHE = os.path.join(os.path.dirname(__file__), "..", "rag_index.pkl")
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


class RAGStore:
    """用于检索相似 问题-SQL 对的向量存储。

    基于 Spider 训练数据（7k 问题-SQL 对）构建可搜索的索引。
    """

    def __init__(self):
        self.model = None
        self.embeddings = None
        self.examples = []
        self._load_cache()

    def _load_cache(self):
        if os.path.exists(INDEX_CACHE):
            with open(INDEX_CACHE, "rb") as f:
                data = pickle.load(f)
            self.examples = data["examples"]
            self.embeddings = data["embeddings"]

    def _save_cache(self):
        with open(INDEX_CACHE, "wb") as f:
            pickle.dump({"examples": self.examples, "embeddings": self.embeddings}, f)

    def _load_model(self):
        if self.model is None:
            self.model = SentenceTransformer(EMBEDDING_MODEL)

    def build(self, max_examples: int = 7000):
        if self.embeddings is not None and len(self.examples) > 0:
            return
        from datasets import load_dataset
        ds = load_dataset("xlangai/spider", split="train")
        questions = []
        for i, item in enumerate(ds):
            if i >= max_examples:
                break
            self.examples.append({
                "question": item["question"],
                "sql": item["query"],
                "db_id": item["db_id"],
            })
            questions.append(item["question"])
        self._load_model()
        self.embeddings = self.model.encode(questions, show_progress_bar=True)
        self._save_cache()

    def search(self, question: str, k: int = 3, db_id: str = None) -> list:
        if self.embeddings is None or len(self.examples) == 0:
            return []
        self._load_model()
        q_vec = self.model.encode([question], show_progress_bar=False)
        scores = np.dot(self.embeddings, q_vec.T).flatten()
        if db_id:
            for i, ex in enumerate(self.examples):
                if ex["db_id"] == db_id:
                    scores[i] *= 1.15
        top_indices = np.argsort(scores)[::-1][: k * 2]
        results, seen_sqls = [], set()
        for idx in top_indices:
            ex = self.examples[idx]
            sql_norm = ex["sql"].strip().lower()
            if sql_norm not in seen_sqls:
                results.append({
                    "question": ex["question"],
                    "sql": ex["sql"],
                    "db_id": ex["db_id"],
                    "score": float(scores[idx]),
                })
                seen_sqls.add(sql_norm)
            if len(results) >= k:
                break
        return results
