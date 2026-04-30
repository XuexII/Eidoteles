
from .agent import AgentConfig
from .channels import HttpConfig
from dataclasses import dataclass
from llm.config import LlmConfig
from pathlib import Path
import tomllib

# Agent的主要配置
@dataclass
class Config:
    owner_id: str
    llm: LlmConfig


    @classmethod
    async def from_env_with_toml(cls, toml_path: Path):

        toml_path = Path(toml_path)
        with toml_path.open("rb") as f:
            data = tomllib.load(f)

        return cls(**data)