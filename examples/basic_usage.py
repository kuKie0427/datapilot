"""DataPilot quickstart: generate SQL from natural language."""

import os
from src.generator import generate_sql

os.environ["DEEPSEEK_API_KEY"] = "sk-..."  # set your key here

questions = [
    "How many templates do we have?",
    "Show the name of the teacher for the math course.",
]

for q in questions:
    sql = generate_sql(q)
    print(f"Q: {q}")
    print(f"SQL: {sql}")
    print()
