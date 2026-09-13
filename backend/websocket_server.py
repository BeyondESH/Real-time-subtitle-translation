"""
WebSocket 服务端模块
"""
import asyncio
import json
import logging
from typing import Callable, Optional

import websockets
from websockets.server import serve, WebSocketServerProtocol

logger = logging.getLogger(__name__)


class WebSocketServer:
    """WebSocket 服务端"""

    def __init__(self, config: dict):
        self.config = config.get('websocket', {})
        self.host = self.config.get('host', 'localhost')
        self.port = self.config.get('port', 8765)

        self._clients: set = set()
        self._server = None
        self._callback: Optional[Callable] = None
        self._running = False

    async def start(self, callback: Callable):
        """
        启动 WebSocket 服务

        Args:
            callback: 接收客户端消息的回调函数
        """
        if self._running:
            logger.warning("WebSocket 服务已在运行")
            return

        self._callback = callback
        self._running = True

        # 启动服务器
        self._server = await serve(
            self._handle_connection,
            self.host,
            self.port
        )

        logger.info(f"WebSocket 服务已启动: ws://{self.host}:{self.port}")

    async def stop(self):
        """停止 WebSocket 服务"""
        self._running = False

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # 关闭所有客户端连接
        for client in self._clients.copy():
            await client.close()

        self._clients.clear()
        logger.info("WebSocket 服务已停止")

    async def _handle_connection(self, websocket: WebSocketServerProtocol):
        """
        处理新的 WebSocket 连接

        Args:
            websocket: WebSocket 连接对象
        """
        self._clients.add(websocket)
        logger.info(f"新客户端连接: {websocket.remote_address}")

        try:
            async for message in websocket:
                await self._process_message(message)
        except websockets.exceptions.ConnectionClosed:
            logger.info("客户端断开连接")
        except Exception as e:
            logger.error(f"处理消息错误: {e}")
        finally:
            self._clients.discard(websocket)

    async def _process_message(self, message: str):
        """
        处理收到的消息

        Args:
            message: JSON 格式的消息字符串
        """
        try:
            data = json.loads(message)

            if self._callback:
                await self._callback(data)

        except json.JSONDecodeError as e:
            logger.error(f"JSON 解析错误: {e}")
        except Exception as e:
            logger.error(f"处理消息错误: {e}")

    async def send(self, data: dict):
        """
        向所有客户端发送消息

        Args:
            data: 要发送的数据字典
        """
        if not self._clients:
            return

        message = json.dumps(data, ensure_ascii=False)
        disconnected = set()

        for client in self._clients:
            try:
                await client.send(message)
            except websockets.exceptions.ConnectionClosed:
                disconnected.add(client)
            except Exception as e:
                logger.error(f"发送消息失败: {e}")
                disconnected.add(client)

        # 清理断开的连接
        self._clients -= disconnected

    async def send_to(self, websocket: WebSocketServerProtocol, data: dict):
        """
        向特定客户端发送消息

        Args:
            websocket: 目标客户端连接
            data: 要发送的数据字典
        """
        try:
            message = json.dumps(data, ensure_ascii=False)
            await websocket.send(message)
        except Exception as e:
            logger.error(f"发送消息失败: {e}")

    def get_client_count(self) -> int:
        """获取当前连接的客户端数量"""
        return len(self._clients)
