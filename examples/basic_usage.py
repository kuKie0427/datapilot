"""DataPilot 快速入门：从自然语言生成 SQL。"""

import os
from src.generator import generate_sql

os.environ["DEEPSEEK_API_KEY"] = "sk-..."  # 在此处设置你的 key

questions = [
    "How many templates do we have?",
    "Show the name of the teacher for the math course.",
]

for q in questions:
    sql = generate_sql(q)
    print(f"Q: {q}")
    print(f"SQL: {sql}")
    print()
