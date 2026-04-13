import asyncpg
from asyncpg.pool import Pool


# 执行一个记忆命令（PostgreSQL 后端）。

async def run_memory_command(
        cmd: MemoryCommand,
        pool,
        embeddings
):
    pass
