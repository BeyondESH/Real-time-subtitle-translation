"""
集成测试 - 端到端流程测试
"""
import asyncio
import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch

# 导入被测模块
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from audio_capture import AudioCapture
from asr_engine import ASREngine
from translator import Translator
from websocket_server import WebSocketServer


class TestAudioCapture:
    """音频捕获测试"""

    def setup_method(self):
        """测试前设置"""
        self.config = {
            'audio': {
                'sample_rate': 16000,
                'channels': 1,
                'chunk_size': 1024
            }
        }
        self.capture = AudioCapture(self.config)

    def test_get_audio_sources(self):
        """测试获取音频源列表"""
        sources = self.capture.get_audio_sources()
        assert isinstance(sources, list)

    def test_default_config(self):
        """测试默认配置"""
        assert self.capture.sample_rate == 16000
        assert self.capture.channels == 1
        assert self.capture.chunk_size == 1024


class TestASREngine:
    """ASR 引擎测试"""

    def setup_method(self):
        """测试前设置"""
        self.config = {
            'asr': {
                'model_size': 'tiny',
                'device': 'cpu',
                'compute_type': 'int8'
            }
        }
        self.engine = ASREngine(self.config)

    def test_supported_models(self):
        """测试支持的模型列表"""
        assert 'tiny' in ASREngine.SUPPORTED_MODELS
        assert 'base' in ASREngine.SUPPORTED_MODELS
        assert 'large-v3' in ASREngine.SUPPORTED_MODELS

    def test_model_info(self):
        """测试获取模型信息"""
        info = self.engine.get_model_info()
        assert info['model_size'] == 'tiny'
        assert info['device'] == 'cpu'


class TestTranslator:
    """翻译引擎测试"""

    def setup_method(self):
        """测试前设置"""
        self.config = {
            'translation': {
                'primary_model': 'Helsinki-NLP/opus-mt-ja-zh',
                'fallback_model': 'facebook/nllb-200-distilled-600M',
                'target_languages': ['zh', 'en'],
                'device': 'cpu'
            }
        }
        self.translator = Translator(self.config)

    def test_detect_language(self):
        """测试语言检测"""
        # 日语
        lang = self.translator.detect_language("こんにちは")
        assert lang == 'ja'

        # 中文
        lang = self.translator.detect_language("你好世界")
        assert lang == 'zh'

        # 英文
        lang = self.translator.detect_language("Hello World")
        assert lang == 'en'

    def test_model_info(self):
        """测试获取模型信息"""
        info = self.translator.get_model_info()
        assert info['primary_model'] == 'Helsinki-NLP/opus-mt-ja-zh'
        assert 'zh' in info['target_languages']


class TestWebSocketServer:
    """WebSocket 服务器测试"""

    def setup_method(self):
        """测试前设置"""
        self.config = {
            'websocket': {
                'host': 'localhost',
                'port': 8766  # 使用不同端口避免冲突
            }
        }
        self.server = WebSocketServer(self.config)

    def test_initial_state(self):
        """测试初始状态"""
        assert self.server.get_client_count() == 0
        assert self.server._running is False


class TestEndToEnd:
    """端到端流程测试"""

    def test_config_loading(self):
        """测试配置加载"""
        import yaml
        config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config.yaml')

        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            assert 'audio' in config
            assert 'asr' in config
            assert 'translation' in config
            assert 'websocket' in config


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
