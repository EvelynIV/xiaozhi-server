import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx
import openai
import websockets
from config.logger import setup_logging

TAG = __name__


class SuppressInvalidHandshakeFilter(logging.Filter):
    """过滤掉无效握手错误日志（如HTTPS访问WS端口）"""

    def filter(self, record):
        msg = record.getMessage()
        suppress_keywords = [
            "opening handshake failed",
            "did not receive a valid HTTP request",
            "connection closed while reading HTTP request",
            "line without CRLF",
        ]
        return not any(keyword in msg for keyword in suppress_keywords)


def _setup_websockets_logger():
    """配置 websockets 相关的所有 logger，过滤无效握手错误"""
    filter_instance = SuppressInvalidHandshakeFilter()
    for logger_name in ["websockets", "websockets.server", "websockets.client"]:
        logger = logging.getLogger(logger_name)
        logger.addFilter(filter_instance)


_setup_websockets_logger()


class GatewayWebSocketServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        gateway_config = config.get("gateway", {})
        openai_config = gateway_config.get("openai", {})

        self.require_device_id = gateway_config.get("require_device_id", True)
        self.allowed_devices = set(gateway_config.get("allowed_devices", []))
        self.model = openai_config.get("model", "gpt-4o-mini")
        self.timeout = int(openai_config.get("timeout", 60))
        base_url = openai_config.get("base_url", "https://api.openai.com/v1")
        api_key = openai_config.get("api_key", "")

        if not api_key or "你" in api_key:
            self.logger.bind(tag=TAG).warning("OpenAI API Key未配置，将无法完成请求。")

        self.client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(self.timeout),
        )

    async def start(self):
        server_config = self.config.get("server", {})
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("port", 8000))

        async with websockets.serve(
            self._handle_connection, host, port, process_request=self._http_response
        ):
            await asyncio.Future()

    async def _http_response(self, websocket, request_headers):
        if request_headers.headers.get("connection", "").lower() == "upgrade":
            return None
        return websocket.respond(200, "Gateway is running\n")

    async def _handle_connection(self, websocket):
        headers = dict(websocket.request.headers)
        device_id = headers.get("device-id")

        if self.require_device_id and not device_id:
            await self._send_error(websocket, "缺少device-id，拒绝连接。")
            await websocket.close()
            return

        if self.allowed_devices and device_id not in self.allowed_devices:
            await self._send_error(websocket, "device-id未授权。")
            await websocket.close()
            return

        self.logger.bind(tag=TAG).info("连接建立: device-id={}", device_id)
        try:
            async for message in websocket:
                await self._handle_message(websocket, message, device_id)
        except websockets.exceptions.ConnectionClosed:
            self.logger.bind(tag=TAG).info("客户端断开连接: device-id={}", device_id)

    async def _handle_message(self, websocket, message, device_id):
        if isinstance(message, bytes):
            await self._send_error(websocket, "仅支持文本消息。")
            return

        payload = self._parse_payload(message)
        if payload is None:
            await self._send_error(websocket, "消息格式不合法。")
            return

        messages = payload.get("messages")
        if not messages:
            text = payload.get("text") or payload.get("content") or payload.get("prompt")
            if not text:
                await self._send_error(websocket, "缺少messages或text字段。")
                return
            messages = [{"role": "user", "content": text}]

        request_id = payload.get("request_id")
        try:
            response_text = await asyncio.to_thread(self._call_openai, messages)
        except Exception as exc:
            self.logger.bind(tag=TAG).error("OpenAI请求失败: {}", exc)
            await self._send_error(websocket, f"OpenAI请求失败: {exc}", request_id)
            return

        response_payload = {
            "type": "response",
            "device_id": device_id,
            "content": response_text,
        }
        if request_id:
            response_payload["request_id"] = request_id
        await websocket.send(json.dumps(response_payload, ensure_ascii=False))

    def _call_openai(self, messages: List[Dict[str, Any]]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
        )
        choice = response.choices[0] if response.choices else None
        if choice and getattr(choice, "message", None):
            return choice.message.content or ""
        return ""

    def _parse_payload(self, message: str) -> Optional[Dict[str, Any]]:
        try:
            parsed = json.loads(message)
        except json.JSONDecodeError:
            return {"text": message}

        if isinstance(parsed, dict):
            return parsed
        return None

    async def _send_error(self, websocket, message: str, request_id: Optional[str] = None):
        payload = {"type": "error", "message": message}
        if request_id:
            payload["request_id"] = request_id
        await websocket.send(json.dumps(payload, ensure_ascii=False))
