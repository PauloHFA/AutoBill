# ADR 0001: Arquitetura Local-First, SQLite e Privacidade (LGPD)

## Contexto
Boletos contêm dados financeiros e pessoais sensíveis (CPF/CNPJ, valores, credores, códigos de pagamento). O sistema precisa operar em hardware modesto (Raspberry Pi / VPS / PC local) sem depender obrigatoriamente de serviços em nuvem.

## Decisão
1. **Persistência única em SQLite (`aiosqlite` + SQLAlchemy 2.0 async)** com migrações via Alembic.
2. **Privacidade por padrão (`EXTRACTOR=local` / `CLOUD_EXTRACTION_CONSENT=false`):** nenhuma imagem ou texto sai da máquina sem consentimento explícito no `.env` ou `/config`.
3. **Alternativa offline de visão:** suporte nativo a Ollama local (`qwen2.5-vl` / `llava`) além de Gemini Flash quando autorizado.
4. **Mascaramento de logs:** `linha_digitavel` e `codigo_barras` são mascarados preservando apenas os últimos 6 dígitos (`••••••123456`), e tokens/senhas são redigidos.
5. **Exclusão e portabilidade:** `/exportar` (CSV) e `/apagar_tudo` (confirmação dupla com remoção física de arquivos e registros).

## Consequências
- Zero custo fixo de infraestrutura de banco ou nuvem.
- Backup simplificado em um único arquivo SQLite com `sqlite3.backup()` consistente.
