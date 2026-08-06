# Runbook de produção

URL oficial: `https://demandas.brandt.com.br` (o DNS deve apontar para
`10.2.8.37`). Para alertas externos, configure `MONITOR_ALERT_EMAILS` e,
opcionalmente, `TEAMS_WEBHOOK_URL` com a URL HTTPS criada por um Workflow do
Microsoft Teams. Sem webhook, os alertas continuam registrados no painel.

1. Crie usuário `geodemandas`, PostgreSQL e `/opt/geodemandas/releases`.
2. Instale o código em uma release imutável e aponte `/opt/geodemandas/current`.
3. Crie `/opt/geodemandas/venv`, instale `requirements.txt` e configure
   `/etc/geodemandas/geodemandas.env` com permissão `0600`.
4. Execute `alembic upgrade head` como o usuário do serviço.
5. Instale as unidades systemd e o virtual host Nginx; valide com
   `systemd-analyze verify` e `nginx -t`.
6. Inicie primeiro `geodemandas-worker`, depois `geodemandas-web`.
7. Valide `/health/live`, `/health`, login LDAP, criação, upload, Graph, IMAP,
   outbox, SLA e auditoria. `/health` deve retornar 200 e `dev_mode=false`.

Rollback: pare os serviços, restaure o link `current` para a release anterior,
execute a migração compatível indicada pela release e reinicie. Não reverta uma
migração destrutiva sem backup testado.

Operação: configure backup diário, teste restauração mensal, monitore os dois
serviços, HTTP 5xx, espaço em disco, PostgreSQL, idade dos heartbeats e falhas
da outbox. Execute apenas um processo worker.

O script `deploy.sh` instala uma release imutável e faz a troca atômica do link
`current`. Revise hostname, certificado, usuário do banco e segredos antes de
executá-lo. O timer realiza backup diário às 02:15 e mantém 30 dias por padrão.
