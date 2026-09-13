"""
实时字幕翻译软件 - Python 后端入口
"""
import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml

from audio_capture import AudioCapture
from asr_engine import ASREngine
from translator import Translator
from websocket_server import WebSocketServer

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SubtitleTranslator:
    """实时字幕翻译主类"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)
        self.audio_capture = AudioCapture(self.config)
        self.asr_engine = ASREngine(self.config)
        self.translator = Translator(self.config)
        self.websocket_server = WebSocketServer(self.config)
        self._running = False

        # 设置下载进度回调
        self.asr_engine.set_download_progress_callback(self._on_model_progress)
        self.translator.set_download_progress_callback(self._on_model_progress)

    def _load_config(self, config_path: str) -> dict:
        """加载配置文件"""
        config_file = Path(config_path)
        if config_file.exists():
            with open(config_file, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        return self._default_config()

    def _default_config(self) -> dict:
        """默认配置"""
        return {
            'audio': {
                'sample_rate': 16000,
                'channels': 1,
                'chunk_size': 1024
            },
            'asr': {
                'model_size': 'base',
                'device': 'auto',
                'language': None  # 自动检测
            },
            'translation': {
                'primary_model': 'Helsinki-NLP/opus-mt-ja-zh',
                'fallback_model': 'facebook/nllb-200-distilled-600M',
                'target_languages': ['zh', 'en']
            },
            'websocket': {
                'host': 'localhost',
                'port': 8765
            }
        }

    def _on_model_progress(self, model_name: str, progress: float, message: str):
        """
        模型下载进度回调

        Args:
            model_name: 模型名称
            progress: 进度百分比 (0-100)
            message: 状态消息
        """
        logger.info(f"模型进度 [{model_name}]: {progress:.1f}% - {message}")

        # 发送进度到前端
        asyncio.create_task(self._send_progress_to_frontend(
            model_name, progress, message
        ))

    async def _send_progress_to_frontend(self, model_name: str, progress: float, message: str):
        """发送进度到前端"""
        await self.websocket_server.send({
            'type': 'model_progress',
            'model_name': model_name,
            'progress': progress,
            'message': message
        })

    async def start(self):
        """启动服务"""
        logger.info("正在启动实时字幕翻译服务...")
        self._running = True

        # 初始化组件
        await self.asr_engine.initialize()
        await self.translator.initialize()
        await self.websocket_server.start(self._on_transcription)

        # 启动音频捕获
        await self.audio_capture.start(self._on_audio_data)

        logger.info("服务启动完成")

    async def stop(self):
        """停止服务"""
        logger.info("正在停止服务...")
        self._running = False
        await self.audio_capture.stop()
        await self.websocket_server.stop()
        logger.info("服务已停止")

    async def _on_audio_data(self, audio_data):
        """音频数据回调"""
        if not self._running:
            return

        # ASR 识别
        transcription = await self.asr_engine.transcribe(audio_data)
        if transcription:
            # 翻译
            translations = await self.translator.translate(
                transcription['text'],
                transcription['language']
            )

            # 发送到前端
            await self.websocket_server.send({
                'type': 'subtitle',
                'original': transcription['text'],
                'source_language': transcription['language'],
                'translations': translations
            })

    async def _on_transcription(self, message):
        """处理来自前端的控制消息"""
        if message.get('type') == 'control':
            action = message.get('action')
            if action == 'pause':
                self.audio_capture.pause()
            elif action == 'resume':
                self.audio_capture.resume()
            elif action == 'change_model':
                model_size = message.get('model_size', 'base')
                await self.asr_engine.change_model(model_size)


async def main():
    """主函数"""
    translator = SubtitleTranslator()

    # 处理信号
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(translator.stop()))

    try:
        await translator.start()
        # 保持运行
        while translator._running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        await translator.stop()


if __name__ == '__main__':
    asyncio.run(main())
