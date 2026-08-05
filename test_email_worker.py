"""Testes de confiabilidade e idempotência da ingestão IMAP."""
from __future__ import annotations

import sys
import unittest
from email.message import EmailMessage
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, call, patch

# Garante que importar os modelos não abra nem aponte para o banco da aplicação.
with patch.dict("os.environ", {"DATABASE_URL": "sqlite:///:memory:"}):
    import email_worker
    import ldap_auth
    from ldap_auth import LDAPOperationalError


class _FakeLDAPException(Exception):
    pass


def _fake_ldap_modules() -> dict[str, ModuleType]:
    ldap3 = ModuleType("ldap3")
    ldap3.SUBTREE = object()

    core = ModuleType("ldap3.core")
    exceptions = ModuleType("ldap3.core.exceptions")
    exceptions.LDAPException = _FakeLDAPException
    core.exceptions = exceptions

    utils = ModuleType("ldap3.utils")
    conv = ModuleType("ldap3.utils.conv")
    conv.escape_filter_chars = lambda value: value
    utils.conv = conv

    ldap3.core = core
    ldap3.utils = utils
    return {
        "ldap3": ldap3,
        "ldap3.core": core,
        "ldap3.core.exceptions": exceptions,
        "ldap3.utils": utils,
        "ldap3.utils.conv": conv,
    }


class EmailWorkerResultTests(unittest.TestCase):
    def test_message_without_id_receives_a_stable_fingerprint(self) -> None:
        msg = EmailMessage()
        msg["From"] = "usuario@brandt.com.br"
        msg["Subject"] = "Sem identificador"
        msg.set_content("Conteúdo estável")

        with patch.object(
            email_worker,
            "_create_ticket_from_email",
            return_value=email_worker.EmailProcessingResult.SUCCESS,
        ) as create_ticket:
            first_result = email_worker._handle_message(msg)
            first_id = create_ticket.call_args.args[3]
            create_ticket.reset_mock()
            second_result = email_worker._handle_message(msg)
            second_id = create_ticket.call_args.args[3]

        self.assertEqual(first_result, email_worker.EmailProcessingResult.SUCCESS)
        self.assertEqual(second_result, email_worker.EmailProcessingResult.SUCCESS)
        self.assertEqual(first_id, second_id)
        self.assertTrue(first_id.startswith("<sha256-"))

    def test_invalid_sender_is_a_permanent_rejection(self) -> None:
        with (
            patch.object(email_worker, "validate_sender", return_value=None),
            patch.object(email_worker, "SessionLocal") as session_factory,
        ):
            result = email_worker._create_ticket_from_email(
                "externo@example.com",
                "Assunto",
                "Corpo",
                "<invalid@example.com>",
            )

        self.assertIs(
            result,
            email_worker.EmailProcessingResult.PERMANENT_REJECTION,
        )
        session_factory.assert_not_called()

    def test_ldap_outage_is_a_transient_failure(self) -> None:
        with (
            patch.object(
                email_worker,
                "validate_sender",
                side_effect=LDAPOperationalError("indisponível"),
            ),
            patch.object(email_worker, "SessionLocal") as session_factory,
            patch.object(email_worker, "logger"),
        ):
            result = email_worker._create_ticket_from_email(
                "usuario@brandt.com.br",
                "Assunto",
                "Corpo",
                "<ldap-failure@brandt.com.br>",
            )

        self.assertIs(
            result,
            email_worker.EmailProcessingResult.TRANSIENT_FAILURE,
        )
        session_factory.assert_not_called()

    def test_database_failure_is_transient_and_rolls_back(self) -> None:
        db = MagicMock()
        db.query.side_effect = RuntimeError("banco indisponível")
        ad_user = {
            "full_name": "Usuário Teste",
            "department": "Teste",
            "active": True,
        }

        with (
            patch.object(email_worker, "validate_sender", return_value=ad_user),
            patch.object(email_worker, "SessionLocal", return_value=db),
            patch.object(email_worker, "logger"),
        ):
            result = email_worker._create_ticket_from_email(
                "usuario@brandt.com.br",
                "Assunto",
                "Corpo",
                "<db-failure@brandt.com.br>",
            )

        self.assertIs(
            result,
            email_worker.EmailProcessingResult.TRANSIENT_FAILURE,
        )
        db.rollback.assert_called_once_with()
        db.close.assert_called_once_with()

    def test_duplicate_message_is_an_idempotent_success(self) -> None:
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = object()
        ad_user = {
            "full_name": "Usuário Teste",
            "department": "Teste",
            "active": True,
        }

        with (
            patch.object(email_worker, "validate_sender", return_value=ad_user),
            patch.object(email_worker, "SessionLocal", return_value=db),
            patch.object(email_worker, "enqueue_ticket_received") as notification,
        ):
            result = email_worker._create_ticket_from_email(
                "usuario@brandt.com.br",
                "Assunto",
                "Corpo",
                "<duplicate@brandt.com.br>",
            )

        self.assertIs(result, email_worker.EmailProcessingResult.SUCCESS)
        db.commit.assert_not_called()
        notification.assert_not_called()
        db.close.assert_called_once_with()

    def test_created_ticket_and_notification_are_committed_together(self) -> None:
        db = MagicMock()
        existing_user = SimpleNamespace(
            id=7,
            email="usuario@brandt.com.br",
            full_name="Usuário Teste",
        )
        db.query.return_value.filter.return_value.first.side_effect = [
            None,
            existing_user,
        ]
        db.refresh.side_effect = lambda ticket: setattr(ticket, "id", 42)
        ad_user = {
            "full_name": "Usuário Teste",
            "department": "Teste",
            "active": True,
        }

        with (
            patch.object(email_worker, "validate_sender", return_value=ad_user),
            patch.object(email_worker, "SessionLocal", return_value=db),
            patch.object(email_worker, "record_event") as audit,
            patch.object(email_worker, "enqueue_ticket_received") as enqueue,
            patch.object(email_worker, "logger"),
        ):
            result = email_worker._create_ticket_from_email(
                "usuario@brandt.com.br",
                "Assunto",
                "Corpo",
                "<success@brandt.com.br>",
            )

        self.assertIs(result, email_worker.EmailProcessingResult.SUCCESS)
        audit.assert_called_once()
        enqueue.assert_called_once()
        db.commit.assert_called_once_with()
        db.close.assert_called_once_with()


class ImapAcknowledgementTests(unittest.TestCase):
    def test_marks_only_final_results_as_seen(self) -> None:
        client = MagicMock()
        client.select.return_value = ("OK", [b""])
        client.search.return_value = ("OK", [b"1 2 3"])
        raw_message = (
            b"From: usuario@brandt.com.br\r\n"
            b"Subject: Teste\r\n"
            b"\r\n"
            b"Corpo"
        )
        client.fetch.side_effect = [
            ("OK", [(b"1 (RFC822)", raw_message)]),
            ("OK", [(b"2 (RFC822)", raw_message)]),
            ("OK", [(b"3 (RFC822)", raw_message)]),
        ]
        client.store.return_value = ("OK", [b""])

        outcomes = [
            email_worker.EmailProcessingResult.SUCCESS,
            email_worker.EmailProcessingResult.PERMANENT_REJECTION,
            email_worker.EmailProcessingResult.TRANSIENT_FAILURE,
        ]

        with (
            patch("imaplib.IMAP4_SSL", return_value=client),
            patch("imaplib.IMAP4", return_value=client),
            patch.object(email_worker, "_handle_message", side_effect=outcomes),
            patch.object(email_worker, "logger"),
        ):
            email_worker._poll_imap_once()

        self.assertEqual(
            client.store.call_args_list,
            [
                call(b"1", "+FLAGS", "\\Seen"),
                call(b"2", "+FLAGS", "\\Seen"),
            ],
        )
        self.assertEqual(
            client.fetch.call_args_list,
            [
                call(b"1", "(BODY.PEEK[])"),
                call(b"2", "(BODY.PEEK[])"),
                call(b"3", "(BODY.PEEK[])"),
            ],
        )


class LdapSenderValidationTests(unittest.TestCase):
    def test_operational_error_is_not_reported_as_invalid_user(self) -> None:
        sensitive_detail = "bind-password=segredo-de-teste"
        with (
            patch.dict(sys.modules, _fake_ldap_modules()),
            patch.object(
                ldap_auth,
                "_get_connection",
                side_effect=_FakeLDAPException(sensitive_detail),
            ),
            self.assertLogs("geodemandas.ldap", level="WARNING") as logs,
            self.assertRaises(LDAPOperationalError) as raised,
        ):
            ldap_auth._validate_sender_ldap("usuario@brandt.com.br")

        visible_output = "\n".join([str(raised.exception), *logs.output])
        self.assertNotIn(sensitive_detail, visible_output)

    def test_unknown_user_remains_a_permanent_invalid_result(self) -> None:
        connection = MagicMock()
        connection.entries = []

        with (
            patch.dict(sys.modules, _fake_ldap_modules()),
            patch.object(ldap_auth, "_get_connection", return_value=connection),
        ):
            result = ldap_auth._validate_sender_ldap("inexistente@brandt.com.br")

        self.assertIsNone(result)
        connection.unbind.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
