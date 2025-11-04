"""
FastAPI-based admin HTTP interface for APIWeaver.

Routes:
- POST /admin/register      -> register an API (body: API config JSON)
- POST /admin/unregister    -> unregister (body: {"api_name":"..."})
- GET  /admin/list          -> list registered APIs
- POST /admin/test          -> test connection (body: {"api_name":"..."})
- GET  /admin/schema/{api_name} -> get schema, optional ?endpoint=xxx

Persistent storage: ./apis.json (configurable)
"""
from typing import Optional, Dict, Any
import asyncio

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from .storage import JsonStore
from .server import APIWeaver
from .models import APIConfig  # 使用库中已有的 Pydantic 模型

app = FastAPI(title="APIWeaver Admin", version="0.1.0")
store = JsonStore("apis.json")
weaver = APIWeaver()  # 在同一进程中持有 APIWeaver 实例
_startup_lock = asyncio.Lock()

# Pydantic 请求模型（可直接传入任意 JSON，内部使用 APIConfig 校验）
class RegisterPayload(BaseModel):
    config: Dict[str, Any]

class UnregisterPayload(BaseModel):
    api_name: str

class TestPayload(BaseModel):
    api_name: str

@app.on_event("startup")
async def startup_event():
    """
    Load persisted configs from apis.json and register them into weaver.
    This replicates what register_api does so the MCP tools are created.
    """
    async with _startup_lock:
        data = await store.load_all()
        for name, cfg in data.items():
            try:
                # Validate/cast via APIConfig model
                api_config = APIConfig(**cfg)
                # store in weaver
                weaver.apis[api_config.name] = api_config
                client = await weaver._create_http_client(api_config)
                weaver.http_clients[api_config.name] = client
                # create endpoint tools
                for ep in api_config.endpoints:
                    tool_name = f"{api_config.name}_{ep.name}"
                    # _create_endpoint_tool registers into weaver.mcp
                    await weaver._create_endpoint_tool(api_config, ep, tool_name)
                app.logger = getattr(app, "logger", None)  # placeholder
            except Exception as e:
                # Skip invalid entries but don't crash startup
                print(f"[admin startup] failed to load {name}: {e}")

@app.post("/admin/register")
async def admin_register(payload: RegisterPayload):
    """
    Register new API config and persist it.
    Body example: { "config": { ... API config as in README ... } }
    """
    config = payload.config
    try:
        api_config = APIConfig(**config)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    name = api_config.name
    if name in weaver.apis:
        raise HTTPException(status_code=400, detail=f"API '{name}' already registered")

    try:
        # Create client and tools (same logic as register_api)
        weaver.apis[name] = api_config
        client = await weaver._create_http_client(api_config)
        weaver.http_clients[name] = client

        created = []
        for ep in api_config.endpoints:
            tool_name = f"{name}_{ep.name}"
            await weaver._create_endpoint_tool(api_config, ep, tool_name)
            created.append(tool_name)

        # persist
        await store.add_api(name, config)

        return {"status": "ok", "message": f"Registered {name}", "created_tools": created}
    except Exception as e:
        # Cleanup on failure
        if name in weaver.http_clients:
            await weaver.http_clients[name].aclose()
            del weaver.http_clients[name]
        if name in weaver.apis:
            del weaver.apis[name]
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/unregister")
async def admin_unregister(payload: UnregisterPayload):
    name = payload.api_name
    if name not in weaver.apis:
        raise HTTPException(status_code=404, detail=f"API '{name}' not found")

    api_config = weaver.apis[name]
    # Remove tools
    for ep in api_config.endpoints:
        tool_name = f"{name}_{ep.name}"
        try:
            weaver.mcp.remove_tool(tool_name)
        except Exception:
            pass
    # Close http client
    if name in weaver.http_clients:
        try:
            await weaver.http_clients[name].aclose()
        except Exception:
            pass
        del weaver.http_clients[name]
    # Remove config
    del weaver.apis[name]
    # Persist removal
    await store.remove_api(name)
    return {"status": "ok", "message": f"Unregistered {name}"}

@app.get("/admin/list")
async def admin_list():
    """
    Return a similar structure to the MCP tool list_apis.
    """
    result = {}
    for name, api in weaver.apis.items():
        result[name] = {
            "base_url": api.base_url,
            "description": api.description,
            "auth_type": api.auth.type if api.auth else "none",
            "endpoints": [
                {
                    "name": ep.name,
                    "method": ep.method,
                    "path": ep.path,
                    "description": ep.description,
                    "parameters": [
                        {
                            "name": param.name,
                            "type": param.type,
                            "location": param.location,
                            "required": param.required,
                            "description": param.description,
                            "default": param.default
                        }
                        for param in ep.params
                    ]
                }
                for ep in api.endpoints
            ]
        }
    return result

@app.post("/admin/test")
async def admin_test(payload: TestPayload):
    name = payload.api_name
    if name not in weaver.apis:
        raise HTTPException(status_code=404, detail=f"API '{name}' not found")
    client = weaver.http_clients.get(name)
    if not client:
        raise HTTPException(status_code=500, detail=f"No HTTP client for '{name}'")
    api_config = weaver.apis[name]
    try:
        # Try HEAD first, fallback to GET
        resp = await client.head(api_config.base_url, timeout=5.0)
        return {"status": "connected", "status_code": resp.status_code, "headers": dict(resp.headers)}
    except Exception as e:
        return {"status": "failed", "error": str(e)}

@app.get("/admin/schema/{api_name}")
async def admin_schema(api_name: str, endpoint: Optional[str] = None):
    if api_name not in weaver.apis:
        raise HTTPException(status_code=404, detail=f"API '{api_name}' not found")
    api_config = weaver.apis[api_name]
    if endpoint:
        ep = next((e for e in api_config.endpoints if e.name == endpoint), None)
        if not ep:
            raise HTTPException(status_code=404, detail=f"Endpoint '{endpoint}' not found in API '{api_name}'")
        return {
            "api_name": api_name,
            "endpoint_name": endpoint,
            "method": ep.method,
            "path": ep.path,
            "description": ep.description,
            "parameters": [
                {
                    "name": p.name,
                    "type": p.type,
                    "location": p.location,
                    "required": p.required,
                    "description": p.description,
                    "default": p.default,
                    "enum": p.enum
                } for p in ep.params
            ],
            "headers": ep.headers,
            "timeout": ep.timeout
        }
    else:
        return {
            "api_name": api_name,
            "base_url": api_config.base_url,
            "description": api_config.description,
            "auth_type": api_config.auth.type if api_config.auth else "none",
            "global_headers": api_config.headers,
            "endpoints": [
                {
                    "name": ep.name,
                    "method": ep.method,
                    "path": ep.path,
                    "description": ep.description,
                    "parameters": [
                        {
                            "name": p.name,
                            "type": p.type,
                            "location": p.location,
                            "required": p.required,
                            "description": p.description,
                            "default": p.default,
                            "enum": p.enum
                        } for p in ep.params
                    ]
                } for ep in api_config.endpoints
            ]
        }

@app.on_event("shutdown")
async def shutdown_event():
    # Persist current configs on shutdown (defensive)
    try:
        to_save = {}
        for name, api in weaver.apis.items():
            # api may be Pydantic model or dict-like; attempt to export
            try:
                cfg = api.dict()
            except Exception:
                # Fallback: try to reconstruct minimal dict
                cfg = {
                    "name": api.name,
                    "base_url": api.base_url,
                    "description": getattr(api, "description", None),
                    "auth": getattr(api, "auth", None),
                    "headers": getattr(api, "headers", None),
                    "endpoints": [ep.dict() if hasattr(ep, "dict") else ep for ep in api.endpoints]
                }
            to_save[name] = cfg
        await store.save_all(to_save)
    except Exception as e:
        print(f"[admin shutdown] failed to save configs: {e}")

# If you want to run this admin server directly:
if __name__ == "__main__":
    uvicorn.run("apiweaver.admin_http:app", host="127.0.0.1", port=9000, reload=False)
