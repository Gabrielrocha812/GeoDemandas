"""
projeto_service.py
------------------
Integração com a API corporativa de projetos.

O cadastro de demandas precisa que o solicitante escolha a qual projeto a
demanda pertence, e essa lista é sempre buscada em
`settings.PROJETOS_API_URL` (header de autenticação `x-token`).

A resposta da API tem o formato:

    [{"cod_projeto": 114626, "cod_projeto_alfa": "1AEGM001"}, ...]

Como a lista muda pouco e a tela de cadastro é aberta com frequência,
guardamos o resultado em cache por `settings.PROJETOS_CACHE_TTL` segundos.
Se a API cair, continuamos servindo o último resultado conhecido (cache
"stale") em vez de bloquear o cadastro — só quando não há nada em cache é
que `ProjetosUnavailableError` é levantado, e aí a tela avisa o usuário.

Usamos `urllib` da biblioteca padrão de propósito: o projeto não depende de
nenhum cliente HTTP externo e as rotas que consomem este módulo são
síncronas (rodam no threadpool do FastAPI).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request

from config import settings

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()
_cached_projetos: list[dict] | None = None
_cached_at: float = 0.0


class ProjetosUnavailableError(RuntimeError):
    """A API de projetos falhou e não há cache anterior para servir."""


def _fetch_projetos() -> list[dict]:
    """Chama a API e devolve os projetos normalizados e ordenados."""
    request = urllib.request.Request(
        settings.PROJETOS_API_URL,
        headers={
            "x-token": settings.PROJETOS_API_TOKEN,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(
        request, timeout=settings.PROJETOS_API_TIMEOUT
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, list):
        raise ValueError("A API de projetos não devolveu uma lista.")

    projetos: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        codigo = item.get("cod_projeto")
        # A API devolve alguns códigos com espaços sobrando (ex.: " 1SVPM003").
        nome = str(item.get("cod_projeto_alfa") or "").strip()
        if codigo is None or not nome:
            continue
        try:
            projetos.append({"cod_projeto": int(codigo), "cod_projeto_alfa": nome})
        except (TypeError, ValueError):
            continue

    projetos.sort(key=lambda projeto: projeto["cod_projeto_alfa"])
    return projetos


def list_projetos(force_refresh: bool = False) -> list[dict]:
    """
    Devolve os projetos ativos, usando cache quando ainda está válido.

    Levanta `ProjetosUnavailableError` apenas se a API falhar e nunca
    tivermos conseguido carregar a lista nesta execução.
    """
    global _cached_projetos, _cached_at

    with _cache_lock:
        is_fresh = (
            _cached_projetos is not None
            and (time.monotonic() - _cached_at) < settings.PROJETOS_CACHE_TTL
        )
        if is_fresh and not force_refresh:
            return list(_cached_projetos)

        try:
            projetos = _fetch_projetos()
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            if _cached_projetos is not None:
                logger.warning(
                    "API de projetos indisponível (%s); usando cache anterior.", exc
                )
                return list(_cached_projetos)
            logger.error("API de projetos indisponível e sem cache: %s", exc)
            raise ProjetosUnavailableError(str(exc)) from exc

        _cached_projetos = projetos
        _cached_at = time.monotonic()
        logger.info("Lista de projetos atualizada (%d projetos).", len(projetos))
        return list(projetos)


def find_projeto(cod_projeto: int | str | None) -> dict | None:
    """Localiza um projeto pelo código numérico; None se não existir."""
    if cod_projeto in (None, ""):
        return None
    try:
        codigo = int(cod_projeto)
    except (TypeError, ValueError):
        return None
    for projeto in list_projetos():
        if projeto["cod_projeto"] == codigo:
            return projeto
    return None
