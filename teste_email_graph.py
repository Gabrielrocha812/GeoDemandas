r"""Valida autenticação e envio de e-mail pelo Microsoft Graph.

Uso:
    .\.venv\Scripts\python.exe teste_email_graph.py
    .\.venv\Scripts\python.exe teste_email_graph.py --to usuario@brandt.com.br
"""
from __future__ import annotations

import argparse
import sys

from config import settings
from notification_service import _graph_access_token, send_ticket_update


def main() -> int:
    parser = argparse.ArgumentParser(description="Testa o Microsoft Graph.")
    parser.add_argument("--to", help="Destinatário de um e-mail real de teste.")
    args = parser.parse_args()

    print("=== Teste Microsoft Graph GeoDemandas ===")
    print(f"Tenant configurado: {'sim' if settings.GRAPH_TENANT_ID else 'não'}")
    print(f"Client ID configurado: {'sim' if settings.GRAPH_CLIENT_ID else 'não'}")
    print(f"Client secret configurado: {'sim' if settings.GRAPH_CLIENT_SECRET else 'não'}")
    print(f"Remetente: {settings.GRAPH_SENDER_EMAIL}")

    if not settings.GRAPH_CLIENT_SECRET:
        print("\nFALHA: preencha GRAPH_CLIENT_SECRET diretamente no arquivo .env.")
        return 2

    try:
        _graph_access_token()
        print("\nOK: token de aplicação obtido no Microsoft Entra ID.")
    except Exception as exc:
        print(f"\nFALHA AO OBTER TOKEN: {type(exc).__name__}: {exc}")
        return 1

    if args.to:
        sent = send_ticket_update(
            args.to,
            "Usuário de teste",
            0,
            "Validação de envio do GeoDemandas",
            "Este é um e-mail de teste enviado pelo Microsoft Graph.",
        )
        if not sent:
            print("FALHA: o token funcionou, mas o Graph recusou o envio.")
            print("Confirme Mail.Send (Application), consentimento e a caixa remetente.")
            return 1
        print(f"OK: e-mail de teste aceito para envio a {args.to}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
