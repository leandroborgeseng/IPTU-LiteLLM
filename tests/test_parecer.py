"""Regressão do motor de parecer com processos sintéticos.

Rodar: python -m unittest discover -s tests
O caso base reproduz a estrutura de um processo real (aposentada e pensionista,
viúva, edícula habitada pelo filho), com dados fictícios.
"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parecer  # noqa: E402

CPF = "111.222.333-44"


def page(n, tipo, obtido=None, texto="", **extra):
    return {"arquivo": "a" * 32, "origem": "processo.pdf", "pagina": n, "status": "extraido",
            "status_ocr": "texto_encontrado", "tipo": tipo, "obtido": obtido or {},
            "texto_ocr": texto, **extra}


def base_process():
    form = page(1, "requerimento", {
        "exercicio": 2027, "reside_no_imovel": True, "nome_requerente": "MARIA D ARC SOUZA",
        "cpf_requerente": CPF, "inscricao_imobiliaria": "01.3.11.12.005.15.00",
        "endereco_declarado": "RUA DAS FLORES, 1547, VILA NOVA",
        "modalidade_criterio": "aposentadoria, pensao", "estado_civil": "Viúvo",
        "beneficio_aposentadoria_numero": "1646598382", "beneficio_aposentadoria_centavos": 162100,
        "beneficio_pensao_numero": "1165851641", "beneficio_pensao_centavos": 162100,
        "outras_rendas": False, "possui_edicula": True, "edicula_habitada": True,
        "edicula_ocupante_familiar": True, "edicula_alugada": False, "edicula_uso_comercial": False,
        "imovel_ponto_comercial": False, "possui_outro_imovel": False, "usufrutuario": False,
    }, requerimento_isencao_confirmado=True)
    continuation = page(2, "requerimento", {"conjuge_informado": False, "atividade_economica_declarada": False,
                                            "trabalha_registrado": False}, continuacao_requerimento=True)
    pages = [
        form, continuation,
        page(3, "identidade", {"nome_documento": "MARIA D'ARC SOUZA", "cpf_documento": CPF}),
        page(6, "beneficio", {"renda_bruta_centavos": 162100, "competencia_renda": "2026-03",
                              "titular_renda": "MARIA DARC SOUZA", "numero_beneficio": "116.585.164-1"}),
        page(7, "beneficio", {"renda_bruta_centavos": 162100, "competencia_renda": "2026-03",
                              "titular_renda": "MARIA D ARC SOUZA", "numero_beneficio": "164.659.838-2"}),
        page(8, "beneficio", {"renda_bruta_centavos": 162100, "competencia_renda": "2026-03",  # cópia
                              "titular_renda": "MARIA D ARC SOUZA", "numero_beneficio": "1646598382"}),
        page(9, "beneficio", {"renda_bruta_centavos": 150000, "competencia_renda": "2026-02",  # mês anterior
                              "titular_renda": "MARIA D ARC SOUZA"}),
        page(10, "holerite", {"renda_bruta_centavos": 300000, "competencia_renda": "2026-03",  # filho
                              "titular_renda": "JOSE SOUZA"}),
        page(13, "posse", texto="testamento ... usufruto vitalício a favor de Maria D'Arc Souza ..."),
    ]
    return {"status": "em_revisao", "requerimentos": [form, continuation], "paginas": pages,
            "arquivos": [{"erro": None}], "numero_processo_sei": "3516200.410.00010892-2026-21"}


def sil_property(cpf=CPF, address="RUA DAS FLORES, 1547", situation="Ativo"):
    return {"imovel": {"LocalImovel": {"Logradouro": "DAS FLORES", "Endereco": address},
                       "Situacao": {"Situacao": situation}, "CNPJCPFResponsavel": cpf,
                       "responsaveis": [{"cpfCnpj": cpf, "nome": "MARIA D ARC SOUZA",
                                         "identificacao": "Usufrutuário"}]},
            "mobiliario": {CPF: None}}


def criteria(report):
    return {row["criterio"]: row for row in report["criterios"]}


def change(process, **fields):
    process = copy.deepcopy(process)
    process["requerimentos"][0]["obtido"].update(fields)
    process["paginas"][0] = process["requerimentos"][0]
    return process


class Parecer(unittest.TestCase):
    def test_caso_base_enquadra_pelos_documentos(self):
        report = parecer.assess(base_process(), None)
        c = criteria(report)
        self.assertEqual(report["resultado"], "enquadra_conferir_cadastro", report["pendencias"])
        self.assertEqual(report["modalidade"], "aposentadoria+pensao")
        for key in ("modalidade", "identidade", "beneficio", "renda", "residencia", "edícula"):
            self.assertEqual(c[key]["status"], "atendido", (key, c[key]["motivo"]))
        # duas pensões distintas; cópia, mês anterior e holerite de terceiro ficam fora
        self.assertEqual(report["resumo"]["renda_familiar_centavos"], 324200)
        self.assertIn("usufruto", c["imovel"]["motivo"])
        self.assertIn("DEFERIMENTO", report["parecer_texto"])

    def test_com_sil_so_falta_unicidade(self):
        report = parecer.assess(base_process(), None, sil_property())
        c = criteria(report)
        self.assertEqual(c["imovel"]["status"], "atendido", c["imovel"]["motivo"])
        self.assertEqual(report["conferencias_cadastrais"], [c["unicidade"]["motivo"]])

    def test_sil_endereco_divergente_fica_em_analise(self):
        report = parecer.assess(base_process(), None, sil_property(address="RUA DAS FLORES, 99"))
        self.assertEqual(criteria(report)["imovel"]["status"], "pendente")
        self.assertEqual(report["resultado"], "em_analise")

    def test_sil_cpf_nao_responsavel(self):
        report = parecer.assess(base_process(), None, sil_property(cpf="999.999.999-99"))
        self.assertEqual(criteria(report)["imovel"]["status"], "pendente")

    def test_inscricao_mobiliaria_nao_gera_pendencia(self):
        cadastro = sil_property()
        cadastro["mobiliario"] = {CPF: {"Nome": "MARIA"}}
        report = parecer.assess(base_process(), None, cadastro)
        self.assertNotIn("mobiliario", criteria(report))
        self.assertEqual(report["resultado"], "enquadra_conferir_cadastro")

    def test_divorciada_com_um_beneficio_comprovado_sem_declaracao(self):
        process = change(base_process(), estado_civil="Divorciada", modalidade_criterio=None,
                         beneficio_aposentadoria_numero=None, beneficio_aposentadoria_centavos=None,
                         beneficio_pensao_numero=None, beneficio_pensao_centavos=None)
        process["paginas"] = [row for row in process["paginas"] if row["pagina"] not in {7, 8, 9}]
        benefit = next(row for row in process["paginas"] if row["pagina"] == 6)
        benefit["obtido"]["especie_beneficio"] = "21 - Pensão por morte"
        report = parecer.assess(process, None)
        self.assertEqual(report["resultado"], "enquadra_conferir_cadastro", report["pendencias"])
        self.assertEqual(report["modalidade"], "pensao")
        self.assertEqual(criteria(report)["modalidade"]["evidencias"][0]["pagina"], 6)

        process["paginas"].append(page(15, "beneficio", {
            **benefit["obtido"], "numero_beneficio": "1646598382",
        }))
        self.assertEqual(criteria(parecer.assess(process, None))["modalidade"]["status"], "pendente")

    def test_renda_acima_do_teto_indefere(self):
        process = base_process()
        process["paginas"][3]["obtido"]["renda_bruta_centavos"] = 200000
        report = parecer.assess(process, None)
        self.assertEqual(criteria(report)["renda"]["status"], "impedimento")
        self.assertEqual(report["resultado"], "nao_enquadra")
        self.assertIn("INDEFERIMENTO", report["parecer_texto"])

    def test_beneficio_declarado_sem_extrato(self):
        process = base_process()
        del process["paginas"][4:6]
        report = parecer.assess(process, None)
        self.assertEqual(criteria(report)["beneficio"]["status"], "pendente")
        self.assertEqual(report["resultado"], "em_analise")

    def test_recupera_numero_no_extrato_e_deduplica_renda(self):
        process = base_process()
        for row in process["paginas"][4:6]:
            row["obtido"].pop("numero_beneficio")
            row["texto_pdf"] = (
                "Nº do benefício: 164.659.838-2" if row["pagina"] == 7 else "NB: 164659838-2"
            )
        report = parecer.assess(process, None)
        self.assertEqual(criteria(report)["beneficio"]["status"], "atendido")
        self.assertEqual(report["resumo"]["renda_familiar_centavos"], 324200)
        self.assertEqual(report["resultado"], "enquadra_conferir_cadastro")
        source = criteria(report)["beneficio"]["evidencias"][1]
        self.assertEqual(source["pagina"], 7)
        self.assertEqual(source["valor"]["fonte_numero_beneficio"], "texto_da_pagina")

    def test_numero_no_texto_exige_rotulo_titular_e_leitura_sem_conflito(self):
        cases = [
            ("Protocolo: 1646598382", "", "MARIA D ARC SOUZA"),
            ("NB: 1646598382", "", "JOAO SOUZA"),
            ("NB: 1646598382", "NB: 1234567890", "MARIA D ARC SOUZA"),
            ("NB: 16465983820", "", "MARIA D ARC SOUZA"),
        ]
        for native, ocr, holder in cases:
            with self.subTest(native=native, ocr=ocr, holder=holder):
                process = base_process()
                del process["paginas"][5]
                row = process["paginas"][4]
                row["obtido"].update(numero_beneficio=None, titular_renda=holder)
                row.update(texto_pdf=native, texto_ocr=ocr)
                report = parecer.assess(process, None)
                self.assertEqual(criteria(report)["beneficio"]["status"], "pendente")
                self.assertEqual(report["resultado"], "em_analise")

    def test_recupera_numero_rotulado_do_conjuge(self):
        process = change(base_process(), estado_civil="Casado", nome_conjuge="JOAO SOUZA",
                         primeira_isencao=True, conjuge_beneficio_numero="1234567890")
        for row in process["paginas"][3:6]:
            row["obtido"]["renda_bruta_centavos"] = 100000
        process["paginas"].append(page(15, "beneficio", {
            "renda_bruta_centavos": 50000, "competencia_renda": "2026-03",
            "titular_renda": "JOAO SOUZA",
        }, texto="NB: 1234567890"))
        report = parecer.assess(process, None)
        self.assertEqual(criteria(report)["renda"]["status"], "atendido")
        self.assertEqual(report["resumo"]["renda_familiar_centavos"], 250000)
        evidence = criteria(report)["renda"]["evidencias"][-1]
        self.assertEqual(evidence["valor"]["fonte_numero_beneficio"], "texto_da_pagina")

    def test_extratos_com_mesmo_beneficio_e_mes_mas_valores_divergentes(self):
        process = base_process()
        process["paginas"][5]["obtido"]["renda_bruta_centavos"] = 330000
        report = parecer.assess(process, None)
        rule = criteria(report)["renda"]
        self.assertEqual(report["resultado"], "em_analise")
        self.assertEqual(rule["status"], "pendente")
        self.assertEqual([item["pagina"] for item in rule["componentes_renda"]], [6])
        self.assertEqual([item["pagina"] for item in rule["comprovantes_divergentes"]], [7, 8])

    def test_nome_identico_nao_supera_cpf_divergente_no_documento(self):
        process = base_process()
        process["paginas"][2]["obtido"]["cpf_documento"] = "999.888.777-66"
        report = parecer.assess(process, None)
        rule = criteria(report)["identidade"]
        self.assertEqual((rule["status"], rule["codigo"]), ("pendente", "dados_divergentes"))

    def test_detalha_resposta_faltante_e_leituras_do_requerimento(self):
        process = change(base_process(), edicula_alugada=None)
        form = process["requerimentos"][0]
        form["divergencias"] = ["estado_civil"]
        form["leitura_detalhada"] = {"respostas": {"estado_civil": "Casado"}}
        rules = criteria(parecer.assess(process, None))
        self.assertIn("aluguel", rules["edícula"]["lacunas"])
        comparison = rules["dupla_leitura"]["evidencias"][0]
        self.assertEqual((comparison["primeira_leitura"], comparison["segunda_leitura"]), ("Viúvo", "Casado"))

    def test_casado_sem_renda_do_conjuge(self):
        process = change(base_process(), estado_civil="Casado", nome_conjuge="JOAO SOUZA",
                         primeira_isencao=False)
        report = parecer.assess(process, {"confirmacoes": {"imovel": "Cadastro conferido"}})
        self.assertEqual(criteria(report)["renda"]["status"], "atendido")
        self.assertEqual(report["resultado"], "enquadra", report["pendencias"])

        process = change(process, possui_edicula=None)
        self.assertEqual(parecer.assess(process, None)["resultado"], "em_analise")

    def test_renda_do_conjuge_e_pendente_sem_renovacao_explicita(self):
        for first in (None, True):
            with self.subTest(primeira_isencao=first):
                process = change(base_process(), estado_civil="Casado", nome_conjuge="JOAO SOUZA",
                                 primeira_isencao=first)
                report = parecer.assess(process, {"confirmacoes": {"imovel": "Cadastro conferido"}})
                self.assertEqual(criteria(report)["renda"]["status"], "pendente")
                self.assertEqual(report["resultado"], "em_analise")

    def test_casado_com_renda_do_conjuge_declarada(self):
        process = change(base_process(), estado_civil="Casado", nome_conjuge="JOAO SOUZA",
                         conjuge_renda_centavos=100000)
        report = parecer.assess(process, None)
        self.assertEqual(criteria(report)["renda"]["status"], "impedimento")  # 3.242 + 1.000

    def test_socio_com_renda_zero_soma_zero(self):
        process = copy.deepcopy(base_process())
        process["requerimentos"][1]["obtido"].update(atividade_economica_declarada=True,
                                                     renda_atividade_centavos=0)
        self.assertEqual(criteria(parecer.assess(process, None))["renda"]["status"], "atendido")

    def test_edicula_alugada_indefere_mesmo_confirmada(self):
        process = change(base_process(), edicula_alugada=True)
        report = parecer.assess(process, {"confirmacoes": {"edícula": "conferido"}})
        self.assertEqual(criteria(report)["edícula"]["status"], "impedimento")

    def test_edicula_com_nao_familiar(self):
        process = change(base_process(), edicula_ocupante_familiar=False)
        self.assertEqual(criteria(parecer.assess(process, None))["edícula"]["status"], "pendente")

    def test_nao_reside_indefere(self):
        report = parecer.assess(change(base_process(), reside_no_imovel=False), None)
        self.assertEqual(report["resultado"], "nao_enquadra")

    def test_outro_imovel_indefere(self):
        report = parecer.assess(change(base_process(), possui_outro_imovel=True), None)
        self.assertEqual(criteria(report)["imovel"]["status"], "impedimento")

    def test_pcd_vai_para_assistencia_social(self):
        report = parecer.assess(change(base_process(), modalidade_criterio="aposentadoria e pcd"), None)
        self.assertEqual(report["resultado"], "em_analise")

    def test_identidade_divergente(self):
        process = base_process()
        process["paginas"][2]["obtido"] = {"nome_documento": "OUTRA PESSOA", "cpf_documento": "999.999.999-99"}
        self.assertEqual(criteria(parecer.assess(process, None))["identidade"]["status"], "pendente")

    def test_sem_requerimento(self):
        process = base_process()
        process["requerimentos"] = []
        self.assertEqual(parecer.assess(process, None)["resultado"], "em_analise")


if __name__ == "__main__":
    unittest.main()


class PadroesDosProcessosReais(unittest.TestCase):
    """Padrões observados em processos reais de Franca (dados fictícios)."""

    def test_extrato_com_nome_abreviado_e_numero_declarado(self):
        process = base_process()
        process["paginas"][3]["obtido"]["titular_renda"] = "MARIA D SOUZA"
        process["paginas"][4]["obtido"]["titular_renda"] = "M D ARC SOUZA"  # só o número liga
        report = parecer.assess(process, None)
        self.assertEqual(criteria(report)["beneficio"]["status"], "atendido")
        self.assertEqual(report["resumo"]["renda_familiar_centavos"], 324200)

    def test_nomes_equivalentes(self):
        self.assertTrue(parecer.same_person("NEUSA C M SICCHIEROLLI", "NEUSA CAROLINA MARQUES SICCHIEROLLI"))
        self.assertTrue(parecer.same_person("JOANA DARC PINTO BARBOSA", "Joana D'Arc Pinto Barbosa"))
        self.assertTrue(parecer.same_person("JOSÉ LUIS DO NASCIMENTO", "JOSE LUIS NASCIMENTO"))
        self.assertFalse(parecer.same_person("JOSE LUIS DO NASCIMENTO", "MARIA DE LOURDES DO NASCIMENTO"))
        self.assertFalse(parecer.same_person("ANA SOUZA", "ANA SILVA"))

    def test_casada_com_extrato_do_conjuge(self):
        process = change(base_process(), estado_civil="Casado", nome_conjuge="JOSÉ LUIS DO NASCIMENTO",
                         conjuge_beneficio_numero="01736939308", modalidade_criterio="aposentadoria",
                         beneficio_pensao_numero=None)
        process["paginas"][3]["obtido"].update(titular_renda="JOSE LUIS DO NASCIMENTO",
                                               numero_beneficio="017.369.393-08")
        report = parecer.assess(process, None)
        self.assertEqual(criteria(report)["renda"]["status"], "atendido", criteria(report)["renda"]["motivo"])
        self.assertEqual(report["resumo"]["renda_familiar_centavos"], 324200)

    def test_outras_rendas_em_branco_com_demais_respostas_negativas(self):
        process = change(base_process(), outras_rendas=None)
        self.assertEqual(criteria(parecer.assess(process, None))["renda"]["status"], "atendido")

    def test_edicula_com_irmao_declarado_como_descendente(self):
        process = change(base_process(), nome_ocupante_edicula="CARLOS SOUZA",
                         cpf_ocupante_edicula="222.333.444-55")
        process["paginas"][2]["obtido"]["filiacao_documento"] = "PEDRO SOUZA / ANA SOUZA"
        process["paginas"].append(page(4, "identidade", {"nome_documento": "CARLOS SOUZA",
                                                          "cpf_documento": "222.333.444-55",
                                                          "filiacao_documento": "PEDRO SOUZA / ANA SOUZA"}))
        report = parecer.assess(process, None)
        self.assertEqual(criteria(report)["edícula"]["status"], "pendente")
        self.assertIn("irmão", criteria(report)["edícula"]["motivo"])

    def test_edicula_com_filho_comprovado_pela_filiacao(self):
        process = change(base_process(), nome_ocupante_edicula="JOSE SOUZA")
        process["paginas"].append(page(4, "identidade", {"nome_documento": "JOSE SOUZA",
                                                          "filiacao_documento": "JOAO SOUZA / MARIA D'ARC SOUZA"}))
        c = criteria(parecer.assess(process, None))
        self.assertEqual(c["edícula"]["status"], "atendido")
        self.assertIn("confirma o parentesco", c["edícula"]["motivo"])

    def test_processo_de_imunidade_fica_fora_do_fluxo(self):
        process = {"status": "em_revisao", "requerimentos": [], "arquivos": [{"erro": None}],
                   "numero_processo_sei": "3516200.410.00006891-2026-82",
                   "paginas": [page(1, "capa", texto="", texto_pdf="PROCESSO: 2024003642 NOME: DIOCESE "
                                    "ASSUNTO: IMUNIDADE\nANDAMENTO")]}
        report = parecer.assess(process, None)
        self.assertEqual(report["resultado"], "fora_do_fluxo")
        self.assertIn("IMUNIDADE", report["parecer_texto"])

    def test_processo_sem_requerimento_e_sem_assunto_conhecido(self):
        process = {"status": "em_revisao", "requerimentos": [], "arquivos": [{"erro": None}],
                   "paginas": [page(1, "outro", texto="documento qualquer")]}
        self.assertEqual(parecer.assess(process, None)["resultado"], "em_analise")


class Prazo(unittest.TestCase):
    def test_data_da_assinatura_nao_gera_pendencia(self):
        report = parecer.assess(change(base_process(), data_requerimento="2026-06-15"), None)
        self.assertNotIn("prazo", criteria(report))
        self.assertEqual(report["resultado"], "enquadra_conferir_cadastro")


class ExercicioNaoBloqueia(unittest.TestCase):
    def test_ano_nao_lido_vira_aviso_e_o_processo_e_aceito(self):
        report = parecer.assess(change(base_process(), exercicio=None), None)
        c = criteria(report)
        self.assertEqual(c["exercicio"]["status"], "aviso")
        self.assertEqual(report["resultado"], "enquadra_conferir_cadastro")
        self.assertEqual((report["exercicio"], report["exercicio_lido"]), (parecer.EXERCICIO, None))

    def test_ano_lido_diferente_aparece_no_aviso(self):
        report = parecer.assess(change(base_process(), exercicio=2026), None)
        self.assertIn("2026", criteria(report)["exercicio"]["motivo"])
        self.assertEqual(report["resultado"], "enquadra_conferir_cadastro")

    def test_ano_correto_nao_gera_aviso(self):
        self.assertNotIn("exercicio", criteria(parecer.assess(base_process(), None)))

    def test_renda_acima_do_teto_continua_indeferindo_sem_ano_lido(self):
        process = change(base_process(), exercicio=None)
        process["paginas"][3]["obtido"]["renda_bruta_centavos"] = 200000
        self.assertEqual(parecer.assess(process, None)["resultado"], "nao_enquadra")


class CruzamentoLocalVisivel(unittest.TestCase):
    def test_mostra_vinculo_individual_sem_cpf_do_conjuge(self):
        process = change(base_process(), estado_civil="Casado", nome_conjuge="JOAO SOUZA")
        process["requerimentos"][0]["obtido"]["cpf_requerente"] = "74757644868"
        key = parecer.normalize_registration(process["requerimentos"][0]["obtido"]["inscricao_imobiliaria"])
        report = parecer.assess(process, None, {"imoveis_por_cpf": {
            "por_cpf": {"74757644868": [key]}, "cobertura_completa": False, "carregada_em": 1}})
        rule = criteria(report)["unicidade"]
        self.assertEqual(rule["status"], "cadastral")
        self.assertIn("1 inscrição(ões)", rule["motivo"])
        self.assertIn("CPF do cônjuge não informado", rule["motivo"])
        self.assertEqual(rule["evidencias"][0]["inscricoes_requerente"], [key])

    def test_informa_carga_ausente(self):
        rule = criteria(parecer.assess(base_process(), None))["unicidade"]
        self.assertEqual(rule["codigo"], "carga_nao_disponivel")


class BeneficioDoCasal(unittest.TestCase):
    def process(self, renda=0):
        process = change(base_process(), estado_civil="Casado", nome_conjuge="JOAO SOUZA",
                         modalidade_criterio=None, beneficio_aposentadoria_numero=None,
                         beneficio_aposentadoria_centavos=None, beneficio_pensao_numero=None,
                         beneficio_pensao_centavos=None, conjuge_beneficio_numero="1234567890",
                         requerente_renda_centavos=renda, primeira_isencao=True)
        process["paginas"] = [row for row in process["paginas"] if row["tipo"] not in {"beneficio", "holerite"}]
        process["paginas"].append(page(20, "beneficio", {
            "renda_bruta_centavos": 162100, "competencia_renda": "2026-07",
            "titular_renda": "JOAO SOUZA", "numero_beneficio": "1234567890",
            "especie_beneficio": "41 aposentadoria por idade"}))
        return process

    def test_beneficio_so_do_conjuge_e_renda_do_requerente(self):
        for amount, expected in [(0, "atendido"), (100000, "atendido"),
                                 (162100, "atendido"), (162101, "impedimento"), (None, "pendente")]:
            with self.subTest(amount=amount):
                report = parecer.assess(self.process(amount), None)
                self.assertEqual(criteria(report)["beneficio"]["beneficiario"], "conjuge")
                self.assertEqual(criteria(report)["renda"]["status"], expected)
                self.assertEqual(report["modalidade"], "aposentadoria")
                if expected == "atendido":
                    self.assertEqual(report["resumo"]["renda_familiar_centavos"], 162100 + amount)

    def test_terceiro_ou_especie_nao_identificada_nao_comprovam(self):
        for field, value in [("titular_renda", "PEDRO SILVA"), ("especie_beneficio", None),
                             ("especie_beneficio", "auxilio temporario")]:
            process = self.process()
            process["paginas"][-1]["obtido"][field] = value
            self.assertEqual(criteria(parecer.assess(process, None))["beneficio"]["status"], "pendente")

    def test_numero_declarado_nao_bloqueia_beneficio_comprovado_do_conjuge(self):
        process = self.process()
        process["requerimentos"][0]["obtido"]["beneficio_pensao_numero"] = "9999999999"
        self.assertEqual(criteria(parecer.assess(process, None))["beneficio"]["status"], "atendido")


class BeneficioPeloNome(unittest.TestCase):
    def test_numero_ilegivel_ou_divergente_com_nome_confere(self):
        for number in (None, "1510750298"):
            with self.subTest(number=number):
                process = change(base_process(), beneficio_pensao_numero=None,
                                 beneficio_pensao_centavos=None, modalidade_criterio="aposentadoria")
                process["paginas"] = [row for row in process["paginas"]
                                      if row["tipo"] != "beneficio" or row["pagina"] == 7]
                process["paginas"][3]["obtido"]["numero_beneficio"] = number
                report = parecer.assess(process, None)
                self.assertEqual(criteria(report)["beneficio"]["codigo"], "titular_conferido_por_nome")
                self.assertEqual(criteria(report)["renda"]["status"], "atendido")
