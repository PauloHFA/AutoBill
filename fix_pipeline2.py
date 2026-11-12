with open('autobill/extract/pipeline.py', 'r') as f:
    lines = f.readlines()

# Replace lines 760-815 with fixed version
new_lines = lines[:760] + [
    '    def _validate_result(self, result: ExtractionResult) -> bool:\n',
    '        """Valida resultado usando regras de boleto."""\n',
    '        if not result.linha_digitavel:\n',
    '            return False\n',
    '\n',
    '        # Valida linha digitável\n',
    '        if not validar_linha_digitavel(result.linha_digitavel):\n',
    '            return False\n',
    '\n',
    '        # Se tem código de barras, valida também\n',
    '        if result.codigo_barras and not validar_codigo_barras(result.codigo_barras):\n',
    '            return False\n',
    '\n',
    '        # Verifica consistência valor/vencimento se ambos presentes\n',
    '        if (\n',
    '            result.valor is not None\n',
    '            and result.codigo_barras\n',
    '        ):\n',
    '            codigo_valor = extrair_valor_codigo_barras(result.codigo_barras)\n',
    '            if (\n',
    '                codigo_valor is not None\n',
    '                and codigo_valor != result.valor\n',
    '                and abs(codigo_valor - result.valor) > 1\n',
    '            ):\n',
    '                return False\n',
    '\n',
    '        return True\n',
    '\n',
] + lines[815:]

with open('autobill/extract/pipeline.py', 'w') as f:
    f.writelines(new_lines)
print('Fixed _validate_result method')