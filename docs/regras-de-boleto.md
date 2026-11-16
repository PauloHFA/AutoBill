# Regras de Validação FEBRABAN no AutoBill

Este documento descreve as fórmulas matemáticas implementadas em `autobill/rules/`.

## 1. Boleto Bancário (47 dígitos na linha digitável / 44 no código de barras)

### Estrutura do Código de Barras (44 posições, índice 0..43)
- `0..2` (3 dígitos): Código do banco (`001` BB, `033` Santander, `104` Caixa, `237` Bradesco, `341` Itaú, `077` Inter, `260` Nubank, etc.)
- `3` (1 dígito): Código da moeda (`9` = Real)
- `4` (1 dígito): **DV Geral** (Módulo 11 bancário dos 43 dígitos restantes)
- `5..8` (4 dígitos): **Fator de Vencimento**
- `9..18` (10 dígitos): **Valor em centavos** (zeros à esquerda)
- `19..43` (25 dígitos): **Campo Livre** (específico do banco)

### Mapeamento da Linha Digitável (47 dígitos)
- **Campo 1 (`0..9`):** `codigo[0:4] + codigo[19:24]` + `DV1` (Módulo 10)
- **Campo 2 (`10..20`):** `codigo[24:34]` + `DV2` (Módulo 10)
- **Campo 3 (`21..31`):** `codigo[34:44]` + `DV3` (Módulo 10)
- **Campo 4 (`32`):** `DV Geral` (`codigo[4]`)
- **Campo 5 (`33..46`):** `Fator de Vencimento (4)` + `Valor (10)` (`codigo[5:19]`)

## 2. Fator de Vencimento e Virada de 2025 (Resolução FEBRABAN)
- **Base original (1997-10-07):** `fator = 1000` corresponde a `2000-07-03` (`1997-10-07 + 1000 dias`).
- **Reinício em 22/02/2025:** Ao atingir `9999` em `2025-02-21`, o fator reiniciou para `1000` em **`2025-02-22`**.
- **Regra no AutoBill (`autobill/rules/due_date.py`):**
  - Para `fator < 1000`: soma a partir da data-base original (`1997-10-07`).
  - Para `fator >= 1000`: resolve automaticamente considerando o ciclo vigente pós-`2025-02-22` (`base 2025-02-22 + (fator - 1000) dias`), escolhendo a data mais próxima do ano de referência e sinalizando `is_fator_ambiguo` quando duas janelas possíveis estiverem próximas.

## 3. Guias de Arrecadação / Convênios (48 dígitos)
- **Estrutura:** 4 blocos de 11 dígitos + 1 DV cada (`4 x 12 = 48`).
- **Identificação pelo 3º dígito (`codigo[2]`):**
  - `'6'` ou `'7'`: **Módulo 10** nos 4 blocos e no DV geral (`codigo[3]`).
  - `'8'` ou `'9'`: **Módulo 11 Arrecadação** (resto `0` ou `1` -> DV = `0`; resto `10` -> DV = `1`; demais -> `11 - resto`).
- **Identificador de Valor (`codigo[2]`):**
  - `'6'` ou `'8'`: valor efetivo em centavos nas posições `4..14` do código de barras.
  - `'7'` ou `'9'`: valor de referência/indexado (exige confirmação no OCR/PDF).

## 4. Segurança Anti-Golpe
- Confronta o código do banco (`0..2`) com o nome do beneficiário extraído quando este menciona explicitamente outro banco conhecido, sinalizando divergência no resumo e nos detalhes da fatura.
