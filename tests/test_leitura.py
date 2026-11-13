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
        """replies: lista na ordem m1, m2…; cada modelo recebe a sua resposta (as chamadas podem ser paralelas)."""
        by_model = {f"m{index + 1}": reply for index, reply in enumerate(replies)}
        def ask(model, prompt, png, *args, **kwargs):
            self.calls.append(model)
            return by_model[model]
        app.ask_json = ask

    def test_requerimento_lido_duas_vezes(self):
        self.fake([("extraido", {"reside_no_imovel": True, "estado_civil": "Viúvo", "cpf_requerente": "091113608/87",
                                 "beneficio_aposentadoria_centavos": 162100.0, "inventado": 1}),
                   ("extraido", {"respostas": {"reside_no_imovel": "Não", "estado_civil": "Viúvo"}})])
        result = app.detail_page("m1", "m2", {"status": "extraido", "tipo": "requerimento", "obtido": {}},
                                 hires=lambda: b"png")
        self.assertEqual(sorted(self.calls), ["m1", "m2"])
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

    def test_extrato_sempre_tem_leitura_detalhada(self):
        calls = []
        app.ask_json = lambda model, *a, **k: calls.append(model) or ("extraido", {"renda_bruta_centavos": 162100})
        result = app.detail_page("m", "v", {"status": "extraido", "tipo": "beneficio", "obtido": {}}, hires=lambda: b"img")
        self.assertEqual((calls, result["obtido"]["renda_bruta_centavos"]), (["m"], 162100))

    def test_falha_passageira_na_leitura_detalhada_devolve_a_pagina_para_a_fila(self):
        app.ask_json = lambda *a, **k: ("falha_de_conexao", None)
        result = app.detail_page("m", "v", {"status": "extraido", "tipo": "requerimento", "obtido": {}}, hires=lambda: b"img")
        self.assertEqual(result["status"], "falha_de_conexao")

    def test_primeira_leitura_e_curta(self):
        prompt = app.classify_prompt()
        self.assertIn("até cerca de 300 caracteres", prompt)
        self.assertNotIn("renda_bruta_centavos", prompt)


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


class PaginasSemModelo(unittest.TestCase):
    ANDAMENTO = ("PREFEITURA MUNICIPAL DE FRANCA FOLHA 01 PROCESSO: 2024003642 NOME: DIOCESE ASSUNTO: IMUNIDADE "
                 "Nro CERTIDÃO: 421445 A N D A M E N T O Data De Para 24/01/2024 PROTOCOLO FISCALIZAÇÃO DE RENDAS "
                 "IPTU imóvel isenção " * 3)

    def test_andamento_com_texto_digital_dispensa_o_modelo(self):
        self.assertEqual(leitura.text_only_kind(self.ANDAMENTO), "capa")

    def test_paginas_que_precisam_do_modelo(self):
        self.assertIsNone(leitura.text_only_kind("pouco texto"))
        self.assertIsNone(leitura.text_only_kind(self.ANDAMENTO + " ESCRITURA PÚBLICA DE VENDA E COMPRA"))
        self.assertIsNone(leitura.text_only_kind(self.ANDAMENTO + " DEMONSTRATIVO DE CRÉDITO DE BENEFÍCIO INSS"))
        self.assertIsNone(leitura.text_only_kind(self.ANDAMENTO + " REQUERIMENTO DE ISENÇÃO DE IPTU 2027"))


class NomesCitadosNaEscritura(unittest.TestCase):
    def test_nome_citado_e_usufruto_sem_texto_completo(self):
        process = base_process()
        process["paginas"][-1].update(texto_ocr="ESCRITURA PÚBLICA DE DOAÇÃO",
                                      obtido={"nomes_citados": "JOSE SOUZA; MARIA D ARC SOUZA", "usufruto_citado": True})
        motivo = criteria(parecer.assess(process, None))["imovel"]["motivo"]
        self.assertIn("cita o requerente", motivo)
        self.assertIn("indica usufruto", motivo)


@unittest.skipIf(app is None, "dependência ausente")
class RespostaCortada(unittest.TestCase):
    def setUp(self):
        self.original = (app.gateway_request, app.time.sleep)
        app.time.sleep = lambda seconds: None

    def tearDown(self):
        app.gateway_request, app.time.sleep = self.original

    def test_repete_com_limite_maior(self):
        sizes = []
        def fake(path, body=None, timeout=5):
            sizes.append(body["max_tokens"])
            if len(sizes) == 1:
                return {"status": "conectado"}, {"choices": [{"finish_reason": "length", "message": {"content": "{"}}]}
            return {"status": "conectado"}, {"choices": [{"finish_reason": "stop", "message": {
                "content": '{"tipo": "outro", "evidencia_tipo": "x", "texto_ocr": "x"}'}}]}
        app.gateway_request = fake
        result = app.extract_form_page("m", b"\\xff\\xd8\\xffimg", inventory=True)
        self.assertEqual((result["status"], sizes), ("extraido", [700, 2800]))

    def test_leitura_detalhada_dobra_o_limite(self):
        sizes = []
        def fake(path, body=None, timeout=5):
            sizes.append(body["max_tokens"])
            reason = "length" if len(sizes) == 1 else "stop"
            return {"status": "conectado"}, {"choices": [{"finish_reason": reason, "message": {"content": '{"a": 1}'}}]}
        app.gateway_request = fake
        self.assertEqual(app.ask_json("m", "p", b"img", 4096), ("extraido", {"a": 1}))
        self.assertEqual(sizes, [4096, 8192])


@unittest.skipIf(app is None, "dependência ausente")
class Tokens(unittest.TestCase):
    def setUp(self):
        self.original = app.gateway_request

    def tearDown(self):
        app.gateway_request = self.original
        app.TOKENS.totais = None

    def test_soma_tokens_da_pagina(self):
        app.gateway_request = lambda path, body=None, timeout=5: ({"status": "conectado"}, {
            "usage": {"prompt_tokens": 1500, "completion_tokens": 60},
            "choices": [{"finish_reason": "stop", "message": {"content": '{"tipo": "outro", "texto_ocr": ""}'}}]})
        app.TOKENS.totais = {"entrada": 0, "saida": 0, "chamadas": 0}
        result = app.extract_form_page("m", b"\xff\xd8\xffimg", inventory=True)
        app.extract_form_page("m", b"\xff\xd8\xffimg", inventory=True)
        self.assertEqual(result["tokens"], {"entrada": 1500, "saida": 60})
        self.assertEqual(app.TOKENS.totais, {"entrada": 3000, "saida": 120, "chamadas": 2})


@unittest.skipIf(app is None, "dependência ausente")
class PrazoAdaptativo(unittest.TestCase):
    def setUp(self):
        self.saved = list(app.RECENT)
        app.RECENT.clear()

    def tearDown(self):
        app.RECENT[:] = self.saved

    def test_sem_historico_usa_prazo_seguro(self):
        self.assertGreaterEqual(app.fast_timeout(), 90)

    def test_servidor_carregado_aumenta_o_prazo(self):
        for _ in range(100):
            app.note_latency(70.0)  # chamadas saudáveis de ~70 s, como no teste de capacidade sob carga
        self.assertEqual(app.fast_timeout(), min(175, app.LLM_TIMEOUT))  # 2,5 × 70 s = 175 s (abaixo do teto de 180 s)

    def test_servidor_livre_mantem_o_piso(self):
        for _ in range(100):
            app.note_latency(12.0)
        self.assertEqual(app.fast_timeout(), 90)
