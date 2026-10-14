import asyncpg
from asyncpg.pool import PoolConnectionProxy

# Conexão avulsa (testes, pipeline) ou emprestada do pool (API)
Conexao = asyncpg.Connection | PoolConnectionProxy
