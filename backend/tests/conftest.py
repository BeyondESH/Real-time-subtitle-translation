"""
pytest 配置：测试根目录加入 backend/，便于直接 import 被测模块
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
