"""
main.py
-------
Ponto de entrada do GeoDemandas Brandt.

- Inicializa o banco (cria tabelas e semeia usuários em DEV_MODE).
- Sobe os workers de ingestao, outbox e alertas de SLA via lifespan.
- Registra os routers web, API e operacoes e a SessionMiddleware.

Como rodar localmente:
    1) python -m venv .venv && .venv\\Scripts\\activate   (Windows)
    2) pip install -r requirements.txt
    3) copy .env.example .env      (e ajuste se quiser)
    4) uvicorn main:app --reload
    5) Acesse http://localhost:8000  ->  login: tecnico.ti@brandt.com.br / senha123

Em DEV_MODE o worker injeta 3 e-mails fictícios ~3s após subir; dois viram
tickets e um (remetente externo) é rejeitado pela validação no AD.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from auth import get_optional_user
from config import settings
from database import SessionLocal, init_db
from sqlalchemy import text
from operational_health import worker_status
from email_worker import email_worker_loop, stop_worker
from outbox_service import (
    notification_outbox_worker_loop,
    stop_notification_outbox_worker,
)
from routes import api, features, management, operations, web
from routes.web import templates
from sla_monitor import sla_alert_worker_loop, stop_sla_alert_worker
from automation_worker import automation_worker_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("geodemandas")


def validate_production_settings() -> None:
    if settings.DEV_MODE:
        return
    errors = []
    if settings.SECRET_KEY == "dev-secret-key" or len(settings.SECRET_KEY) < 32:
        errors.append("SECRET_KEY deve ter pelo menos 32 caracteres")
    if not settings.APP_BASE_URL.lower().startswith("https://"):
        errors.append("APP_BASE_URL deve usar HTTPS")
    if not settings.LDAP_USE_REAL:
        errors.append("LDAP_USE_REAL deve ser true")
    if settings.DATABASE_URL.startswith("sqlite"):
        errors.append("DATABASE_URL deve apontar para PostgreSQL")
    if settings.EMBEDDED_WORKERS:
        errors.append("EMBEDDED_WORKERS deve ser false")
    if not settings.REQUIRE_ANTIVIRUS:
        errors.append("REQUIRE_ANTIVIRUS deve ser true")
    if errors:
        raise RuntimeError("Configuracao de producao invalida: " + "; ".join(errors))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    logger.info("Inicializando GeoDemandas Brandt...")
    validate_production_settings()
    init_db()
    worker_tasks = []
    if settings.EMBEDDED_WORKERS:
        worker_tasks = [asyncio.create_task(email_worker_loop()), asyncio.create_task(notification_outbox_worker_loop()), asyncio.create_task(sla_alert_worker_loop()), asyncio.create_task(automation_worker_loop())]
    yield
    # --- shutdown ---
    logger.info("Encerrando workers de e-mail, notificações e SLA...")
    if worker_tasks:
        stop_worker()
        stop_notification_outbox_worker()
        stop_sla_alert_worker()
    for worker_task in worker_tasks:
        worker_task.cancel()
    await asyncio.gather(*worker_tasks, return_exceptions=True)


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

allowed_hosts = [item.strip() for item in settings.ALLOWED_HOSTS.split(",") if item.strip()]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["localhost"])

# Sessão assinada (cookie) para autenticação da plataforma web.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    max_age=60 * 60 * 8,
    same_site="lax",
    https_only=settings.APP_BASE_URL.lower().startswith("https://"),
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if settings.FORCE_HTTPS and request.url.scheme != "https":
        return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
    if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        origin = request.headers.get("origin")
        if origin:
            expected = urlparse(settings.APP_BASE_URL)
            supplied = urlparse(origin)
            if (supplied.scheme, supplied.netloc) != (expected.scheme, expected.netloc):
                return JSONResponse({"detail": "Origem da requisicao nao autorizada"}, status_code=403)
        elif request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Requisicao entre sites bloqueada"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", settings.CONTENT_SECURITY_POLICY)
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if settings.APP_BASE_URL.lower().startswith("https://"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

# Rotas
app.include_router(web.router)
app.include_router(api.router)
app.include_router(operations.router)
app.include_router(management.router)
app.include_router(features.router)


# --------------------------------------------------------------------------
# Tratamento de erros
# --------------------------------------------------------------------------
# Sem isso, um navegador que abre "/" sem sessão recebe o JSON cru
# {"detail": "Não autenticado"} em vez da tela de login, e uma URL inexistente
# devolve JSON em vez do 404.html. As rotas /api continuam respondendo JSON.
def _wants_html(request: Request) -> bool:
    if request.url.path.startswith("/api"):
        return False
    return "text/html" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def html_aware_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if _wants_html(request):
        if exc.status_code == 401:
            return RedirectResponse(url="/login", status_code=303)
        if exc.status_code == 404:
            return templates.TemplateResponse(
                request,
                "404.html",
                {
                    "current_user": get_optional_user(request),
                    "title": "Página não encontrada",
                    "message": "O endereço que você acessou não existe neste sistema.",
                },
                status_code=404,
            )
    return await http_exception_handler(request, exc)


@app.get("/health")
def health():
    email_provider = settings.EMAIL_PROVIDER.strip().lower()
    email_enabled = (
        bool(settings.GRAPH_CLIENT_SECRET)
        if email_provider == "graph"
        else settings.SMTP_ENABLED
    )
    checks = {}
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        checks["database"] = {"healthy": True}
    except Exception:
        logger.exception("Falha no health check do banco")
        checks["database"] = {"healthy": False}
    finally:
        if "db" in locals():
            db.close()
    upload_root = Path(settings.UPLOAD_DIR)
    upload_probe = upload_root if upload_root.exists() else upload_root.parent
    checks["uploads"] = {
        "healthy": upload_probe.exists() and os.access(upload_probe, os.W_OK),
        "configured": str(upload_root),
    }
    checks["workers"] = worker_status(
        max_age_seconds=settings.WORKER_HEARTBEAT_MAX_AGE_SECONDS
    )
    workers_healthy = all(item["healthy"] for item in checks["workers"].values())
    healthy = checks["database"]["healthy"] and checks["uploads"]["healthy"] and workers_healthy
    payload = {
        "status": "ok" if healthy else "degraded",
        "app": settings.APP_NAME,
        "dev_mode": settings.DEV_MODE,
        "ldap_real": settings.LDAP_USE_REAL,
        "smtp_enabled": settings.SMTP_ENABLED,
        "email_provider": email_provider,
        "email_enabled": email_enabled,
        "checks": checks,
    }
    return JSONResponse(payload, status_code=200 if healthy else 503)


@app.get("/health/live")
def liveness():
    return {"status": "ok"}
