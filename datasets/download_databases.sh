#!/usr/bin/env bash
# 下载 Spider SQLite 数据库，用于基于执行的评测。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${SCRIPT_DIR}/spider_databases"
ZIP_FILE="${DEST_DIR}/spider_data.zip"

mkdir -p "${DEST_DIR}"

if command -v gdown &> /dev/null; then
    gdown "https://drive.google.com/uc?id=1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J" -O "${ZIP_FILE}"
else
    pip install gdown
    gdown "https://drive.google.com/uc?id=1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J" -O "${ZIP_FILE}"
fi

echo "Extracting tables.json..."
python3 -c "
import zipfile, json, os
with zipfile.ZipFile('${ZIP_FILE}') as z:
    z.extract('spider_data/tables.json', '${DEST_DIR}')
    os.rename('${DEST_DIR}/spider_data/tables.json', '${DEST_DIR}/../tables.json')
    os.rmdir('${DEST_DIR}/spider_data')
"

echo "Done. tables.json extracted to project root."
echo "To use execution mode, also extract databases:"
echo "  unzip ${ZIP_FILE} -d ${DEST_DIR}"
echo "  python -m src.evaluate --execution --db-dir ${DEST_DIR}/database"
