"""Leitura detalhada por tipo de documento e dupla leitura do requerimento.

A primeira chamada (app.extract_form_page) classifica a página e transcreve o texto.
Para requerimento, extrato e documento de identidade, uma segunda chamada usa
instruções específicas do documento e imagem em alta resolução. O requerimento
ainda recebe uma leitura independente em formato de perguntas e respostas; campos
decisivos que divergem entre as leituras ficam marcados para conferência.
"""
import re
import unicodedata

CHECKBOX = ("Uma caixa está marcada quando contém X, ✓, ☒, ⊠ ou preenchimento; está vazia quando "
            "só tem a borda. Responda pela opção cuja caixa está marcada, não pelo texto impresso. "
            "Se nenhuma ou mais de uma opção excludente estiver marcada, ou a marca for ilegível, use null. ")

REQUERIMENTO = (
    "Esta imagem é uma página do formulário da Prefeitura de Franca \"Requerimento de Isenção de IPTU\" "
    "ou \"Declaração de Isenção de IPTU\" (o formulário pode ter 2 ou 3 páginas). Leia apenas os campos "
    "presentes NESTA página e devolva um objeto JSON com as chaves abaixo (null quando o campo não estiver "
    "nesta página, estiver vazio ou ilegível). " + CHECKBOX +
    "Valores em reais viram centavos inteiros (1621,00 → 162100). Números de benefício e CPF: copie os "
    "dígitos como escritos (inclusive manuscritos). Chaves: "
    "exercicio (ano no título), nome_requerente (Nome), cpf_requerente (CIN/CPF), "
    "endereco_declarado (Endereço + Nº + Complemento + Bairro), estado_civil (Solteiro, Casado, "
    "União Estável, Divorciado ou Viúvo, conforme marcado), "
    "modalidade_criterio (aposentadoria, pensao, bpc, renda_mensal_vitalicia, pcd; várias separadas por vírgula; "
    "SOMENTE pelas perguntas sobre o próprio requerente — É Aposentado?, É Pensionista?, Tipo de Benefício — "
    "marcadas Sim; NUNCA pelas perguntas sobre cônjuge ou dependente, como \"Cônjuge é aposentado/pensionista/BPC\" "
    "ou \"Dependente é beneficiário de BPC\"), "
    "beneficio_aposentadoria_numero e beneficio_aposentadoria_centavos (É Aposentado?/Aposentadoria), "
    "beneficio_pensao_numero e beneficio_pensao_centavos (É Pensionista?), "
    "beneficio_bpc_numero e beneficio_bpc_centavos (BPC/LOAS/Amparo), "
    "outras_rendas (Possui Outras Fontes de Renda?) e outras_rendas_centavos, "
    "inscricao_imobiliaria (Nº Cadastro do Imóvel), usufrutuario (É Caso de Usufrutuário?), "
    "reside_no_imovel (Beneficiário Reside no Imóvel?), imovel_ponto_comercial (O Imóvel Possui Ponto Comercial?), "
    "possui_edicula (O Imóvel Possui Edícula?), edicula_habitada (Edícula está Habitada?; Não se aplica → null), "
    "edicula_ocupante_familiar (Quem Reside na Edícula?: Ascendente/Descendente → true, Outros → false, "
    "Não se aplica → null), edicula_alugada (Edícula está Alugada?; Não se aplica → null), "
    "nome_ocupante_edicula, cpf_ocupante_edicula, possui_outro_imovel (Possui Outro Imóvel?), "
    "atividade_economica_declarada (true se É Sócio de Empresa? ou Trabalha como Autônomo? estiver Sim; false se "
    "ambas Não), renda_atividade_centavos (soma das Rendas de sócio/autônomo), "
    "trabalha_registrado (Trabalha Registrado?), nome_conjuge, conjuge_informado (false se o nome do cônjuge "
    "estiver marcado Não se aplica; true se preenchido), conjuge_beneficio_numero, conjuge_renda_centavos "
    "(benefício do cônjuge + outras rendas do cônjuge), data_requerimento (AAAA-MM-DD do Local e data). "
    "Booleanos: Sim → true, Não → false.")

EXTRATO = (
    "Esta imagem é um demonstrativo de crédito de benefício do INSS, extrato bancário de benefício, carta de "
    "concessão ou holerite. Devolva um objeto JSON com: renda_bruta_centavos (inteiro em centavos da renda "
    "MENSAL bruta: rubrica 101 \"valor total renda mensal\"/\"valor total mr do período\" ou salário bruto; nunca "
    "o valor líquido; NÃO some 13º salário, abono anual, atrasados ou complementos), competencia_renda (AAAA-MM), "
    "titular_renda (nome do beneficiário como impresso), numero_beneficio (NB/NIT/benefício, como impresso) e "
    "especie_beneficio (espécie, ex.: 21 pensão por morte, 41 aposentadoria por idade). Use null quando ausente.")

IDENTIDADE = (
    "Esta imagem é um documento de identificação (RG, CIN, CNH ou CPF). Devolva um objeto JSON com "
    "nome_documento (nome do titular), cpf_documento (CPF do titular, se impresso) e filiacao_documento "
    "(nomes do pai e da mãe separados por \" / \"; null se não houver). Use null quando ilegível.")

PROMPTS = {"requerimento": REQUERIMENTO, "beneficio": EXTRATO, "holerite": EXTRATO,
           "identidade": IDENTIDADE, "cpf": IDENTIDADE}

# Segunda leitura do requerimento: perguntas literais, respostas como texto marcado.
QUESTIONS = [
    ("estado_civil", "Estado Civil", "estado"),
    ("beneficio_aposentadoria_numero", "É Aposentado? / Aposentadoria — Nº Benefício", "digitos"),
    ("beneficio_aposentadoria_centavos", "É Aposentado? / Aposentadoria — Valor", "dinheiro"),
    ("beneficio_pensao_numero", "É Pensionista? — Nº Benefício", "digitos"),
    ("beneficio_pensao_centavos", "É Pensionista? — Valor", "dinheiro"),
    ("outras_rendas", "Possui Outras Fontes de Renda?", "simnao"),
    ("reside_no_imovel", "Beneficiário Reside no Imóvel?", "simnao"),
    ("imovel_ponto_comercial", "O Imóvel Possui Ponto Comercial?", "simnao"),
    ("possui_edicula", "O Imóvel Possui Edícula?", "simnao"),
    ("edicula_alugada", "Edícula está Alugada?", "simnao"),
    ("edicula_ocupante_familiar", "Quem Reside na Edícula? (Ascendente/Descendente, Outros ou Não se Aplica)",
     "ocupante"),
    ("possui_outro_imovel", "Possui Outro Imóvel?", "simnao"),
    ("trabalha_registrado", "Trabalha Registrado?", "simnao"),
]
DECISIVE = [key for key, _, _ in QUESTIONS]


def verification_prompt():
    lines = "\n".join(f"- {key}: {label}" for key, label, _ in QUESTIONS)
    return ("Formulário de isenção de IPTU da Prefeitura de Franca. Para cada pergunta abaixo que aparecer "
            "NESTA página, copie a resposta exatamente como marcada ou escrita no formulário: para caixas, o "
            "texto da opção marcada (\"Sim\", \"Não\", \"Não se aplica\", \"Viúvo\" etc.); para campos, o "
            "conteúdo escrito. " + CHECKBOX + "Devolva um objeto JSON {\"respostas\": {chave: texto ou null}} "
            "só com as chaves das perguntas que aparecem nesta página.\n" + lines)


FORM_TITLES = ("requerimento de isencao de iptu", "declaracao de isencao de iptu")
FORM_QUESTIONS = ("beneficiario reside no imovel", "o imovel possui edicula", "edicula esta habitada",
                  "e caso de usufrutuario", "possui outras fontes de renda", "quem reside na edicula")


def normalized(text):
    return " ".join(re.findall(r"[a-z0-9]+", _fold(text or "")))


def is_form_page(text):
    """Página do formulário de isenção da Prefeitura, pelo texto (independe da classificação do modelo)."""
    words = normalized(text)
    if any(title in words for title in FORM_TITLES):
        return True
    if sum(question in words for question in FORM_QUESTIONS) >= 2:
        return True
    # Página de continuação: perguntas de cônjuge e atividades.
    return "conjuge ou companheiro" in words and ("socio de empresa" in words or "trabalha registrado" in words)


# Indicam documentos usados no parecer. Palavras genéricas de processo tributário (IPTU, imóvel,
# isenção, certidão do cabeçalho do SEI) não entram: o formulário é detectado à parte.
IMPORTANT_WORDS = ("beneficio", "inss", "renda mensal", "demonstrativo de credito", "holerite", "salario",
                   "carteira de identidade", "registro geral", "habilitacao", "escritura", "matricula",
                   "testamento", "usufruto", "procuracao", "obito", "casamento")


def text_only_kind(text):
    """Classifica sem o modelo uma página com texto digital que não interessa ao parecer.

    Retorna o tipo (despacho, capa, execucao_fiscal, outro) ou None quando a página deve
    passar pelo modelo (pouco texto, formulário, extrato, identidade, documento do imóvel).
    """
    if len((text or "").strip()) < 400 or is_form_page(text):
        return None
    words = normalized(text)
    if any(word in words for word in IMPORTANT_WORDS):
        return None
    if "execucao fiscal" in words:
        return "execucao_fiscal"
    if "despacho" in words:
        return "despacho"
    if "andamento" in words or "processo" in words[:300]:
        return "capa"
    return "outro"


def _fold(text):
    text = unicodedata.normalize("NFKD", str(text).casefold())
    return "".join(c for c in text if not unicodedata.combining(c)).strip()


def to_value(kind, answer):
    """Converte a resposta textual da segunda leitura para o tipo do campo."""
    if answer is None or str(answer).strip() == "":
        return None
    text = _fold(answer)
    if kind == "simnao":
        if text.startswith("nao se aplica") or text in {"n/a", "na"}:
            return None
        return True if text.startswith("sim") else False if text.startswith("nao") else None
    if kind == "ocupante":
        return True if "ascendente" in text or "descendente" in text else False if "outro" in text else None
    if kind == "digitos":
        return re.sub(r"\D", "", text) or None
    if kind == "dinheiro":
        match = re.search(r"(\d+(?:\.\d{3})*)(?:,(\d{1,2}))?", text)
        if not match:
            return None
        return int(match.group(1).replace(".", "")) * 100 + int((match.group(2) or "0").ljust(2, "0"))
    if kind == "estado":
        for prefix, value in (("solteir", "solteiro"), ("casad", "casado"), ("uniao", "uniao_estavel"),
                              ("divorc", "divorciado"), ("separad", "separado"), ("viuv", "viuvo")):
            if text.startswith(prefix):
                return value
        return None
    return answer


def comparable(key, value):
    if value is None:
        return None
    kind = next(kind for k, _, kind in QUESTIONS if k == key)
    if kind == "estado":
        return to_value("estado", value)
    if kind == "digitos":
        return re.sub(r"\D", "", str(value)) or None
    return value


def divergences(first, answers):
    """Campos decisivos lidos de forma diferente nas duas leituras (ignora campos ausentes na página)."""
    found = []
    no_annex = first.get("possui_edicula") is False and to_value("simnao", answers.get("possui_edicula")) is False
    for key, _, kind in QUESTIONS:
        if key not in answers:
            continue
        if no_annex and key in {"edicula_alugada", "edicula_ocupante_familiar"}:
            continue  # sem edícula, as perguntas sobre ela não afetam o parecer
        a = comparable(key, first.get(key))
        b = comparable(key, to_value(kind, answers[key]))
        if key == "edicula_alugada":
            a, b = a or False, b or False  # "Não" e "Não se aplica" têm o mesmo efeito
        if a != b:
            found.append(key)
    return found
