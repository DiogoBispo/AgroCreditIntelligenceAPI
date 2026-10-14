"""Camada Bronze: download imutável com proveniência (URL, data, tamanho, SHA-256)."""

import hashlib
import json
import shutil
import ssl
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# Alguns servidores públicos (PAMGIA, FUNAI) recusam o User-Agent padrão do urllib
USER_AGENT = "Mozilla/5.0 (compatible; agrocredit-pipeline/0.1)"
TAMANHO_MAXIMO = 4 * 1024**3  # 4 GiB por arquivo: proteção contra resposta anômala
_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ArquivoBaixado:
    url: str
    arquivo: str  # relativo ao diretório da versão
    sha256: str
    tamanho_bytes: int
    baixado_em: str  # ISO-8601 UTC


@dataclass
class Manifesto:
    fonte: str
    versao: str
    arquivos: list[ArquivoBaixado] = field(default_factory=list)
    source_as_of: str | None = None  # preenchido na transformação, quando a data vem do conteúdo
    parametros: dict[str, Any] = field(default_factory=dict)  # ex.: envelopes do escopo usados no download

    def salvar(self, diretorio: Path) -> None:
        (diretorio / "manifest.json").write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))

    @classmethod
    def carregar(cls, diretorio: Path) -> "Manifesto":
        raw = json.loads((diretorio / "manifest.json").read_text())
        arquivos = [ArquivoBaixado(**a) for a in raw.pop("arquivos")]
        return cls(arquivos=arquivos, **raw)

    @property
    def as_of(self) -> date:
        if self.source_as_of:
            return date.fromisoformat(self.source_as_of)
        return min(datetime.fromisoformat(a.baixado_em).date() for a in self.arquivos)


def baixar(
    url: str,
    destino: Path,
    *,
    ssl_context: ssl.SSLContext | None = None,
    auth_header: str | None = None,
    timeout: int = 600,
) -> ArquivoBaixado:
    """Baixa em streaming para `destino`, calculando o SHA-256. Não sobrescreve arquivo existente."""
    if destino.exists():
        raise FileExistsError(f"Bronze é imutável: {destino} já existe")
    if not url.startswith("https://"):
        raise ValueError(f"Somente HTTPS: {url}")
    destino.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    if auth_header:
        headers["Authorization"] = auth_header
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 (esquema validado acima)
    hasher = hashlib.sha256()
    total = 0
    parcial = destino.with_suffix(destino.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp, parcial.open("wb") as f:  # noqa: S310  # nosec B310
        while chunk := resp.read(_CHUNK):
            total += len(chunk)
            if total > TAMANHO_MAXIMO:
                f.close()
                parcial.unlink()
                raise ValueError(f"Arquivo excede {TAMANHO_MAXIMO} bytes: {url}")
            hasher.update(chunk)
            f.write(chunk)
    shutil.move(parcial, destino)
    return ArquivoBaixado(
        url=url,
        arquivo=destino.name,
        sha256=hasher.hexdigest(),
        tamanho_bytes=total,
        baixado_em=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def registrar_manual(origem: Path, destino: Path, descricao: str) -> ArquivoBaixado:
    """Arquivo obtido fora do pipeline (ex.: link que exige login, resposta a pedido via LAI)."""
    if destino.exists():
        raise FileExistsError(f"Bronze é imutável: {destino} já existe")
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(origem, destino)
    dados = destino.read_bytes()
    return ArquivoBaixado(
        url=f"manual:{descricao}",
        arquivo=destino.name,
        sha256=hashlib.sha256(dados).hexdigest(),
        tamanho_bytes=len(dados),
        baixado_em=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def nova_versao() -> str:
    """Versão = timestamp UTC (14 dígitos), usada no nome da tabela publicada."""
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S")
