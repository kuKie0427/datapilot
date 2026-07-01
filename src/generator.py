"""Spider 评估专用的独立 SQL 生成器。

注意：主流水线请使用 src/agent/nodes.py 的 LangGraph 四阶段流水线。
本模块仅用于 Spider 基准评估（src/evaluate.py），不走 RBAC / 行级过滤 / 沙箱。
"""

import os
import json
import sqlite3
import hashlib
from openai import OpenAI
from .rag_store import RAGStore
from .config import config

RAG = RAGStore()
_SCHEMA_CACHE = {}
_CACHED_SYSTEM_PROMPTS = {}
DB_DIR = os.environ.get("SPIDER_DB_DIR", "datasets/spider_databases/database")


def _load_schemas(path: str = ""):
    if _SCHEMA_CACHE:
        return
    if path == "":
        path = os.path.join(os.path.dirname(__file__), "..", "tables.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for t in json.load(f):
            db_id = t["db_id"]
            lines = []
            for ti, tname in enumerate(t["table_names_original"]):
                cols_in_table = [
                    (c[1], c_type)
                    for c, c_type in zip(t["column_names_original"], t["column_types"])
                    if c[0] == ti
                ]
                col_strs = [f"    {cname} ({ctype})" for cname, ctype in cols_in_table]
                lines.append(f"  TABLE {tname}:")
                lines.extend(col_strs)
            if t.get("foreign_keys"):
                lines.append("  RELATIONSHIPS:")
                for src, dst in t["foreign_keys"]:
                    src_t = t["table_names_original"][t["column_names_original"][src][0]]
                    src_c = t["column_names_original"][src][1]
                    dst_t = t["table_names_original"][t["column_names_original"][dst][0]]
                    dst_c = t["column_names_original"][dst][1]
                    lines.append(f"    {src_t}.{src_c} -> {dst_t}.{dst_c}")
            _SCHEMA_CACHE[db_id] = "\n".join(lines)


def _get_system_prompt(question: str, db_id: str = "") -> str:
    cache_key = (question, db_id)
    if cache_key in _CACHED_SYSTEM_PROMPTS:
        return _CACHED_SYSTEM_PROMPTS[cache_key]

    system = (
        "You are a Text-to-SQL expert. Generate SQLite SQL from natural language.\n"
        "Rules:\n"
        "- Output ONLY the SQL query, no explanations, no markdown\n"
        "- Use SQLite-compatible syntax\n"
        "- Use standard SQL functions (COUNT, SUM, AVG, etc.)\n"
        "- Do NOT wrap in ```sql blocks\n"
        "- End with a semicolon;"
    )

    _load_schemas()
    schema = _SCHEMA_CACHE.get(db_id, "") if db_id else ""
    if schema:
        system += f"\n\nDatabase schema for {db_id}:\n{schema}"

    examples = RAG.search(question, k=3, db_id=db_id)
    if examples:
        system += "\n\nSimilar examples (question -> SQL):"
        for ex in examples:
            system += f'\n# Q: {ex["question"]}'
            system += f'\n# SQL: {ex["sql"]}\n'

    _CACHED_SYSTEM_PROMPTS[cache_key] = system
    return system


def _call_llm(question: str,  system_prompt: str = "", temperature: float = 0.0) -> str:
    user = f"Generate a SQL query for: {question}"
    client = OpenAI(api_key=config.llm.api_key, base_url=config.llm.base_url)
    resp = client.chat.completions.create(
        model=config.llm.model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=1000,
        # DeepSeek 默认启用 thinking
    )
    # 兼容部分模型 reasoning 模式返回 content=None 的情况
    sql = (resp.choices[0].message.content or "").strip()
    if sql.startswith("```"):
        sql = sql.split("\n", 1)[-1] if "\n" in sql else sql
        sql = sql.rsplit("```", 1)[0] if "```" in sql else sql
    return sql.strip()


def _execute_and_get_result_hash(sql: str, db_id: str):
    # 安全校验：仅允许 SELECT / WITH 语句，防止 LLM 生成破坏性 SQL
    # PRAGMA query_only = ON 已提供底层只读保护，这里做语句类型白名单
    stripped = sql.strip().lstrip("(").strip().upper()
    if not (stripped.startswith("SELECT") or stripped.startswith("WITH")):
        return (None, "Only SELECT/WITH statements are allowed")

    db_path = os.path.join(DB_DIR, db_id, f"{db_id}.sqlite")
    if not os.path.exists(db_path):
        return (None, None)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA query_only = ON;")
        cur = conn.execute(sql)
        rows = cur.fetchall()
        conn.close()
        # 用 hashlib 替代内置 hash()，保证跨进程稳定（不受 PYTHONHASHSEED 影响）
        return (hashlib.md5(str(sorted(rows)).encode()).hexdigest(), None)
    except Exception as e:
        return (None, str(e))


def generate_sql(question: str, db_id: str = "") -> str:
    if not config.llm.api_key:
        return "-- Set LLM_API_KEY environment variable"

    if not RAG.examples:
        RAG.build(max_examples=config.rag.max_examples)

    system_prompt = _get_system_prompt(question, db_id)

    if not db_id:
        return _call_llm(question, system_prompt)

    greedy_sql = _call_llm(question, system_prompt)
    greedy_hash, greedy_err = _execute_and_get_result_hash(greedy_sql, db_id)

    if greedy_err is None and greedy_hash is not None:
        return greedy_sql

    candidates = [("greedy", greedy_sql)]
    for i in range(config.llm.consistency_n - 1):
        sql = _call_llm(question, system_prompt, temperature=config.llm.consistency_temperature)
        if sql.strip():
            candidates.append((f"sample-{i}", sql))

    results_by_hash = {}
    sql_by_hash = {}
    for _, sql in candidates:
        r_hash, error = _execute_and_get_result_hash(sql, db_id)
        key = f"__error__{error}" if error else (str(r_hash) if r_hash is not None else "__no_db__")
        results_by_hash[key] = results_by_hash.get(key, 0) + 1
        if key not in sql_by_hash:
            sql_by_hash[key] = sql

    if results_by_hash:
        winner_key = max(results_by_hash, key=lambda k: results_by_hash[k])
        return sql_by_hash.get(winner_key, greedy_sql)

    return greedy_sql
