from dataclasses import dataclass
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Union
from pathlib import Path


@dataclass
class WasmToolLoader:
    runtime: WasmToolRuntime
    registry: ToolRegistry
    secrets_store: Optional[SecretsStore]


    async def load_from_dir(self, dir: Path) -> Union[LoadResults, WasmLoadError]:
        """
        没有能力文件的工具将没有任何权限（默认拒绝）。
        """
        pass
