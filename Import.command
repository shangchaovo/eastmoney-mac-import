#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
FILE="${1:-stocks.txt}"
if [[ ! -f "$FILE" ]]; then
  echo "找不到代码文件: $FILE"
  echo "把 stocks.txt 放到本目录，或把 txt 拖到这个图标上。"
  read -r -p "按回车退出"
  exit 1
fi
echo "预览导入计划（不会写入）。确认后请在终端运行:"
echo "  python3 \"$(pwd)/import_watchlist.py\" --file \"$FILE\" --apply"
echo
python3 import_watchlist.py --file "$FILE"
echo
read -r -p "按回车退出"
