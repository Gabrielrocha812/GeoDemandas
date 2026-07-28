"""
config.py
---------
Centraliza todas as variáveis de ambiente do GeoDemandas Brandt.

Usamos pydantic-settings para ler o arquivo `.env` automaticamente e já
converter os tipos (int, bool, etc). Basta importar `settings` em qualquer
módulo:

    from config import settings
    print(settings.IMAP_HOST)

Para testar localmente sem AD/IMAP reais, deixe DEV_MODE=true no .env.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Geral ---
    APP_NAME: str = "GeoDemandas Brandt"
    APP_BASE_URL: str = "http://localhost:8000"
    SECRET_KEY: str = "dev-secret-key"
    DEV_MODE: bool = True
    LDAP_USE_REAL: bool = False
    INITIAL_ADMIN_EMAILS: str = ""

    # --- Banco de dados ---
    DATABASE_URL: str = "sqlite:///./geodemandas.db"

    # --- IMAP (caixa monitorada) ---
    IMAP_HOST: str = "imap.brandt.com.br"
    IMAP_PORT: int = 993
    IMAP_USE_SSL: bool = True
    IMAP_USER: str = "geodemandas@brandt.com.br"
    IMAP_PASSWORD: str = ""
    IMAP_MAILBOX: str = "INBOX"
    EMAIL_POLL_INTERVAL: int = 30

    # --- SMTP (notificações aos solicitantes) ---
    EMAIL_PROVIDER: str = "smtp"
    SMTP_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USE_TLS: bool = True
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "geodemandas@brandt.com.br"
    SMTP_FROM_NAME: str = "GeoDemandas Brandt"

    # --- Microsoft Graph (alternativa recomendada ao SMTP) ---
    GRAPH_TENANT_ID: str = ""
    GRAPH_CLIENT_ID: str = ""
    GRAPH_CLIENT_SECRET: str = ""
    GRAPH_SENDER_EMAIL: str = "geodemandas@brandt.com.br"

    # --- Anexos ---
    UPLOAD_DIR: str = "uploads"
    MAX_ATTACHMENT_SIZE_MB: int = 25
    MAX_ATTACHMENTS_PER_MESSAGE: int = 10

    # --- LDAP / Active Directory ---
    LDAP_SERVER: str = "ldap://ad.brandt.com.br"
    LDAP_PORT: int = 389
    LDAP_USE_SSL: bool = False
    LDAP_BASE_DN: str = "DC=brandt,DC=com,DC=br"
    LDAP_BIND_DN: str = ""
    LDAP_BIND_PASSWORD: str = ""
    LDAP_DOMAIN: str = "brandt.com.br"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Cache das configurações para não reler o .env a cada chamada."""
    return Settings()


settings = get_settings()
