import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class SecurityTests(unittest.TestCase):
    def test_cross_site_mutation_is_blocked(self):
        with patch.object(main.settings, "DEV_MODE", True), patch.object(main.settings, "EMBEDDED_WORKERS", False):
            with TestClient(main.app) as client:
                response = client.post(
                    "/login",
                    data={"username": "x", "password": "x"},
                    headers={"Origin": "https://evil.example"},
                )
        self.assertEqual(response.status_code, 403)

    def test_security_headers_are_present(self):
        with patch.object(main.settings, "DEV_MODE", True), patch.object(main.settings, "EMBEDDED_WORKERS", False):
            with TestClient(main.app) as client:
                response = client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["content-security-policy"])

    def test_production_configuration_rejects_unsafe_defaults(self):
        with (
            patch.object(main.settings, "DEV_MODE", False),
            patch.object(main.settings, "APP_BASE_URL", "http://localhost:8000"),
            patch.object(main.settings, "DATABASE_URL", "sqlite:///./x.db"),
        ):
            with self.assertRaises(RuntimeError):
                main.validate_production_settings()


if __name__ == "__main__":
    unittest.main()
