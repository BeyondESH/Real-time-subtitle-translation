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
from device_support import VALID_DEVICES
from model_downloader import DownloadError
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

        # 统一设备偏好（SUBTITLE_DEVICE > config.yaml > auto），同一偏好作用于两引擎
        self.device_preference = self._resolve_device_preference()
        for section in ('asr', 'translation'):
            self.config.setdefault(section, {})['device'] = self.device_preference

        # 管线配置
        pipeline_cfg = self.config.get('pipeline', {})
        vad_cfg = self.config.get('vad', {})
        audio_cfg = self.config.get('audio', {})
        sample_rate = audio_cfg.get('sample_rate', 16000)

        buffer_seconds = pipeline_cfg.get('buffer_seconds', 30)
        self._tick_interval = pipeline_cfg.get('tick_ms', 125) / 1000.0
        queue_size = pipeline_cfg.get('queue_size', 8)
        stall_timeout_s = pipeline_cfg.get('stall_timeout_s', 30)

        # 组件
        self.audio_capture = AudioCapture(self.config)
        self.asr_engine = ASREngine(self.config)
        self.translator = Translator(self.config)
        self.websocket_server = WebSocketServer(self.config)
        self.ring_buffer = RingBuffer(int(buffer_seconds * sample_rate))
        self.segmenter = UtteranceSegmenter(
            sample_rate=sample_rate,
            threshold=vad_cfg.get('threshold', 0.5),
            min_silence_ms=vad_cfg.get('min_silence_duration_ms', 400),
            speech_pad_ms=vad_cfg.get('speech_pad_ms', 200),
            max_utterance_s=pipeline_cfg.get('max_utterance_s', 15),
        )
        self.worker = PipelineWorker(
            self.asr_engine, self.translator, self.websocket_server, queue_size,
            get_active_language=lambda: self.active_target_language,
            stall_timeout_s=stall_timeout_s,
            on_stall=lambda: self.asr_engine.notify_stall(),
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

        # ASR 健康事件（运行期降级/持续失败）→ device_state 广播与告警
        self.asr_engine.set_health_callback(self._on_asr_health)

        # 翻译健康事件（运行期降级/持续失败）→ device_state 广播与告警
        self.translator.set_health_callback(self._on_translation_health)

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

    def _resolve_device_preference(self) -> str:
        """
        解析统一设备偏好，优先级：SUBTITLE_DEVICE（非空且≠auto）> config.yaml
        的 asr/translation.device > auto。非法 env 值忽略并回落 config。
        """
        env_value = os.environ.get('SUBTITLE_DEVICE', '').strip()
        if env_value and env_value != 'auto':
            if env_value in VALID_DEVICES:
                logger.info(f"设备偏好来自 SUBTITLE_DEVICE: {env_value}")
                return env_value
            logger.warning(f"SUBTITLE_DEVICE 非法的设备值 {env_value!r}，忽略")

        for section in ('asr', 'translation'):
            value = self.config.get(section, {}).get('device')
            if value in ('cpu', 'cuda'):
                logger.info(f"设备偏好来自 config.yaml {section}.device: {value}")
                return value
        return 'auto'

    def _default_config(self) -> dict:
        """默认配置"""
        return {
            'audio': {
                'sample_rate': 16000,
                'channels': 1,
                'chunk_size': 512
            },
            'pipeline': {
                'buffer_seconds': 30,
                'tick_ms': 125,
                'max_utterance_s': 15,
                'queue_size': 8,
                'stall_timeout_s': 30
            },
            'vad': {
                'threshold': 0.5,
                'min_silence_duration_ms': 400,
                'speech_pad_ms': 200
            },
            'asr': {
                'model': 'funasr-nano',
                'device': 'auto',
                'language': 'ja',  # 源语言（识别提示 + 翻译源语言；非法值回退 ja）
                'itn': True,
                'hotwords': '',
                'num_threads': 2,
            },
            'translation': {
                'default_model': 'hy-mt2-1.8b-q4km',
                'models': [],
                'download': {'source': 'auto'},
                'n_ctx': 4096,
                'timeout_s': 30,
                'stream': True,
                'target_languages': ['zh', 'en'],
                'device': 'auto',
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

        # ASR 初始化完成（含降级）即广播设备状态
        await self._broadcast_device_state()

        # 后台预载默认翻译模型（llama-server；模型缺失时先下载，失败不阻塞启动）
        self._preload_task = asyncio.create_task(self._preload_default_model())

        # 启动管线工作器
        await self.worker.start()

        # 启动音频捕获（同步回调，只写环形缓冲）
        await self.audio_capture.start(self._on_audio_chunk)

        # 启动 VAD 切句循环
        self._tick_task = asyncio.create_task(self._segmentation_loop())

        logger.info("服务启动完成")

    async def _preload_default_model(self):
        """后台预载默认翻译模型（llama-server），失败转首次使用时重试；完成后广播设备状态"""
        try:
            await self.translator.ensure_default()
            logger.info("默认翻译模型后台预载完成")
        except Exception as e:
            logger.warning(f"默认翻译模型预载失败，将在首次使用时重试: {e}")
        await self._broadcast_device_state()

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
        await self.translator.stop()  # 终止 llama-server，无残留进程
        await self.websocket_server.stop()
        logger.info("服务已停止")

    async def _method_get_audio_sources(self, _params):
        """WS 方法：枚举音频源"""
        return await asyncio.to_thread(self.audio_capture.get_audio_sources)

    async def _method_get_config(self, _params):
        """WS 方法：返回后端运行配置摘要（含两引擎实际设备、原因与当前音频源）"""
        return {
            'asr': self.asr_engine.get_model_info(),
            'translation': self.translator.get_model_info(),
            'active_language': self.active_target_language,
            'audio': {'source': self.audio_capture.get_current_source()},
        }

    async def _broadcast_device_state(self):
        """广播两引擎实际设备与选择/降级原因（无客户端时 send 自动跳过）"""
        await self.websocket_server.send({
            'type': 'device_state',
            'asr': {
                'resolved': self.asr_engine.resolved_device,
                'reason': self.asr_engine.device_reason,
            },
            'translation': {
                'resolved': self.translator.resolved_device,
                'reason': self.translator.device_reason,
            },
        })

    # ------------------------------------------------------------------ #
    # ASR 健康事件：运行期降级 → 设备状态广播 + engine_degraded 告警
    # ------------------------------------------------------------------ #

    def _on_asr_health(self, payload: dict):
        """ASR 健康回调（事件循环上下文）：调度到主事件循环处理。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(self._schedule_asr_health, payload)

    def _schedule_asr_health(self, payload: dict):
        """在主事件循环中调度健康事件处理。"""
        asyncio.create_task(self._handle_asr_health(payload))

    async def _handle_asr_health(self, payload: dict):
        """运行期降级：广播设备状态与 engine_degraded；持续失败：仅告警。"""
        event = payload.get('event')
        if event == 'runtime_degraded':
            await self._broadcast_device_state()
            await self.websocket_server.send({
                'type': 'pipeline_warning',
                'reason': 'engine_degraded',
                'dropped': self.worker.dropped_count,
                'message': '识别引擎 GPU 运行时不可用，已自动回退 CPU（原因：运行时推理失败）',
                'detail': {
                    'engine': 'asr',
                    'from': 'cuda',
                    'to': 'cpu',
                    'device_reason': 'runtime_failed',
                },
            })
        elif event == 'persistent_failure':
            await self.websocket_server.send({
                'type': 'pipeline_warning',
                'reason': 'engine_degraded',
                'dropped': self.worker.dropped_count,
                'message': '识别引擎持续失败（CPU），请检查日志',
                'detail': {
                    'engine': 'asr',
                    'failures': payload.get('failures'),
                    'last_error': payload.get('last_error'),
                },
            })

    # ------------------------------------------------------------------ #
    # 翻译健康事件：运行期降级 → 设备状态广播 + engine_degraded 告警
    # ------------------------------------------------------------------ #

    def _on_translation_health(self, payload: dict):
        """翻译健康回调（事件循环上下文）：调度到主事件循环处理。"""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(self._schedule_translation_health, payload)

    def _schedule_translation_health(self, payload: dict):
        """在主事件循环中调度健康事件处理。"""
        asyncio.create_task(self._handle_translation_health(payload))

    async def _handle_translation_health(self, payload: dict):
        """运行期降级：广播设备状态与 engine_degraded；持续失败：仅告警。"""
        event = payload.get('event')
        if event == 'runtime_degraded':
            await self._broadcast_device_state()
            await self.websocket_server.send({
                'type': 'pipeline_warning',
                'reason': 'engine_degraded',
                'dropped': self.worker.dropped_count,
                'message': '翻译引擎 GPU 运行时不可用，已自动回退 CPU（原因：运行时推理失败）',
                'detail': {
                    'engine': 'translation',
                    'from': 'cuda',
                    'to': 'cpu',
                    'device_reason': 'runtime_failed',
                },
            })
        elif event == 'persistent_failure':
            await self.websocket_server.send({
                'type': 'pipeline_warning',
                'reason': 'engine_degraded',
                'dropped': self.worker.dropped_count,
                'message': '翻译引擎持续失败，请检查日志',
                'detail': {
                    'engine': 'translation',
                    'failures': payload.get('failures'),
                    'last_error': payload.get('last_error'),
                },
            })

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
            # 新格式优先；旧格式 source_id（裸设备 id，''=默认）向后兼容
            if message.get('source') is not None:
                source = message.get('source')
            elif 'source_id' in message:
                source = message.get('source_id')
            else:
                source = None
            try:
                self.audio_capture.set_audio_source(source)
                await self.audio_capture.restart()
            except (ValueError, RuntimeError) as e:
                logger.warning(f"切换音频源失败: {e}")
                await self.websocket_server.send({
                    'type': 'error',
                    'code': 'invalid_audio_source',
                    'message': str(e)
                })
                return
            logger.info(f"音频源已切换: {self.audio_capture.get_current_source()}")
        elif action == 'change_model':
            # 单引擎模型语义（线格式保持 model_size 字段名；spec: pipeline-control）
            model_id = message.get('model_size')
            try:
                await self.asr_engine.change_model(model_id)
            except ValueError as e:
                logger.warning(f"切换模型失败: {e}")
                await self.websocket_server.send({
                    'type': 'error',
                    'code': 'invalid_model',
                    'message': str(e)
                })
        elif action == 'set_source_language':
            # 源语言控制（ja/zh/en，热生效；spec: pipeline-control「源语言控制」）
            language = message.get('language')
            try:
                applied = self.asr_engine.change_source_language(language)
            except ValueError as e:
                await self.websocket_server.send({
                    'type': 'error',
                    'code': 'invalid_language',
                    'message': str(e)
                })
            else:
                logger.info(f"源语言切换为: {applied}")
        elif action == 'change_llm':
            model_id = message.get('model_id')
            try:
                await self.translator.change_llm(model_id)
            except ValueError as e:
                logger.warning(f"切换翻译模型失败: {e}")
                await self.websocket_server.send({
                    'type': 'error',
                    'code': 'invalid_llm',
                    'message': str(e)
                })
                return
            except DownloadError as e:
                logger.error(f"翻译模型下载失败: {e}")
                await self.websocket_server.send({
                    'type': 'error',
                    'code': 'model_download_failed',
                    'message': str(e)
                })
                return
            except Exception as e:  # noqa: BLE001 - 不得冒泡到连接级 catch 导致断连
                logger.error(f"切换翻译模型失败: {e}", exc_info=True)
                await self.websocket_server.send({
                    'type': 'error',
                    'code': 'llm_load_failed',
                    'message': str(e)
                })
                return
            # 成功后设备可能因静默降级变化，广播实际状态
            await self._broadcast_device_state()
        elif action == 'change_device':
            device = message.get('device')
            if device not in VALID_DEVICES:
                await self.websocket_server.send({
                    'type': 'error',
                    'code': 'invalid_device',
                    'message': f'不支持的设备: {device}，支持: {VALID_DEVICES}'
                })
                return
            try:
                await self.asr_engine.change_device(device)
                await self.translator.change_device(device)
            except Exception as e:  # noqa: BLE001 - 不得冒泡到连接级 catch 导致断连
                logger.error(f"切换设备失败: {e}", exc_info=True)
                return
            # 切换完成（无论是否降级）广播实际设备状态；降级不产生 error 回执
            await self._broadcast_device_state()
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
