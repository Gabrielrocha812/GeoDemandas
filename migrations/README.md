# Migrações

Execute `alembic upgrade head` antes de iniciar a aplicação. Para adotar um
banco legado, faça backup primeiro; a revisão inicial usa `checkfirst` e marca
o esquema sem apagar tabelas existentes. Novas mudanças devem ser criadas com
`alembic revision --autogenerate -m "descricao"` e revisadas manualmente.
