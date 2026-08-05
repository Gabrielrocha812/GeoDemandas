"""
auth.py
-------
Autenticação da plataforma web (login dos técnicos/colaboradores).

- O login usa `ldap_auth.authenticate()` (mock em DEV_MODE).
- A sessão é guardada num cookie assinado (Starlette SessionMiddleware).
- `get_current_user` é a dependency que protege as rotas: além de checar a
  sessão, confirma que o usuário AINDA existe e está ATIVO no banco local
  (sincronizado do AD). Isso atende ao requisito de segurança de que apenas
  usuários do AD/LDAP presentes na base podem interagir com chamados.

Credenciais de teste (DEV_MODE), senha "senha123":
    tecnico.ti@brandt.com.br   (técnico)
    joao.silva@brandt.com.br   (colaborador)
    maria.souza@brandt.com.br  (colaborador)
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from database import SessionLocal, User, get_db

ROLE_ADMIN = "administrador"
ROLE_TECHNICIAN = "tecnico"
ROLE_REQUESTER = "solicitante"
VALID_ROLES = {ROLE_ADMIN, ROLE_TECHNICIAN, ROLE_REQUESTER}


def login_user(request: Request, user: User) -> None:
    """Grava a identidade do usuário na sessão."""
    request.session["user_id"] = user.id
    request.session["user_email"] = user.email


def logout_user(request: Request) -> None:
    request.session.clear()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """
    Dependency que retorna o usuário logado.
    Levanta 401 se não houver sessão válida ou o usuário não estiver ativo.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Não autenticado",
            headers={"Location": "/login"},
        )
    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if not user:
        # Usuário removido/desativado no AD -> derruba a sessão.
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuário inválido ou inativo",
            headers={"Location": "/login"},
        )
    if user.role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Perfil de acesso inválido. Procure um administrador.",
        )
    return user


def get_optional_user(request: Request) -> User | None:
    """
    Versão não-obrigatória de `get_current_user`: devolve o usuário logado ou
    None, sem levantar 401. Usada nas páginas de erro (ex.: 404), que precisam
    renderizar a sidebar do layout mesmo fora do fluxo normal de dependências.
    """
    if "session" not in request.scope:
        return None
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    finally:
        db.close()


def require_roles(*allowed_roles: str):
    """Dependency que restringe uma rota aos papéis informados."""
    def dependency(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Você não tem permissão para realizar esta ação.",
            )
        return current_user

    return dependency
