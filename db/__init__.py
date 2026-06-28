import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class DatabaseHandles:
    """
    数据库连接后保留的后端特定句柄。

    这些是卫星存储（例如 `SecretsStore`）所需要的，它们需要
    后端特定的句柄而不是通用的 `Database`。
    """
    # PostgreSQL 连接池（如果启用了 postgres 特性）
    pg_pool: Optional[Any] = None  # deadpool_postgres.Pool
    # libSQL 数据库实例（如果启用了 libsql 特性）
    libsql_db: Optional[Any] = None  # libsql.Database


async def connect_from_config(config: DatabaseConfig) -> Database:
    """
    从配置创建数据库后端，运行迁移，并返回它。

    这是 CLI 命令和其他需要简单 `Database` 而不保留后端特定句柄
    （例如，用于密钥存储的 `pg_pool` 或 `libsql_conn`）的调用点的共享辅助函数。
    `main.rs` 中的主代理启动使用自己的初始化块，因为它还捕获
    这些后端特定的句柄。
    """
    db, _handles = await connect_with_handles(config)
    return db


async def connect_with_handles(
    config: DatabaseConfig,
) -> Tuple[Database, DatabaseHandles]:
    """
    连接到数据库，运行迁移，并返回通用的 `Database` trait 对象
    和后端特定的句柄。
    """
    handles = DatabaseHandles()
    backend = config.backend

    if backend == DatabaseBackend.LibSql:
        default_path = default_libsql_path()
        db_path = config.libsql_path if config.libsql_path is not None else default_path

        if config.libsql_url is not None:
            token = config.libsql_auth_token
            if token is None:
                raise DatabaseError(
                    "设置 LIBSQL_URL 时需要 LIBSQL_AUTH_TOKEN"
                )
            backend_impl = await LibSqlBackend.new_remote_replica(
                db_path, config.libsql_url, token
            )
        else:
            backend_impl = await LibSqlBackend.new_local(db_path)

        await backend_impl.run_migrations()
        logger.debug("libSQL 数据库已连接并应用了迁移")

        handles.libsql_db = backend_impl.shared_db()
        return backend_impl, handles

    elif backend == DatabaseBackend.Postgres:
        pg = await PgBackend.new(config)
        await pg.run_migrations()
        logger.info("PostgreSQL 数据库已连接并应用了迁移")

        handles.pg_pool = pg.pool()
        return pg, handles

    else:
        raise DatabaseError(
            f"数据库后端 '{backend}' 不可用。请使用适当的功能标志重新构建。"
        )