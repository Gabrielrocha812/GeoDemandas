"""
ldap_auth.py
------------
Integração com o Active Directory via LDAP.

Duas responsabilidades:
  1. `validate_sender(email)`  -> valida se o remetente de um e-mail existe
     e está ATIVO no AD. Retorna um dicionário com os dados do usuário ou None.
  2. `authenticate(email, pwd)` -> autentica um usuário (login na plataforma).

Em DEV_MODE (config), tudo funciona com MOCKS em memória — assim você testa
o fluxo completo sem um servidor AD real. Para produção, defina DEV_MODE=false
e configure as variáveis LDAP_* no .env.

Detalhe importante de segurança do AD:
  - No AD, a conta "ativa" é verificada pelo atributo `userAccountControl`.
    O bit 0x2 (ACCOUNTDISABLE) ligado significa conta DESABILITADA.
"""
from __future__ import annotations

import logging
import re

from config import settings

logger = logging.getLogger("geodemandas.ldap")

# --------------------------------------------------------------------------
# MOCK para desenvolvimento (DEV_MODE=true)
# --------------------------------------------------------------------------
# Estrutura: email -> dados que "viriam" do AD.
# Note que "externo@gmail.com" NÃO está aqui -> será rejeitado.
_MOCK_AD = {
    "joao.silva@brandt.com.br": {
        "full_name": "João Silva",
        "department": "Geotecnia",
        "active": True,
        "password": "senha123",  # apenas para testar authenticate() em dev
    },
    "maria.souza@brandt.com.br": {
        "full_name": "Maria Souza",
        "department": "Meio Ambiente",
        "active": True,
        "password": "senha123",
    },
    "tecnico.ti@brandt.com.br": {
        "full_name": "Carlos Técnico",
        "department": "TI",
        "active": True,
        "password": "senha123",
    },
    "demitido@brandt.com.br": {  # existe mas está inativo -> rejeitar
        "full_name": "Usuário Desligado",
        "department": "RH",
        "active": False,
        "password": "senha123",
    },
}

ACCOUNT_DISABLE_FLAG = 0x2  # bit de conta desabilitada no userAccountControl


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------
def validate_sender(email: str) -> dict | None:
    """
    Verifica se `email` corresponde a um usuário ATIVO no AD.

    Retorna:
        dict com {email, full_name, department, active} se válido e ativo;
        None caso não exista ou esteja desabilitado.
    """
    email = (email or "").strip().lower()
    if not email:
        return None

    if not settings.LDAP_USE_REAL:
        return _validate_sender_mock(email)
    return _validate_sender_ldap(email)


def authenticate(email: str, password: str) -> dict | None:
    """
    Autentica um usuário fazendo bind no AD com as credenciais informadas.
    Retorna os dados do usuário se as credenciais forem válidas, senão None.
    """
    email = (email or "").strip().lower()
    if not settings.LDAP_USE_REAL:
        mock_email = (
            email if "@" in email else f"{email}@{settings.LDAP_DOMAIN}"
        )
        user = _MOCK_AD.get(mock_email)
        if user and user["active"] and user["password"] == password:
            return _to_public(mock_email, user)
        return None
    return _authenticate_ldap(email, password)


def list_active_ldap_users() -> list[dict]:
    """
    Lista usuários ativos do AD com paginação.

    Não atribui permissões: retorna somente identidade, departamento e Hubs.
    """
    from ldap3 import SUBTREE
    from ldap3.core.exceptions import LDAPException

    conn = _get_connection()
    users: list[dict] = []
    try:
        search_filter = (
            "(&(objectCategory=person)(objectClass=user)"
            "(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"
        )
        results = conn.extend.standard.paged_search(
            search_base=settings.LDAP_BASE_DN,
            search_filter=search_filter,
            search_scope=SUBTREE,
            attributes=[
                "sAMAccountName",
                "userPrincipalName",
                "mail",
                "displayName",
                "department",
                "memberOf",
            ],
            paged_size=500,
            generator=True,
        )
        seen_emails: set[str] = set()
        for result in results:
            if result.get("type") != "searchResEntry":
                continue
            attrs = result.get("attributes", {})
            username = str(attrs.get("sAMAccountName") or "").strip().lower()
            if not username or username.endswith("$"):
                continue
            email = str(attrs.get("mail") or "").strip().lower()
            if not email:
                email = f"{username}@{settings.LDAP_DOMAIN.lower()}"
            if email in seen_emails:
                continue
            seen_emails.add(email)
            groups = attrs.get("memberOf") or []
            if isinstance(groups, str):
                groups = [groups]
            users.append(
                {
                    "email": email,
                    "full_name": str(attrs.get("displayName") or username).strip(),
                    "department": str(attrs.get("department") or "").strip() or None,
                    "hubs": _extract_hubs(groups),
                }
            )
        return users
    except LDAPException:
        logger.exception("Falha ao listar usuários ativos do LDAP")
        raise
    finally:
        conn.unbind()


# --------------------------------------------------------------------------
# Implementação MOCK
# --------------------------------------------------------------------------
def _validate_sender_mock(email: str) -> dict | None:
    user = _MOCK_AD.get(email)
    if not user:
        logger.info("[MOCK-LDAP] Remetente não encontrado no AD: %s", email)
        return None
    if not user["active"]:
        logger.info("[MOCK-LDAP] Remetente inativo no AD: %s", email)
        return None
    return _to_public(email, user)


def _to_public(email: str, user: dict) -> dict:
    return {
        "email": email,
        "full_name": user["full_name"],
        "department": user.get("department"),
        "active": user["active"],
    }


# --------------------------------------------------------------------------
# Implementação LDAP real (produção)
# --------------------------------------------------------------------------
def _get_connection():
    """Abre e retorna uma conexão LDAP já com bind do usuário de serviço."""
    from ldap3 import Connection, Server

    server = Server(
        _ldap_host(),
        port=settings.LDAP_PORT,
        use_ssl=settings.LDAP_USE_SSL,
        connect_timeout=10,
    )
    return Connection(
        server,
        user=_bind_user(),
        password=settings.LDAP_BIND_PASSWORD,
        auto_bind=True,
        receive_timeout=10,
        raise_exceptions=True,
    )


def _validate_sender_ldap(email: str) -> dict | None:
    from ldap3 import SUBTREE
    from ldap3.core.exceptions import LDAPException
    from ldap3.utils.conv import escape_filter_chars

    try:
        conn = _get_connection()
    except LDAPException as exc:
        logger.error("Falha ao conectar no AD: %s", exc)
        return None

    try:
        # Alguns usuários da Brandt não possuem o atributo `mail` e usam UPN
        # @brandt.local, embora o endereço corporativo seja @brandt.com.br.
        # Nesses casos, também buscamos pelo sAMAccountName (parte antes do @).
        local_part, _, domain = email.partition("@")
        safe_email = escape_filter_chars(email)
        safe_username = escape_filter_chars(local_part)
        email_filters = ""
        if domain:
            email_filters = (
                f"(mail={safe_email})(userPrincipalName={safe_email})"
                f"(proxyAddresses=SMTP:{safe_email})"
            )
        sam_filter = ""
        if not domain or domain.lower() == settings.LDAP_DOMAIN.lower():
            sam_filter = f"(sAMAccountName={safe_username})"
        search_filter = (
            f"(&(objectClass=user)(|{email_filters}{sam_filter}))"
        )
        attrs = [
            "displayName",
            "department",
            "userAccountControl",
            "mail",
            "userPrincipalName",
            "memberOf",
        ]
        conn.search(
            settings.LDAP_BASE_DN,
            search_filter,
            search_scope=SUBTREE,
            attributes=attrs,
        )

        for entry in conn.entries:
            uac = int(entry.userAccountControl.value or 0)
            is_disabled = bool(uac & ACCOUNT_DISABLE_FLAG)
            if is_disabled:
                logger.info("Remetente inativo no AD: %s", email)
                return None
            return {
                "email": (
                    entry.mail.value
                    or (email if domain else f"{local_part}@{settings.LDAP_DOMAIN}")
                ).lower(),
                "full_name": entry.displayName.value or email,
                "department": entry.department.value or None,
                "hubs": _extract_hubs(entry.memberOf.values),
                "user_principal_name": entry.userPrincipalName.value or email,
                "active": True,
            }

        logger.info("Remetente não encontrado no AD: %s", email)
        return None
    except LDAPException as exc:
        logger.error("Erro na busca LDAP para %s: %s", email, exc)
        return None
    finally:
        conn.unbind()


def _authenticate_ldap(email: str, password: str) -> dict | None:
    from ldap3 import Connection, Server
    from ldap3.core.exceptions import LDAPBindError, LDAPException

    # Primeiro confirma que existe e está ativo; depois tenta o bind com a senha.
    data = _validate_sender_ldap(email)
    if not data:
        return None

    server = Server(
        _ldap_host(),
        port=settings.LDAP_PORT,
        use_ssl=settings.LDAP_USE_SSL,
        connect_timeout=10,
    )
    # Login estilo UPN: usuario@dominio
    upn = data.get("user_principal_name") or (
        email if "@" in email else f"{email}@{settings.LDAP_DOMAIN}"
    )
    conn = Connection(
        server,
        user=upn,
        password=password,
        receive_timeout=10,
        raise_exceptions=True,
    )
    try:
        conn.bind()
        return data
    except LDAPBindError:
        logger.info("Credenciais inválidas para %s", email)
        return None
    except LDAPException as exc:
        logger.error("Erro no bind de autenticação para %s: %s", email, exc)
        return None
    finally:
        conn.unbind()


def _ldap_host() -> str:
    """Retorna somente o hostname quando LDAP_SERVER contém um esquema."""
    server = settings.LDAP_SERVER.strip()
    if "://" in server:
        server = server.split("://", 1)[1]
    return server.split("/", 1)[0].split(":", 1)[0]


def _bind_user() -> str:
    """Compatibiliza o DN antigo do template com a base real do domínio."""
    bind_user = settings.LDAP_BIND_DN.strip()
    old_base = "DC=brandt,DC=com,DC=br"
    if bind_user.lower().endswith(old_base.lower()):
        return bind_user[: -len(old_base)] + settings.LDAP_BASE_DN
    return bind_user


def _extract_hubs(group_dns: list[str]) -> list[str]:
    """Extrai nomes de Hub de grupos AD no formato HUB_<nome>_<recurso>."""
    hubs: set[str] = set()
    for group_dn in group_dns or []:
        cn = group_dn.split(",", 1)[0]
        if not cn.upper().startswith("CN=HUB_"):
            continue
        name = cn[7:]
        name = re.sub(
            r"_(Sharepoint|Gravacao|Gravação)$",
            "",
            name,
            flags=re.IGNORECASE,
        )
        hubs.add(name.replace("_", " ").strip())
    return sorted(hubs, key=str.casefold)
