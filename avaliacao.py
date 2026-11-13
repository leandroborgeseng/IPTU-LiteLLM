"""Mede a assertividade de um lote comparando com um gabarito conferido por servidor.

Gabarito (JSON), chave = número SEI do processo:
{
  "3516200.410.00010886-2026-74": {
    "resultado": "enquadra",            # enquadra | nao_enquadra | em_analise | fora_do_fluxo
    "renda_familiar_centavos": 162100,  # opcional
    "campos": {"estado_civil": "solteiro", "reside_no_imovel": true, "possui_edicula": false,
               "modalidade": "aposentadoria"}  # opcional; qualquer campo do requerimento
  }
}
"enquadra_conferir_cadastro" conta como "enquadra" (a diferença é só a conferência cadastral).
"""
import batch
import leitura


def _same(field, expected, got):
    if field == "estado_civil":
        return leitura.to_value("estado", expected) == leitura.to_value("estado", got)
    if field.endswith("_numero") or field.startswith("cpf"):
        return leitura.to_value("digitos", expected) == leitura.to_value("digitos", got)
    return expected == got


def evaluate(path, answer_key):
    rows, totals = [], {"processos": 0, "resultado_ok": 0, "renda_ok": 0, "renda_total": 0,
                        "campos_ok": 0, "campos_total": 0}
    manifest = batch.load(path / "manifest.json") if (path / "manifest.json").exists() else []
    by_sei = {}
    for item in manifest:
        detail = batch.process_detail(path, item["id"])
        by_sei[detail.get("numero_processo_sei") or item["processo"]] = detail
    for sei, expected in answer_key.items():
        detail = by_sei.get(sei)
        row = {"processo": sei, "esperado": expected.get("resultado")}
        if detail is None:
            row["obtido"] = "ausente_no_lote"
            rows.append(row)
            totals["processos"] += 1
            continue
        report = detail["parecer"]
        got = report["resultado"].replace("enquadra_conferir_cadastro", "enquadra")
        row.update(obtido=got, sintese=report["resumo"].get("sintese"),
                   motivo=(report["motivos_indeferimento"] or report["pendencias"] or [""])[0])
        totals["processos"] += 1
        totals["resultado_ok"] += got == expected.get("resultado")
        if "renda_familiar_centavos" in expected:
            totals["renda_total"] += 1
            row["renda_esperada"] = expected["renda_familiar_centavos"]
            row["renda_obtida"] = report["resumo"]["renda_familiar_centavos"]
            totals["renda_ok"] += row["renda_esperada"] == row["renda_obtida"]
        facts = report["fatos_do_requerimento"]
        wrong = []
        for field, value in (expected.get("campos") or {}).items():
            totals["campos_total"] += 1
            if field == "modalidade":
                found = report["modalidade"]
            else:
                values = {item["valor"] for item in facts.get(field, [])}
                found = next(iter(values)) if len(values) == 1 else None
            if _same(field, value, found):
                totals["campos_ok"] += 1
            else:
                wrong.append({"campo": field, "esperado": value, "obtido": found})
        row["campos_divergentes"] = wrong
        rows.append(row)
    pct = lambda ok, total: round(100 * ok / total, 1) if total else None  # noqa: E731
    return {"resumo": {**totals,
                       "acerto_resultado_pct": pct(totals["resultado_ok"], totals["processos"]),
                       "acerto_renda_pct": pct(totals["renda_ok"], totals["renda_total"]),
                       "acerto_campos_pct": pct(totals["campos_ok"], totals["campos_total"])},
            "processos": rows}
