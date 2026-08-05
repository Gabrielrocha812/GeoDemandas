"""Testes da serialização UTC e apresentação no fuso da aplicação."""
from __future__ import annotations

import unittest
from datetime import datetime

from time_utils import format_local_datetime, iso_utc


class TimeFormattingTests(unittest.TestCase):
    def test_naive_database_value_is_treated_as_utc(self) -> None:
        value = datetime(2026, 7, 29, 12, 30)

        self.assertEqual(
            format_local_datetime(value),
            "29/07/2026 09:30",
        )

    def test_api_timestamp_has_explicit_utc_marker(self) -> None:
        value = datetime(2026, 7, 29, 12, 30)

        self.assertEqual(iso_utc(value), "2026-07-29T12:30:00Z")


if __name__ == "__main__":
    unittest.main()
