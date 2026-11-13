"""Parecer auditável para o exercício de 2027 em Franca (LC 456/2025).

Cada critério recebe um status:

- atendido: comprovado pelos documentos do processo ou por conferência registrada.
- impedimento: condição de indeferimento da LC 456/2025 comprovada.
- pendente: os documentos não permitem decidir; precisa de conferência humana.
- cadastral: depende de consulta ao cadastro da SIL ou ao SEI/F4. Não impede a
  proposta de deferimento documental, mas fica listado para conferência.
- aviso: informação administrativa que não afeta o enquadramento.
"""

import os
import re
import unicodedata

# Parâmetros do exercício (variáveis de ambiente; padrão: exercício 2027, salário mínimo de R$ 1.621,00).
EXERCICIO = int(os.environ.get("IPTU_EXERCICIO", "2027"))
SALARIO_MINIMO_CENTAVOS = int(os.environ.get("IPTU_SALARIO_MINIMO_CENTAVOS", "162100"))
TETO_CENTAVOS = 2 * SALARIO_MINIMO_CENTAVOS  # LC 456/2025: até 2 salários mínimos
PRAZO_INICIO = os.environ.get("IPTU_PRAZO_INICIO", "")  # AAAA-MM-DD (opcional)
PRAZO_FIM = os.environ.get("IPTU_PRAZO_FIM", "")
FONTE_TETO = "https://www3.franca.sp.gov.br/noticia/39898/prazo-para-solicitar-reduo-de-10-no-iptu-termina-nesta-quarta-feira.html"
FONTE_CRITERIOS = "https://www3.franca.sp.gov.br/cartadeservicos/servicos/iseno-de-iptu-para-aposentados-pensionistas-e-beneficirios-do-bpcloas"
CONFIRMACOES = {
    "identidade": "Identidade, composição familiar e representação",
    "beneficio": "Titularidade de benefício elegível",
    "imovel": "Vínculo com o imóvel e unicidade conferidos no cadastro",
    "residencia": "Residência no imóvel confirmada",
    "edícula": "Edícula ausente ou com uso enquadrado",
    "prazo": "Data oficial do protocolo e prazo conferidos",
    "mobiliario": "Cadastro Mobiliário conferido",
}
ELEGIVEIS = {"aposentadoria", "pensao", "bpc", "renda_mensal_vitalicia"}
SOCIAIS = {"pcd", "doenca_grave", "neoplasia"}
SEM_CONJUGE = {"solteiro", "solteira", "viuvo", "viuva", "divorciado", "divorciada",
               "separado", "separada", "separado_judicialmente", "separada_judicialmente"}
BENEFICIOS_DECLARADOS = ("aposentadoria", "pensao", "bpc")
NOMES_MODALIDADE = {"aposentadoria": "aposentadoria", "pensao": "pensão", "bpc": "BPC",
                    "renda_mensal_vitalicia": "renda mensal vitalícia", "pcd": "PCD",
                    "doenca_grave": "doença grave", "neoplasia": "neoplasia"}


def modality_label(modality):
    return " e ".join(NOMES_MODALIDADE.get(part, part) for part in (modality or "").split("+")) or None


def fold(value):
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(character for character in text if not unicodedata.combining(character))


def normalize_modality(value):
    key = "_".join(fold(value).replace("-", " ").replace("_", " ").split())
    if key.startswith(("pensao", "pensionista")):
        return "pensao"
    if key.startswith("aposentad"):
        return "aposentadoria"
    if key.startswith(("bpc", "loas", "beneficio_de_prestacao")):
        return "bpc"
    return key


def modality_parts(value):
    """Uma requerente pode ser aposentada e pensionista ao mesmo tempo."""
    parts = re.split(r"[,;/+]|\s+e\s+", value if isinstance(value, str) else "")
    return {normalize_modality(part) for part in parts if part.strip()}


def money(cents):
    return "R$ " + f"{cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def person_key(name):
    return "".join(character for character in fold(name) if character.isalpha())


PARTICULAS = {"de", "da", "do", "das", "dos", "e", "d"}


def name_tokens(name):
    words = re.findall(r"[a-z]+", fold(name).replace("'", ""))
    return [word for word in words if word not in PARTICULAS]


def same_person(a, b):
    """Compara nomes tolerando abreviações (NEUSA C M SICCHIEROLLI) e partículas (D'ARC/DARC)."""
    if not a or not b:
        return False
    if person_key(a) == person_key(b):
        return True
    x, y = name_tokens(a), name_tokens(b)
    if len(x) < 2 or len(y) < 2 or x[0] != y[0] or x[-1] != y[-1]:
        return False
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    rest = iter(long_[1:-1])
    for word in short[1:-1]:
        if not any(other == word or (len(word) == 1 and other.startswith(word))
                   or (len(other) == 1 and word.startswith(other)) for other in rest):
            return False
    return True


def digits(value):
    return re.sub(r"\D", "", str(value or ""))


def page_text(row):
    return " ".join(fold(row.get(key) or "") for key in ("texto_ocr", "texto_pdf"))


def evidence(row, value):
    return {"arquivo": row["arquivo"], "origem": row["origem"], "pagina": row["pagina"], "valor": value}


def where(rows):
    return ", ".join(f"p. {row['pagina']}" for row in rows)


def assess(process, review, cadastro=None):
    rules = []

    def add(key, status, reason, sources=None):
        rules.append({"criterio": key, "status": status, "motivo": reason, "evidencias": sources or []})

    forms = process["requerimentos"]
    pages = process["paginas"]
    review = review or {}
    confirmed = review.get("confirmacoes", {})
    facts = {}
    doubtful = []  # campos decisivos lidos de forma diferente nas duas leituras do requerimento
    for row in forms:
        skip = set(row.get("divergencias") or [])
        for field in sorted(skip):
            doubtful.append(evidence(row, field))
        for field, value in row.get("obtido", {}).items():
            if field in skip:
                continue
            if value is not None and value != "":
                facts.setdefault(field, []).append(evidence(row, value))

    def unique(field):
        values = {item["valor"] for item in facts.get(field, [])}
        return next(iter(values)) if len(values) == 1 else None

    def flag(field):
        value = unique(field)
        return value if type(value) is bool else None

    def sources_of(*fields):
        return [item for field in fields for item in facts.get(field, [])]

    def confirmation(key):
        source = confirmed.get(key)
        if source:
            add(key, "atendido", CONFIRMACOES[key], [{"referencia_conferencia": source}])
            return True
        return False

    # Inventário das páginas e possíveis comprovantes.
    inventory = []
    proof_types = {
        "identificacao": {"identidade", "cpf"},
        "beneficio_e_renda": {"beneficio", "holerite"},
        "residencia": {"residencia"},
        "vinculo_com_imovel": {"posse"},
        "representacao": {"procuracao"},
        "certidoes": {"certidao"},
    }
    support = {key: [] for key in proof_types}
    readable = [row for row in pages if row["status"] == "extraido"]
    for row in pages:
        kind = row.get("tipo")
        source = evidence(row, kind)
        inventory.append({**source, "status_leitura": row["status"],
                          "status_ocr": row.get("status_ocr"),
                          "trecho_classificacao": row.get("evidencia_tipo"),
                          "continuacao_requerimento": bool(row.get("continuacao_requerimento"))})
        for key, kinds in proof_types.items():
            if row["status"] == "extraido" and kind in kinds:
                support[key].append(source)

    income = []
    for row in readable:
        data = row.get("obtido", {})
        if row.get("tipo") in {"beneficio", "holerite"} and type(data.get("renda_bruta_centavos")) is int:
            income.append(evidence(row, {key: data.get(key) for key in (
                "renda_bruta_centavos", "competencia_renda", "titular_renda",
                "numero_beneficio", "especie_beneficio",
            )} | {"tipo": row["tipo"]}))

    applicant = unique("nome_requerente")
    applicant_key = person_key(applicant)
    applicant_cpf = digits(unique("cpf_requerente"))
    spouse = unique("nome_conjuge")
    spouse_key = person_key(spouse)

    # Modalidade e condição para propor indeferimento.
    exercise = unique("exercicio")
    # Modalidade pelos benefícios do próprio requerente declarados no formulário; as perguntas sobre
    # cônjuge e dependente (BPC, pensão) não contam. Sem números declarados, usa a marcação lida.
    declared_kinds = {kind for kind in BENEFICIOS_DECLARADOS
                      if unique(f"beneficio_{kind}_numero") or unique(f"beneficio_{kind}_centavos")}
    marked = set().union(*(modality_parts(item["valor"]) for item in facts.get("modalidade_criterio", [])))
    modalities = declared_kinds | (marked & SOCIAIS) if declared_kinds else marked
    modality = "+".join(sorted(modalities)) or None
    complete = process["status"] == "em_revisao"
    eligible_modality = bool(modalities) and modalities <= ELEGIVEIS
    ordinary = complete and exercise == EXERCICIO and eligible_modality

    if not complete:
        add("processamento", "pendente", "A leitura do processo ainda está em andamento.")
    if not process.get("numero_processo_sei"):
        reason = ("Números SEI divergentes entre nomes e pastas." if process.get("sei_divergente")
                  else "Número do SEI não encontrado nos nomes das pastas e arquivos.")
        add("processo_sei", "aviso", reason, process.get("fontes_numero_sei"))
    if exercise != EXERCICIO:
        add("exercicio", "pendente", f"Confirme se o exercício solicitado é {EXERCICIO}.", facts.get("exercicio"))
    if modalities & SOCIAIS:
        add("modalidade", "pendente", "Modalidade PCD ou doença grave: encaminhar à assistência social.",
            facts.get("modalidade_criterio"))
    elif not eligible_modality:
        add("modalidade", "pendente", "Modalidade do pedido ausente, divergente ou fora do fluxo.",
            facts.get("modalidade_criterio"))
    else:
        add("modalidade", "atendido", "Modalidade elegível: " + modality_label(modality) + ".",
            facts.get("modalidade_criterio"))

    # Identidade: CPF ou nome do requerimento conferem com documento de identificação.
    if not confirmation("identidade"):
        ids = [row for row in readable if row.get("tipo") in {"identidade", "cpf"}]
        by_cpf = [row for row in ids if applicant_cpf and (
            digits((row.get("obtido") or {}).get("cpf_documento")) == applicant_cpf
            or applicant_cpf in digits(page_text(row)))]
        by_name = [row for row in ids if applicant_key and (
            person_key((row.get("obtido") or {}).get("nome_documento")) == applicant_key
            or applicant_key in person_key(page_text(row)))]
        matched = by_cpf or by_name
        if matched:
            how = "CPF" if by_cpf else "nome"
            add("identidade", "atendido",
                f"Documento de identificação ({where(matched)}) confere com o {how} do requerimento.",
                [evidence(row, row.get("tipo")) for row in matched])
        elif not ids:
            add("identidade", "pendente", "Nenhum documento de identificação do requerente foi localizado.")
        else:
            add("identidade", "pendente",
                "Os documentos de identificação não conferem com o nome ou CPF do requerimento.",
                [evidence(row, row.get("tipo")) for row in ids])

    # Benefício: os benefícios declarados têm extrato em nome do requerente.
    # O número do benefício declarado liga o extrato à pessoa mesmo com nome abreviado.
    declared = {kind: digits(unique(f"beneficio_{kind}_numero")) for kind in BENEFICIOS_DECLARADOS}
    declared = {kind: number for kind, number in declared.items() if number}
    spouse_number = digits(unique("conjuge_beneficio_numero"))
    owner = {}
    for item in income:
        number = digits(item["valor"].get("numero_beneficio"))
        holder = item["valor"].get("titular_renda")
        if number and number in declared.values():
            owner[id(item)] = "requerente"
        elif number and number == spouse_number:
            owner[id(item)] = "conjuge"
        elif holder and same_person(holder, applicant):
            owner[id(item)] = "requerente"
        elif holder and same_person(holder, spouse):
            owner[id(item)] = "conjuge"
        elif not holder:
            owner[id(item)] = "requerente"
        else:
            owner[id(item)] = "terceiro"
    applicant_income = [item for item in income if owner[id(item)] == "requerente"]
    if applicant_income:
        latest = max(item["valor"].get("competencia_renda") or "" for item in applicant_income)
        applicant_income = [item for item in applicant_income
                            if (item["valor"].get("competencia_renda") or "") == latest]
    found_numbers = {digits(item["valor"].get("numero_beneficio")) for item in applicant_income}
    missing = {kind: number for kind, number in declared.items() if number not in found_numbers}
    benefit_ok = False
    if not confirmation("beneficio"):
        if not applicant_income:
            add("beneficio", "pendente", "Nenhum extrato de benefício em nome do requerente foi localizado.",
                income)
        elif missing:
            add("beneficio", "pendente", "Benefício declarado sem extrato no processo: "
                + ", ".join(f"{kind} nº {number}" for kind, number in missing.items()) + ".",
                applicant_income)
        else:
            benefit_ok = True
            add("beneficio", "atendido",
                f"Extrato(s) de benefício em nome do requerente ({where(applicant_income)})"
                + (" com os números declarados no requerimento." if declared else "."),
                applicant_income)
    else:
        benefit_ok = True

    # Renda: requerente + cônjuge, competência mais recente, sem duplicar benefícios.
    estimate = income_estimate([item for item in income if owner[id(item)] != "terceiro"])
    marital = "_".join(fold(unique("estado_civil")).split())
    has_spouse = bool(spouse_key) or marital.startswith(("casad", "uniao", "companheir"))
    no_spouse = not spouse_key and (marital in SEM_CONJUGE or flag("conjuge_informado") is False)
    gaps, extra, extra_sources = [], 0, []
    if not (has_spouse or no_spouse):
        gaps.append("estado civil ou cônjuge não identificado no requerimento")
    if has_spouse:
        spouse_income = [item for item in (estimate or {}).get("comprovantes", [])
                         if owner[id(item)] == "conjuge"]
        declared_spouse = unique("conjuge_renda_centavos")
        if not spouse_income and type(declared_spouse) is int:
            extra += declared_spouse
            extra_sources += facts.get("conjuge_renda_centavos", [])
        elif not spouse_income:
            gaps.append("renda do cônjuge sem comprovante ou valor declarado")
    for field, value_field, label in (
        ("outras_rendas", "outras_rendas_centavos", "outras rendas"),
        ("atividade_economica_declarada", "renda_atividade_centavos", "renda de sócio/autônomo"),
    ):
        answer = flag(field)
        amount = unique(value_field)
        if answer is True and type(amount) is int:
            extra += amount
            extra_sources += facts.get(value_field, [])
        elif answer is True:
            gaps.append(label + " declarada sem valor")
        elif answer is None and field == "outras_rendas" and not (
            flag("atividade_economica_declarada") is False and flag("trabalha_registrado") is False
        ):
            gaps.append("resposta sobre outras rendas não identificada")
    if flag("trabalha_registrado") is True and not any(
        item["valor"]["tipo"] == "holerite" for item in (estimate or {}).get("comprovantes", [])
    ):
        gaps.append("trabalho registrado declarado sem holerite")
    if not benefit_ok:
        gaps.append("benefícios declarados sem extrato conferido")

    amount = review.get("renda_bruta_familiar_centavos")
    income_source = review.get("fonte_renda")
    total = None
    if type(amount) is int and income_source and exercise == EXERCICIO:
        total = amount
        above = amount > TETO_CENTAVOS
        add("renda", ("impedimento" if ordinary else "pendente") if above else "atendido",
            f"Renda bruta familiar conferida de {money(amount)} "
            + ("acima do" if above else "dentro do") + f" teto de {money(TETO_CENTAVOS)}.",
            [{"referencia_conferencia": income_source, "valor_centavos": amount}])
    elif estimate:
        total = estimate["total_centavos"] + extra
        parts = (f"{len(estimate['comprovantes'])} comprovante(s) distinto(s) da competência "
                 f"{estimate['competencia']}" + (f" + {money(extra)} declarados" if extra else ""))
        sources = estimate["comprovantes"] + extra_sources
        if total > TETO_CENTAVOS and ordinary:
            # Faltar algum componente só aumentaria a soma: o teto já foi ultrapassado.
            add("renda", "impedimento", f"Renda bruta familiar de {money(total)} ({parts}) acima do teto "
                f"de {money(TETO_CENTAVOS)}.", sources)
        elif gaps:
            add("renda", "pendente", f"Comprovantes somam {money(total)} ({parts}), mas falta: "
                + "; ".join(gaps) + ".", sources)
            total = None
        else:
            add("renda", "atendido", f"Renda bruta familiar de {money(total)} ({parts}) dentro do teto "
                f"de {money(TETO_CENTAVOS)} (até 2 salários mínimos).", sources)
    else:
        add("renda", "pendente", "Nenhum comprovante de renda legível. Confira a renda familiar e "
            "registre o total bruto mensal com a fonte.")

    # Residência.
    residence = unique("reside_no_imovel")
    if not confirmation("residencia"):
        if residence is False:
            add("residencia", "impedimento" if ordinary else "pendente",
                "O requerimento declara que o requerente não reside no imóvel.", facts["reside_no_imovel"])
        elif residence is True:
            add("residencia", "atendido", "O requerimento declara que o requerente reside no imóvel.",
                facts.get("reside_no_imovel"))
        else:
            add("residencia", "pendente", "Resposta sobre residência no imóvel não identificada.")

    # Edícula. Aluguel ou comércio prevalece sobre conferência manual.
    rented, commercial = flag("edicula_alugada"), flag("edicula_uso_comercial")
    shop = flag("imovel_ponto_comercial")
    has, inhabited, family = flag("possui_edicula"), flag("edicula_habitada"), flag("edicula_ocupante_familiar")
    annex = ("possui_edicula", "edicula_habitada", "edicula_ocupante_familiar",
             "edicula_alugada", "edicula_uso_comercial", "imovel_ponto_comercial")
    if True in (rented, commercial, shop):
        add("edícula", "impedimento" if ordinary else "pendente",
            "Edícula alugada ou imóvel com ponto comercial (LC 456/2025).",
            [item for item in sources_of(*annex) if item["valor"] is True])
    elif not confirmation("edícula"):
        if has is False:
            add("edícula", "atendido", "O requerimento declara que o imóvel não possui edícula.",
                facts.get("possui_edicula"))
        elif has and rented is False and commercial is False and (inhabited is False or family is True):
            kinship = occupant_kinship(readable, unique("nome_ocupante_edicula"),
                                       digits(unique("cpf_ocupante_edicula")), applicant, applicant_cpf)
            if family and kinship and kinship[0] == "irmao":
                add("edícula", "pendente", "O requerimento declara ascendente/descendente na edícula, mas o "
                    f"documento do ocupante ({where([kinship[1]])}) tem a mesma filiação do requerente "
                    "(irmão/irmã). Confirme o parentesco: a LC 456/2025 só admite ascendente ou descendente.",
                    sources_of(*annex) + [evidence(kinship[1], kinship[1].get("tipo"))])
            else:
                proof = (f" Filiação do ocupante ({where([kinship[1]])}) confirma o parentesco."
                         if kinship and kinship[0] == "descendente" else "")
                add("edícula", "atendido", "Edícula sem aluguel nem uso comercial, "
                    + ("habitada por ascendente/descendente." if family else "sem moradores declarados.") + proof,
                    sources_of(*annex))
        elif has and inhabited and family is False:
            add("edícula", "pendente", "Edícula habitada por quem não é ascendente/descendente. "
                "Confira o vínculo e se há cobrança de aluguel.", sources_of(*annex))
        else:
            add("edícula", "pendente", "Respostas sobre edícula incompletas no requerimento.",
                sources_of(*annex))

    # Imóvel: documentos de vínculo, consulta à SIL pela inscrição e unicidade.
    cadastro = cadastro or {}
    registration = unique("inscricao_imobiliaria")
    deeds = [row for row in readable if row.get("tipo") == "posse"]
    def cites_applicant(row):
        names = re.split(r"[;\n]", (row.get("obtido") or {}).get("nomes_citados") or "")
        return bool(applicant_key) and (applicant_key in person_key(page_text(row))
                                        or any(same_person(name, applicant) for name in names if name.strip()))

    def cites_usufruct(row):
        return (row.get("obtido") or {}).get("usufruto_citado") is True or "usufruto" in page_text(row)

    mentions = [row for row in deeds if cites_applicant(row)]
    notes = []
    if mentions:
        notes.append(f"documento de vínculo cita o requerente ({where(mentions)})")
        if flag("usufrutuario") is False and any(cites_usufruct(row) for row in mentions):
            notes.append("o documento indica usufruto, mas o requerimento marcou que não é usufrutuário")
    elif deeds:
        notes.append(f"documento de vínculo localizado ({where(deeds)}) sem o nome do requerente")
    deed_sources = [evidence(row, "posse") for row in (mentions or deeds)]
    prop = cadastro.get("imovel")
    if flag("possui_outro_imovel") is True:
        add("imovel", "impedimento" if ordinary else "pendente",
            "O requerimento declara que o requerente possui outro imóvel.", facts.get("possui_outro_imovel"))
    elif confirmation("imovel"):
        pass
    elif "imovel" in cadastro and prop is None:
        add("imovel", "pendente", f"Inscrição imobiliária {registration} não localizada na SIL.",
            facts.get("inscricao_imobiliaria"))
    elif prop:
        holders = [item for item in prop.get("responsaveis") or [] if isinstance(item, dict)]
        cpfs = {digits(item.get("cpfCnpj")) for item in holders} | {digits(prop.get("CNPJCPFResponsavel"))}
        names = {person_key(item.get("nome")) for item in holders} | {person_key(prop.get("NomeResponsavel"))}
        role = next((item.get("identificacao") for item in holders
                     if applicant_cpf and digits(item.get("cpfCnpj")) == applicant_cpf), None)
        situation = fold((prop.get("Situacao") or {}).get("Situacao"))
        place = prop.get("LocalImovel") or {}
        declared_address = fold(unique("endereco_declarado"))
        street = person_key(place.get("Logradouro"))
        sil_numbers = set(re.findall(r"\b\d{1,6}\b", re.sub(r"\d{5}-?\d{3}", " ", place.get("Endereco") or "")))
        address_ok = None
        if declared_address and street:
            address_ok = street in person_key(declared_address) and (
                not sil_numbers or bool(sil_numbers & set(re.findall(r"\b\d{1,6}\b", declared_address))))
        sil_source = [{"referencia_conferencia": f"SIL · inscrição {registration}"}]
        if applicant_cpf in cpfs or (not applicant_cpf and applicant_key in names):
            if situation and situation != "ativo":
                add("imovel", "pendente", f"Imóvel com situação '{situation}' na SIL.", sil_source)
            elif address_ok is False:
                add("imovel", "pendente", "Endereço declarado não coincide com o endereço da SIL "
                    f"({place.get('Endereco') or place.get('Logradouro')}). Inconsistência de endereço.",
                    sil_source + facts.get("endereco_declarado", []))
            else:
                add("imovel", "atendido", "SIL confirma o requerente como responsável pelo imóvel"
                    + (f" ({role})" if role else "") + (" e o endereço declarado." if address_ok else "."),
                    sil_source + deed_sources)
        elif mentions:
            add("imovel", "pendente", "O CPF do requerente não consta entre os responsáveis na SIL, mas há "
                "documento de vínculo no processo: " + "; ".join(notes) + ". Atualizar o cadastro ou conferir.",
                sil_source + deed_sources)
        else:
            add("imovel", "pendente", "O CPF do requerente não consta entre os responsáveis do imóvel na SIL.",
                sil_source)
    else:
        add("imovel", "cadastral", f"Conferir na SIL se o requerente é responsável pela inscrição "
            f"{registration or '(não identificada)'} e se o endereço coincide"
            + (". Nos documentos: " + "; ".join(notes) if notes else "") + ".",
            deed_sources + facts.get("inscricao_imobiliaria", []))
    if flag("possui_outro_imovel") is not True and not confirmed.get("imovel"):
        add("unicidade", "cadastral", "Conferir no Cadastro Imobiliário se o CPF do requerente tem um único "
            "imóvel e o do cônjuge nenhum (a API da SIL ainda não consulta imóveis por CPF).")

    activity = cadastro.get("mobiliario")
    if confirmation("mobiliario"):
        pass
    elif activity is not None and not cadastro.get("erro_mobiliario"):
        found = {cpf: data for cpf, data in activity.items() if data}
        if not found:
            add("mobiliario", "atendido", "SIL: nenhuma inscrição mobiliária para o CPF do requerente"
                + (" ou do cônjuge." if len(activity) > 1 else "."))
        elif flag("atividade_economica_declarada") is True and type(unique("renda_atividade_centavos")) is int:
            add("mobiliario", "atendido", "Inscrição mobiliária na SIL com renda da atividade declarada e "
                "somada à renda familiar.")
        else:
            add("mobiliario", "pendente", "SIL indica inscrição mobiliária ativa sem renda da atividade "
                "declarada. Verificar pró-labore ou faturamento.")
    else:
        add("mobiliario", "cadastral", "Conferir no Cadastro Mobiliário (SIL) se o requerente ou o cônjuge "
            "tem inscrição municipal ativa (sócio ou autônomo).")
    if not confirmation("prazo"):
        signed = unique("data_requerimento")
        signed = signed if isinstance(signed, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", signed) else None
        if signed and PRAZO_INICIO and PRAZO_FIM and not PRAZO_INICIO <= signed <= PRAZO_FIM:
            add("prazo", "pendente", f"Requerimento assinado em {signed}, fora do prazo de {PRAZO_INICIO} a "
                f"{PRAZO_FIM}. Confira a data oficial do protocolo no SEI/F4.", facts.get("data_requerimento"))
        else:
            add("prazo", "cadastral", "Conferir no SEI/F4 a data oficial do protocolo e o prazo"
                + (f" (requerimento assinado em {signed}" + (", dentro do prazo" if PRAZO_INICIO and PRAZO_FIM else "")
                   + ")" if signed else "") + ".", facts.get("data_requerimento"))

    if any(row["status"] != "extraido" or row.get("status_ocr") == "falha_ocr" for row in pages) or any(
        doc["erro"] and doc["erro"] != "formato_nao_processado" for doc in process["arquivos"]
    ):
        failures = {}
        for row in pages:
            if row["status"] != "extraido" or row.get("status_ocr") == "falha_ocr":
                reason = row["status"] if row["status"] != "extraido" else "falha_ocr"
                failures[reason] = failures.get(reason, 0) + 1
        for doc in process["arquivos"]:
            if doc["erro"] and doc["erro"] != "formato_nao_processado":
                failures[doc["erro"]] = failures.get(doc["erro"], 0) + 1
        add("leitura", "pendente", "Há arquivos ou páginas que não puderam ser lidos: "
            + ", ".join(f"{count} com {reason}" for reason, count in sorted(failures.items())) + ".",
            [evidence(row, row["status"]) for row in pages
             if row["status"] != "extraido" or row.get("status_ocr") == "falha_ocr"][:20])

    if doubtful:
        add("dupla_leitura", "pendente", "Leituras divergentes no requerimento: "
            + "; ".join(f"{CAMPOS.get(item['valor'], item['valor'])} (p. {item['pagina']})" for item in doubtful)
            + ". Confira na página e registre a conferência.", doubtful)

    out_of_flow = None
    if not forms:
        rules[:] = [row for row in rules if row["criterio"] in {"processamento", "processo_sei", "leitura"}]
        out_of_flow = other_subject(pages)
        if out_of_flow and complete:
            add("requerimento", "fora_do_fluxo", "O processo não é pedido de isenção de IPTU para aposentado, "
                f"pensionista ou BPC (assunto: {out_of_flow[0]}). Encaminhar ao fluxo próprio.",
                [evidence(out_of_flow[1], out_of_flow[1].get("tipo"))])
        else:
            add("requerimento", "pendente", "Nenhum requerimento de isenção de IPTU foi localizado no processo.")

    blockers = [row["motivo"] for row in rules if row["status"] == "impedimento"]
    pending = [row["motivo"] for row in rules if row["status"] == "pendente"]
    registry = [row["motivo"] for row in rules if row["status"] == "cadastral"]
    status = "proposta_indeferimento" if blockers else "em_analise" if pending else "proposta_deferimento"
    result = ("fora_do_fluxo" if any(row["status"] == "fora_do_fluxo" for row in rules)
              else "nao_enquadra" if blockers else "em_analise" if pending
              else "enquadra_conferir_cadastro" if registry else "enquadra")
    if result == "fora_do_fluxo":
        status = "fora_do_fluxo"
    report = {"status": status, "resultado": result,
            "pode_receber_isencao": False if blockers else None if pending else True,
            "motivos_indeferimento": blockers, "pendencias": pending, "conferencias_cadastrais": registry,
            "criterios": rules,
            "resumo": {"requerente": applicant, "cpf": unique("cpf_requerente"),
                       "inscricao_imobiliaria": unique("inscricao_imobiliaria"),
                       "modalidade": modality, "renda_familiar_centavos": total},
            "numero_processo_sei": process.get("numero_processo_sei"),
            "fontes_numero_sei": process.get("fontes_numero_sei", []),
            "sei_divergente": process.get("sei_divergente", False),
            "exercicio": exercise, "modalidade": modality, "fatos_do_requerimento": facts,
            "inventario_paginas": inventory, "possiveis_comprovantes": support,
            "comprovantes_de_renda_lidos": income, "renda_bruta_familiar_centavos": amount,
            "renda_calculada_comprovantes_centavos": estimate["total_centavos"] if estimate else None,
            "teto_centavos": TETO_CENTAVOS if exercise == EXERCICIO else None,
            "fontes": {"criterios": FONTE_CRITERIOS, "teto": FONTE_TETO}}
    report["resumo"]["sintese"] = synthesis(facts, unique, flag, modality, total, result)
    report["parecer_texto"] = write_opinion(report)
    return report


ASSUNTOS_FORA = ("imunidade", "revisao do lancamento", "revisao de lancamento", "cancelamento de debito",
                  "execucao fiscal", "divida ativa", "restituicao", "contencioso", "remissao", "parcelamento")


def other_subject(pages):
    """Assunto de um processo que não traz requerimento de isenção de aposentado/pensionista."""
    for row in pages:
        text = " ".join((row.get(key) or "") for key in ("texto_pdf", "texto_ocr"))
        match = re.search(r"assunto\s*:\s*([^\n]{3,120})", fold(text))
        if match and any(word in match.group(1) for word in ASSUNTOS_FORA):
            return match.group(1).strip(" .-").upper(), row
    for row in pages:
        text = fold(" ".join((row.get(key) or "") for key in ("texto_pdf", "texto_ocr")))
        for word in ASSUNTOS_FORA:
            if word in text:
                return word.upper(), row
    return None


def occupant_kinship(pages, name, cpf, applicant, applicant_cpf):
    """Compara a filiação no documento do ocupante da edícula com o requerente.

    Retorna ("descendente", página) se o requerente consta na filiação do ocupante,
    ("irmao", página) se ambos têm a mesma filiação, ou None.
    """
    ids = [row for row in pages if row.get("tipo") in {"identidade", "cpf"}]

    def doc_of(person_name, person_cpf):
        for row in ids:
            data = row.get("obtido") or {}
            if (person_cpf and digits(data.get("cpf_documento")) == person_cpf) or (
                    person_name and same_person(data.get("nome_documento"), person_name)):
                return row
        return None

    occupant = doc_of(name, cpf)
    if not occupant:
        return None
    parents = [part for part in re.split(r"[/;\n]| e ", fold((occupant.get("obtido") or {}).get("filiacao_documento")))
               if part.strip()]
    if applicant and any(same_person(part, applicant) for part in parents):
        return "descendente", occupant
    own = doc_of(applicant, applicant_cpf)
    own_parents = [part for part in re.split(r"[/;\n]| e ", fold((own or {}).get("obtido", {}).get("filiacao_documento")))
                   if part.strip()]
    if parents and own_parents and any(same_person(a, b) for a in parents for b in own_parents):
        return "irmao", occupant
    return None


def income_estimate(income):
    """Soma comprovantes distintos da competência mais recente (já filtrados por titular).

    Dois comprovantes com o mesmo número de benefício, ou sem número e com mesmo
    valor e titular, contam uma vez. Meses anteriores ficam de fora.
    """
    rows = [item for item in income if item["valor"].get("competencia_renda")]
    if not rows:
        return None
    latest = max(item["valor"]["competencia_renda"] for item in rows)
    seen, used = set(), []
    for item in rows:
        data = item["valor"]
        if data["competencia_renda"] != latest:
            continue
        number = digits(data.get("numero_beneficio"))
        key = ("beneficio", number) if number else (
            "valor", data["renda_bruta_centavos"], person_key(data.get("titular_renda")))
        if key in seen:
            continue
        seen.add(key)
        used.append(item)
    if not used:
        return None
    return {"total_centavos": sum(item["valor"]["renda_bruta_centavos"] for item in used),
            "competencia": latest, "comprovantes": used}


CAMPOS = {
    "estado_civil": "estado civil", "beneficio_aposentadoria_numero": "nº do benefício de aposentadoria",
    "beneficio_aposentadoria_centavos": "valor da aposentadoria", "beneficio_pensao_numero": "nº da pensão",
    "beneficio_pensao_centavos": "valor da pensão", "outras_rendas": "outras fontes de renda",
    "reside_no_imovel": "reside no imóvel", "imovel_ponto_comercial": "ponto comercial",
    "possui_edicula": "possui edícula", "edicula_alugada": "edícula alugada",
    "edicula_ocupante_familiar": "quem reside na edícula", "possui_outro_imovel": "possui outro imóvel",
    "trabalha_registrado": "trabalha registrado",
}


def synthesis(facts, unique, flag, modality, total, result):
    """Uma linha com o essencial do caso, como na análise humana."""
    if result == "fora_do_fluxo":
        return "Processo fora do fluxo de isenção para aposentado/pensionista."
    parts = []
    marital = fold(unique("estado_civil"))
    for prefix, label in (("solteir", "solteiro(a)"), ("casad", "casado(a)"), ("uniao", "em união estável"),
                          ("divorc", "divorciado(a)"), ("separad", "separado(a)"), ("viuv", "viúvo(a)")):
        if marital.startswith(prefix):
            parts.append(label)
    if modality:
        parts.append(modality_label(modality))
    if total is not None:
        parts.append(money(total) + (" (no limite)" if total == TETO_CENTAVOS else ""))
    has, family = flag("possui_edicula"), flag("edicula_ocupante_familiar")
    if has is False:
        parts.append("sem edícula")
    elif has and flag("edicula_alugada"):
        parts.append("edícula alugada")
    elif has and family:
        parts.append("edícula com familiar")
    elif has:
        parts.append("com edícula")
    if flag("usufrutuario"):
        parts.append("usufrutuário(a)")
    text = ", ".join(parts)
    return text[:1].upper() + text[1:] if text else ""


ROTULOS = {
    "modalidade": "Modalidade", "identidade": "Identificação do requerente", "beneficio": "Benefício",
    "renda": "Renda bruta familiar", "residencia": "Residência no imóvel", "edícula": "Edícula e uso",
    "imovel": "Vínculo com o imóvel", "mobiliario": "Atividade econômica", "unicidade": "Imóvel único", "prazo": "Protocolo e prazo",
    "exercicio": "Exercício", "processo_sei": "Processo SEI", "leitura": "Leitura dos documentos",
    "processamento": "Processamento", "requerimento": "Requerimento",
    "dupla_leitura": "Dupla leitura do requerimento",
}


def write_opinion(result):
    """Redige o parecer em texto corrido a partir dos critérios avaliados."""
    summary = result["resumo"]
    who = summary.get("requerente") or "o(a) requerente"
    head = [f"PARECER — ISENÇÃO DE IPTU {result.get('exercicio') or EXERCICIO}",
            f"Processo SEI: {result.get('numero_processo_sei') or 'não identificado'}",
            f"Requerente: {who}" + (f" · CPF {summary['cpf']}" if summary.get("cpf") else ""),
            f"Inscrição imobiliária: {summary.get('inscricao_imobiliaria') or 'não identificada'}",
            f"Modalidade: {modality_label(summary.get('modalidade')) or 'não identificada'}",
            "Fundamento: Lei Complementar nº 456, de 18 de dezembro de 2025.", ""]
    marks = {"atendido": "Atende", "impedimento": "NÃO atende", "pendente": "Pendente",
             "cadastral": "Conferir no cadastro", "aviso": "Aviso", "fora_do_fluxo": "Fora do fluxo"}
    body = ["Análise dos requisitos:"]
    for row in result["criterios"]:
        body.append(f"- {ROTULOS.get(row['criterio'], row['criterio'])} — {marks[row['status']]}: {row['motivo']}")
    outcome = result["resultado"]
    if outcome == "fora_do_fluxo":
        end = ("Conclusão: o processo não trata de isenção de IPTU para aposentado, pensionista ou BPC "
               "(LC 456/2025) e deve seguir o fluxo próprio do assunto.")
    elif outcome == "nao_enquadra":
        end = (f"Conclusão: o pedido NÃO se enquadra nos requisitos da LC 456/2025 (não aceito). Opina-se pelo "
               f"INDEFERIMENTO da isenção, pelos seguintes motivos: " + " ".join(result["motivos_indeferimento"]))
    elif outcome == "em_analise":
        end = ("Conclusão: os documentos não permitem decidir. O processo segue para REVISÃO MANUAL, com a "
               "conferência dos itens pendentes: " + " ".join(result["pendencias"]))
    elif outcome == "enquadra_conferir_cadastro":
        end = ("Conclusão: pelos documentos apresentados, o pedido se enquadra nos requisitos da LC 456/2025. "
               "Opina-se pelo DEFERIMENTO da isenção, condicionado à confirmação no cadastro municipal: "
               + " ".join(result["conferencias_cadastrais"]))
    else:
        end = ("Conclusão: o pedido se enquadra nos requisitos da LC 456/2025. Opina-se pelo DEFERIMENTO "
               "da isenção.")
    return "\n".join(head + body + ["", end, "",
                                     "Parecer gerado automaticamente para conferência do servidor responsável."])
