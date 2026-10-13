"""麦当劳 MCP 客户端 —— 只读调用封装

Token 读取优先级：
  1. 环境变量 MCD_MCP_TOKEN
  2. ~/.workbuddy/.mcp.json 中 mcd-mcp 条目（本地开发用，绝不入库）
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"


class McdMcpError(RuntimeError):
    pass


def unwrap(text: Any) -> Any:
    """统一解析麦当劳 MCP 的三种返回形态。

    实测发现同一服务混用了三种格式：
      1. 纯 JSON 字符串
      2. Markdown 包裹 —— "# API Response Information ... ## Original Response\\n{json}"
      3. 纯 Markdown 报告（券列表、活动日历），无 JSON 可言

    返回：能解析成 dict/list 就返回对象，否则原样返回字符串。
    """
    if not isinstance(text, str):
        return text
    seg = text
    m = re.search(r"##\s*Original Response\s*\n+(.*)", text, re.S)
    if m:
        seg = m.group(1).strip()
    seg = re.sub(r"^```[a-zA-Z]*\n", "", seg)
    seg = re.sub(r"\n```\s*$", "", seg.strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = seg.find(opener), seg.rfind(closer)
        if i >= 0 and j > i:
            try:
                return json.loads(seg[i:j + 1])
            except Exception:
                continue
    return text


def load_token() -> str:
    tok = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if tok:
        return tok
    cfg = Path.home() / ".workbuddy" / ".mcp.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            node = data.get("mcpServers", {}).get("mcd-mcp", {})
            auth = node.get("headers", {}).get("Authorization", "")
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
        except Exception:
            pass
    raise McdMcpError(
        "未找到 MCP Token。请设置环境变量 MCD_MCP_TOKEN，"
        "或在 ~/.workbuddy/.mcp.json 中配置 mcd-mcp。"
    )


class McdMcpClient:
    def __init__(self, token: str | None = None, endpoint: str = ENDPOINT):
        self.token = token or load_token()
        self.endpoint = endpoint
        self._session_id: str | None = None
        self._initialized = False

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
        except urllib.error.HTTPError as e:
            raise McdMcpError(f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:300]}")
        except urllib.error.URLError as e:
            raise McdMcpError(f"网络错误: {e.reason}")
        return self._parse(raw)

    @staticmethod
    def _parse(raw: str) -> dict:
        raw = raw.strip()
        if not raw:
            raise McdMcpError("服务端返回空响应")
        if raw.startswith("{"):
            return json.loads(raw)
        # SSE 流：取最后一条 data:
        last = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                last = line[5:].strip()
        if last is None:
            raise McdMcpError(f"无法解析响应: {raw[:200]}")
        return json.loads(last)

    def initialize(self) -> None:
        if self._initialized:
            return
        r = self._post({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-countdown", "version": "1.0.0"},
            },
        })
        if "error" in r:
            raise McdMcpError(f"initialize 失败: {r['error']}")
        self._initialized = True

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """调用 MCP 工具，返回其文本内容（尽力解析 JSON，否则返回原始字符串）。"""
        self.initialize()
        r = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        if "error" in r:
            raise McdMcpError(f"{name} 调用失败: {r['error']}")
        result = r.get("result", {})
        if result.get("isError"):
            raise McdMcpError(f"{name} 返回错误: {result.get('content')}")
        texts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
        joined = "\n".join(texts).strip()
        try:
            return json.loads(joined)
        except Exception:
            return joined
