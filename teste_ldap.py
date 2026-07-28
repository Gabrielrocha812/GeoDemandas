r"""
Teste de conexão com o Active Directory da Brandt.

Uso:
    .\.venv\Scripts\python.exe teste_ldap.py
    .\.venv\Scripts\python.exe teste_ldap.py --email usuario@brandt.com.br

As configurações são lidas do arquivo .env. A senha nunca é exibida.
"""
from __future__ import annotations

import argparse
import sys

from ldap3 import SUBTREE
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from config import settings
from ldap_auth import _get_connection


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Testa a conexão LDAP/AD.")
    parser.add_argument(
        "--email",
        help="E-mail opcional para confirmar que um usuário existe no AD.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("=== Teste LDAP GeoDemandas ===")
    print(f"Servidor: {settings.LDAP_SERVER}:{settings.LDAP_PORT}")
    print(f"SSL: {'sim' if settings.LDAP_USE_SSL else 'não'}")
    print(f"Base DN: {settings.LDAP_BASE_DN}")
    print(f"Usuário de serviço configurado: {'sim' if settings.LDAP_BIND_DN else 'não'}")
    print(f"Senha configurada: {'sim' if settings.LDAP_BIND_PASSWORD else 'não'}")

    if not settings.LDAP_BIND_DN or not settings.LDAP_BIND_PASSWORD:
        print("\nFALHA: configure LDAP_BIND_DN e LDAP_BIND_PASSWORD no .env.")
        return 2

    conn = None
    try:
        print("\n1. Conectando e autenticando...")
        conn = _get_connection()
        print("OK: conexão e bind realizados.")

        print("\n2. Consultando a base do domínio...")
        found_domain = conn.search(
            settings.LDAP_BASE_DN,
            "(objectClass=domain)",
            search_scope=SUBTREE,
            attributes=["distinguishedName"],
        )
        if not found_domain or not conn.entries:
            print("FALHA: bind funcionou, mas a base DN não retornou o domínio.")
            return 3
        print("OK: base DN encontrada e consulta autorizada.")

        if args.email:
            print(f"\n3. Procurando usuário: {args.email}")
            safe_email = escape_filter_chars(args.email.strip().lower())
            local_part = escape_filter_chars(
                args.email.strip().lower().partition("@")[0]
            )
            found_user = conn.search(
                settings.LDAP_BASE_DN,
                (
                    "(&(objectClass=user)"
                    f"(|(mail={safe_email})(userPrincipalName={safe_email})"
                    f"(proxyAddresses=SMTP:{safe_email})"
                    f"(sAMAccountName={local_part})))"
                ),
                search_scope=SUBTREE,
                attributes=[
                    "displayName",
                    "userAccountControl",
                    "userPrincipalName",
                ],
            )
            if not found_user or not conn.entries:
                print("AVISO: usuário não encontrado com esse e-mail.")
                return 4

            entry = conn.entries[0]
            disabled = bool(int(entry.userAccountControl.value or 0) & 0x2)
            print(f"OK: usuário encontrado ({'inativo' if disabled else 'ativo'}).")
            print(f"UPN do AD: {entry.userPrincipalName.value or 'não informado'}")

        print("\nSUCESSO: a integração LDAP está operacional.")
        return 0

    except LDAPException as exc:
        print("\nFALHA LDAP:")
        print(f"{type(exc).__name__}: {exc}")
        return 1
    except Exception as exc:
        print("\nFALHA INESPERADA:")
        print(f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if conn is not None:
            conn.unbind()


if __name__ == "__main__":
    sys.exit(main())
