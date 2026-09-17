"""
实时字幕翻译软件 - Python 后端入口

数据流：
  录音线程                事件循环线程
    │ 音频块(同步回调)        │
    ▼                       │
  RingBuffer ───────────────┤ 每 tick_ms 一次
                            ▼
                     UtteranceSegmenter (VAD 切句)
                            │ 语句
                            ▼
                     PipelineWorker (有界队列: ASR→翻译→WS)
"""
import asyncio
import logging
import os
import signal
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml

from audio_buffer import RingBuffer, UtteranceSegmenter
from audio_capture import AudioCapture
from asr_engine import ASREngine
from pipeline_worker import PipelineWorker
from translator import Translator
from vad_events import VadStateBroadcaster
from websocket_server import WebSocketServer


def _setup_logging():
    """控制台 + 滚动文件日志（打包无控制台时排障的唯一通道）"""
    log_dir = Path(
        os.environ.get(
            'SUBTITLE_LOG_DIR',
            Path.home() / '.cache' / 'subtitle-translator' / 'logs'
        )
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    handlers = [logging.StreamHandler()]
    try:
        handlers.append(
            RotatingFileHandler(
                log_dir / 'backend.log',
                maxBytes=2 * 1024 * 1024,
                backupCount=2,  # 共 3 个文件
                encoding='utf-8'
            )
        )
    except OSError as e:
        # 日志文件不可写不阻断启动
        print(f"文件日志初始化失败: {e}")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers
    )


_setup_logging()
logger = logging.getLogger(__name__)


class SubtitleTranslator:
    """实时字幕翻译主类"""

    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)

        # 管线配置
        pipeline_cfg = self.config.get('pipeline', {})
        vad_cfg = self.config.get('vad', {})
        audio_cfg = self.config.get('audio', {})
        sample_rate = audio_cfg.get('sample_rate', 16000)

        buffer_seconds = pipeline_cfg.get('buffer_seconds', 30)
        self._tick_interval = pipeline_cfg.get('tick_ms', 250) / 1000.0
        queue_size = pipeline_cfg.get('queue_size', 8)

        # 组件
        self.audio_capture = AudioCapture(self.config)
        self.asr_engine = ASREngine(self.config)
        self.translator = Translator(self.config)
        self.websocket_server = WebSocketServer(self.config)
        self.ring_buffer = RingBuffer(int(buffer_seconds * sample_rate))
        self.segmenter = UtteranceSegmenter(
            sample_rate=sample_rate,
            threshold=vad_cfg.get('threshold', 0.5),
            min_silence_ms=vad_cfg.get('min_silence_duration_ms', 600),
            speech_pad_ms=vad_cfg.get('speech_pad_ms', 200),
            max_utterance_s=pipeline_cfg.get('max_utterance_s', 15),
        )
        self.worker = PipelineWorker(
            self.asr_engine, self.translator, self.websocket_server, queue_size,
            get_active_language=lambda: self.active_target_language
        )

        # VAD 状态翻转广播（speech 即时 / silence 去抖 300ms）
        self._vad_broadcaster = VadStateBroadcaster()

        # 激活目标语言（默认目标列表首项；前端 config_sync 可覆盖）
        translation_cfg = self.config.get('translation', {})
        target_languages = translation_cfg.get('target_languages', ['zh'])
        self.active_target_language: str = target_languages[0]

        # 注册 WebSocket 请求/响应方法（设置面板使用）
        self.websocket_server.register_method('get_audio_sources', self._method_get_audio_sources)
        self.websocket_server.register_method('get_config', self._method_get_config)

        self._running = False
        self._loop: asyncio.AbstractEventLoop = None
        self._tick_task: asyncio.Task = None
        self._preload_task: asyncio.Task = None

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
            'pipeline': {
                'buffer_seconds': 30,
                'tick_ms': 250,
                'max_utterance_s': 15,
                'queue_size': 8
            },
            'vad': {
                'threshold': 0.5,
                'min_silence_duration_ms': 600,
                'speech_pad_ms': 200
            },
            'asr': {
                'model_size': 'base',
                'device': 'auto',
                'language': None  # 自动检测
            },
            'translation': {
                'primary_model': 'Helsinki-NLP/opus-mt-ja-zh',
                'fallback_model': 'facebook/nllb-200-distilled-600M',
                'target_languages': ['zh', 'en'],
                'lazy_load': True,
                'preload_primary': True
            },
            'websocket': {
                'host': 'localhost',
                'port': 8765
            }
        }

    def _on_model_progress(self, model_name: str, progress: float, message: str):
        """
        模型下载进度回调（可能被任意线程触发）。

        经 call_soon_threadsafe 切回主事件循环后再发 WebSocket，
        避免 worker 线程中 create_task 崩溃。
        """
        logger.info(f"模型进度 [{model_name}]: {progress:.1f}% - {message}")

        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(
            self._schedule_progress_send, model_name, progress, message
        )

    def _schedule_progress_send(self, model_name: str, progress: float, message: str):
        """在主事件循环中调度进度发送"""
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
        self._loop = asyncio.get_running_loop()

        # 初始化组件
        await self.websocket_server.start(self._on_control_message)
        await self.asr_engine.initialize()
        await self.translator.initialize()

        # 后台预载日中主翻译模型（失败降级为纯懒加载）
        translation_cfg = self.config.get('translation', {})
        if translation_cfg.get('preload_primary', True):
            self._preload_task = asyncio.create_task(self._preload_primary_model())

        # 启动管线工作器
        await self.worker.start()

        # 启动音频捕获（同步回调，只写环形缓冲）
        await self.audio_capture.start(self._on_audio_chunk)

        # 启动 VAD 切句循环
        self._tick_task = asyncio.create_task(self._segmentation_loop())

        logger.info("服务启动完成")

    async def _preload_primary_model(self):
        """后台预载主翻译模型，失败时降级为懒加载"""
        try:
            await self.translator.ensure_primary()
            logger.info("主翻译模型后台预载完成")
        except Exception as e:
            logger.warning(f"主翻译模型预载失败，将在首次使用时重试: {e}")

    def _on_audio_chunk(self, audio_data):
        """音频块回调（录音线程，同步，只写环形缓冲）"""
        self.ring_buffer.append(audio_data)

    async def _segmentation_loop(self):
        """VAD 切句循环（事件循环线程）"""
        while self._running:
            await asyncio.sleep(self._tick_interval)
            try:
                utterances = self.segmenter.tick(self.ring_buffer)
                for utterance in utterances:
                    self.worker.submit(utterance)

                # vad_state 翻转广播（仅状态变化时发送；无客户端由 send 自动跳过）
                vad_change = self._vad_broadcaster.on_tick(
                    self.segmenter.speech_active, time.monotonic()
                )
                if vad_change is not None:
                    await self.websocket_server.send({
                        'type': 'vad_state',
                        'state': vad_change
                    })
            except Exception as e:
                logger.error(f"切句循环错误: {e}")

    async def stop(self):
        """停止服务"""
        logger.info("正在停止服务...")
        self._running = False

        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass

        if self._preload_task and not self._preload_task.done():
            self._preload_task.cancel()
            try:
                await self._preload_task
            except asyncio.CancelledError:
                pass

        await self.audio_capture.stop()
        await self.worker.stop()
        await self.websocket_server.stop()
        logger.info("服务已停止")

    async def _method_get_audio_sources(self, _params):
        """WS 方法：枚举音频源"""
        return await asyncio.to_thread(self.audio_capture.get_audio_sources)

    async def _method_get_config(self, _params):
        """WS 方法：返回后端运行配置摘要"""
        return {
            'asr': self.asr_engine.get_model_info(),
            'translation': self.translator.get_model_info(),
            'active_language': self.active_target_language,
        }

    async def _on_control_message(self, message: dict):
        """处理来自前端的控制/同步消息"""
        if not isinstance(message, dict):
            logger.warning(f"收到非字典消息: {message}")
            return

        # 配置同步（前端 electron-store 是用户偏好的唯一来源）
        if message.get('type') == 'config_sync':
            target_languages = message.get('target_languages')
            active_language = message.get('active_language')
            if isinstance(target_languages, list) and target_languages:
                self.translator.target_languages = target_languages
                logger.info(f"目标语言同步: {target_languages}")
            if isinstance(active_language, str) and active_language in self.translator.target_languages:
                self.active_target_language = active_language
                logger.info(f"激活语言同步: {active_language}")
            return

        if message.get('type') != 'control':
            logger.warning(f"收到未知类型消息: {message}")
            return

        action = message.get('action')

        if action == 'pause':
            self.audio_capture.pause()
        elif action == 'resume':
            self.audio_capture.resume()
        elif action == 'set_language':
            language = message.get('language')
            if language in self.translator.target_languages:
                self.active_target_language = language
                logger.info(f"激活语言切换为: {language}")
            else:
                await self.websocket_server.send({
                    'type': 'error',
                    'code': 'invalid_language',
                    'message': f'语言 {language} 不在目标列表: {self.translator.target_languages}'
                })
        elif action == 'set_audio_source':
            source_id = message.get('source_id')
            try:
                self.audio_capture.set_audio_source(source_id)
                await self.audio_capture.restart()
            except (ValueError, RuntimeError) as e:
                logger.warning(f"切换音频源失败: {e}")
                await self.websocket_server.send({
                    'type': 'error',
                    'code': 'invalid_audio_source',
                    'message': str(e)
                })
        elif action == 'change_model':
            model_size = message.get('model_size', 'base')
            try:
                await self.asr_engine.change_model(model_size)
            except ValueError as e:
                logger.warning(f"切换模型失败: {e}")
                await self.websocket_server.send({
                    'type': 'error',
                    'code': 'invalid_model',
                    'message': str(e)
                })
        else:
            logger.warning(f"未知控制指令: {action}")


async def main():
    """主函数"""
    # 配置文件路径：环境变量优先（打包后指向用户数据目录副本）
    config_path = os.environ.get('SUBTITLE_CONFIG_PATH', 'config.yaml')
    translator = SubtitleTranslator(config_path=config_path)

    # 处理信号（Windows 仅支持 SIGINT，其余优雅降级）
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda: asyncio.create_task(translator.stop())
            )
        except NotImplementedError:
            pass

    try:
        await translator.start()
        # 保持运行
        while translator._running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        await translator.stop()


if __name__ == '__main__':
    asyncio.run(main())
