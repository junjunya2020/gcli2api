"""
代理管理路由模块 - 处理 /proxies/* 相关请求（本 fork 增强）

提供代理实体的增删改查与连通性测试，供控制面板"代理管理"区使用。
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

import config
from log import log
from src.utils import verify_panel_token
from src import proxies as proxy_store


router = APIRouter(prefix="/proxies", tags=["proxies"])


class ProxyAddRequest(BaseModel):
    name: str
    url: str
    enabled: bool = True


class ProxyUpdateRequest(BaseModel):
    id: str
    name: Optional[str] = None
    url: Optional[str] = None
    enabled: Optional[bool] = None


class ProxyIdRequest(BaseModel):
    id: Optional[str] = None
    url: Optional[str] = None


@router.get("/list")
async def list_proxies(token: str = Depends(verify_panel_token)):
    """列出所有代理实体 + 当前全局代理"""
    try:
        proxies = await proxy_store.list_proxies()
        global_proxy = await config.get_proxy_config()
        return JSONResponse(content={"proxies": proxies, "global_proxy": global_proxy or ""})
    except Exception as e:
        log.error(f"获取代理列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/add")
async def add_proxy(request: ProxyAddRequest, token: str = Depends(verify_panel_token)):
    """新增代理实体"""
    try:
        proxy = await proxy_store.add_proxy(request.name, request.url, request.enabled)
        return JSONResponse(content={"success": True, "proxy": proxy})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.error(f"新增代理失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update")
async def update_proxy(request: ProxyUpdateRequest, token: str = Depends(verify_panel_token)):
    """更新代理实体"""
    try:
        proxy = await proxy_store.update_proxy(
            request.id, name=request.name, url=request.url, enabled=request.enabled
        )
        if proxy is None:
            raise HTTPException(status_code=404, detail="代理不存在")
        return JSONResponse(content={"success": True, "proxy": proxy})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"更新代理失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/delete")
async def delete_proxy(request: ProxyIdRequest, token: str = Depends(verify_panel_token)):
    """删除代理实体（引用它的凭据将自动回退到全局代理）"""
    try:
        if not request.id:
            raise HTTPException(status_code=400, detail="缺少代理 id")
        ok = await proxy_store.delete_proxy(request.id)
        if not ok:
            raise HTTPException(status_code=404, detail="代理不存在")
        return JSONResponse(content={"success": True})
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"删除代理失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/test")
async def test_proxy(request: ProxyIdRequest, token: str = Depends(verify_panel_token)):
    """
    测试代理连通性。可用代理 id（查实体）或直接传 url。
    返回出口 IP、耗时、是否可达 Google。
    """
    try:
        url = request.url
        name = None
        if request.id:
            p = await proxy_store.get_proxy(request.id)
            if not p:
                raise HTTPException(status_code=404, detail="代理不存在")
            url = p.get("url")
            name = p.get("name")
        if not url:
            raise HTTPException(status_code=400, detail="缺少代理 id 或 url")

        result = await proxy_store.test_proxy_url(url)
        result["name"] = name
        result["url"] = url
        log.info(f"[PROXY] 测试 {name or url}: ok={result.get('ok')} ip={result.get('exit_ip')} err={result.get('error')}")
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"测试代理失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
