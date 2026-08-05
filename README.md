# GeoDemandas Brandt

Sistema interno de gestão de demandas. Colaboradores abrem chamados **enviando um e-mail** para `geodemandas@brandt.com.br`; um worker de background lê a caixa via **IMAP**, valida o remetente no **Active Directory (LDAP)** e cria o ticket automaticamente. Técnicos acompanham tudo por uma plataforma web premium.

Stack: **FastAPI · SQLAlchemy · ldap3 · Jinja2 · Tailwind CSS · Alpine.js**

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

> A integração usa **ldap3**, que é compatível com Windows e Linux sem depender
> da compilação nativa do antigo `python-ldap`.

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
├── main.py              # App FastAPI + lifespan dos workers
├── config.py            # Variáveis de ambiente (pydantic-settings)
├── database.py          # SQLAlchemy: domínio, outbox e auditoria
├── ldap_auth.py         # Validação/autenticação no AD (mock em DEV_MODE)
├── email_worker.py      # Loop assíncrono IMAP -> valida -> cria ticket
├── outbox_service.py    # Notificações persistentes, locks e retentativas
├── sla_monitor.py       # Alertas preventivos e de vencimento de SLA
├── audit_service.py     # Auditoria estruturada sem conteúdo sensível
├── projeto_service.py   # Lista de projetos da API corporativa (com cache)
├── workflow_service.py  # Transições, reabertura, atividade e SLA
├── auth.py              # Sessão/login da plataforma web
├── routes/
│   ├── web.py           # Páginas HTML (login, dashboard, detalhe)
│   ├── api.py           # Endpoints JSON (comentar, status, atribuir)
│   └── operations.py    # Saúde operacional, entregas e auditoria
└── templates/
    ├── base.html        # Layout premium + sidebar animada
    ├── login.html
    ├── dashboard.html   # Métricas + lista de chamados (cascata)
    ├── ticket_detail.html  # Timeline + comentários + ações
    └── 404.html
```

### Fluxo operacional

- Ciclo controlado: **Aberto → Em Triagem → Em Andamento**, com etapas de
  **Aguardando Solicitante**, **Bloqueado**, **Resolvido**, **Concluído**,
  **Cancelado** e **Reaberto**.
- Técnicos veem filas de demandas próprias, não atribuídas, aguardando retorno,
  em risco e vencidas. Busca, status e prioridade podem ser combinados.
- Respostas do solicitante retomam automaticamente demandas que estavam
  aguardando e reabrem atendimentos resolvidos/concluídos.
- Notas internas e seus anexos ficam restritos a técnicos e administradores e
  não disparam notificação ao solicitante.
- Os prazos de primeira resposta e resolução são configurados pelas variáveis
  `SLA_*` e contam somente de segunda a sexta, dentro da jornada definida por
  `SLA_BUSINESS_START_HOUR` e `SLA_BUSINESS_END_HOUR`; o prazo de resolução
  fica pausado enquanto se aguarda o solicitante.
- O worker IMAP só marca uma mensagem como lida após sucesso, duplicidade já
  processada ou rejeição definitiva. Falhas temporárias de LDAP/banco ficam na
  caixa para nova tentativa.
- Confirmações e atualizações entram em uma **outbox na mesma transação** da
  demanda. O envio tem claim exclusivo, retentativa exponencial, recuperação de
  locks expirados e fila de falhas visível ao administrador.
- O monitor de SLA avisa o responsável — ou os administradores quando a demanda
  ainda não foi atribuída — antes do prazo e após o vencimento, sem notificar o
  solicitante com alertas internos.
- A página **Operações** (somente administrador) reúne backlog, entregas,
  falhas, riscos de SLA e eventos de auditoria. Uma falha definitiva pode ser
  reenfileirada manualmente.
- A auditoria é gravada na mesma transação da alteração e armazena apenas
  metadados estruturados; corpo da demanda, comentários, notas internas,
  credenciais e respostas brutas de integrações são rejeitados.

Para executar os testes automatizados:

```powershell
python -m unittest discover -s . -p "test_*.py" -v
```

### Integração com a API de projetos

Ao cadastrar uma demanda pelo portal, a lista de projetos é sempre buscada em
`PROJETOS_API_URL` (autenticação pelo header `x-token`, valor em
`PROJETOS_API_TOKEN`). O resultado fica em cache por `PROJETOS_CACHE_TTL`
segundos; se a API cair, o último resultado conhecido continua sendo servido.
Sem nenhum cache disponível, o formulário exibe um aviso e permite cadastrar a
demanda sem projeto — o campo é obrigatório apenas quando a lista carrega.

Demandas criadas por e-mail não têm projeto (`project_code`/`project_name`
ficam nulos), pois o remetente não passa por essa tela.

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
4. Use PostgreSQL em produção: ajuste `DATABASE_URL` e instale
   `psycopg2-binary`. SQLite deve ficar restrito a desenvolvimento ou operação
   pequena em disco local.
5. Enquanto os loops IMAP/outbox/SLA estiverem embutidos no `lifespan`, rode
   **um único worker Uvicorn** atrás de um proxy (Nginx) e HTTPS. A separação
   desses loops em um processo dedicado é requisito antes de aumentar o número
   de workers web.
6. Compile o Tailwind localmente em vez do CDN para reduzir o payload.
