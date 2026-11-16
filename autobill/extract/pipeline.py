"""Pipeline de extração orquestrado com múltiplas estratégias e verificação cruzada."""

import time
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path

from autobill.config import settings
from autobill.extract.barcode import (
    extract_barcode_from_image,
    extract_barcode_from_pdf,
    is_zxing_available,
)
from autobill.extract.llm_vision import (
    extract_with_llm_vision,
    extract_with_ollama_vision,
    is_genai_available,
    is_ollama_available,
)
from autobill.extract.ocr import (
    extract_text_with_ocr,
    extract_text_with_ocr_from_pdf,
    is_tesseract_available,
)
from autobill.extract.pdf_text import (
    BoletoFields,
    PDFPasswordRequiredError,
    extract_boleto_fields_from_text,
    extract_boleto_from_pdf,
)
from autobill.logging_config import get_logger
from autobill.rules.boleto import (
    ARRECADACAO_LENGTH,
    BOLETO_LENGTH,
    CODIGO_BARRAS_LENGTH,
    codigo_barras_para_linha,
    extrair_fator_vencimento,
    extrair_valor_codigo_barras,
    is_valor_confirmado_codigo_barras,
    linha_para_codigo_barras,
    parse_linha_digitavel,
    validar_codigo_barras,
    validar_linha_digitavel,
)
from autobill.rules.due_date import fator_para_data_ou_none, is_fator_ambiguo
from autobill.rules.money import formatar_centavos

logger = get_logger(__name__)


class ExtractionMethod(Enum):
    """Métodos de extração disponíveis."""

    PDF_TEXT = "pdf_text"
    BARCODE = "barcode"
    LLM_VISION = "llm_vision"
    OLLAMA_VISION = "ollama_vision"
    OCR = "ocr"


@dataclass
class ExtractionResult:
    """Resultado consolidado da extração e validação cruzada."""

    linha_digitavel: str | None = None
    codigo_barras: str | None = None
    valor: int | None = None  # centavos
    vencimento: str | None = None  # ISO format YYYY-MM-DD
    beneficiario: str | None = None
    pagador: str | None = None
    nosso_numero: str | None = None
    documento: str | None = None
    data_documento: str | None = None
    especie: str | None = None
    aceite: str | None = None
    data_processamento: str | None = None
    instrucoes: str | None = None
    local_pagamento: str | None = None

    # Metadados & validação
    kind: str = "outro"  # bancario, arrecadacao, outro
    method_used: ExtractionMethod | None = None
    confidence: float = 0.0
    validation_passed: bool = False
    valor_confirmado: bool = False
    needs_review: bool = True
    discrepancies: list[str] = field(default_factory=list)
    raw_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Converte para dicionário."""
        return {
            "linha_digitavel": self.linha_digitavel,
            "codigo_barras": self.codigo_barras,
            "valor": self.valor,
            "valor_formatado": formatar_centavos(self.valor) if self.valor is not None else None,
            "vencimento": self.vencimento,
            "beneficiario": self.beneficiario,
            "pagador": self.pagador,
            "kind": self.kind,
            "method_used": self.method_used.value if self.method_used else None,
            "confidence": self.confidence,
            "validation_passed": self.validation_passed,
            "valor_confirmado": self.valor_confirmado,
            "needs_review": self.needs_review,
            "discrepancies": self.discrepancies,
        }

    def is_valid(self) -> bool:
        """Verifica se há dados mínimos extraídos (permite revisão humana se faltarem campos)."""
        return bool(
            self.linha_digitavel is not None
            or self.codigo_barras is not None
            or (self.valor is not None and self.vencimento is not None)
            or self.beneficiario is not None
        )

    def can_auto_confirm(self, min_confidence: float = 0.8) -> bool:
        """Retorna True apenas se todas as validações e cruzamentos aprovarem (RF09)."""
        return (
            self.validation_passed
            and not self.needs_review
            and not self.discrepancies
            and self.valor is not None
            and self.vencimento is not None
            and self.confidence >= min_confidence
        )


@dataclass
class ExtractionConfig:
    """Configuração do pipeline de extração."""

    method_order: list[ExtractionMethod] | None = None
    min_confidence: float = 0.5
    validate_boleto: bool = True
    try_all_methods: bool = False
    extractor_mode: str | None = None  # auto, cloud, local
    cloud_consent: bool | None = None
    llm_model: str = "gemini-2.0-flash"
    ollama_model: str | None = None
    ocr_lang: str = "por"
    pdf_page_num: int = 0
    pdf_password: str | None = None

    def resolved_method_order(self) -> list[ExtractionMethod]:
        if self.method_order is not None:
            return self.method_order
        mode = (self.extractor_mode or settings.extractor).lower()
        consent = (
            self.cloud_consent
            if self.cloud_consent is not None
            else settings.cloud_extraction_consent
        )
        if mode == "local":
            return [
                ExtractionMethod.PDF_TEXT,
                ExtractionMethod.BARCODE,
                ExtractionMethod.OLLAMA_VISION,
                ExtractionMethod.OCR,
            ]
        if mode == "cloud":
            if consent:
                return [
                    ExtractionMethod.PDF_TEXT,
                    ExtractionMethod.BARCODE,
                    ExtractionMethod.LLM_VISION,
                ]
            return [ExtractionMethod.PDF_TEXT, ExtractionMethod.BARCODE]
        order = [ExtractionMethod.PDF_TEXT, ExtractionMethod.BARCODE]
        if consent:
            order.append(ExtractionMethod.LLM_VISION)
        order.extend([ExtractionMethod.OLLAMA_VISION, ExtractionMethod.OCR])
        return order


class ExtractionPipeline:
    """Pipeline orquestrado de extração de boletos."""

    def __init__(self, config: ExtractionConfig | None = None):
        self.config = config or ExtractionConfig()

    def extract(self, file_path: str | Path) -> ExtractionResult:
        """Extrai dados de boleto tentando estratégias em cascata conforme modo configurado."""
        file_path = Path(file_path)
        if not file_path.exists():
            return ExtractionResult(confidence=0.0, raw_data={"error": "File not found"})

        results: list[ExtractionResult] = []
        for method in self.config.resolved_method_order():
            t0 = time.perf_counter()
            try:
                result = self._extract_with_method(file_path, method)
                elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
                if result:
                    logger.info(
                        "extraction_strategy_done",
                        strategy=method.value,
                        duration_ms=elapsed_ms,
                        validation_passed=result.validation_passed,
                        confidence=result.confidence,
                        linha_digitavel=result.linha_digitavel or "",
                    )
                    results.append(result)
                    if not self.config.try_all_methods and result.can_auto_confirm(
                        min_confidence=0.8
                    ):
                        return result
            except PDFPasswordRequiredError:
                raise
            except Exception as e:
                logger.warning("extraction_strategy_error", strategy=method.value, error=str(e))
                results.append(
                    ExtractionResult(
                        method_used=method,
                        confidence=0.0,
                        raw_data={"error": str(e)},
                    )
                )

        return self._select_best_result(results)

    def _extract_with_method(
        self, file_path: Path, method: ExtractionMethod
    ) -> ExtractionResult | None:
        if method == ExtractionMethod.PDF_TEXT:
            return self._extract_pdf_text(file_path)
        if method == ExtractionMethod.BARCODE:
            return self._extract_barcode(file_path)
        if method == ExtractionMethod.LLM_VISION:
            return self._extract_llm_vision(file_path)
        if method == ExtractionMethod.OLLAMA_VISION:
            return self._extract_ollama_vision(file_path)
        if method == ExtractionMethod.OCR:
            return self._extract_ocr(file_path)
        return None

    def _extract_pdf_text(self, file_path: Path) -> ExtractionResult | None:
        if file_path.suffix.lower() != ".pdf":
            return None

        fields = extract_boleto_from_pdf(file_path, password=self.config.pdf_password)
        if not any([fields.linha_digitavel, fields.codigo_barras, fields.valor, fields.vencimento]):
            return None

        result = ExtractionResult(
            linha_digitavel=fields.linha_digitavel,
            codigo_barras=fields.codigo_barras,
            valor=fields.valor,
            vencimento=fields.vencimento,
            beneficiario=fields.beneficiario,
            pagador=fields.pagador,
            nosso_numero=fields.nosso_numero,
            documento=fields.documento,
            data_documento=fields.data_documento,
            especie=fields.especie,
            aceite=fields.aceite,
            data_processamento=fields.data_processamento,
            instrucoes=fields.instrucoes,
            local_pagamento=fields.local_pagamento,
            method_used=ExtractionMethod.PDF_TEXT,
            confidence=self._calculate_pdf_text_confidence(fields),
        )
        if self.config.validate_boleto:
            self._validate_and_crosscheck(result)
        return result

    def _extract_barcode(self, file_path: Path) -> ExtractionResult | None:
        if not is_zxing_available():
            return None

        if file_path.suffix.lower() == ".pdf":
            barcode = extract_barcode_from_pdf(file_path, password=self.config.pdf_password)
        else:
            barcode = extract_barcode_from_image(file_path)

        if not barcode:
            return None

        linha_digitavel: str | None = None
        codigo_barras: str | None = None

        if len(barcode) == CODIGO_BARRAS_LENGTH:
            codigo_barras = barcode
            linha_digitavel = codigo_barras_para_linha(barcode)
        elif len(barcode) in (BOLETO_LENGTH, ARRECADACAO_LENGTH):
            linha_digitavel = barcode
            parsed = parse_linha_digitavel(barcode)
            if parsed:
                codigo_barras = linha_para_codigo_barras(parsed)

        valor = extrair_valor_codigo_barras(codigo_barras) if codigo_barras else None
        fator = extrair_fator_vencimento(codigo_barras) if codigo_barras else None
        vencimento_dt = fator_para_data_ou_none(fator) if fator else None
        vencimento = vencimento_dt.isoformat() if vencimento_dt else None

        result = ExtractionResult(
            linha_digitavel=linha_digitavel,
            codigo_barras=codigo_barras,
            valor=valor if (valor and valor > 0) else None,
            vencimento=vencimento,
            method_used=ExtractionMethod.BARCODE,
            confidence=0.9 if codigo_barras and valor else 0.65,
        )
        if self.config.validate_boleto:
            self._validate_and_crosscheck(result)
        return result

    def _extract_llm_vision(self, file_path: Path) -> ExtractionResult | None:
        consent = (
            self.config.cloud_consent
            if self.config.cloud_consent is not None
            else settings.cloud_extraction_consent
        )
        if not consent or not is_genai_available():
            return None

        llm_result = extract_with_llm_vision(
            file_path,
            model=self.config.llm_model,
            page_num=self.config.pdf_page_num,
            password=self.config.pdf_password,
        )
        if not llm_result:
            return None

        result = ExtractionResult(
            linha_digitavel=llm_result.linha_digitavel,
            codigo_barras=llm_result.codigo_barras,
            valor=llm_result.valor,
            vencimento=llm_result.vencimento,
            beneficiario=llm_result.beneficiario,
            pagador=llm_result.pagador,
            nosso_numero=llm_result.nosso_numero,
            documento=llm_result.documento,
            data_documento=llm_result.data_documento,
            especie=llm_result.especie,
            aceite=llm_result.aceite,
            data_processamento=llm_result.data_processamento,
            instrucoes=llm_result.instrucoes,
            local_pagamento=llm_result.local_pagamento,
            method_used=ExtractionMethod.LLM_VISION,
            confidence=llm_result.confidence,
            raw_data={"llm_raw": llm_result.raw_response},
        )
        if self.config.validate_boleto:
            self._validate_and_crosscheck(result)
        return result

    def _extract_ollama_vision(self, file_path: Path) -> ExtractionResult | None:
        if not is_ollama_available():
            return None

        llm_result = extract_with_ollama_vision(
            file_path,
            model=self.config.ollama_model or settings.llm_vision_model,
            password=self.config.pdf_password,
        )
        if not llm_result:
            return None

        result = ExtractionResult(
            linha_digitavel=llm_result.linha_digitavel,
            codigo_barras=llm_result.codigo_barras,
            valor=llm_result.valor,
            vencimento=llm_result.vencimento,
            beneficiario=llm_result.beneficiario,
            pagador=llm_result.pagador,
            method_used=ExtractionMethod.OLLAMA_VISION,
            confidence=llm_result.confidence,
            raw_data={"ollama_raw": llm_result.raw_response},
        )
        if self.config.validate_boleto:
            self._validate_and_crosscheck(result)
        return result

    def _extract_ocr(self, file_path: Path) -> ExtractionResult | None:
        if not is_tesseract_available():
            return None

        if file_path.suffix.lower() == ".pdf":
            text = extract_text_with_ocr_from_pdf(
                file_path, lang=self.config.ocr_lang, password=self.config.pdf_password
            )
        else:
            text = extract_text_with_ocr(file_path, lang=self.config.ocr_lang)

        if not text:
            return None

        fields = extract_boleto_fields_from_text(text)
        result = ExtractionResult(
            linha_digitavel=fields.linha_digitavel,
            codigo_barras=fields.codigo_barras,
            valor=fields.valor,
            vencimento=fields.vencimento,
            beneficiario=fields.beneficiario,
            pagador=fields.pagador,
            nosso_numero=fields.nosso_numero,
            documento=fields.documento,
            data_documento=fields.data_documento,
            especie=fields.especie,
            aceite=fields.aceite,
            data_processamento=fields.data_processamento,
            instrucoes=fields.instrucoes,
            local_pagamento=fields.local_pagamento,
            method_used=ExtractionMethod.OCR,
            confidence=self._calculate_ocr_confidence(fields),
        )
        if self.config.validate_boleto:
            self._validate_and_crosscheck(result)
        return result

    def _calculate_pdf_text_confidence(self, fields: BoletoFields) -> float:
        score = 0.0
        if fields.linha_digitavel:
            score += 0.4
        if fields.valor is not None:
            score += 0.2
        if fields.vencimento:
            score += 0.2
        if fields.beneficiario:
            score += 0.1
        if fields.pagador:
            score += 0.1
        return round(score, 3)

    def _calculate_ocr_confidence(self, fields: BoletoFields) -> float:
        return round(self._calculate_pdf_text_confidence(fields) * 0.7, 3)

    def _validate_and_crosscheck(self, result: ExtractionResult) -> bool:
        """
        Valida DV (RF07) e executa verificação cruzada valor/vencimento (RF08, RN05, RN06, RN10).
        """
        result.discrepancies.clear()
        result.needs_review = False

        if not result.linha_digitavel and result.codigo_barras:
            result.linha_digitavel = codigo_barras_para_linha(result.codigo_barras)

        if not result.linha_digitavel:
            result.validation_passed = False
            result.needs_review = True
            result.discrepancies.append("Linha digitável não encontrada")
            return False

        parsed = parse_linha_digitavel(result.linha_digitavel)
        if not parsed:
            result.validation_passed = False
            result.needs_review = True
            result.discrepancies.append("Tamanho da linha digitável inválido (esperado 47 ou 48)")
            return False

        result.kind = "arrecadacao" if parsed.tipo == "arrecadacao" else "bancario"

        if not validar_linha_digitavel(parsed.raw):
            result.validation_passed = False
            result.needs_review = True
            result.discrepancies.append("Dígito verificador da linha digitável inválido")
            return False

        codigo = linha_para_codigo_barras(parsed)
        if not codigo or not validar_codigo_barras(codigo):
            result.validation_passed = False
            result.needs_review = True
            result.discrepancies.append("Código de barras inválido (módulo 11)")
            return False

        result.codigo_barras = codigo
        result.linha_digitavel = parsed.raw
        result.validation_passed = True

        codigo_valor = extrair_valor_codigo_barras(codigo)
        valor_efetivo = is_valor_confirmado_codigo_barras(codigo)
        if valor_efetivo and codigo_valor is not None and codigo_valor > 0:
            if result.valor is None:
                result.valor = codigo_valor
                result.valor_confirmado = True
            elif abs(codigo_valor - result.valor) > 1:
                result.valor_confirmado = False
                result.needs_review = True
                result.discrepancies.append(
                    f"Divergência de valor: documento={formatar_centavos(result.valor)} "
                    f"vs código={formatar_centavos(codigo_valor)}"
                )
            else:
                result.valor_confirmado = True
        else:
            result.valor_confirmado = False

        fator = extrair_fator_vencimento(codigo)
        if fator and fator > 0:
            data_codigo = fator_para_data_ou_none(fator)
            if is_fator_ambiguo(fator) and not result.vencimento:
                result.needs_review = True
                result.discrepancies.append(
                    f"Fator de vencimento ambíguo ({fator}) — confirmar data"
                )
            if data_codigo:
                if not result.vencimento:
                    result.vencimento = data_codigo.isoformat()
                else:
                    try:
                        data_ext = date.fromisoformat(result.vencimento)
                        if data_ext != data_codigo:
                            result.needs_review = True
                            result.discrepancies.append(
                                f"Divergência de vencimento: "
                                f"documento={data_ext.strftime('%d/%m/%Y')} "
                                f"vs código={data_codigo.strftime('%d/%m/%Y')}"
                            )
                    except ValueError:
                        result.needs_review = True
                        result.discrepancies.append(
                            "Data de vencimento extraída em formato inválido"
                        )

        if result.vencimento is None:
            result.needs_review = True
            result.discrepancies.append("Vencimento não identificado")

        if result.confidence < self.config.min_confidence:
            result.needs_review = True

        return True

    def _validate_result(self, result: ExtractionResult) -> bool:
        """Compatibilidade retroativa com chamadas internas/testes."""
        return self._validate_and_crosscheck(result)

    def _select_best_result(self, results: list[ExtractionResult]) -> ExtractionResult:
        if not results:
            return ExtractionResult(confidence=0.0)

        clean_valid = [r for r in results if r.validation_passed and not r.discrepancies]
        if clean_valid:
            clean_valid.sort(key=lambda r: r.confidence, reverse=True)
            return clean_valid[0]

        valid_dv = [r for r in results if r.validation_passed]
        if valid_dv:
            valid_dv.sort(key=lambda r: r.confidence, reverse=True)
            return valid_dv[0]

        results.sort(key=lambda r: r.confidence, reverse=True)
        return results[0]


def create_pipeline(config: ExtractionConfig | None = None) -> ExtractionPipeline:
    """Factory para criar pipeline de extração."""
    return ExtractionPipeline(config)


def extract_boleto(
    file_path: str | Path, config: ExtractionConfig | None = None
) -> ExtractionResult:
    """Função de conveniência para extrair boleto."""
    pipeline = create_pipeline(config)
    return pipeline.extract(file_path)
