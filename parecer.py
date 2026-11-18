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

from sil import normalize_cpf, normalize_registration

# Parâmetros do exercício (variáveis de ambiente; padrão: exercício 2027, salário mínimo de R$ 1.621,00).
EXERCICIO = int(os.environ.get("IPTU_EXERCICIO", "2027"))
SALARIO_MINIMO_CENTAVOS = int(os.environ.get("IPTU_SALARIO_MINIMO_CENTAVOS", "162100"))
TETO_CENTAVOS = 2 * SALARIO_MINIMO_CENTAVOS  # LC 456/2025: até 2 salários mínimos
FONTE_TETO = "https://www3.franca.sp.gov.br/noticia/39898/prazo-para-solicitar-reduo-de-10-no-iptu-termina-nesta-quarta-feira.html"
FONTE_CRITERIOS = "https://www3.franca.sp.gov.br/cartadeservicos/servicos/iseno-de-iptu-para-aposentados-pensionistas-e-beneficirios-do-bpcloas"
CONFIRMACOES = {
    "identidade": "Identidade, composição familiar e representação",
    "beneficio": "Titularidade de benefício elegível",
    "imovel": "Vínculo com o imóvel e unicidade conferidos no cadastro",
    "residencia": "Residência no imóvel confirmada",
    "edícula": "Edícula ausente ou com uso enquadrado",
}
PENDENCIA_RENOVACAO = "renda do cônjuge ausente sem indicação expressa de que não é a primeira isenção"
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


NUMERO_ROTULADO_BENEFICIO = re.compile(
    r"\b(?:(?:n[.o°]+|numero)[ \t]*(?:do[ \t]+)?beneficio|nb)"
    r"[ \t]*[:#]?[ \t]*(?:\r?\n[ \t]*)?"
    r"([0-9]{10}|[0-9]{9}-[0-9]|[0-9]{3}\.[0-9]{3}\.[0-9]{3}-[0-9])(?![0-9]|[.-][0-9])"
)


def numero_beneficio_no_texto(row):
    matches = {
        digits(number)
        for key in ("texto_ocr", "texto_pdf")
        for number in NUMERO_ROTULADO_BENEFICIO.findall(fold(row.get(key)))
    }
    return next(iter(matches)) if len(matches) == 1 else None


def evidence(row, value):
    return {"arquivo": row["arquivo"], "origem": row["origem"], "pagina": row["pagina"], "valor": value}


def where(rows):
    return ", ".join(f"p. {row['pagina']}" for row in rows)


def assess(process, review, cadastro=None):
    rules = []

    def add(key, status, reason, sources=None, **details):
        rules.append({"criterio": key, "status": status, "motivo": reason, "evidencias": sources or [], **details})

    forms = process["requerimentos"]
    pages = process["paginas"]
    review = review or {}
    confirmed = review.get("confirmacoes", {})
    facts = {}
    doubtful = []  # campos decisivos lidos de forma diferente nas duas leituras do requerimento
    for row in forms:
        skip = set(row.get("divergencias") or [])
        for field in sorted(skip):
            details = (row.get("leitura_detalhada") or {}).get("respostas") or {}
            doubtful.append({
                **evidence(row, field),
                "campo": CAMPOS.get(field, field),
                "primeira_leitura": (row.get("obtido") or {}).get(field),
                "segunda_leitura": details.get(field),
            })
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

    applicant = unique("nome_requerente")
    applicant_key = person_key(applicant)
    applicant_cpf = digits(unique("cpf_requerente"))
    spouse = unique("nome_conjuge")
    spouse_key = person_key(spouse)
    declared = {kind: digits(unique(f"beneficio_{kind}_numero")) for kind in BENEFICIOS_DECLARADOS}
    declared = {kind: number for kind, number in declared.items() if number}
    spouse_number = digits(unique("conjuge_beneficio_numero"))

    income = []
    for row in readable:
        data = row.get("obtido", {})
        if row.get("tipo") not in {"beneficio", "holerite"} or type(data.get("renda_bruta_centavos")) is not int:
            continue
        values = {key: data.get(key) for key in (
            "renda_bruta_centavos", "competencia_renda", "titular_renda",
            "numero_beneficio", "especie_beneficio",
        )} | {"tipo": row["tipo"]}
        if row["tipo"] == "beneficio" and not values["numero_beneficio"]:
            holder = values["titular_renda"]
            is_applicant = same_person(holder, applicant)
            is_spouse = same_person(holder, spouse)
            if is_applicant != is_spouse:
                number = numero_beneficio_no_texto(row)
                matches_applicant = is_applicant and number in declared.values() and number != spouse_number
                matches_spouse = is_spouse and number == spouse_number and number not in declared.values()
                if number and (matches_applicant or matches_spouse):
                    values["numero_beneficio"] = number
                    values["fonte_numero_beneficio"] = "texto_da_pagina"
        income.append(evidence(row, values))

    # Modalidade e condição para propor indeferimento.
    read_exercise = unique("exercicio")
    # Todos os requerimentos recebidos são do exercício configurado: o ano não lido (ou lido diferente,
    # por erro de leitura ou formulário antigo) vira aviso e não bloqueia o resultado.
    exercise = EXERCICIO
    # Modalidade pelos benefícios do próprio requerente declarados no formulário; as perguntas sobre
    # cônjuge e dependente (BPC, pensão) não contam. Sem números declarados, usa a marcação lida.
    declared_kinds = {kind for kind in BENEFICIOS_DECLARADOS
                      if unique(f"beneficio_{kind}_numero") or unique(f"beneficio_{kind}_centavos")}
    marked = set().union(*(modality_parts(item["valor"]) for item in facts.get("modalidade_criterio", [])))
    modalities = declared_kinds | (marked & SOCIAIS) if declared_kinds else marked
    marital = "_".join(fold(unique("estado_civil")).split())
    modality_sources = facts.get("modalidade_criterio")
    if not modalities and marital in {"divorciado", "divorciada"}:
        personal_benefits = [item for item in income if item["valor"]["tipo"] == "beneficio"
                             and same_person(item["valor"]["titular_renda"], applicant)]
        numbers = {digits(item["valor"]["numero_beneficio"]) for item in personal_benefits}
        single_benefit = len(personal_benefits) == 1 or (len(numbers) == 1 and "" not in numbers)
        if single_benefit:
            species = set().union(*(modality_parts(re.sub(
                r"^\s*\d+\W*", "", item["valor"]["especie_beneficio"] or ""
            )) for item in personal_benefits))
            if len(species) == 1 and species <= ELEGIVEIS:
                modalities = species
                modality_sources = personal_benefits
    # O benefício de cônjuge identificado também pode fundamentar o enquadramento.
    spouse_benefits = []
    if spouse_key and marital.startswith(("casad", "uniao", "companheir")):
        spouse_benefits = [
            item for item in income if item["valor"]["tipo"] == "beneficio"
            and same_person(item["valor"]["titular_renda"], spouse)
            and not same_person(item["valor"]["titular_renda"], applicant)
            and modality_parts(re.sub(r"^\s*\d+\W*", "", item["valor"]["especie_beneficio"] or "")) <= ELEGIVEIS
            and modality_parts(re.sub(r"^\s*\d+\W*", "", item["valor"]["especie_beneficio"] or ""))
        ]
    if not modalities and spouse_benefits:
        modalities = set().union(*(modality_parts(re.sub(
            r"^\s*\d+\W*", "", item["valor"]["especie_beneficio"]
        )) for item in spouse_benefits))
        modality_sources = spouse_benefits
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
    if read_exercise != EXERCICIO:
        add("exercicio", "aviso", (f"O requerimento indica o exercício {read_exercise}; considerado {EXERCICIO}."
                                   if read_exercise else f"Exercício não identificado no requerimento; considerado {EXERCICIO}."),
            facts.get("exercicio"))
    if modalities & SOCIAIS:
        add("modalidade", "pendente", "Modalidade PCD ou doença grave: encaminhar à assistência social.",
            facts.get("modalidade_criterio"), codigo="assistencia_social")
    elif not eligible_modality:
        if not modality_sources:
            code, explanation = "nao_identificada", "Modalidade do pedido não identificada no requerimento."
        elif len({frozenset(modality_parts(item["valor"])) for item in modality_sources}) > 1:
            code, explanation = "divergente", "Marcações divergentes sobre a modalidade do pedido."
        else:
            code, explanation = "fora_do_fluxo", "Modalidade do pedido não reconhecida no fluxo automático."
        add("modalidade", "pendente", explanation, modality_sources, codigo=code)
    else:
        add("modalidade", "atendido", "Modalidade elegível: " + modality_label(modality) + ".",
            modality_sources)

    # Identidade: CPF ou nome do requerimento conferem com documento de identificação.
    if not confirmation("identidade"):
        ids = [row for row in readable if row.get("tipo") in {"identidade", "cpf"}]
        by_cpf, by_name = [], []
        for row in ids:
            document = row.get("obtido") or {}
            document_cpf = digits(document.get("cpf_documento"))
            if applicant_cpf and document_cpf:
                if document_cpf == applicant_cpf:
                    by_cpf.append(row)
                continue
            text = page_text(row)
            if applicant_cpf and applicant_cpf in digits(text):
                by_cpf.append(row)
            elif applicant_key and (
                person_key(document.get("nome_documento")) == applicant_key
                or applicant_key in person_key(text)
            ):
                by_name.append(row)
        matched = by_cpf or by_name
        if matched:
            how = "CPF" if by_cpf else "nome"
            add("identidade", "atendido",
                f"Documento de identificação ({where(matched)}) confere com o {how} do requerimento.",
                [evidence(row, row.get("tipo")) for row in matched])
        elif not ids:
            add("identidade", "pendente", "Nenhum documento de identificação do requerente foi localizado.",
                codigo="documento_nao_localizado")
        else:
            add("identidade", "pendente",
                "Os documentos de identificação não conferem com o nome ou CPF do requerimento.",
                [evidence(row, row.get("tipo")) for row in ids], codigo="dados_divergentes")

    # Benefício: os benefícios declarados têm extrato em nome do requerente.
    # O número do benefício declarado liga o extrato à pessoa mesmo com nome abreviado.
    owner = {}
    for item in income:
        number = digits(item["valor"].get("numero_beneficio"))
        holder = item["valor"].get("titular_renda")
        if holder and not (same_person(holder, applicant) or same_person(holder, spouse)):
            owner[id(item)] = "terceiro"
        elif number and number in declared.values():
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
    applicant_income = [item for item in income if owner[id(item)] == "requerente"
                        and item["valor"]["tipo"] == "beneficio"]
    if applicant_income:
        latest = max(item["valor"].get("competencia_renda") or "" for item in applicant_income)
        applicant_income = [item for item in applicant_income
                            if (item["valor"].get("competencia_renda") or "") == latest]
    found_numbers = {digits(item["valor"].get("numero_beneficio")) for item in applicant_income}
    missing = {kind: number for kind, number in declared.items() if number not in found_numbers}
    named_benefits = [item for item in income if item["valor"]["tipo"] == "beneficio"
                      and same_person(item["valor"].get("titular_renda"), applicant)]
    name_fallback = bool(missing and len(declared) == 1 and named_benefits)
    benefit_ok = False
    if not confirmation("beneficio"):
        benefit_pages = [row for row in readable if row.get("tipo") == "beneficio"]
        if not benefit_pages:
            add("beneficio", "pendente", "Nenhum extrato de benefício foi localizado.",
                codigo="extrato_nao_localizado")
        elif not applicant_income and spouse_benefits and len(declared) <= 1:
            benefit_ok = True
            add("beneficio", "atendido",
                f"Benefício elegível comprovado em nome do cônjuge {spouse} ({where(spouse_benefits)}).",
                spouse_benefits, beneficiario="conjuge")
        elif not applicant_income:
            add("beneficio", "pendente", "Extrato(s) localizado(s) sem renda bruta legível ou vínculo com o requerente.",
                [evidence(row, row.get("tipo")) for row in benefit_pages], codigo="extrato_sem_vinculo_ou_valor")
        elif name_fallback:
            benefit_ok = True
            add("beneficio", "atendido",
                f"Benefício conferido pelo nome do requerente {applicant} ({where(named_benefits)}). "
                "O número lido no extrato não confirmou o número declarado.",
                named_benefits, beneficiario="requerente", codigo="titular_conferido_por_nome")
        elif missing:
            older_numbers = {digits(item["valor"].get("numero_beneficio")) for item in income
                             if owner[id(item)] == "requerente" and item["valor"]["tipo"] == "beneficio"}
            add("beneficio", "pendente", "Benefício declarado sem extrato correspondente na competência conferida: "
                + ", ".join(f"{kind} nº {number}" for kind, number in missing.items()) + ".",
                [evidence(row, row.get("tipo")) for row in benefit_pages],
                codigo="beneficio_sem_extrato_na_competencia",
                beneficios_pendentes=[{"tipo": kind, "numero": number, "tem_extrato_em_outra_competencia":
                                       number in older_numbers} for kind, number in missing.items()])
        else:
            benefit_ok = True
            add("beneficio", "atendido",
                f"Extrato(s) de benefício em nome do requerente ({where(applicant_income)})"
                + (" com os números declarados no requerimento." if declared else "."),
                applicant_income, beneficiario="requerente")
    else:
        benefit_ok = True

    # Renda: requerente + cônjuge, competência mais recente, sem duplicar benefícios.
    estimate = income_estimate([item for item in income if owner[id(item)] != "terceiro"])
    has_spouse = bool(spouse_key) or marital.startswith(("casad", "uniao", "companheir"))
    no_spouse = not spouse_key and (marital in SEM_CONJUGE or flag("conjuge_informado") is False)
    gaps, extra, extra_sources = [], 0, []
    conflicting_income = (estimate or {}).get("divergencias", [])
    if conflicting_income:
        gaps.append("valores brutos divergentes para o mesmo benefício na mesma competência")
    if not (has_spouse or no_spouse):
        gaps.append("estado civil ou cônjuge não identificado no requerimento")
    declared_components = []
    if benefit_ok and not applicant_income and spouse_benefits:
        applicant_proofs = [item for item in (estimate or {}).get("comprovantes", [])
                            if owner[id(item)] == "requerente"]
        declared_applicant = unique("requerente_renda_centavos")
        if not applicant_proofs and type(declared_applicant) is int and declared_applicant >= 0:
            extra += declared_applicant
            sources = facts["requerente_renda_centavos"]
            extra_sources += sources
            declared_components.append({**sources[0], "papel": "requerente",
                                        "valor": {"renda_bruta_centavos": declared_applicant,
                                                  "titular_renda": applicant, "tipo": "declarado"}})
        elif not applicant_proofs:
            gaps.append("renda total do requerente não beneficiário não identificada")
    renewal_without_spouse_income = False
    if has_spouse:
        spouse_income = [item for item in (estimate or {}).get("comprovantes", [])
                         if owner[id(item)] == "conjuge"]
        declared_spouse = unique("conjuge_renda_centavos")
        if not spouse_income and type(declared_spouse) is int:
            extra += declared_spouse
            sources = facts["conjuge_renda_centavos"]
            extra_sources += sources
            declared_components.append({**sources[0], "papel": "conjuge",
                                        "valor": {"renda_bruta_centavos": declared_spouse,
                                                  "titular_renda": spouse, "tipo": "declarado"}})
        elif not spouse_income and facts.get("conjuge_renda_centavos"):
            gaps.append("valores declarados de renda do cônjuge divergentes")
        elif not spouse_income:
            if flag("primeira_isencao") is False:
                renewal_without_spouse_income = True
                extra_sources += facts["primeira_isencao"]
            else:
                gaps.append(PENDENCIA_RENOVACAO)
    for field, value_field, label in (
        ("outras_rendas", "outras_rendas_centavos", "outras rendas"),
        ("atividade_economica_declarada", "renda_atividade_centavos", "renda de sócio/autônomo"),
    ):
        answer = flag(field)
        amount = unique(value_field)
        if (answer is True and not applicant_income and spouse_benefits
                and type(unique("requerente_renda_centavos")) is int):
            continue
        if answer is True and type(amount) is int:
            extra += amount
            sources = facts[value_field]
            extra_sources += sources
            declared_components.append({**sources[0], "papel": "requerente",
                                        "valor": {"renda_bruta_centavos": amount, "titular_renda": applicant,
                                                  "tipo": "declarado", "descricao": label}})
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
        sources = estimate["comprovantes"] + extra_sources + conflicting_income
        components = [
            {**item, "papel": owner[id(item)]} for item in estimate["comprovantes"]
        ] + declared_components
        detail = {"componentes_renda": components, "lacunas": gaps,
                  "comprovantes_divergentes": conflicting_income}
        if total > TETO_CENTAVOS and ordinary:
            # Faltar algum componente só aumentaria a soma: o teto já foi ultrapassado.
            add("renda", "impedimento", f"Renda bruta familiar de {money(total)} ({parts}) acima do teto "
                f"de {money(TETO_CENTAVOS)}.", sources, **detail)
        elif gaps:
            add("renda", "pendente", f"Comprovantes somam {money(total)} ({parts}), mas falta: "
                + "; ".join(gaps) + ".", sources, **detail)
            total = None
        else:
            add("renda", "atendido", f"Renda bruta familiar de {money(total)} ({parts}) dentro do teto "
                f"de {money(TETO_CENTAVOS)} (até 2 salários mínimos)."
                + (" Renda do cônjuge não apresentada em pedido de renovação."
                   if renewal_without_spouse_income else ""), sources, **detail)
    else:
        add("renda", "pendente", "Nenhum comprovante de renda legível. Confira a renda familiar e "
            "registre o total bruto mensal com a fonte.", income, lacunas=gaps)

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
            [item for item in sources_of(*annex) if item["valor"] is True],
            codigo="+".join(key for key, value in (("aluguel", rented), ("uso_comercial", commercial),
                                                   ("ponto_comercial", shop)) if value is True))
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
            missing_fields = []
            if has is None:
                missing_fields.append("existência de edícula")
            if has:
                if rented is None:
                    missing_fields.append("aluguel")
                if commercial is None:
                    missing_fields.append("uso comercial")
                if inhabited is None and family is not True:
                    missing_fields.append("ocupação")
                if inhabited is not False and family is None:
                    missing_fields.append("parentesco do ocupante")
            add("edícula", "pendente", "Respostas sobre edícula incompletas no requerimento.",
                sources_of(*annex), lacunas=missing_fields)

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
        local_property = prop.get("fonte_local")
        sil_source = [{"referencia_conferencia": (
            f"Cadastro local SIL · inscrição {registration}" if local_property else f"SIL · inscrição {registration}"
        )}]
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
        elif local_property:
            add("imovel", "cadastral", "A inscrição consta na carga local, mas a responsabilidade do requerente "
                "precisa de conferência.", sil_source + deed_sources)
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
        property_lookup = cadastro.get("imoveis_por_cpf") or {}
        by_cpf = property_lookup.get("por_cpf", {})
        lookup_cpf = normalize_cpf(unique("cpf_requerente"))
        spouse_cpf = normalize_cpf(unique("cpf_conjuge"))
        registration_key = normalize_registration(registration)
        applicant_properties = set(by_cpf.get(lookup_cpf, []))
        spouse_properties = set(by_cpf.get(spouse_cpf, []))
        other_properties = (applicant_properties | spouse_properties) - {registration_key}
        source = [{"referencia_conferencia": "Carga local do Cadastro Imobiliário da SIL",
                   "carregada_em": property_lookup.get("carregada_em"),
                   "inscricoes_requerente": sorted(applicant_properties),
                   "inscricoes_conjuge": sorted(spouse_properties)}]
        if registration_key and other_properties:
            add("unicidade", "pendente", "A carga local encontrou outras inscrições. Confira os vínculos e se há unidade anexa.", source)
        elif (property_lookup.get("cobertura_completa") and registration_key
              and lookup_cpf in by_cpf and applicant_properties == {registration_key}
              and (no_spouse or spouse_cpf in by_cpf)):
            add("unicidade", "atendido", "A carga completa da SIL confirma apenas a inscrição do pedido para os CPFs aplicáveis.", source)
        elif property_lookup:
            findings = []
            if lookup_cpf in by_cpf:
                findings.append(f"A carga local encontrou {len(applicant_properties)} inscrição(ões) para o CPF do requerente"
                                + (" e inclui a inscrição deste pedido." if registration_key in applicant_properties else "."))
            else:
                findings.append("CPF do requerente não identificado ou inválido para consulta na carga local.")
            if has_spouse and not spouse_cpf:
                findings.append("CPF do cônjuge não informado; cruzamento do cônjuge pendente.")
            elif not no_spouse and not spouse_cpf:
                findings.append("Composição familiar não esclarecida para definir o cruzamento do cônjuge.")
            if not property_lookup.get("cobertura_completa"):
                findings.append("A carga está registrada como parcial; a unicidade depende de conferência da cobertura.")
            add("unicidade", "cadastral", " ".join(findings), source)
        else:
            add("unicidade", "cadastral", "Carga local da SIL não disponível no aplicativo. "
                "Importe o CSV em Cadastro imobiliário da SIL para cruzar os CPFs.", codigo="carga_nao_disponivel")


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
            "exercicio": exercise, "exercicio_lido": read_exercise, "modalidade": modality, "fatos_do_requerimento": facts,
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
    valor e titular, contam uma vez. Valores conflitantes no mesmo benefício
    ficam pendentes de conferência. Meses anteriores ficam de fora.
    """
    rows = [item for item in income if item["valor"].get("competencia_renda")]
    if not rows:
        return None
    latest = max(item["valor"]["competencia_renda"] for item in rows)
    by_key = {}
    for item in rows:
        data = item["valor"]
        if data["competencia_renda"] != latest:
            continue
        number = digits(data.get("numero_beneficio"))
        key = ("beneficio", number) if number else (
            "valor", data["renda_bruta_centavos"], person_key(data.get("titular_renda")))
        by_key.setdefault(key, []).append(item)
    used, conflicts = [], []
    for items in by_key.values():
        if len({item["valor"]["renda_bruta_centavos"] for item in items}) == 1:
            used.append(items[0])
        else:
            conflicts.extend(items)
    return {"total_centavos": sum(item["valor"]["renda_bruta_centavos"] for item in used),
            "competencia": latest, "comprovantes": used, "divergencias": conflicts}


CAMPOS = {
    "primeira_isencao": "primeira isenção", "estado_civil": "estado civil", "beneficio_aposentadoria_numero": "nº do benefício de aposentadoria",
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
