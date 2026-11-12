"""Proposta auditável para o exercício de 2027 em Franca."""

EXERCICIO = 2027
TETO_CENTAVOS = 324200
FONTE_TETO = "https://www3.franca.sp.gov.br/noticia/39898/prazo-para-solicitar-reduo-de-10-no-iptu-termina-nesta-quarta-feira.html"
FONTE_CRITERIOS = "https://www3.franca.sp.gov.br/cartadeservicos/servicos/iseno-de-iptu-para-aposentados-pensionistas-e-beneficirios-do-bpcloas"
CONFIRMACOES = {
    "identidade": "Identidade, composição familiar e representação",
    "beneficio": "Titularidade de benefício elegível",
    "imovel": "Vínculo com o imóvel e unicidade conferidos no cadastro",
    "residencia": "Residência no imóvel confirmada",
    "edícula": "Edícula ausente ou com uso enquadrado",
    "prazo": "Data oficial do protocolo e prazo conferidos",
}


def evidence(row, value):
    return {"arquivo": row["arquivo"], "origem": row["origem"], "pagina": row["pagina"], "valor": value}


def assess(process, review):
    rules = []

    def add(key, status, reason, sources=None):
        rules.append({"criterio": key, "status": status, "motivo": reason, "evidencias": sources or []})

    forms = process["requerimentos"]
    pages = process["paginas"]
    confirmed = (review or {}).get("confirmacoes", {})
    facts = {}
    for row in forms:
        for field, value in row.get("obtido", {}).items():
            if value is not None:
                facts.setdefault(field, []).append(evidence(row, value))

    def unique(field):
        values = {item["valor"] for item in facts.get(field, [])}
        return next(iter(values)) if len(values) == 1 else None

    income = []
    for row in pages:
        data = row.get("obtido", {})
        if row.get("tipo") in {"beneficio", "holerite"} and type(data.get("renda_bruta_centavos")) is int:
            income.append(evidence(row, {key: data.get(key) for key in (
                "renda_bruta_centavos", "competencia_renda", "titular_renda",
            )}))

    exercise = unique("exercicio")
    modality = unique("modalidade_criterio")
    complete = process["status"] == "em_revisao"
    eligible_modality = modality in {"aposentadoria", "pensao", "bpc", "renda_mensal_vitalicia"}
    ordinary = (complete and exercise == EXERCICIO and eligible_modality
                and bool(process.get("numero_processo_sei")) and not process.get("sei_divergente"))
    if not complete:
        add("processamento", "pendente", "A leitura do processo ainda está em andamento.")
    if not process.get("numero_processo_sei"):
        add("processo_sei", "pendente", "Número do SEI ausente ou divergente entre nomes e pastas.")
    if exercise != EXERCICIO:
        add("exercicio", "pendente", "Confirme se o exercício solicitado é 2027.", facts.get("exercicio"))
    if modality in {"pcd", "doenca_grave", "neoplasia"}:
        add("modalidade", "pendente", "Encaminhar à assistência social.", facts.get("modalidade_criterio"))
    elif not eligible_modality:
        add("modalidade", "pendente", "Modalidade do pedido ausente, divergente ou fora do fluxo.", facts.get("modalidade_criterio"))

    residence = unique("reside_no_imovel")
    if residence is False:
        add("residencia", "impedimento" if ordinary else "pendente",
            "O requerimento indica que o solicitante não reside no imóvel.", facts["reside_no_imovel"])
    elif residence is not True and "residencia" in confirmed:
        add("residencia", "pendente", "Confirme a resposta de residência no requerimento.", facts.get("reside_no_imovel"))

    for key, label in CONFIRMACOES.items():
        if key == "residencia" and (residence is False or (residence is not True and key in confirmed)):
            continue
        if key == "edícula":
            sources = [item for field in ("edicula_alugada", "edicula_uso_comercial")
                       for item in facts.get(field, []) if item["valor"] is True]
            if sources:
                add(key, "pendente", "Há indicação de aluguel ou comércio na edícula. Confirme o efeito sobre o pedido.", sources)
                continue
        source = confirmed.get(key)
        add(key, "atendido" if source else "pendente", label,
            [{"referencia_conferencia": source}] if source else facts.get("reside_no_imovel") if key == "residencia" else None)

    amount = (review or {}).get("renda_bruta_familiar_centavos")
    income_source = (review or {}).get("fonte_renda")
    if type(amount) is int and income_source and exercise == EXERCICIO:
        above = amount > TETO_CENTAVOS
        add("renda", ("impedimento" if ordinary else "pendente") if above else "atendido",
            "Renda bruta familiar conferida acima do teto." if above else "Renda bruta familiar conferida dentro do teto.",
            [{"referencia_conferencia": income_source, "valor_centavos": amount}])
    else:
        add("renda", "pendente", "Confira a renda familiar e registre o total bruto mensal com a fonte.")
    if any(row["status"] != "extraido" for row in pages) or any(
        doc["erro"] and doc["erro"] != "formato_nao_processado" for doc in process["arquivos"]
    ):
        add("leitura", "pendente", "Há arquivos ou páginas que precisam de conferência.")

    blockers = [row["motivo"] for row in rules if row["status"] == "impedimento"]
    pending = [row["motivo"] for row in rules if row["status"] == "pendente"]
    status = "proposta_indeferimento" if blockers else "em_analise" if pending else "proposta_deferimento"
    return {"status": status, "pode_receber_isencao": False if blockers else None if pending else True,
            "motivos_indeferimento": blockers, "pendencias": pending, "criterios": rules,
            "numero_processo_sei": process.get("numero_processo_sei"),
            "fontes_numero_sei": process.get("fontes_numero_sei", []),
            "sei_divergente": process.get("sei_divergente", False),
            "exercicio": exercise, "modalidade": modality, "fatos_do_requerimento": facts,
            "comprovantes_de_renda_lidos": income, "renda_bruta_familiar_centavos": amount,
            "teto_centavos": TETO_CENTAVOS if exercise == EXERCICIO else None,
            "fontes": {"criterios": FONTE_CRITERIOS, "teto": FONTE_TETO}}
