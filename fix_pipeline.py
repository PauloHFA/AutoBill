with open('autobill/extract/pipeline.py', 'r') as f:
    content = f.read()

# Fix the _validate_result method - remove extra blank lines and fix indentation
old = """    def _validate_result(self, result: ExtractionResult) -> bool:

        \"\"\"Valida resultado usando regras de boleto.\"\"\"

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


            return True"""

new = """    def _validate_result(self, result: ExtractionResult) -> bool:
        \"\"\"Valida resultado usando regras de boleto.\"\"\"
        if not result.linha_digitavel:
            return False

        # Valida linha digitável
        if not validar_linha_digitavel(result.linha_digitavel):
            return False

        # Se tem código de barras, valida também
        if result.codigo_barras and not validar_codigo_barras(result.codigo_barras):
            return False

        # Verifica consistência valor/vencimento se ambos presentes
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

        return True"""

if old in content:
    content = content.replace(old, new)
    with open('autobill/extract/pipeline.py', 'w') as f:
        f.write(content)
    print('Fixed _validate_result')
else:
    print('Pattern not found')