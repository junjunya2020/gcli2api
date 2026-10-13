"""
代理实体管理模块（per-credential proxy 支持，本 fork 增强）

设计：
- 代理实体集中存储于统一配置表中（key = "proxies"），结构为列表：
    [{"id": "<uuid>", "name": "香港-01", "url": "socks5://host:port", "enabled": true}]
- 凭据与代理的绑定：凭据数据中的 "proxy" 字段 = 代理实体 id（可为空）
    - 空 / 不存在  -> 使用全局代理（config 中的 proxy / 环境变量 PROXY）
    - 非空         -> 使用该代理实体；若实体不存在或已禁用，回退全局代理
- 每次请求时按凭据动态解析代理（支持热更新），OAuth 授权、凭据测试同样遵循此规则。
"""

import re
import time
import uuid
from typing import Any, Dict, List, Optional

import config
from log import log

# 配置表中存储代理列表的 key
CONFIG_KEY = "proxies"

# 支持的代理 URL 协议前缀
_PROXY_URL_RE = re.compile(r"^(https?|socks5h?|socks4a?)://", re.IGNORECASE)


def normalize_proxy_url(url: str) -> str:
    """校验并规整代理 URL。

    支持 http/https/socks5/socks5h/socks4/socks4a。
    若未带协议前缀，默认按 http 处理。

    Raises:
        ValueError: URL 非法
    """
    if not url or not isinstance(url, str):
        raise ValueError("代理地址不能为空")

    url = url.strip()
    if not _PROXY_URL_RE.match(url):
        # 未写协议，默认 http（兼容用户直接填 host:port）
        url = "http://" + url

    # 去掉尾部斜杠
    url = url.rstrip("/")

    # 简单结构校验：scheme://host[:port]
    m = re.match(r"^(https?|socks5h?|socks4a?)://(\[[0-9a-fA-F:]+\]|[^/\s:]+)(?::(\d+))?$", url, re.IGNORECASE)
    if not m:
        raise ValueError(f"代理地址格式非法: {url}（示例: http://127.0.0.1:7890 或 socks5://127.0.0.1:1080）")
    host = m.group(2)
    port = m.group(3)
    if port is not None and not (1 <= int(port) <= 65535):
        raise ValueError(f"代理端口非法: {port}")
    if not host:
        raise ValueError("代理主机为空")
    return url


def proxy_scheme(url: str) -> str:
    """返回代理协议（小写，如 http / socks5）"""
    m = _PROXY_URL_RE.match(url or "")
    return m.group(1).lower() if m else ""


async def list_proxies() -> List[Dict[str, Any]]:
    """返回所有代理实体"""
    from src.storage_adapter import get_storage_adapter

    storage_adapter = await get_storage_adapter()
    proxies = await storage_adapter.get_config(CONFIG_KEY, [])
    if not isinstance(proxies, list):
        return []
    return proxies


async def get_proxy(proxy_id: str) -> Optional[Dict[str, Any]]:
    """按 id 获取代理实体"""
    if not proxy_id:
        return None
    for p in await list_proxies():
        if p.get("id") == proxy_id:
            return p
    return None


async def get_proxy_by_name(name: str) -> Optional[Dict[str, Any]]:
    """按名称获取代理实体（名称唯一）"""
    if not name:
        return None
    for p in await list_proxies():
        if p.get("name") == name:
            return p
    return None


async def _save(proxies: List[Dict[str, Any]]) -> bool:
    from src.storage_adapter import get_storage_adapter

    storage_adapter = await get_storage_adapter()
    ok = await storage_adapter.set_config(CONFIG_KEY, proxies)
    if ok:
        # 配置缓存需要刷新，保证读写一致
        try:
            await storage_adapter._backend.reload_config_cache()
        except Exception:
            pass
    return ok


async def add_proxy(name: str, url: str, enabled: bool = True) -> Dict[str, Any]:
    """新增代理实体"""
    name = (name or "").strip()
    if not name:
        raise ValueError("代理名称不能为空")
    url = normalize_proxy_url(url)

    proxies = await list_proxies()
    if any(p.get("name") == name for p in proxies):
        raise ValueError(f"代理名称已存在: {name}")

    proxy = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "url": url,
        "enabled": bool(enabled),
        "created_at": time.time(),
    }
    proxies.append(proxy)
    if not await _save(proxies):
        raise RuntimeError("保存代理失败")
    log.info(f"[PROXY] 新增代理: {name} ({url}) id={proxy['id']}")
    return proxy


async def update_proxy(
    proxy_id: str,
    name: Optional[str] = None,
    url: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    """更新代理实体"""
    proxies = await list_proxies()
    target = None
    for p in proxies:
        if p.get("id") == proxy_id:
            target = p
            break
    if target is None:
        return None

    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("代理名称不能为空")
        if any(p.get("name") == name and p.get("id") != proxy_id for p in proxies):
            raise ValueError(f"代理名称已存在: {name}")
        target["name"] = name
    if url is not None:
        target["url"] = normalize_proxy_url(url)
    if enabled is not None:
        target["enabled"] = bool(enabled)

    if not await _save(proxies):
        raise RuntimeError("保存代理失败")
    log.info(f"[PROXY] 更新代理: id={proxy_id}")
    return target


async def delete_proxy(proxy_id: str) -> bool:
    """删除代理实体（同时从引用它的凭据与流程中解绑由调用方处理）"""
    proxies = await list_proxies()
    new_proxies = [p for p in proxies if p.get("id") != proxy_id]
    if len(new_proxies) == len(proxies):
        return False
    if not await _save(new_proxies):
        raise RuntimeError("保存代理失败")
    log.info(f"[PROXY] 删除代理: id={proxy_id}")
    return True


async def resolve_proxy_for_credential(credential_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    解析某条凭据应使用的代理 URL。

    - 凭据含 "proxy" 字段且该代理实体存在且启用 -> 用该代理
    - 否则 -> 回退到全局代理（config: proxy / 环境变量 PROXY）

    返回代理 URL 字符串或 None（不使用代理）。
    """
    if credential_data:
        bound = credential_data.get("proxy")
        if bound:
            p = await get_proxy(bound)
            if p and p.get("enabled", True) and p.get("url"):
                return p["url"]
            if p and not p.get("enabled", True):
                log.debug(f"[PROXY] 凭据绑定的代理已禁用({bound})，回退全局代理")
            elif not p:
                log.debug(f"[PROXY] 凭据绑定的代理不存在({bound})，回退全局代理")
    # 回退全局
    return await config.get_proxy_config()


async def test_proxy_url(url: str, timeout: float = 12.0) -> Dict[str, Any]:
    """
    测试代理可用性：通过该代理访问 Google 端点，返回出口 IP 与耗时。

    返回: {"ok": bool, "exit_ip": str|None, "latency_ms": int|None, "status": int|None, "error": str|None}
    """
    import httpx

    result: Dict[str, Any] = {
        "ok": False,
        "exit_ip": None,
        "latency_ms": None,
        "status": None,
        "error": None,
    }
    try:
        url = normalize_proxy_url(url)
    except ValueError as e:
        result["error"] = str(e)
        return result

    t0 = time.time()
    try:
        async with httpx.AsyncClient(proxy=url, timeout=timeout) as client:
            # 1) 取出口 IP
            try:
                r = await client.get("https://api.ipify.org?format=json")
                if r.status_code == 200:
                    result["exit_ip"] = r.json().get("ip")
            except Exception:
                pass

            # 2) 测试能否到达 Google（gcli2api 的实际用途）
            try:
                r2 = await client.get("https://www.googleapis.com")
                result["status"] = r2.status_code
            except Exception as e:
                result["error"] = f"访问 Google 失败: {e}"
                result["latency_ms"] = int((time.time() - t0) * 1000)
                return result

        result["latency_ms"] = int((time.time() - t0) * 1000)
        result["ok"] = True
        return result
    except Exception as e:
        result["error"] = str(e)
        result["latency_ms"] = int((time.time() - t0) * 1000)
        return result
