"""
Simple JSON-backed store for API configurations.

Provides async-safe read/write helpers (uses asyncio.to_thread for file IO).
"""
from typing import Any, Dict, List
import json
from pathlib import Path
import asyncio

class JsonStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        # Ensure file exists
        if not self.path.exists():
            self.path.write_text(json.dumps({}))

    async def _read_file(self) -> Dict[str, Any]:
        def _read():
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        return await asyncio.to_thread(_read)

    async def _write_file(self, data: Dict[str, Any]):
        def _write():
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(self.path)
        await asyncio.to_thread(_write)

    async def load_all(self) -> Dict[str, Any]:
        async with self._lock:
            return await self._read_file()

    async def save_all(self, data: Dict[str, Any]):
        async with self._lock:
            await self._write_file(data)

    async def add_api(self, name: str, config: Dict[str, Any]):
        async with self._lock:
            data = await self._read_file()
            data[name] = config
            await self._write_file(data)

    async def remove_api(self, name: str):
        async with self._lock:
            data = await self._read_file()
            if name in data:
                del data[name]
                await self._write_file(data)

    async def list_names(self) -> List[str]:
        async with self._lock:
            data = await self._read_file()
            return list(data.keys())
