"""
LangChain agent that uses OpenAI as the LLM and calls APIWeaver admin HTTP endpoints as tools.

Usage:
  - Ensure APIWeaver MCP server and admin HTTP server are running (see docs).
  - Set OPENAI_API_KEY in environment.
  - pip install -r requirements (langchain, openai, httpx)
  - python examples/langchain_agent.py

This script defines several tools that wrap admin HTTP endpoints:
- register_api
- list_apis
- call_api
- get_api_schema
- unregister_api
- test_api_connection

Tools expect a JSON string input describing parameters (see examples below).
"""
import os
import json
from typing import Any, Dict
import httpx

# LangChain imports (version compatibility note below)
from langchain.llms import OpenAI
from langchain.agents import initialize_agent, Tool
from langchain.agents import AgentType

# Config: where admin HTTP server is listening
ADMIN_BASE = os.environ.get("APIWEAVER_ADMIN_URL", "http://127.0.0.1:9000")
DEFAULT_SERVER = os.environ.get("APIWEAVER_DEFAULT_SERVER", "server_alpha")

client = httpx.Client(timeout=30.0)


# ---------- Helper HTTP wrappers ----------
def _post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{ADMIN_BASE}{path}"
    resp = client.post(url, json=payload)
    try:
        return {"status_code": resp.status_code, "body": resp.json()}
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text}


def _get(path: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    url = f"{ADMIN_BASE}{path}"
    resp = client.get(url, params=params)
    try:
        return {"status_code": resp.status_code, "body": resp.json()}
    except Exception:
        return {"status_code": resp.status_code, "text": resp.text}


# ---------- Tool implementations ----------
def tool_register_api(input_str: str) -> str:
    """
    Input: JSON string like:
    {
      "server": "server_alpha",         # optional, default uses DEFAULT_SERVER
      "config": { ... API config ... }  # APIWeaver APIConfig dict
    }
    """
    try:
        payload = json.loads(input_str)
    except Exception as e:
        return f"Invalid JSON input: {e}"

    server = payload.get("server", DEFAULT_SERVER)
    config = payload.get("config")
    if not config:
        return "Missing 'config' in payload"
    res = _post(f"/admin/{server}/register", {"config": config})
    return json.dumps(res, indent=2, ensure_ascii=False)


def tool_list_apis(input_str: str) -> str:
    """
    Input: server name (string) or empty -> uses DEFAULT_SERVER
    Example: "server_alpha"
    """
    server = input_str.strip() or DEFAULT_SERVER
    res = _get(f"/admin/{server}/list")
    return json.dumps(res, indent=2, ensure_ascii=False)


def tool_call_api(input_str: str) -> str:
    """
    Input JSON string:
    {
      "server": "server_alpha",         # optional
      "api_name": "weather",
      "endpoint_name": "get_current_weather",
      "parameters": { "q": "London", "units": "metric" }
    }
    This tool uses admin HTTP call_api route via /admin/{server}/... -> we map to admin test/call endpoints (we defined call via /admin/{server}/call? If not present, use call_api tool endpoint we added earlier)
    """
    try:
        payload = json.loads(input_str)
    except Exception as e:
        return f"Invalid JSON input: {e}"

    server = payload.get("server", DEFAULT_SERVER)
    api_name = payload.get("api_name")
    endpoint_name = payload.get("endpoint_name")
    parameters = payload.get("parameters", {})

    if not api_name or not endpoint_name:
        return "Missing 'api_name' or 'endpoint_name'"

    # Our admin_http doesn't expose a direct /call route; we can call the MCP generic call_api tool
    # via the admin if we had such route; in our admin_http we created call-like capabilities via call_api tool originally;
    # For simplicity, use the admin tool call_api via POST /admin/{server}/call (if you implemented it),
    # Otherwise, use the admin HTTP to contact the API's base directly via the call_api generic route we implemented as MCP tool
    # We'll attempt /admin/{server}/call first, fallback to using the /admin/{server}/test path to show connectivity.
    call_path = f"/admin/{server}/call"
    payload_call = {"api_name": api_name, "endpoint_name": endpoint_name, "parameters": parameters}
    try:
        resp = client.post(ADMIN_BASE + call_path, json=payload_call)
        if resp.status_code == 404:
            # fallback: call the MCP 'call_api' via admin isn't available; return instruction for user
            return json.dumps({"error": "admin call endpoint not found. Please implement /admin/{server}/call or use call_api tool via MCP."}, indent=2, ensure_ascii=False)
        try:
            return json.dumps({"status_code": resp.status_code, "body": resp.json()}, indent=2, ensure_ascii=False)
        except Exception:
            return json.dumps({"status_code": resp.status_code, "text": resp.text}, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"HTTP request failed: {e}"


def tool_get_api_schema(input_str: str) -> str:
    """
    Input JSON or plain text:
    - Plain server name: returns all apis
    - JSON: {"server":"server_alpha", "api_name":"weather", "endpoint":"get_current_weather"}
    """
    try:
        payload = json.loads(input_str)
        server = payload.get("server", DEFAULT_SERVER)
        api_name = payload.get("api_name")
        endpoint = payload.get("endpoint")
    except Exception:
        # treat as plain server or api name
        parts = input_str.strip().split()
        if len(parts) == 0 or parts[0] == "":
            server = DEFAULT_SERVER
            api_name = None
            endpoint = None
        elif len(parts) == 1:
            server = parts[0]
            api_name = None
            endpoint = None
        else:
            server = parts[0]
            api_name = parts[1]
            endpoint = parts[2] if len(parts) > 2 else None

    if api_name:
        path = f"/admin/{server}/schema/{api_name}"
        params = {"endpoint": endpoint} if endpoint else None
    else:
        # return list for server
        path = f"/admin/{server}/list"
        params = None

    res = _get(path, params=params)
    return json.dumps(res, indent=2, ensure_ascii=False)


def tool_unregister_api(input_str: str) -> str:
    """
    Input JSON: {"server":"server_alpha", "api_name":"weather"}
    """
    try:
        payload = json.loads(input_str)
    except Exception as e:
        return f"Invalid JSON input: {e}"
    server = payload.get("server", DEFAULT_SERVER)
    api_name = payload.get("api_name")
    if not api_name:
        return "Missing 'api_name'"
    res = _post(f"/admin/{server}/unregister", {"api_name": api_name})
    return json.dumps(res, indent=2, ensure_ascii=False)


def tool_test_api_connection(input_str: str) -> str:
    """
    Input JSON: {"server":"server_alpha", "api_name":"weather"}
    """
    try:
        payload = json.loads(input_str)
    except Exception as e:
        return f"Invalid JSON input: {e}"
    server = payload.get("server", DEFAULT_SERVER)
    api_name = payload.get("api_name")
    if not api_name:
        return "Missing 'api_name'"
    res = _post(f"/admin/{server}/test", {"api_name": api_name})
    return json.dumps(res, indent=2, ensure_ascii=False)


# ---------- Build LangChain tools ----------
tools = [
    Tool(name="register_api", func=tool_register_api, description="Register an API. Input is JSON with 'server' and 'config'."),
    Tool(name="list_apis", func=tool_list_apis, description="List APIs registered on a server. Input is server name or empty."),
    Tool(name="call_api", func=tool_call_api, description="Call a registered API endpoint. Input JSON with server, api_name, endpoint_name, parameters."),
    Tool(name="get_api_schema", func=tool_get_api_schema, description="Get schema for an API or list APIs. Input JSON or plain text."),
    Tool(name="unregister_api", func=tool_unregister_api, description="Unregister an API. Input JSON with server and api_name."),
    Tool(name="test_api_connection", func=tool_test_api_connection, description="Test connectivity for a registered API. Input JSON with server and api_name."),
]

# ---------- Create LLM and Agent ----------
def build_agent():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError("OPENAI_API_KEY not set in environment")
    llm = OpenAI(temperature=0)
    agent = initialize_agent(
        tools,
        llm,
        agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
        verbose=True
    )
    return agent


# ---------- Example usage ----------
def example_register_and_call():
    agent = build_agent()

    # 1) Register an API (example payload)
    register_payload = {
        "server": "server_alpha",
        "config": {
            "name": "weather",
            "base_url": "https://api.openweathermap.org/data/2.5",
            "description": "OpenWeatherMap API",
            "auth": {
                "type": "api_key",
                "api_key": "YOUR_API_KEY",
                "api_key_param": "appid"
            },
            "headers": {"Accept": "application/json"},
            "endpoints": [
                {
                    "name": "get_current_weather",
                    "description": "Get current weather for a city",
                    "method": "GET",
                    "path": "/weather",
                    "params": [
                        {"name": "q", "type": "string", "location": "query", "required": True}
                    ]
                }
            ]
        }
    }
    print("=== Register API ===")
    print(agent.run(f"register_api: {json.dumps(register_payload)}"))

    # 2) List apis
    print("=== List APIs ===")
    print(agent.run("list_apis: server_alpha"))

    # 3) Call the endpoint (if your admin supports /admin/{server}/call)
    call_payload = {
        "server": "server_alpha",
        "api_name": "weather",
        "endpoint_name": "get_current_weather",
        "parameters": {"q": "London"}
    }
    print("=== Call API ===")
    print(agent.run(f"call_api: {json.dumps(call_payload)}"))


if __name__ == "__main__":
    # Run example
    example_register_and_call()
