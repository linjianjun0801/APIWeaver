"""
FastAPI admin HTTP interface with multi-server support.

Routes (server-scoped):
- POST /admin/{server_name}/register      -> register an API (body: API config JSON)
- POST /admin/{server_name}/unregister    -> unregister (body: {"api_name":"..."})
- GET  /admin/{server_name}/list          -> list registered APIs for that server
- POST /admin/{server_name}/test          -> test connection (body: {"api_name":"..."})
- GET  /admin/{server_name}/schema/{api_name} -> get schema, optional ?endpoint=xxx

Behavior:
- Persist configs into single apis.json organized by server name (see storage.JsonStore).
- On FastAPI startup loads all servers and registers their APIs into per-server APIWeaver instances.
- Each server gets its own APIWeaver instance stored in `weavers` dict.
- Note: running multiple MCP transports (HTTP ports) from same process requires configuring each APIWeaver.run(...) appropriately
"""
from typing import Optional, Dict, Any
import asyncio

from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel
import uvicorn

from .storage import JsonStore
from .server import APIWeaver
from .models import APIConfig  # reuse repo models

app = FastAPI(title="APIWeaver Admin (multi-server)", version="0.1.0")
store = JsonStore("apis.json")

# Map server_name -> APIWeaver instance
weavers: Dict[str, APIWeaver] = {}
_startup_lock = asyncio.Lock()

# Request models
class RegisterPayload(BaseModel):
    config: Dict[str, Any]

class UnregisterPayload(BaseModel):
    api_name: str

class TestPayload(BaseModel):
    api_name: str

async def _ensure_weaver_for(server_name: str) -> APIWeaver:
    """
    Ensure an APIWeaver instance exists for server_name.
    If it does not exist, create one and keep it in weavers.
    (We do not automatically call its .run() here; you can run it separately.)
    """
    if server_name in weavers:
        return weavers[server_name]
    # Create new APIWeaver instance with a unique name
    weaver = APIWeaver(name=f"APIWeaver-{server_name}")
    weavers[server_name] = weaver
    return weaver

@app.on_event("startup")
async def startup_event():
    """
    Load persisted configs from apis.json and register them into per-server weavers.
    """
    async with _startup_lock:
        all_servers = await store.load_all_servers()
        for server_name, apis in all_servers.items():
            try:
                weaver = await _ensure_weaver_for(server_name)
                # register each api in that server
                for name, cfg in apis.items():
                    try:
                        api_config = APIConfig(**cfg)
                        weaver.apis[api_config.name] = api_config
                        client = await weaver._create_http_client(api_config)
                        weaver.http_clients[api_config.name] = client
                        for ep in api_config.endpoints:
                            tool_name = f"{api_config.name}_{ep.name}"
                            await weaver._create_endpoint_tool(api_config, ep, tool_name)
                    except Exception as e:
                        print(f"[admin startup] server={server_name} failed to load api={name}: {e}")
            except Exception as e:
                print(f"[admin startup] failed to init weaver for server={server_name}: {e}")

@app.post("/admin/{server_name}/register")
async def admin_register(server_name: str, payload: RegisterPayload):
    """
    Register new API config under server_name and persist it.
    Body: {"config": { ... API config ... }}
    """
    config = payload.config
    try:
        api_config = APIConfig(**config)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    name = api_config.name
    # ensure weaver instance
    weaver = await _ensure_weaver_for(server_name)
    if name in weaver.apis:
        raise HTTPException(status_code=400, detail=f"API '{name}' already registered on server '{server_name}'")

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

        # persist under server
        await store.add_api(server_name, name, config)

        return {"status": "ok", "message": f"Registered {name} on server {server_name}", "created_tools": created}
    except Exception as e:
        # Cleanup on failure
        if name in weaver.http_clients:
            await weaver.http_clients[name].aclose()
            del weaver.http_clients[name]
        if name in weaver.apis:
            del weaver.apis[name]
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/{server_name}/unregister")
async def admin_unregister(server_name: str, payload: UnregisterPayload):
    name = payload.api_name
    weaver = weavers.get(server_name)
    if not weaver or name not in weaver.apis:
        raise HTTPException(status_code=404, detail=f"API '{name}' not found on server '{server_name}'")

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
    await store.remove_api(server_name, name)
    return {"status": "ok", "message": f"Unregistered {name} from server {server_name}"}

@app.get("/admin/{server_name}/list")
async def admin_list(server_name: str):
    """
    Return all registered APIs for the given server (similar to list_apis tool).
    """
    weaver = weavers.get(server_name)
    if not weaver:
        return {}  # empty server
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

@app.post("/admin/{server_name}/test")
async def admin_test(server_name: str, payload: TestPayload):
    name = payload.api_name
    weaver = weavers.get(server_name)
    if not weaver or name not in weaver.apis:
        raise HTTPException(status_code=404, detail=f"API '{name}' not found on server '{server_name}'")
    client = weaver.http_clients.get(name)
    if not client:
        raise HTTPException(status_code=500, detail=f"No HTTP client for '{name}'")
    api_config = weaver.apis[name]
    try:
        resp = await client.head(api_config.base_url, timeout=5.0)
        return {"status": "connected", "status_code": resp.status_code, "headers": dict(resp.headers)}
    except Exception as e:
        return {"status": "failed", "error": str(e)}

@app.get("/admin/{server_name}/schema/{api_name}")
async def admin_schema(server_name: str, api_name: str, endpoint: Optional[str] = None):
    weaver = weavers.get(server_name)
    if not weaver or api_name not in weaver.apis:
        raise HTTPException(status_code=404, detail=f"API '{api_name}' not found on server '{server_name}'")
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
        for server_name, weaver in weavers.items():
            server_cfg = {}
            for name, api in weaver.apis.items():
                try:
                    cfg = api.dict()
                except Exception:
                    cfg = {
                        "name": api.name,
                        "base_url": api.base_url,
                        "description": getattr(api, "description", None),
                        "auth": getattr(api, "auth", None),
                        "headers": getattr(api, "headers", None),
                        "endpoints": [ep.dict() if hasattr(ep, "dict") else ep for ep in api.endpoints]
                    }
                server_cfg[name] = cfg
            to_save[server_name] = server_cfg
        await store.save_server("__all__", {})  # noop ensure file exists (not required)
        # write whole structure
        # Use store._write_file directly under lock to replace full file
        async with store._lock:
            all_existing = await store._read_file()
            # merge with others if present
            all_existing.update(to_save)
            await store._write_file(all_existing)
    except Exception as e:
        print(f"[admin shutdown] failed to save configs: {e}")

# Run admin server standalone:
if __name__ == "__main__":
    uvicorn.run("apiweaver.admin_http:app", host="127.0.0.1", port=9000, reload=False)
