"""业务术语字典模块 —— 业务术语到 SQL 映射的检索与注入。"""

from .glossary import (
    Glossary,
    GlossaryEntry,
    get_glossary,
    load_glossary,
    search_terms,
)

__all__ = [
    "Glossary",
    "GlossaryEntry",
    "get_glossary",
    "load_glossary",
    "search_terms",
]
