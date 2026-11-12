# AutoBill

Monitor inteligente de faturas e alertas de vencimento (Python, Telegram, Docker)

## Visão Geral

O AutoBill recebe faturas e boletos (PDF ou imagem), extrai vencimento, valor, beneficiário e linha digitável, valida os dados matematicamente, guarda tudo em banco local e envia alertas proativos antes e no dia do vencimento, com a linha digitável pronta para copiar e botões para marcar como pago.

## Funcionalidades

- **Múltiplas entradas**: Pasta monitorada, Telegram Bot, E-mail (IMAP opcional), Lançamento manual
- **Extração inteligente**: Texto do PDF → Código de barras → LLM com visão → OCR local
- **Validação rigorosa**: Módulo 10/11, fator de vencimento, verificação cruzada
- **Alertas escalonados**: D-3, D-1, D0, em atraso com idempotência
- **Revisão humana**: Confirmação/Correção/Descarte para baixa confiança
- **Comandos Telegram**: /pendentes, /proximos, /mes, /fatura, /pagas, /pago, /desfazer, /adicionar
- **Privacidade**: Modo 100% local, consentimento explícito para nuvem, mascaramento de logs

## Requisitos

- Python 3.11+
- Docker & Docker Compose (recomendado)
- Token do Telegram Bot (@BotFather)

## Instalação Rápida (Docker)

```bash
# Clone o repositório
git clone <repo-url>
cd AutoBill

# Configure variáveis de ambiente
cp .env.example .env
# Edite .env com seu TELEGRAM_BOT_TOKEN e ALLOWED_CHAT_IDS

# Suba os containers
docker compose up -d

# Para usar LLM local (Ollama)
docker compose --profile local-llm up -d
```

## Instalação Local (Desenvolvimento)

```bash
# Crie ambiente virtual
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# Instale dependências
pip install -e ".[dev]"

# Configure variáveis
cp .env.example .env
# Edite .env

# Execute migrações
alembic upgrade head

# Execute
python -m autobill.main
```

## Configuração

Veja `.env.example` para todas as opções. Principais:

| Variável | Descrição | Obrigatório |
|----------|-----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Token do @BotFather | Sim |
| `ALLOWED_CHAT_IDS` | IDs de chats autorizados (vírgula) | Sim |
| `EXTRACTOR` | `auto`, `cloud`, `local` | Não (padrão: auto) |
| `CLOUD_EXTRACTION_CONSENT` | Consentimento para nuvem | Para cloud |
| `GEMINI_API_KEY` | Chave API Gemini | Para cloud |
| `DEFAULT_TIMEZONE` | Fuso horário (IANA) | Não (padrão: America/Fortaleza) |
| `ALERT_TIME` | Horário alertas (HH:MM) | Não (padrão: 07:30) |
| `LEAD_DAYS` | Dias de antecedência | Não (padrão: 3,1,0) |

## Uso no Telegram

1. Inicie o bot com `/start`
2. Envie PDF/foto de boleto → processamento automático
3. Use `/adicionar` para lançamento manual
4. Comandos principais:
   - `/pendentes` - Faturas pendentes ordenadas
   - `/proximos [dias]` - Próximos vencimentos
   - `/mes AAAA-MM` - Resumo do mês
   - `/fatura <id>` - Detalhes da fatura
   - `/pagas` - Faturas pagas
   - `/pago <id>` - Marcar como paga
   - `/desfazer` - Desfazer último pagamento

## Estrutura do Projeto

```
autobill/
├── autobill/
│   ├── bot/           # Handlers, keyboards, auth do Telegram
│   ├── ingest/        # Pasta, Telegram, IMAP, fila
│   ├── extract/       # PDF, barras, LLM, OCR, pipeline
│   ├── rules/         # Validação boleto, datas, dinheiro
│   ├── alerts/        # Política, agendador, formatação
│   ├── db/            # Models, repository, migrations
│   └── ops/           # Backup, retenção, health
├── data/              # Volumes persistentes
├── tests/             # Testes unitários, propriedade, integração
└── docs/              # ADRs, documentação
```

## Desenvolvimento

### Testes

```bash
# Todos os testes
pytest

# Com cobertura
pytest --cov=autobill

# Apenas testes de propriedade (Hypothesis)
pytest tests/property -v
```

### Linting & Type Checking

```bash
ruff check .
ruff format .
mypy autobill
```

### Migrações de Banco

```bash
# Criar nova migração
alembic revision --autogenerate -m "descrição"

# Aplicar migrações
alembic upgrade head
```

## Segurança

- **Nunca** commite o arquivo `.env`
- Tokens e chaves via variáveis de ambiente ou Docker secrets
- Logs mascaram dados sensíveis (linha digitável, tokens)
- Modo local disponível para privacidade total

## Licença

MIT