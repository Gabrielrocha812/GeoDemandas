# GeoDemandas Brandt

Sistema interno de gestão de demandas. Colaboradores abrem chamados **enviando um e-mail** para `geodemandas@brandt.com.br`; um worker de background lê a caixa via **IMAP**, valida o remetente no **Active Directory (LDAP)** e cria o ticket automaticamente. Técnicos acompanham tudo por uma plataforma web premium.

Stack: **FastAPI · SQLAlchemy · python-ldap · Jinja2 · Tailwind CSS · Alpine.js**

---

## Como rodar localmente (modo desenvolvimento)

Em `DEV_MODE=true` (padrão), o sistema **não precisa de AD nem servidor de e-mail reais** — usa mocks embutidos e injeta 3 e-mails fictícios ao subir.

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt      # veja a nota sobre python-ldap abaixo
Copy-Item .env.example .env
uvicorn main:app --reload
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn main:app --reload
```

Acesse **http://localhost:8000** e faça login:

| E-mail                      | Senha      | Papel       |
|-----------------------------|------------|-------------|
| `tecnico.ti@brandt.com.br`  | `senha123` | Técnico     |
| `joao.silva@brandt.com.br`  | `senha123` | Colaborador |
| `maria.souza@brandt.com.br` | `senha123` | Colaborador |

> ⚠️ **python-ldap:** a biblioteca só é importada quando `DEV_MODE=false`. No
> `requirements.txt` ela tem o marcador `sys_platform != "win32"`, ou seja, é
> **pulada automaticamente no Windows** (onde exige compilação) — o fluxo com
> mocks roda sem ela. Para usar AD **real** no Windows, instale à parte com um
> wheel pré-compilado; no Linux basta `apt-get install libldap2-dev libsasl2-dev`.

---

## O que acontece ao subir (DEV_MODE)

Cerca de 3 segundos após iniciar, o worker injeta e-mails de teste:

1. `joao.silva@brandt.com.br` → ✅ vira ticket **URGENTE** (validado no AD).
2. `maria.souza@brandt.com.br` → ✅ vira ticket (validado no AD).
3. `externo@gmail.com` → ❌ **rejeitado** (remetente não existe no AD).

Você verá os logs no terminal e os tickets 1 e 2 já no dashboard.

---

## Arquitetura

```
GeoDemandas/
├── main.py              # App FastAPI + lifespan que sobe o worker
├── config.py            # Variáveis de ambiente (pydantic-settings)
├── database.py          # SQLAlchemy: engine, sessão, modelos, seed
├── ldap_auth.py         # Validação/autenticação no AD (mock em DEV_MODE)
├── email_worker.py      # Loop assíncrono IMAP -> valida -> cria ticket
├── auth.py              # Sessão/login da plataforma web
├── routes/
│   ├── web.py           # Páginas HTML (login, dashboard, detalhe)
│   └── api.py           # Endpoints JSON (comentar, status, atribuir)
└── templates/
    ├── base.html        # Layout premium + sidebar animada
    ├── login.html
    ├── dashboard.html   # Métricas + lista de chamados (cascata)
    ├── ticket_detail.html  # Timeline + comentários + ações
    └── 404.html
```

### Fluxo de segurança

- O worker só cria ticket se `ldap_auth.validate_sender()` confirmar que o
  remetente **existe e está ativo** no AD.
- Toda ação na web (comentar, mudar status) passa por `get_current_user`, que
  exige um usuário **presente e ativo no banco local** (sincronizado do AD).
- No AD real, "ativo" é aferido pelo bit `ACCOUNTDISABLE` de `userAccountControl`.

---

## Indo para produção

1. No `.env`: `DEV_MODE=false` e preencha as seções `IMAP_*` e `LDAP_*`.
2. Para notificações aos solicitantes, configure `SMTP_*`, ajuste
   `APP_BASE_URL` para o endereço público e defina `SMTP_ENABLED=true`.
   Como alternativa recomendada no Microsoft 365, use
   `EMAIL_PROVIDER=graph`, configure `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`,
   `GRAPH_CLIENT_SECRET` e `GRAPH_SENDER_EMAIL`. O aplicativo precisa da
   permissão Microsoft Graph `Mail.Send` do tipo Application com consentimento
   de administrador.
3. Troque `SECRET_KEY` por um valor aleatório e longo.
4. (Opcional) Use PostgreSQL: ajuste `DATABASE_URL` e instale `psycopg2-binary`.
5. Rode com Gunicorn/Uvicorn workers atrás de um proxy (Nginx) e HTTPS.
6. Compile o Tailwind localmente em vez do CDN para reduzir o payload.
