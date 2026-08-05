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
    APP_TIMEZONE: str = "America/Sao_Paulo"
    SECRET_KEY: str = "dev-secret-key"
    DEV_MODE: bool = True
    LDAP_USE_REAL: bool = False
    INITIAL_ADMIN_EMAILS: str = ""

    # --- Banco de dados ---
    DATABASE_URL: str = "sqlite:///./geodemandas.db"

    # --- SLA operacional (horas corridas; calendário útil entra na próxima etapa) ---
    SLA_FIRST_RESPONSE_HOURS_BAIXA: int = 24
    SLA_FIRST_RESPONSE_HOURS_MEDIA: int = 8
    SLA_FIRST_RESPONSE_HOURS_ALTA: int = 4
    SLA_FIRST_RESPONSE_HOURS_URGENTE: int = 1
    SLA_RESOLUTION_HOURS_BAIXA: int = 120
    SLA_RESOLUTION_HOURS_MEDIA: int = 72
    SLA_RESOLUTION_HOURS_ALTA: int = 24
    SLA_RESOLUTION_HOURS_URGENTE: int = 8
    SLA_RISK_WINDOW_HOURS: int = 4
    SLA_ALERT_POLL_INTERVAL_SECONDS: int = 300
    DASHBOARD_PAGE_SIZE: int = 25

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

    # --- Outbox persistente de notificacoes ---
    NOTIFICATION_OUTBOX_POLL_INTERVAL: int = 5
    NOTIFICATION_OUTBOX_BATCH_SIZE: int = 20
    NOTIFICATION_OUTBOX_MAX_ATTEMPTS: int = 5
    NOTIFICATION_OUTBOX_RETRY_BASE_SECONDS: int = 30
    NOTIFICATION_OUTBOX_RETRY_MAX_SECONDS: int = 3600
    NOTIFICATION_OUTBOX_LOCK_TIMEOUT_SECONDS: int = 300

    # --- Microsoft Graph (alternativa recomendada ao SMTP) ---
    GRAPH_TENANT_ID: str = ""
    GRAPH_CLIENT_ID: str = ""
    GRAPH_CLIENT_SECRET: str = ""
    GRAPH_SENDER_EMAIL: str = "geodemandas@brandt.com.br"

    # --- Anexos ---
    UPLOAD_DIR: str = "uploads"
    MAX_ATTACHMENT_SIZE_MB: int = 25
    MAX_ATTACHMENTS_PER_MESSAGE: int = 10

    # --- API corporativa de projetos ---
    # Lista os projetos ativos que podem ser vinculados a uma demanda.
    # O endpoint exige o token no header "x-token".
    PROJETOS_API_URL: str = "http://69.64.32.23:8000/projetos"
    PROJETOS_API_TOKEN: str = "meu_token_secreto"
    PROJETOS_API_TIMEOUT: int = 10
    PROJETOS_CACHE_TTL: int = 300

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
