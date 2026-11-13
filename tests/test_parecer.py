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
        self.assertEqual(c["mobiliario"]["status"], "atendido")
        self.assertEqual(report["conferencias_cadastrais"], [c["unicidade"]["motivo"], c["prazo"]["motivo"]])

    def test_sil_endereco_divergente_fica_em_analise(self):
        report = parecer.assess(base_process(), None, sil_property(address="RUA DAS FLORES, 99"))
        self.assertEqual(criteria(report)["imovel"]["status"], "pendente")
        self.assertEqual(report["resultado"], "em_analise")

    def test_sil_cpf_nao_responsavel(self):
        report = parecer.assess(base_process(), None, sil_property(cpf="999.999.999-99"))
        self.assertEqual(criteria(report)["imovel"]["status"], "pendente")

    def test_sil_mobiliario_ativo_sem_renda(self):
        cadastro = sil_property()
        cadastro["mobiliario"] = {CPF: {"Nome": "MARIA"}}
        report = parecer.assess(base_process(), None, cadastro)
        self.assertEqual(criteria(report)["mobiliario"]["status"], "pendente")

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

    def test_casado_sem_renda_do_conjuge(self):
        process = change(base_process(), estado_civil="Casado", nome_conjuge="JOAO SOUZA")
        self.assertEqual(criteria(parecer.assess(process, None))["renda"]["status"], "pendente")

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
