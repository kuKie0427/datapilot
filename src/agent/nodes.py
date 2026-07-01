"""LangGraph 节点实现 —— 每个节点对应流水线的一个阶段。"""

import time
import json
from openai import OpenAI
from .state import AgentState, QueryStage, OutputType
from ..config import config
from ..rag_store import RAGStore
from ..datasources import get_shared_registry
from ..tools import chart_tool


# ---- 共享单例 ----

_rag: RAGStore = None
_llm_client: OpenAI = None


def _get_rag() -> RAGStore:
    global _rag
    if _rag is None:
        _rag = RAGStore()
    return _rag


def _get_llm() -> OpenAI:
    global _llm_client
    if _llm_client is None:
        _llm_client = OpenAI(
            api_key=config.llm.api_key,
            base_url=config.llm.base_url,
        )
    return _llm_client


def _add_step(state: AgentState, stage: str, status: str, detail: str = ""):
    state.setdefault("steps", []).append({
        "stage": stage,
        "status": status,
        "detail": detail,
        "timestamp": time.time(),
    })


def _effective_question(state: AgentState) -> str:
    """获取有效问题：优先用改写后的，回退到原始问题。"""
    return state.get("rewritten_question") or state["question"]


def _infer_dialect(source_id: str, registry=None) -> str:
    """根据 source_id 与数据源类型推断 SQL 方言。

    - source_id 含 "mysql" → mysql
    - 数据源类型为 csv（DuckDB 引擎）→ duckdb
    - 其他 → sqlite（Spider 默认）
    """
    sid = (source_id or "").lower()
    if "mysql" in sid:
        return "mysql"
    if "csv" in sid:
        return "duckdb"
    # 通过 registry 查实际数据源类型
    if registry is not None:
        try:
            src = registry.get_source(source_id)
            if src and getattr(src, "source_type", "") == "csv":
                return "duckdb"
        except Exception:
            pass
    return "sqlite"


# ---- 节点 0：多轮对话查询改写 ----

REWRITE_PROMPT = """You are a query rewriter for a multi-turn data query conversation.
Given the conversation history and the latest user question, rewrite the latest question
into a self-contained, context-independent question that can be answered without prior context.

Rules:
- If the latest question is already self-contained, return it as-is
- Resolve pronouns and ellipsis using conversation context
- Preserve the time range and filters from previous turns if still relevant
- If the user is clearly starting a new topic, return the latest question as-is
- Output ONLY the rewritten question, no explanation, no quotes

Conversation history (most recent first):
{history}

Latest question: {question}

Rewritten question:"""


async def node_query_rewrite(state: AgentState) -> AgentState:
    """阶段 0：多轮对话查询改写。

    把残缺问题（如"按地区拆分"）改写为独立可执行的问题
    （如"上月销售额按地区拆分"），基于会话历史做指代消解与省略恢复。
    """
    _add_step(state, QueryStage.QUERY_REWRITE.value, "running")
    question = state["question"]
    session_id = state.get("session_id")

    rewritten = question  # 默认不改写

    if session_id:
        try:
            from ..session import get_session_manager
            mgr = get_session_manager()
            session = mgr.get_session(session_id)
            if session and session.messages:
                # 取最近 5 轮历史
                history = "\n".join(
                    f"{m['role']}: {m['content']}"
                    for m in session.messages[-10:]
                )
                llm = _get_llm()
                prompt = REWRITE_PROMPT.format(
                    history=history or "(empty)",
                    question=question,
                )
                resp = llm.chat.completions.create(
                    model=config.llm.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    max_tokens=200,
                )
                raw = resp.choices[0].message.content.strip()
                # 去除可能的引号
                if raw.startswith('"') and raw.endswith('"'):
                    raw = raw[1:-1]
                if raw:
                    rewritten = raw
                    _add_step(state, QueryStage.QUERY_REWRITE.value, "done",
                              f"Rewritten: {question} -> {rewritten}")
                else:
                    _add_step(state, QueryStage.QUERY_REWRITE.value, "done",
                              "No rewrite needed")
            else:
                _add_step(state, QueryStage.QUERY_REWRITE.value, "done",
                          "No session history; question unchanged")
        except Exception as e:
            _add_step(state, QueryStage.QUERY_REWRITE.value, "done",
                      f"Rewrite error: {e}; question unchanged")
    else:
        _add_step(state, QueryStage.QUERY_REWRITE.value, "done",
                  "No session_id; question unchanged")

    state["rewritten_question"] = rewritten
    # 保留 state["question"] 为原始用户问题（用于 chart 标题、回显）
    # 下游节点读取 state["rewritten_question"] 作为有效问题
    state["current_stage"] = QueryStage.INTENT
    return state


# ---- 节点 1：意图解析 ----

INTENT_PROMPT = """Analyze the following data query question and classify it.

Question: {question}

Return a JSON object with:
  "intent": one of "aggregation", "lookup", "comparison", "trend", "ratio", "unknown"
  "output_type": one of "kpi", "table", "chart", "sql_only"
  "chart_type_hint": one of "bar", "line", "pie", "table", "kpi", or null

Rules:
- If the question asks for a single number (count, sum, total), use "kpi".
- If the question asks for a list or ranking, use "table".
- If the question asks about trends, changes over time, or comparisons, use "chart".
- If unsure, default to "table".

Return ONLY the JSON, no markdown."""


async def node_intent_parsing(state: AgentState) -> AgentState:
    """阶段 1：解析用户意图并确定输出类型。"""
    _add_step(state, QueryStage.INTENT.value, "running")
    question = _effective_question(state)

    try:
        llm = _get_llm()
        resp = llm.chat.completions.create(
            model=config.llm.model,
            messages=[{"role": "user", "content": INTENT_PROMPT.format(question=question)}],
            temperature=0.0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        # 去除 markdown 代码围栏
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)

        state["intent"] = parsed.get("intent", "unknown")
        state["output_type"] = OutputType(parsed.get("output_type", "table"))
        state["chart_type_hint"] = parsed.get("chart_type_hint")

        _add_step(state, QueryStage.INTENT.value, "done",
                  f"Intent: {state['intent']}, Output: {state['output_type'].value}")
    except Exception as e:
        # 回退：启发式规则
        q_lower = question.lower()
        if any(kw in q_lower for kw in ["趋势", "trend", "走势"]):
            state["output_type"] = OutputType.CHART
            state["chart_type_hint"] = "line"
        elif any(kw in q_lower for kw in ["多少", "总共", "total", "count"]):
            state["output_type"] = OutputType.KPI
        else:
            state["output_type"] = OutputType.TABLE
        state["intent"] = "unknown"
        _add_step(state, QueryStage.INTENT.value, "done", f"Heuristic fallback: {state['output_type'].value}")

    state["current_stage"] = QueryStage.RAG
    return state


# ---- 节点 2：RAG 检索 ----

async def node_rag_retrieval(state: AgentState) -> AgentState:
    """阶段 2：检索 top-k 相似的问题-SQL 对作为 few-shot 示例。"""
    _add_step(state, QueryStage.RAG.value, "running")
    question = _effective_question(state)
    db_id = state.get("db_id")

    rag = _get_rag()
    if not rag.examples:
        try:
            rag.build(max_examples=config.rag.max_examples)
        except Exception:
            pass

    examples = rag.search(question, k=config.rag.top_k, db_id=db_id)
    state["few_shot_examples"] = examples
    _add_step(state, QueryStage.RAG.value, "done", f"Retrieved {len(examples)} similar examples")

    state["current_stage"] = QueryStage.GENERATION
    return state


# ---- 节点 3a：语义层 plan 编译（双轨制优先路径）----

SEMANTIC_FALLBACK = False  # 语义层未命中时回退到原 SQL 生成路径


async def node_semantic_compile(state: AgentState) -> AgentState:
    """阶段 3a：尝试用语义层编译 SQL。

    让 LLM 输出 JSON plan（查什么指标 / 哪些维度 / 什么过滤），
    再由 compile_plan_to_sql 确定性编译为 SQL。
    命中则跳过 schema_aware_generation，未命中则回退。
    """
    _add_step(state, QueryStage.SEMANTIC_COMPILE.value, "running")
    question = _effective_question(state)

    try:
        from ..semantic import build_planner_prompt, parse_plan_from_llm, compile_plan_to_sql, get_semantic_layer
        layer = get_semantic_layer()
        if not layer.list_metrics():
            # 没有注册任何指标，直接回退
            _add_step(state, QueryStage.SEMANTIC_COMPILE.value, "done",
                      "No metrics defined; fallback to schema-aware generation")
            state["semantic_plan"] = None  # 显式清理，避免状态残留
            state["current_stage"] = QueryStage.GENERATION
            return state

        # 让 LLM 输出 plan
        llm = _get_llm()
        planner_prompt = build_planner_prompt(question)
        resp = llm.chat.completions.create(
            model=config.llm.model,
            messages=[{"role": "user", "content": planner_prompt}],
            temperature=0.0,
            max_tokens=500,
        )
        raw = resp.choices[0].message.content.strip()

        plan = parse_plan_from_llm(raw)
        if not plan or not layer.get(plan.metric):
            _add_step(state, QueryStage.SEMANTIC_COMPILE.value, "done",
                      "Plan parse failed or metric not found; fallback")
            state["semantic_plan"] = None
            state["current_stage"] = QueryStage.GENERATION
            return state

        # 推断方言：考虑 CSV（DuckDB）/ MySQL / SQLite
        source_id = state.get("source_id", "default")
        dialect = _infer_dialect(source_id)

        sql = compile_plan_to_sql(plan, dialect=dialect)
        if not sql:
            _add_step(state, QueryStage.SEMANTIC_COMPILE.value, "done",
                      f"Compile failed for metric {plan.metric}; fallback")
            state["semantic_plan"] = None
            state["current_stage"] = QueryStage.GENERATION
            return state

        # 命中：跳过 schema_aware_generation
        state["generated_sql"] = sql
        state["candidate_sqls"] = [sql]
        state["semantic_plan"] = plan.to_dict()
        _add_step(state, QueryStage.SEMANTIC_COMPILE.value, "done",
                  f"Compiled SQL for metric '{plan.metric}' (dims={plan.dimensions})")
        state["current_stage"] = QueryStage.EXECUTION
        return state
    except Exception as e:
        _add_step(state, QueryStage.SEMANTIC_COMPILE.value, "done",
                  f"Semantic layer error: {e}; fallback")
        state["semantic_plan"] = None
        state["current_stage"] = QueryStage.GENERATION
        return state


# ---- 节点 3：模式感知生成 ----

GENERATION_PROMPT = """You are a Text-to-SQL expert. Generate a SQL query from the natural language question.

Rules:
- Output ONLY the SQL query, no explanations, no markdown
- Use SQLite-compatible syntax (unless told otherwise)
- Do NOT wrap in ```sql blocks
- End with a semicolon

Database schema:
{schema}

Similar examples (question -> SQL):
{examples}

Question: {question}

Generate the SQL query:"""


async def node_schema_aware_generation(state: AgentState) -> AgentState:
    """阶段 3：使用模式上下文和 few-shot 示例生成 SQL。"""
    _add_step(state, QueryStage.GENERATION.value, "running")
    question = _effective_question(state)
    source_id = state.get("source_id", "default")

    # 获取模式
    schema_text = ""
    try:
        registry = get_shared_registry()
        schema = await registry.get_schema(source_id)
        if schema:
            schema_text = schema.to_prompt_text()
            state["schema_text"] = schema_text
    except Exception:
        # 回退到 Spider 的 tables.json
        from ..generator import _load_schemas, _SCHEMA_CACHE
        _load_schemas()
        db_id = state.get("db_id")
        if db_id and db_id in _SCHEMA_CACHE:
            schema_text = _SCHEMA_CACHE[db_id]
            state["schema_text"] = schema_text

    # ---- 业务术语字典检索 ----
    glossary_text = ""
    try:
        from ..glossary import get_glossary
        glossary = get_glossary()
        hits = glossary.search(question, top_k=5)
        if hits:
            glossary_text = glossary.to_prompt_text(hits, max_entries=5)
            state["glossary_hits"] = [
                {"term": h.term, "definition": h.definition} for h in hits
            ]
            _add_step(state, QueryStage.GENERATION.value, "running",
                      f"Glossary hits: {[h.term for h in hits]}")
    except Exception:
        pass

    # 格式化 few-shot 示例
    examples_text = ""
    for ex in state.get("few_shot_examples", []):
        examples_text += f'Q: {ex["question"]}\nSQL: {ex["sql"]}\n\n'

    # 拼接 prompt：schema + 术语字典 + few-shot
    base_prompt = GENERATION_PROMPT.format(
        schema=schema_text or "(no schema available)",
        examples=examples_text or "(no examples)",
        question=question,
    )
    if glossary_text:
        prompt = f"{base_prompt}\n\n{glossary_text}"
    else:
        prompt = base_prompt

    llm = _get_llm()

    # 贪心生成
    resp = llm.chat.completions.create(
        model=config.llm.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=config.llm.max_tokens,
    )
    greedy_sql = _clean_sql(resp.choices[0].message.content)
    state["generated_sql"] = greedy_sql
    state["candidate_sqls"] = [greedy_sql]

    _add_step(state, QueryStage.GENERATION.value, "done", "Greedy SQL generated")
    state["current_stage"] = QueryStage.EXECUTION
    return state


# ---- 节点 4：执行与校验 ----

async def node_execution_validation(state: AgentState) -> AgentState:
    """阶段 4：执行 SQL，校验结果，必要时触发纠错。

    在执行前做 SQL 静态校验（白名单 + 强制 LIMIT），
    并按用户行级权限注入过滤条件。
    """
    _add_step(state, QueryStage.EXECUTION.value, "running")
    sql = state.get("generated_sql", "")
    source_id = state.get("source_id", "default")
    user = state.get("user")

    if not sql:
        state["execution_error"] = "No SQL generated"
        state["current_stage"] = QueryStage.ERROR
        _add_step(state, QueryStage.EXECUTION.value, "error", "No SQL to execute")
        return state

    # ---- SQL 静态校验：白名单 + 强制 LIMIT ----
    from ..security import validate_sql, DEFAULT_MAX_ROWS, DEFAULT_ADMIN_MAX_ROWS

    # admin 角色放宽行数上限
    max_rows = DEFAULT_MAX_ROWS
    if user is not None and hasattr(user, "roles") and "admin" in (user.roles or []):
        max_rows = DEFAULT_ADMIN_MAX_ROWS

    # 推断方言：mysql / duckdb（CSV）/ sqlite
    dialect = _infer_dialect(source_id)

    validation = validate_sql(sql, max_rows=max_rows, dialect=dialect)
    if not validation.ok:
        # 校验失败 → 走纠错循环（把违规信息作为错误反馈给 LLM 重写）
        state["execution_error"] = "SQL validation failed: " + "; ".join(validation.errors)
        _add_step(state, QueryStage.EXECUTION.value, "error", state["execution_error"])
        if state.get("retry_count", 0) < config.llm.max_retries:
            state["current_stage"] = QueryStage.CORRECTION
        else:
            state["current_stage"] = QueryStage.ERROR
            state["success"] = False
        return state

    sql = validation.sql  # 使用改写后的 SQL（已注入 LIMIT）

    # ---- 行级过滤注入（RBAC，fail-closed） ----
    # 注入失败必须拒绝执行，否则会绕过行级权限
    if user is not None and hasattr(user, "row_filters"):
        row_filter = user.row_filters.get(source_id)
        if row_filter:
            from ..security.sql_validator import SQLValidator
            validator = SQLValidator(dialect=dialect)
            injected = validator.inject_row_filter(sql, row_filter)
            if injected is None:
                # 解析失败 → fail-closed，拒绝执行
                state["execution_error"] = (
                    f"Row-level filter injection failed for source {source_id}; "
                    f"execution refused to protect row-level security"
                )
                _add_step(state, QueryStage.EXECUTION.value, "error", state["execution_error"])
                if state.get("retry_count", 0) < config.llm.max_retries:
                    state["current_stage"] = QueryStage.CORRECTION
                else:
                    state["current_stage"] = QueryStage.ERROR
                    state["success"] = False
                return state
            sql = injected
            _add_step(state, QueryStage.EXECUTION.value, "running",
                      f"Injected row-level filter for user {user.id}")

    if validation.warnings:
        _add_step(state, QueryStage.EXECUTION.value, "running",
                  " | ".join(validation.warnings))

    # 通过数据源适配器执行
    try:
        registry = get_shared_registry()
        result = await registry.execute(source_id, sql)

        if result.success:
            # ---- 列脱敏（RBAC） ----
            # 用副本避免污染 registry 可能缓存的 result 对象
            cols, rows_data = list(result.columns), [list(r) for r in result.rows]
            if user is not None and hasattr(user, "masked_columns"):
                from ..auth.rbac import mask_columns
                cols, rows_data = mask_columns(user, source_id, cols, rows_data)

            # 构造脱敏后的结果 dict（不修改原 result）
            result_dict = result.to_dict()
            result_dict["columns"] = cols
            result_dict["rows"] = rows_data
            state["query_result"] = result_dict
            state["execution_error"] = ""
            _add_step(state, QueryStage.EXECUTION.value, "done",
                      f"Executed: {result.row_count} rows in {result.execution_time_ms:.0f}ms")

            # 如有需要则生成图表（标题用原始问题）
            output_type = state.get("output_type", OutputType.TABLE)
            if output_type == OutputType.CHART or output_type == OutputType.KPI:
                chart_spec = chart_tool.generate(
                    question=state["question"],
                    columns=cols,
                    rows=rows_data,
                    chart_type_hint=state.get("chart_type_hint"),
                )
                state["chart_spec"] = chart_spec.to_dict()

            state["current_stage"] = QueryStage.DONE
            state["success"] = True
        else:
            state["execution_error"] = result.error
            _add_step(state, QueryStage.EXECUTION.value, "error", result.error)
            # 触发纠错
            if state.get("retry_count", 0) < config.llm.max_retries:
                state["current_stage"] = QueryStage.CORRECTION
            else:
                state["current_stage"] = QueryStage.ERROR
                state["success"] = False
    except Exception as e:
        state["execution_error"] = str(e)
        _add_step(state, QueryStage.EXECUTION.value, "error", str(e))
        if state.get("retry_count", 0) < config.llm.max_retries:
            state["current_stage"] = QueryStage.CORRECTION
        else:
            state["current_stage"] = QueryStage.ERROR
            state["success"] = False

    return state


# ---- 节点 5：错误驱动的自我纠错 ----

CORRECTION_PROMPT = """The following SQL query failed during execution. Fix it based on the error.

Original question: {question}

Schema:
{schema}

Failed SQL:
{sql}

Error:
{error}

Similar examples:
{examples}

Generate a corrected SQL query. Output ONLY the SQL, no explanation:"""


async def node_error_correction(state: AgentState) -> AgentState:
    """错误驱动的自我纠错：根据执行错误重写 SQL。

    策略：
    1. 将裁剪后的错误反馈给 LLM 进行重写
    2. 同时以较高温度采样多个候选
    3. 执行所有候选并选取通过的那个
    """
    _add_step(state, QueryStage.CORRECTION.value, "running")
    retry_count = state.get("retry_count", 0)
    state["retry_count"] = retry_count + 1

    question = _effective_question(state)
    schema_text = state.get("schema_text", "")
    failed_sql = state.get("generated_sql", "")
    error = state.get("execution_error", "")
    examples_text = "\n".join(
        f'Q: {ex["question"]}\nSQL: {ex["sql"]}'
        for ex in state.get("few_shot_examples", [])
    )

    # 记录纠错历史
    state.setdefault("correction_history", []).append({
        "attempt": retry_count + 1,
        "failed_sql": failed_sql,
        "error": error,
    })

    # 将错误裁剪到最相关的部分
    trimmed_error = _trim_error(error)

    llm = _get_llm()
    prompt = CORRECTION_PROMPT.format(
        question=question,
        schema=schema_text or "(no schema)",
        sql=failed_sql,
        error=trimmed_error,
        examples=examples_text or "(no examples)",
    )

    # 生成纠错后的 SQL
    resp = llm.chat.completions.create(
        model=config.llm.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=config.llm.max_tokens,
    )
    corrected_sql = _clean_sql(resp.choices[0].message.content)

    # 同时采样候选用于投票
    candidates = [corrected_sql]
    for i in range(config.llm.consistency_n - 1):
        resp = llm.chat.completions.create(
            model=config.llm.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=config.llm.consistency_temperature,
            max_tokens=config.llm.max_tokens,
        )
        cand = _clean_sql(resp.choices[0].message.content)
        if cand and cand not in candidates:
            candidates.append(cand)

    state["candidate_sqls"] = candidates
    state["generated_sql"] = corrected_sql

    # 执行所有候选，选取第一个通过的
    source_id = state.get("source_id", "default")
    user = state.get("user")
    registry = get_shared_registry()

    # 纠错阶段同样做 SQL 校验 + 行级过滤注入（fail-closed）
    from ..security import validate_sql, DEFAULT_MAX_ROWS, DEFAULT_ADMIN_MAX_ROWS
    from ..security.sql_validator import SQLValidator
    max_rows = DEFAULT_MAX_ROWS
    if user is not None and hasattr(user, "roles") and "admin" in (user.roles or []):
        max_rows = DEFAULT_ADMIN_MAX_ROWS
    dialect = _infer_dialect(source_id)
    row_filter = user.row_filters.get(source_id) if (user is not None and hasattr(user, "row_filters")) else None

    valid_candidates = []
    for cand_sql in candidates:
        v = validate_sql(cand_sql, max_rows=max_rows, dialect=dialect)
        if not v.ok:
            continue
        sql_to_run = v.sql
        if row_filter:
            injected = SQLValidator(dialect=dialect).inject_row_filter(sql_to_run, row_filter)
            if injected is None:
                # 注入失败 → fail-closed，跳过此候选
                continue
            sql_to_run = injected
        valid_candidates.append(sql_to_run)

    if not valid_candidates:
        state["execution_error"] = "All candidates failed SQL validation or row-filter injection"
        _add_step(state, QueryStage.CORRECTION.value, "error", state["execution_error"])
        if state["retry_count"] < config.llm.max_retries:
            state["current_stage"] = QueryStage.CORRECTION
        else:
            state["current_stage"] = QueryStage.ERROR
            state["success"] = False
        return state

    last_error = "All candidates failed"
    for cand_sql in valid_candidates:
        try:
            result = await registry.execute(source_id, cand_sql)
        except Exception as e:
            last_error = f"Candidate execution raised: {e}"
            continue
        if not result.success:
            last_error = result.error or "Candidate returned failure"
            continue

        # 列脱敏（用副本，不污染原 result）
        cols, rows_data = list(result.columns), [list(r) for r in result.rows]
        if user is not None and hasattr(user, "masked_columns"):
            from ..auth.rbac import mask_columns
            cols, rows_data = mask_columns(user, source_id, cols, rows_data)

        result_dict = result.to_dict()
        result_dict["columns"] = cols
        result_dict["rows"] = rows_data
        state["generated_sql"] = cand_sql
        state["query_result"] = result_dict
        state["execution_error"] = ""
        _add_step(state, QueryStage.CORRECTION.value, "done",
                  f"Corrected on attempt {retry_count + 1}")

        # 如有需要则生成图表（标题用原始问题）
        output_type = state.get("output_type", OutputType.TABLE)
        if output_type in (OutputType.CHART, OutputType.KPI):
            chart_spec = chart_tool.generate(
                question=state["question"],
                columns=cols,
                rows=rows_data,
                chart_type_hint=state.get("chart_type_hint"),
            )
            state["chart_spec"] = chart_spec.to_dict()

        state["current_stage"] = QueryStage.DONE
        state["success"] = True
        return state

    # 所有候选均失败
    state["execution_error"] = last_error
    _add_step(state, QueryStage.CORRECTION.value, "error", last_error)

    if state["retry_count"] < config.llm.max_retries:
        state["current_stage"] = QueryStage.CORRECTION
    else:
        state["current_stage"] = QueryStage.ERROR
        state["success"] = False

    return state


# ---- 辅助函数 ----

def _clean_sql(raw: str) -> str:
    """去除 LLM 输出中的 markdown 围栏和空白。"""
    sql = raw.strip()
    if sql.startswith("```"):
        sql = sql.split("\n", 1)[-1] if "\n" in sql else sql
        sql = sql.rsplit("```", 1)[0] if "```" in sql else sql
    return sql.strip()


def _trim_error(error: str) -> str:
    """将错误信息裁剪到最相关的行，便于 LLM 重写。"""
    lines = error.strip().split("\n")
    # 保留前 3 行 + 后 3 行
    if len(lines) <= 6:
        return error
    return "\n".join(lines[:3] + ["..."] + lines[-3:])
