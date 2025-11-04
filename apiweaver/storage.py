"""
JSON-backed store that supports multiple APIWeaver servers.

Data layout (example):
{
  "server1": {
    "weather": { ... api config ... },
    "github": { ... api config ... }
  },
  "server2": {
    "internal": { ... }
  }
}

This module provides async-safe read/write helpers and helpers scoped by server name.
"""
from typing import Any, Dict, List
import json
from pathlib import Path
import asyncio

class JsonStore:
    def __init__(self, path: str = "apis.json"):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        # Ensure file exists and is valid
        if not self.path.exists():
            self.path.write_text(json.dumps({}, ensure_ascii=False, indent=2))

    async def _read_file(self) -> Dict[str, Any]:
        def _read():
            with self.path.open("r", encoding="utf-8") as f:
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    return {}
        return await asyncio.to_thread(_read)

    async def _write_file(self, data: Dict[str, Any]):
        def _write():
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(self.path)
        await asyncio.to_thread(_write)

    async def load_all_servers(self) -> Dict[str, Dict[str, Any]]:
        """Return the full mapping: server_name -> (api_name -> config)."""
        async with self._lock:
            data = await self._read_file()
            # Ensure top-level is dict
            if not isinstance(data, dict):
                data = {}
            return data

    async def load_server(self, server_name: str) -> Dict[str, Any]:
        """Return all apis config for a specific server (may be empty)."""
        async with self._lock:
            data = await self._read_file()
            return data.get(server_name, {})

    async def save_server(self, server_name: str, apis: Dict[str, Any]):
        """Overwrite the apis for a specific server."""
        async with self._lock:
            data = await self._read_file()
            data[server_name] = apis
            await self._write_file(data)

    async def add_api(self, server_name: str, api_name: str, config: Dict[str, Any]):
        """Add or replace an API config under server_name."""
        async with self._lock:
            data = await self._read_file()
            server = data.get(server_name, {})
            server[api_name] = config
            data[server_name] = server
            await self._write_file(data)

    async def remove_api(self, server_name: str, api_name: str):
        async with self._lock:
            data = await self._read_file()
            server = data.get(server_name, {})
            if api_name in server:
                del server[api_name]
                data[server_name] = server
                await self._write_file(data)

    async def list_servers(self) -> List[str]:
        async with self._lock:
            data = await self._read_file()
            return list(data.keys())

    async def list_names(self, server_name: str) -> List[str]:
        async with self._lock:
            data = await self._read_file()
            server = data.get(server_name, {})
            return list(server.keys())
