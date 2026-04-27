#!/bin/bash

# 设置 Python 路径
export PYTHONPATH="${PYTHONPATH:+${PYTHONPATH}:}$(pwd)/src"

# 运行测试
python3 -m pytest tests/ -v "$@"
