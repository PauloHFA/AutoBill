# ADR 0002: Pipeline de Extração em Camadas e Validação Cruzada

## Contexto
Boletos chegam em formatos heterogêneos: PDFs nativos com texto selecionável, PDFs escaneados, PDFs protegidos por senha e fotos de celular.

## Decisão
Adotar um pipeline determinístico em 4 camadas (`autobill/extract/pipeline.py`), parando ou combinando evidências conforme validação matemática:
1. **Camada 1 — Texto nativo PDF (`pypdf`)**: extração instantânea + regex de linha digitável, valor, vencimento e beneficiário; trata PDF criptografado solicitando senha em memória sem persistência.
2. **Camada 2 — Código de barras (`pypdfium2` + OpenCV/pyzbar)**: leitura direta do código de barras ITF-25 (44 dígitos) em PDFs renderizados e imagens, convertendo para linha digitável e validando Módulo 10/11.
3. **Camada 3 — OCR local (`pytesseract` + `Pillow`)**: pré-processamento de imagem e extração textual offline.
4. **Camada 4 — Visão Multimodal (Ollama local ou Gemini Flash se consentido)**: fallback com limite diário (`MAX_LLM_CALLS_PER_DAY`) para layouts complexos.

## Validação Cruzada
Quando a linha digitável possui valor/vencimento embutidos e o OCR/LLM retorna valores divergentes, o pipeline marca a fatura como `EM_REVISAO` com observação detalhada em `review_notes`, evitando falsos positivos silenciosos.
