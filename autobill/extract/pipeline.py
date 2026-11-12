"""Pipeline de extraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o orquestrado com mÃÂÃÂÃÂÃÂºltiplas estratÃÂÃÂÃÂÃÂ©gias."""















from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from autobill.extract.barcode import (
    extract_barcode_from_image,
    extract_barcode_from_pdf,
    is_zxing_available,
)
from autobill.extract.llm_vision import (
    extract_with_llm_vision,
    is_genai_available,
)
from autobill.extract.ocr import (
    extract_text_with_ocr,
    extract_text_with_ocr_from_pdf,
    is_tesseract_available,
)
from autobill.extract.pdf_text import (
    BoletoFields,
    extract_boleto_fields_from_text,
    extract_boleto_from_pdf,
)
from autobill.rules.boleto import (
    ARRECADACAO_LENGTH,
    BOLETO_LENGTH,
    CODIGO_BARRAS_LENGTH,
    codigo_barras_para_linha,
    extrair_fator_vencimento,
    extrair_valor_codigo_barras,
    linha_para_codigo_barras,
    parse_linha_digitavel,
    validar_codigo_barras,
    validar_linha_digitavel,
)
from autobill.rules.due_date import fator_para_data_auto
from autobill.rules.money import formatar_centavos


class ExtractionMethod(Enum):







    """MÃÂÃÂÃÂÃÂ©todos de extraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o disponÃÂÃÂÃÂÃÂ­veis."""







    PDF_TEXT = "pdf_text"







    BARCODE = "barcode"







    LLM_VISION = "llm_vision"







    OCR = "ocr"























@dataclass







class ExtractionResult:







    """Resultado consolidado da extraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o."""







    # Campos principais







    linha_digitavel: str | None = None







    codigo_barras: str | None = None







    valor: int | None = None  # centavos







    vencimento: str | None = None  # ISO format







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















    # Metadados







    method_used: ExtractionMethod | None = None







    confidence: float = 0.0







    validation_passed: bool = False







    raw_data: dict = field(default_factory=dict)















    def to_dict(self) -> dict:







        """Converte para dicionÃÂÃÂÃÂÃÂ¡rio."""







        return {







            "linha_digitavel": self.linha_digitavel,







            "codigo_barras": self.codigo_barras,







            "valor": self.valor,







            "valor_formatado": formatar_centavos(self.valor) if self.valor else None,







            "vencimento": self.vencimento,







            "beneficiario": self.beneficiario,







            "pagador": self.pagador,







            "nosso_numero": self.nosso_numero,







            "documento": self.documento,







            "data_documento": self.data_documento,







            "especie": self.especie,







            "aceite": self.aceite,







            "data_processamento": self.data_processamento,







            "instrucoes": self.instrucoes,







            "local_pagamento": self.local_pagamento,







            "method_used": self.method_used.value if self.method_used else None,







            "confidence": self.confidence,







            "validation_passed": self.validation_passed,







        }















    def is_valid(self) -> bool:







        """Verifica se a extraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o tem dados mÃÂÃÂÃÂÃÂ­nimos vÃÂÃÂÃÂÃÂ¡lidos."""







        return (







            self.validation_passed







            and self.linha_digitavel is not None







            and self.valor is not None







            and self.vencimento is not None







        )























@dataclass







class ExtractionConfig:







    """ConfiguraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o do pipeline de extraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o."""







    # Ordem de preferÃÂÃÂÃÂÃÂªncia dos mÃÂÃÂÃÂÃÂ©todos







    method_order: list[ExtractionMethod] = field(default_factory=lambda: [







        ExtractionMethod.PDF_TEXT,







        ExtractionMethod.BARCODE,







        ExtractionMethod.LLM_VISION,







        ExtractionMethod.OCR,







    ])















    # ConfianÃÂÃÂÃÂÃÂ§a mÃÂÃÂÃÂÃÂ­nima para aceitar resultado







    min_confidence: float = 0.5















    # Se deve validar com regras de boleto







    validate_boleto: bool = True















    # Se deve tentar mÃÂÃÂÃÂÃÂºltiplos mÃÂÃÂÃÂÃÂ©todos e escolher o melhor







    try_all_methods: bool = True















    # ConfiguraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂµes especÃÂÃÂÃÂÃÂ­ficas







    llm_model: str = "gemini-2.0-flash-exp"







    ocr_lang: str = "por"







    pdf_page_num: int = 0























class ExtractionPipeline:







    """Pipeline orquestrado de extraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o de boletos."""















    def __init__(self, config: ExtractionConfig | None = None):







        self.config = config or ExtractionConfig()















    def extract(self, file_path: str | Path) -> ExtractionResult:







        """







        Extrai dados de boleto de um arquivo.















        Tenta mÃÂÃÂÃÂÃÂ©todos na ordem configurada atÃÂÃÂÃÂÃÂ© obter resultado vÃÂÃÂÃÂÃÂ¡lido







        ou esgotar opÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂµes.















        Args:







            file_path: Caminho para PDF ou imagem















        Returns:







            ExtractionResult com dados extraÃÂÃÂÃÂÃÂ­dos







        """







        file_path = Path(file_path)







        if not file_path.exists():







            return ExtractionResult(







                confidence=0.0,







                raw_data={"error": "File not found"},







            )















        results = []















        for method in self.config.method_order:







            try:







                result = self._extract_with_method(file_path, method)







                if result:







                    results.append(result)















                    # Se nÃÂÃÂÃÂÃÂ£o deve tentar todos, para no primeiro vÃÂÃÂÃÂÃÂ¡lido







                    if not self.config.try_all_methods and result.is_valid():







                        return result















            except Exception as e:







                # Log do erro mas continua







                results.append(ExtractionResult(







                    method_used=method,







                    confidence=0.0,







                    raw_data={"error": str(e)},







                ))















        # Escolhe o melhor resultado







        return self._select_best_result(results)















    def _extract_with_method(







        self,







        file_path: Path,







        method: ExtractionMethod







    ) -> ExtractionResult | None:







        """Executa extraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o com um mÃÂÃÂÃÂÃÂ©todo especÃÂÃÂÃÂÃÂ­fico."""















        if method == ExtractionMethod.PDF_TEXT:







            return self._extract_pdf_text(file_path)















        if method == ExtractionMethod.BARCODE:







            return self._extract_barcode(file_path)















        if method == ExtractionMethod.LLM_VISION:







            return self._extract_llm_vision(file_path)















        if method == ExtractionMethod.OCR:







            return self._extract_ocr(file_path)















        return None















    def _extract_pdf_text(self, file_path: Path) -> ExtractionResult | None:







        """ExtraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o via texto do PDF."""







        if file_path.suffix.lower() != ".pdf":







            return None















        fields = extract_boleto_from_pdf(file_path)















        # Converte para ExtractionResult







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















        # Valida se configurado







        if self.config.validate_boleto:







            result.validation_passed = self._validate_result(result)















        return result















    def _extract_barcode(self, file_path: Path) -> ExtractionResult | None:







        """ExtraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o via leitura de cÃÂÃÂÃÂÃÂ³digo de barras."""







        if not is_zxing_available():







            return None















        barcode = None







        if file_path.suffix.lower() == ".pdf":







            barcode = extract_barcode_from_pdf(file_path)







        else:







            barcode = extract_barcode_from_image(file_path)















        if not barcode:







            return None















        # # Converte barcode para linha digitÃÂÃÂ¡vel se necessÃÂÃÂ¡rio







        linha_digitavel = None







        codigo_barras = None















        if len(barcode) == CODIGO_BARRAS_LENGTH:







            codigo_barras = barcode







            linha_digitavel = codigo_barras_para_linha(barcode)







        elif len(barcode) in (BOLETO_LENGTH, ARRECADACAO_LENGTH):







            linha_digitavel = barcode







            parsed = parse_linha_digitavel(barcode)







            if parsed:







                codigo_barras = linha_para_codigo_barras(parsed)







        # Extrai valor e vencimento do cÃÂÃÂÃÂÃÂ³digo de barras







        valor = None







        vencimento = None















        if codigo_barras:







            valor = extrair_valor_codigo_barras(codigo_barras)







            fator = extrair_fator_vencimento(codigo_barras)







            if fator:







                vencimento = fator_para_data_auto(fator).isoformat()















        result = ExtractionResult(







            linha_digitavel=linha_digitavel,







            codigo_barras=codigo_barras,







            valor=valor,







            vencimento=vencimento,







            method_used=ExtractionMethod.BARCODE,







            confidence=0.9 if codigo_barras and valor and vencimento else 0.6,







        )















        if self.config.validate_boleto:







            result.validation_passed = self._validate_result(result)















        return result















    def _extract_llm_vision(self, file_path: Path) -> ExtractionResult | None:







        """ExtraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o via LLM Vision."""







        if not is_genai_available():







            return None















        llm_result = extract_with_llm_vision(







            file_path,







            model=self.config.llm_model,







            page_num=self.config.pdf_page_num,







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







            result.validation_passed = self._validate_result(result)















        return result















    def _extract_ocr(self, file_path: Path) -> ExtractionResult | None:







        """ExtraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o via OCR local."""







        if not is_tesseract_available():







            return None















        text = ""







        if file_path.suffix.lower() == ".pdf":







            text = extract_text_with_ocr_from_pdf(file_path, lang=self.config.ocr_lang)







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







            result.validation_passed = self._validate_result(result)















        return result















    def _calculate_pdf_text_confidence(self, fields: BoletoFields) -> float:







        """Calcula confianÃÂÃÂÃÂÃÂ§a baseada nos campos extraÃÂÃÂÃÂÃÂ­dos via PDF text."""







        score = 0.0







        total = 0















        # Campos principais (peso maior)







        if fields.linha_digitavel:







            score += 0.4







        total += 0.4















        if fields.valor is not None:







            score += 0.2







        total += 0.2















        if fields.vencimento:







            score += 0.2







        total += 0.2















        # Campos secundÃÂÃÂÃÂÃÂ¡rios







        if fields.beneficiario:







            score += 0.1







        total += 0.1















        if fields.pagador:







            score += 0.1







        total += 0.1















        return score / total if total > 0 else 0.0















    def _calculate_ocr_confidence(self, fields: BoletoFields) -> float:







        """Calcula confianÃÂÃÂÃÂÃÂ§a para OCR (geralmente menor)."""







        base = self._calculate_pdf_text_confidence(fields)







        return base * 0.7  # OCR ÃÂÃÂÃÂÃÂ© menos confiÃÂÃÂÃÂÃÂ¡vel















    def _validate_result(self, result: ExtractionResult) -> bool:



        """Valida resultado usando regras de boleto."""

        if not result.linha_digitavel:

            return False



        # Valida linha digitÃ¡vel

        if not validar_linha_digitavel(result.linha_digitavel):

            return False



        # Se tem cÃ³digo de barras, valida tambÃ©m

        if result.codigo_barras and not validar_codigo_barras(result.codigo_barras):

            return False



        # Verifica consistÃªncia valor/vencimento se ambos presentes

        if (

            result.valor is not None

            and result.codigo_barras

        ):

            codigo_valor = extrair_valor_codigo_barras(result.codigo_barras)

            if (

                codigo_valor is not None

                and codigo_valor != result.valor

                and abs(codigo_valor - result.valor) > 1

            ):

                return False



        return True









    def _select_best_result(self, results: list[ExtractionResult]) -> ExtractionResult:







        """Seleciona o melhor resultado entre os mÃÂÃÂÃÂÃÂ©todos."""







        if not results:







            return ExtractionResult(confidence=0.0)















        # Filtra apenas vÃÂÃÂÃÂÃÂ¡lidos







        valid_results = [r for r in results if r.is_valid()]















        if valid_results:







            # Ordena por confianÃÂÃÂÃÂÃÂ§a







            valid_results.sort(key=lambda r: r.confidence, reverse=True)







            return valid_results[0]















        # Se nenhum vÃÂÃÂÃÂÃÂ¡lido, retorna o de maior confianÃÂÃÂÃÂÃÂ§a







        results.sort(key=lambda r: r.confidence, reverse=True)







        return results[0]























def create_pipeline(config: ExtractionConfig | None = None) -> ExtractionPipeline:







    """Factory para criar pipeline de extraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o."""







    return ExtractionPipeline(config)























def extract_boleto(







    file_path: str | Path,







    config: ExtractionConfig | None = None







) -> ExtractionResult:







    """







    FunÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o de conveniÃÂÃÂÃÂÃÂªncia para extrair boleto.















    Args:







        file_path: Caminho para PDF ou imagem







        config: ConfiguraÃÂÃÂÃÂÃÂ§ÃÂÃÂÃÂÃÂ£o opcional do pipeline















    Returns:







        ExtractionResult com dados extraÃÂÃÂÃÂÃÂ­dos







    """







    pipeline = create_pipeline(config)







    return pipeline.extract(file_path)







