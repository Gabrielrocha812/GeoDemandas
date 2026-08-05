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
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from auth import get_optional_user
from config import settings
from database import init_db
from email_worker import email_worker_loop, stop_worker
from outbox_service import (
    notification_outbox_worker_loop,
    stop_notification_outbox_worker,
)
from routes import api, management, operations, web
from routes.web import templates
from sla_monitor import sla_alert_worker_loop, stop_sla_alert_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("geodemandas")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    logger.info("Inicializando GeoDemandas Brandt...")
    init_db()
    worker_tasks = [
        asyncio.create_task(email_worker_loop()),
        asyncio.create_task(notification_outbox_worker_loop()),
        asyncio.create_task(sla_alert_worker_loop()),
    ]
    yield
    # --- shutdown ---
    logger.info("Encerrando workers de e-mail, notificações e SLA...")
    stop_worker()
    stop_notification_outbox_worker()
    stop_sla_alert_worker()
    for worker_task in worker_tasks:
        worker_task.cancel()
    await asyncio.gather(*worker_tasks, return_exceptions=True)


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Sessão assinada (cookie) para autenticação da plataforma web.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    max_age=60 * 60 * 8,
    same_site="lax",
    https_only=settings.APP_BASE_URL.lower().startswith("https://"),
)

# Rotas
app.include_router(web.router)
app.include_router(api.router)
app.include_router(operations.router)
app.include_router(management.router)


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
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "dev_mode": settings.DEV_MODE,
        "ldap_real": settings.LDAP_USE_REAL,
        "smtp_enabled": settings.SMTP_ENABLED,
        "email_provider": email_provider,
        "email_enabled": email_enabled,
    }
