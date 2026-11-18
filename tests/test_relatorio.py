"""Relatório Excel preserva decisões, ressalvas e conteúdo literal."""
import io
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook
import historico
import relatorio


class ConferenciaExcel(unittest.TestCase):
    def test_abas_e_fundamentos_por_processo(self):
        with tempfile.TemporaryDirectory() as directory:
            historico.init(Path(directory) / "db.sqlite")
            for key, outcome, decision, status in (
                ("A", "enquadra_conferir_cadastro", "deferido", "cadastral"),
                ("B", "nao_enquadra", "indeferido", "impedimento"),
                ("C", "em_analise", "pendente", "pendente"),
            ):
                body = {"parecer": {
                    "resultado": outcome, "exercicio": 2027, "modalidade": "pensao",
                    "resumo": {"requerente": "=NOME", "renda_familiar_centavos": 162100, "cpf": "01234567890"},
                    "criterios": [{"criterio": "renda", "status": status, "motivo": "Fundamento individual"}],
                    "conferencias_cadastrais": ["Conferir SIL"] if key == "A" else [],
                    "parecer_texto": "Parecer completo",
                }}
                historico.upsert("L", key, "Lote", key,
                                 {"resultado": outcome, "decisao": decision, "requerente": "=NOME"},
                                 body, "sig", "teste")
            workbook = load_workbook(io.BytesIO(relatorio.spreadsheet("L")))
            self.assertEqual(workbook.sheetnames, ["Resumo", "Aceitos", "Negados"])
            for title in ("Aceitos", "Negados"):
                sheet = workbook[title]
                self.assertEqual(sheet.max_row, 2)
                self.assertEqual(sheet["B2"].data_type, "s")
                self.assertEqual(sheet["B2"].value, "=NOME")
                self.assertEqual(sheet["C2"].value, "01234567890")
                self.assertEqual(sheet["G2"].value, 1621)
                self.assertEqual(sheet["R2"].value, "Parecer completo")
                self.assertIsNone(sheet["S2"].value)
            self.assertEqual(workbook["Aceitos"]["N2"].value, "Conferir SIL")
            self.assertEqual(workbook["Negados"]["L2"].value, "Fundamento individual")
