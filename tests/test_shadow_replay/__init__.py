# 使 tests/test_shadow_replay 成为 Python 包，
# 避免 test_state.py 等同名文件与 tests/test_grid_realtime/test_state.py 冲突
# （pytest 在无 __init__.py 时用 basename 收集模块）。
