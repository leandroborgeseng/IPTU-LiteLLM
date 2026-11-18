"""Relatório de conferência humana a partir dos pareceres atuais."""
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

import historico


COLUMNS = [
    "Processo SEI", "Requerente", "CPF", "Inscrição imobiliária", "Exercício",
    "Modalidade", "Renda familiar (R$)", "Conclusão", "Situação", "Tipo de parecer",
    "Código do tipo", "Fundamento", "Pendências", "Conferências cadastrais",
    "Versão", "Servidor que decidiu", "Justificativa da decisão humana",
    "Parecer individual", "Concordância do conferente", "Observação do conferente",
    "Servidor conferente", "Data da conferência",
]


def spreadsheet(lote_id="", q=""):
    _, opinions = historico.opinion_export(lote_id)
    by_process = {(row["lote_id"], row["processo_id"]): row for row in opinions}
    records, _, _ = historico.search(lote_id=lote_id, q=q, limite=100000, completo=True,
                                    ordem="processo", direcao="asc")
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Resumo"
    generated = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y %H:%M")
    counts = {"deferido": 0, "indeferido": 0}
    sheets = {}
    for title, decision in (("Aceitos", "deferido"), ("Negados", "indeferido")):
        sheet = workbook.create_sheet(title)
        sheet.append(COLUMNS)
        sheet.freeze_panes = "B2"
        sheet.sheet_view.zoomScale = 85
        sheet.print_title_rows = "1:1"
        validation = DataValidation(type="list", formula1='"Concordo,Discordo,Complementar"', allow_blank=True)
        sheet.add_data_validation(validation)
        validation.add("S2:S1048576")
        sheets[decision] = sheet
    for record in records:
        opinion = by_process.get((record["lote_id"], record["processo_id"]))
        if not opinion or record["resultado"] == "processando":
            continue
        document = record.get("parecer") or {}
        report = document.get("parecer") or {}
        person = report.get("resumo") or {}
        human = (document.get("conferencia_manual") or {}).get("decisao") or {}
        decision = opinion["conclusao"]
        amount = person.get("renda_familiar_centavos")
        row = [
            record.get("sei") or record["processo"], person.get("requerente"), person.get("cpf"),
            person.get("inscricao_imobiliaria"), report.get("exercicio"), report.get("modalidade"),
            amount / 100 if type(amount) is int else None,
            "Deferido" if decision == "deferido" else "Indeferido", opinion["situacao"],
            opinion["tipo"], opinion["codigo_tipo"],
            "\n".join(opinion["motivos"]) or "Requisitos documentais atendidos.",
            "\n".join(opinion["pendencias"]), "\n".join(opinion["conferencias_cadastrais"]),
            record.get("versao"), human.get("usuario"), human.get("justificativa"),
            report.get("parecer_texto"), "", "", "", "",
        ]
        sheet = sheets[decision]
        sheet.append(row)
        # Todos os textos são literais, inclusive valores que iniciem com "=".
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.value = ILLEGAL_CHARACTERS_RE.sub("", cell.value)
                cell.data_type = "s"
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        sheet.cell(sheet.max_row, 7).number_format = '#,##0.00'
        counts[decision] += 1
    summary.append(["Relatório para conferência de isenção de IPTU"])
    summary.append(["Gerado em (horário de São Paulo)", generated])
    summary.append(["Lote", lote_id or "Todos"])
    summary.append(["Busca", q or "Todos"])
    summary.append(["Aceitos", counts["deferido"]])
    summary.append(["Negados", counts["indeferido"]])
    summary.append(["Situação", "A coluna Situação identifica propostas automáticas e decisões humanas registradas."])
    summary.append(["Conferência cadastral", "Consulte as ressalvas individuais antes de concluir a revisão."])
    summary.append(["Escopo", "Inclui aceitos e negados do lote e da busca selecionados."])
    summary.append(["Tipos de parecer", "Use o código do tipo para comparar exportações. A numeração depende do lote."])
    widths = {1: 32, 2: 30, 3: 20, 4: 25, 5: 12, 6: 22, 7: 18, 8: 18, 9: 44,
              10: 20, 11: 22, 12: 65, 13: 50, 14: 65, 15: 12, 16: 25, 17: 60,
              18: 90, 19: 25, 20: 60, 21: 28, 22: 22}
    from openpyxl.utils import get_column_letter
    for sheet in sheets.values():
        sheet.auto_filter.ref = sheet.dimensions
        for index, width in widths.items():
            sheet.column_dimensions[get_column_letter(index)].width = width
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="23445B")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        sheet.row_dimensions[1].height = 32
    summary.column_dimensions["A"].width = 35
    summary.column_dimensions["B"].width = 100
    for row in summary:
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            if isinstance(cell.value, str):
                cell.value = ILLEGAL_CHARACTERS_RE.sub("", cell.value)
                cell.data_type = "s"
    summary["A1"].font = Font(size=16, bold=True)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
