"""Leitura detalhada e dupla leitura do requerimento (modelo simulado)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import leitura  # noqa: E402
import parecer  # noqa: E402
from test_parecer import base_process, criteria  # noqa: E402

try:
    import app  # noqa: E402
except ModuleNotFoundError as error:
    app = None
    MISSING = str(error)


class Conversao(unittest.TestCase):
    def test_respostas_textuais(self):
        self.assertIs(leitura.to_value("simnao", "Sim"), True)
        self.assertIs(leitura.to_value("simnao", "NÃO"), False)
        self.assertIsNone(leitura.to_value("simnao", "Não se aplica"))
        self.assertEqual(leitura.to_value("dinheiro", "1.621,00"), 162100)
        self.assertEqual(leitura.to_value("dinheiro", "R$ 2894,36"), 289436)
        self.assertEqual(leitura.to_value("digitos", "154102961-2"), "1541029612")
        self.assertEqual(leitura.to_value("estado", "Viúvo"), "viuvo")
        self.assertIs(leitura.to_value("ocupante", "Ascendente/Descendente"), True)

    def test_divergencias(self):
        first = {"estado_civil": "Viúvo", "reside_no_imovel": True, "possui_edicula": False,
                 "beneficio_aposentadoria_numero": "1755548610", "beneficio_aposentadoria_centavos": 162100,
                 "edicula_alugada": None}
        same = {"estado_civil": "Viúvo", "reside_no_imovel": "Sim", "possui_edicula": "Não",
                "beneficio_aposentadoria_numero": "175554861-0", "beneficio_aposentadoria_centavos": "1621,00",
                "edicula_alugada": "Não se aplica"}
        self.assertEqual(leitura.divergences(first, same), [])
        other = {**same, "reside_no_imovel": "Não", "beneficio_aposentadoria_centavos": "1521,00"}
        self.assertEqual(leitura.divergences(first, other),
                         ["beneficio_aposentadoria_centavos", "reside_no_imovel"])


class ParecerComDivergencia(unittest.TestCase):
    def test_campo_divergente_vira_pendencia_e_nao_e_usado(self):
        process = base_process()
        process["requerimentos"][0]["divergencias"] = ["reside_no_imovel"]
        report = parecer.assess(process, None)
        c = criteria(report)
        self.assertEqual(c["dupla_leitura"]["status"], "pendente")
        self.assertIn("reside no imóvel (p. 1)", c["dupla_leitura"]["motivo"])
        self.assertEqual(c["residencia"]["status"], "pendente")
        self.assertEqual(report["resultado"], "em_analise")

    def test_sintese(self):
        report = parecer.assess(base_process(), None)
        self.assertEqual(report["resumo"]["sintese"],
                         "Viúvo(a), aposentadoria e pensão, R$ 3.242,00 (no limite), edícula com familiar")


@unittest.skipIf(app is None, "dependência ausente")
class DetailPage(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.original = app.ask_json

    def tearDown(self):
        app.ask_json = self.original

    def fake(self, replies):
        def ask(model, prompt, png, max_tokens=2048, timeout=60):
            self.calls.append(model)
            return replies[len(self.calls) - 1]
        app.ask_json = ask

    def test_requerimento_lido_duas_vezes(self):
        self.fake([("extraido", {"reside_no_imovel": True, "estado_civil": "Viúvo", "cpf_requerente": "091113608/87",
                                 "beneficio_aposentadoria_centavos": 162100.0, "inventado": 1}),
                   ("extraido", {"respostas": {"reside_no_imovel": "Não", "estado_civil": "Viúvo"}})])
        result = app.detail_page("m1", "m2", {"status": "extraido", "tipo": "requerimento", "obtido": {}},
                                 hires=lambda: b"png")
        self.assertEqual(self.calls, ["m1", "m2"])
        self.assertEqual(result["obtido"]["cpf_requerente"], "091.113.608-87")
        self.assertEqual(result["obtido"]["beneficio_aposentadoria_centavos"], 162100)
        self.assertNotIn("inventado", result["obtido"])
        self.assertEqual(result["divergencias"], ["reside_no_imovel"])

    def test_extrato_lido_uma_vez_sem_campos_de_formulario(self):
        self.fake([("extraido", {"renda_bruta_centavos": 162100, "numero_beneficio": "1", "reside_no_imovel": True})])
        result = app.detail_page("m1", "m2", {"status": "extraido", "tipo": "beneficio", "obtido": {}},
                                 hires=lambda: b"png")
        self.assertEqual(result["obtido"], {"renda_bruta_centavos": 162100, "numero_beneficio": "1"})
        self.assertEqual(self.calls, ["m1"])

    def test_outros_tipos_nao_chamam_o_modelo(self):
        self.fake([])
        app.detail_page("m1", "m2", {"status": "extraido", "tipo": "posse", "obtido": {}}, hires=lambda: b"png")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(app is None, "dependência ausente")
class RespostaTolerante(unittest.TestCase):
    def test_um_campo_fora_do_formato_nao_derruba_a_pagina(self):
        import batch
        fields = {"tipo": str, "evidencia_tipo": str, "texto_ocr": str, **batch.FIELDS, **batch.ASSESSMENT_FIELDS}
        messy = {
            "tipo": "requerimento", "texto_ocr": "REQUERIMENTO DE ISENÇÃO DE IPTU 2027", "evidencia_tipo": "REQUERIMENTO",
            "exercicio": "2027", "reside_no_imovel": "Sim", "nome_requerente": "ROZELI DE OLIVEIRA",
            "beneficio_aposentadoria_numero": 1871219237,          # número em vez de texto
            "beneficio_aposentadoria_centavos": "1.621,00",        # reais como texto
            "beneficio_pensao_centavos": 1621.0,                   # reais como número
            "conjuge_renda_centavos": 2894.36,                     # reais com decimais
            "outras_rendas": "Não", "possui_edicula": "não se aplica",
            "edicula_alugada": [1, 2],                             # lixo
            "observacao": "campo inventado",                       # chave desconhecida
        }
        clean, dropped = app.coerce_fields(messy, fields)
        self.assertEqual(clean["tipo"], "requerimento")
        self.assertEqual(clean["exercicio"], 2027)
        self.assertIs(clean["reside_no_imovel"], True)
        self.assertEqual(clean["beneficio_aposentadoria_numero"], "1871219237")
        self.assertEqual(clean["beneficio_aposentadoria_centavos"], 162100)
        self.assertEqual(clean["beneficio_pensao_centavos"], 162100)
        self.assertEqual(clean["conjuge_renda_centavos"], 289436)
        self.assertIs(clean["outras_rendas"], False)
        self.assertIsNone(clean["possui_edicula"])
        self.assertIsNone(clean["edicula_alugada"])
        self.assertEqual(dropped, ["edicula_alugada", "observacao"])

    def test_tipo_desconhecido_vira_outro(self):
        import batch
        clean, _ = app.coerce_fields({"tipo": "carta", "texto_ocr": None}, {"tipo": str, "texto_ocr": str, **batch.FIELDS})
        self.assertEqual((clean["tipo"], clean["texto_ocr"]), ("outro", ""))

    def test_centavos_ja_corretos_nao_mudam(self):
        self.assertEqual(app.coerce_value("renda_bruta_centavos", 162100, int), (162100, True))
        self.assertEqual(app.coerce_value("renda_bruta_centavos", "162100", int), (162100, True))
        self.assertEqual(app.coerce_value("exercicio", 2027.0, int), (2027, True))


@unittest.skipIf(app is None, "dependência ausente")
class ChamadasAoModelo(unittest.TestCase):
    def setUp(self):
        self.original = (app.gateway_request, app.time.sleep, app.ask_json)
        app.time.sleep = lambda seconds: None

    def tearDown(self):
        app.gateway_request, app.time.sleep, app.ask_json = self.original

    def replies(self, *results):
        calls = []
        def fake(path, body=None, timeout=5):
            calls.append(timeout)
            return results[len(calls) - 1]
        app.gateway_request = fake
        return calls

    def test_tenta_de_novo_em_429_e_tempo_esgotado(self):
        calls = self.replies(({"status": "erro_http", "codigo": 429}, None),
                             ({"status": "falha_de_conexao", "detalhe": "tempo_limite"}, None),
                             ({"status": "conectado"}, {"ok": 1}))
        result, payload = app.llm_call({})
        self.assertEqual((result["status"], result["tentativas_llm"], payload), ("conectado", 3, {"ok": 1}))
        self.assertEqual(calls, [app.LLM_TIMEOUT] * 3)

    def test_erro_definitivo_nao_repete(self):
        calls = self.replies(({"status": "erro_http", "codigo": 400}, None))
        self.assertEqual(app.llm_call({})[0]["tentativas_llm"], 1)
        self.assertEqual(len(calls), 1)

    def test_desiste_apos_quatro_tentativas(self):
        self.replies(*[({"status": "erro_http", "codigo": 503}, None)] * 4)
        self.assertEqual(app.llm_call({})[0]["tentativas_llm"], 4)

    def test_tipo_da_imagem(self):
        self.assertTrue(app.image_url(b"\xff\xd8\xff\xe0abc").startswith("data:image/jpeg;base64,"))
        self.assertTrue(app.image_url(b"\x89PNG....").startswith("data:image/png;base64,"))

    def test_extrato_completo_dispensa_leitura_detalhada(self):
        app.ask_json = lambda *a, **k: self.fail("não deveria chamar o modelo")
        result = app.detail_page("m", "v", {"status": "extraido", "tipo": "beneficio", "obtido": {
            "renda_bruta_centavos": 162100, "competencia_renda": "2026-03", "titular_renda": "X",
            "numero_beneficio": "1"}}, hires=lambda: b"img")
        self.assertEqual(result["leitura_detalhada"]["status"], "dispensada")


class FormularioPeloTexto(unittest.TestCase):
    def test_paginas_do_formulario(self):
        self.assertTrue(leitura.is_form_page("Prefeitura Municipal de Franca\nREQUERIMENTO DE ISENÇÃO DE IPTU 2027\nNome:"))
        self.assertTrue(leitura.is_form_page("DECLARAÇÃO DE ISENÇÃO DE IPTU\nNº Processo"))
        self.assertTrue(leitura.is_form_page("É Caso de Usufrutuário? Beneficiário Reside no Imóvel? O Imóvel Possui Edícula?"))
        self.assertTrue(leitura.is_form_page("É Sócio de Empresa? Trabalha Registrado? Nome do Cônjuge ou Companheiro:"))

    def test_outros_documentos(self):
        self.assertFalse(leitura.is_form_page("ESCRITURA PÚBLICA DE VENDA E COMPRA ... imóvel residencial"))
        self.assertFalse(leitura.is_form_page("Despacho: deferida a isenção de IPTU ao requerente"))
        self.assertFalse(leitura.is_form_page(""))

    def test_pagina_classificada_errado_vira_requerimento(self):
        try:
            import batch
        except ModuleNotFoundError:
            self.skipTest("dependência ausente")
        result = batch.validate_page_kind({"status": "extraido", "tipo": "outro", "obtido": {"x": 1}},
                                          {"texto_ocr": "REQUERIMENTO DE ISENÇÃO DE IPTU 2027"})
        self.assertEqual((result["tipo"], result.get("requerimento_isencao_confirmado")), ("requerimento", True))


class ModalidadePeloBeneficio(unittest.TestCase):
    def test_bpc_e_pensao_do_conjuge_nao_contam(self):
        process = base_process()
        form = process["requerimentos"][0]["obtido"]
        form.update(modalidade_criterio="aposentadoria, bpc, pensao", beneficio_pensao_numero=None,
                    beneficio_pensao_centavos=None)
        del process["paginas"][3]  # sem extrato de pensão
        report = parecer.assess(process, None)
        self.assertEqual(report["modalidade"], "aposentadoria")

    def test_sem_numeros_declarados_usa_a_marcacao(self):
        process = base_process()
        form = process["requerimentos"][0]["obtido"]
        for key in ("beneficio_aposentadoria_numero", "beneficio_pensao_numero"):
            form[key] = None
        self.assertEqual(parecer.assess(process, None)["modalidade"], "aposentadoria+pensao")
