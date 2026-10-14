"""Configuração validada a partir do ambiente. Segredos nunca têm valor padrão."""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ChavesHMAC(BaseSettings):
    """Pepper do HMAC do documento (pseudonimização, ADR-005), versionado para permitir rotação.

    Ao rotacionar: a chave atual vira uma entrada de `hmac_peppers_anteriores` (JSON {"1": "..."}) e
    `hmac_key_version` sobe. Novas análises usam a atual; buscas do titular calculam o hash com todas.
    """

    model_config = SettingsConfigDict(env_prefix="AGROCREDIT_", extra="ignore")

    hmac_pepper: SecretStr = Field(min_length=32)
    hmac_key_version: int = 1
    hmac_peppers_anteriores: dict[int, SecretStr] = Field(default_factory=dict)

    def todas(self) -> dict[int, bytes]:
        chaves = {v: p.get_secret_value().encode() for v, p in self.hmac_peppers_anteriores.items()}
        if self.hmac_key_version in chaves:
            raise ValueError("hmac_key_version atual repetida em hmac_peppers_anteriores")
        chaves[self.hmac_key_version] = self.hmac_pepper.get_secret_value().encode()
        return chaves


class Settings(ChavesHMAC):
    database_url: SecretStr
    pool_min_size: int = 5
    pool_max_size: int = 20
    statement_timeout_ms: int = 3000

    # JWT assimétrico (EdDSA/RS256): chaves públicas em JWKS (JSON) e claims obrigatórios
    # JWKS: JSON fixo (jwt_jwks) ou URL do provedor de identidade (jwt_jwks_url), com cache e
    # recarga quando chega um `kid` desconhecido (rotação de chaves sem reiniciar a API)
    jwt_jwks: str | None = None
    jwt_jwks_arquivo: str | None = None  # JWKS montado como arquivo (segredo do orquestrador)
    jwt_jwks_url: str | None = None
    jwt_issuer: str
    jwt_audience: str
    jwt_tenant_claim: str = "tenant_id"

    # /metrics: se definido, exige "Authorization: Bearer <token>" (scraper do Prometheus)
    metrics_token: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # campos obrigatórios vêm do ambiente
