"""Importação e montagem do processo (sem modelo): continuação do formulário e HTML do SEI."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from threading import BoundedSemaphore
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    import batch  # noqa: E402
except ModuleNotFoundError as error:  # Pillow ausente fora do container
    raise unittest.SkipTest(f"dependência ausente: {error}")

SEI = "3516200.410.00010895-2026-65"
HTML = ("<!DOCTYPE html><html><head><style>p{}</style></head><body><p>Despacho</p>"
        "<p>Assunto: REVIS&Atilde;O DO LAN&Ccedil;AMENTO DO IPTU</p></body></html>").encode("latin-1")


class Importacao(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_zip_com_html_sem_extensao_e_subpasta(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(f"Lote/{SEI}/requerimento.pdf", b"%PDF-1.4")
            archive.writestr(f"Lote/{SEI}/sei-0264226-Despacho 0264226", HTML)
            archive.writestr("Lote/Sem numero/doc.pdf", b"%PDF-1.4")
            archive.writestr("Lote/Sem numero/anexos/foto.png", b"x")
            archive.writestr("Lote/Sem numero/notas.txt", b"x")
        (self.dir / "upload").write_bytes(buffer.getvalue())
        processes = {p["processo"]: p for p in batch.inventory(self.dir, "Lote.zip")}
        self.assertEqual(set(processes), {SEI, "Lote/Sem numero"})
        sei = processes[SEI]["arquivos"]
        self.assertTrue(all(item["id"] for item in sei), sei)  # o HTML foi aceito
        other = processes["Lote/Sem numero"]["arquivos"]
        self.assertEqual(len(other), 3)  # subpasta "anexos" pertence ao processo
        self.assertEqual([i["erro"] for i in other if i["erro"]], ["formato_nao_processado"])

    def test_html_vira_pagina_de_texto(self):
        source = self.dir / "despacho"
        source.write_bytes(HTML)
        evidence = self.dir / "x-1.json"
        batch.process_page(source, 1, evidence, analyze=None, extension="")
        page = json.loads(evidence.read_text())
        self.assertEqual(page["tipo"], "despacho")
        self.assertIn("REVISÃO DO LANÇAMENTO", page["texto_pdf"])

    def test_continuacao_antes_do_requerimento(self):
        key = "a" * 32
        folder = self.dir / ("b" * 32)
        folder.mkdir()
        manifest = [{"id": folder.name, "processo": SEI, "arquivos": [{"id": key, "origem": "p.pdf", "erro": None}]}]
        batch.save(self.dir / "manifest.json", manifest)
        batch.save(folder / f"{key}.json", {"paginas": 3, "total": 3})
        pages = {
            1: {"status": "extraido", "tipo": "outro", "obtido": {}, "status_ocr": "texto_encontrado",
                "obtido_bruto": {"conjuge_informado": False},
                "texto_ocr": "É Sócio de Empresa? Nome do Cônjuge ou Companheiro"},
            2: {"status": "extraido", "tipo": "outro", "obtido": {}, "texto_ocr": "", "status_ocr": "sem_texto"},
            3: {"status": "extraido", "tipo": "requerimento", "requerimento_isencao_confirmado": True,
                "status_ocr": "texto_encontrado", "obtido": {"exercicio": 2027}},
        }
        for number, value in pages.items():
            batch.save(folder / f"{key}-{number}.json", value)
        batch.save(folder / "complete.json", {"concluido": True})
        detail = batch.process_detail(self.dir, folder.name)
        self.assertEqual(sorted(row["pagina"] for row in detail["requerimentos"]), [1, 3])
        self.assertEqual(detail["parecer"]["fatos_do_requerimento"]["conjuge_informado"][0]["valor"], False)


if __name__ == "__main__":
    unittest.main()


class Reanalise(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "upload").write_bytes(b"x")
        self.folder = self.dir / ("c" * 32)
        self.folder.mkdir()
        key = "d" * 32
        batch.save(self.dir / "manifest.json", [{"id": self.folder.name, "processo": SEI,
                                                 "arquivos": [{"id": key, "origem": "p.pdf", "erro": None}]}])
        for name, value in {f"{key}.json": {"paginas": 1, "total": 1}, f"{key}-1.json": {"status": "extraido"},
                            "complete.json": {"concluido": True}, "review.json": {"nota": "ok"}}.items():
            batch.save(self.folder / name, value)

    def state(self, status):
        batch.save(self.dir / "batch.json", {"id": "x", "nome": "l.zip", "bytes": 1, "status": status})

    def test_processo_concluido_rele_todas_as_paginas_e_mantem_a_conferencia(self):
        self.state("concluido")
        code, result = batch.request_rerun(self.dir, self.folder.name)
        self.assertEqual((code, result["status"]), (202, "na_fila"))
        names = sorted(p.name for p in self.folder.iterdir())
        self.assertEqual(names, ["d" * 32 + ".json", "review.json"])

    def test_durante_o_processamento_agenda_nova_rodada(self):
        self.state("processando")
        code, result = batch.request_rerun(self.dir)
        self.assertEqual((code, result["status"]), (202, "processando"))
        self.assertTrue((self.dir / ".rodar_novamente").exists())
        self.assertFalse((self.folder / "complete.json").exists())
        self.assertTrue((self.folder / ("d" * 32 + "-1.json")).exists())  # lote: só falhas são relidas

    def test_envio_incompleto_nao_reanalisa(self):
        self.state("recebendo")
        self.assertEqual(batch.request_rerun(self.dir)[0], 409)


class SeparacaoImediata(unittest.TestCase):
    def test_lote_e_separado_e_o_zip_enviado_e_descartado(self):
        path = Path(tempfile.mkdtemp()) / ("f" * 32)
        path.mkdir()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for n in range(3):
                archive.writestr(f"Lote/3516200.410.0001090{n}-2026-21/req.pdf", b"%PDF-1.4")
        (path / "upload").write_bytes(buffer.getvalue())
        batch.save(path / "batch.json", {"id": path.name, "nome": "Lote.zip", "bytes": len(buffer.getvalue()),
                                         "status": "na_fila", "criado": 1})
        processes = batch.import_lot(path)
        state = batch.load(path / "batch.json")
        self.assertEqual((len(processes), state["processos"], state["status"]), (3, 3, "na_fila"))
        self.assertNotIn("etapa", state)
        self.assertFalse((path / "upload").exists())
        self.assertEqual(batch.import_lot(path), processes)  # segunda chamada não refaz



class SegundaPassada(unittest.TestCase):
    def test_paginas_com_falha_passageira_sao_relidas_com_prazo_longo(self):
        lot = Path(tempfile.mkdtemp()) / ("9" * 32)
        (lot / "P").mkdir(parents=True)
        batch.save(lot / "batch.json", {"status": "processando"})
        evidence = lot / "P" / ("a" * 32 + "-1.json")
        batch.save(evidence, {"status": "falha_de_conexao"})
        ok = lot / "P" / ("a" * 32 + "-2.json")
        batch.save(ok, {"status": "extraido"})
        calls = []
        original = batch.process_page
        batch.process_page = lambda pdf, number, ev, analyze, ext, slow=False: calls.append((number, slow)) or ({}, 1, False)
        batch.PAGE_SLOTS = __import__("threading").BoundedSemaphore(2)
        try:
            progress = {}
            batch.second_pass(lot, {"progresso": progress}, progress,
                              [("P", Path("x.pdf"), 1, evidence, ".pdf"), ("P", Path("x.pdf"), 2, ok, ".pdf")], None, 4)
        finally:
            batch.process_page = original
        self.assertEqual(calls, [(1, True)])
        self.assertTrue((lot / "P" / "complete.json").exists())


class ReleituraDeFormato(unittest.TestCase):
    def test_releitura_de_lote_concluido_troca_so_falha_recuperada(self):
        with tempfile.TemporaryDirectory() as directory:
            lot = Path(directory)
            folder = lot / "processo"
            folder.mkdir()
            batch.save(folder / "complete.json", {"concluido": True})
            meta = lot / "batch.json"
            batch.save(meta, {"status": "concluido"})
            key = "a" * 32
            statuses = ["resposta_truncada", "json_invalido", "extraido"]
            pages = []
            for number, status in enumerate(statuses, 1):
                result = {"status": status, "versao_extracao": batch.PAGE_SCHEMA,
                          "tipo": None if number < 3 else "requerimento"}
                batch.save(folder / f"{key}-{number}.json", result)
                pages.append({**result, "arquivo": key, "pagina": number, "origem": "documento.pdf"})
            detail = {"id": folder.name, "status": "em_revisao", "paginas": pages, "requerimentos": [],
                      "parecer": {"resultado": "em_analise", "fatos_do_requerimento": {},
                                  "criterios": [], "resumo": {"requerente": None}, "modalidade": None}}
            batch.schedule_rule_reads(lot, detail)

            def reread(pdf, number, staging, analyze, extension, slow=False):
                self.assertTrue(slow)
                failed = number == 2
                batch.save(staging, {"status": "resposta_sem_conteudo" if failed else "extraido",
                                     "tipo": "beneficio"})
                return {}, 1, failed

            with (patch.object(batch, "PAGE_SLOTS", BoundedSemaphore(1)),
                  patch.object(batch, "process_page", side_effect=reread) as process):
                batch.run_rule_reads(meta, None)
                self.assertEqual([call.args[1] for call in process.call_args_list], [1, 2])
            self.assertEqual(batch.load(folder / f"{key}-1.json")["tipo"], "beneficio")
            self.assertEqual(batch.load(folder / f"{key}-2.json")["status"], "json_invalido")
            self.assertEqual(batch.load(folder / f"{key}-3.json")["tipo"], "requerimento")
            self.assertEqual(batch.load(folder / batch.AUTO_DONE), {"paginas_atualizadas": 1, "falhas": 1})
            batch.schedule_rule_reads(lot, detail)
            self.assertFalse((lot / batch.AUTO_QUEUE / (folder.name + ".json")).exists())


    def test_status_distingue_fila_worker_e_pagina_sem_movimentacao(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lot = root / ("c" * 32)
            queue = lot / batch.AUTO_QUEUE
            queue.mkdir(parents=True)
            batch.save(lot / "batch.json", {"status": "concluido", "nome": "Lote teste"})
            task = queue / "processo.json"
            batch.save(task, [])
            with patch.object(batch, "ROOT", root), patch.object(batch.time, "time", return_value=1200):
                self.assertEqual(batch.rule_read_overview()["lotes"][0]["status"], "sem_worker")
                batch.save(root / batch.WORKER_HEARTBEAT, {"id": "worker-novo", "atualizado_em": 1199})
                progress = {"worker_id": "worker-antigo", "atual": {"processo_id": "processo"},
                            "atualizado_em": 1199, "processos_concluidos": 2}
                batch.save(lot / batch.AUTO_PROGRESS, progress)
                self.assertEqual(batch.rule_read_overview()["lotes"][0]["status"], "aguardando")
                progress["worker_id"] = "worker-novo"
                batch.save(lot / batch.AUTO_PROGRESS, progress)
                self.assertEqual(batch.rule_read_overview()["lotes"][0]["status"], "rodando")
                progress["atualizado_em"] = 200
                batch.save(lot / batch.AUTO_PROGRESS, progress)
                self.assertEqual(batch.rule_read_overview()["lotes"][0]["status"], "sem_progresso")
                task.unlink()
                row = batch.rule_read_overview()["lotes"][0]
                self.assertEqual((row["status"], row["aguardando"], row["processos_concluidos"]),
                                 ("concluida", 0, 2))
                self.assertEqual(batch.rule_read_overview("outro-lote")["lotes"], [])


class TempoEfetivo(unittest.TestCase):
    def test_usa_duracao_concluida_e_divide_pelos_processos_do_lote(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(batch, "ROOT", Path(directory)):
            for key, status, duration, count in (
                ("L1", "concluido", 6_000_000, 150),
                ("L2", "concluido", 3_000_000, 100),
                ("L3", "processando", 1_000_000, 100),
            ):
                lot = batch.ROOT / key
                lot.mkdir()
                batch.save(lot / "batch.json", {"status": status, "desempenho": {"duracao_ms": duration}})
                batch.save(lot / "manifest.json", [{}] * count)
            self.assertEqual(batch.reading_rate(), {"ritmo_processos_hora": 100.0, "tempo_efetivo_processo_ms": 36_000})
            self.assertEqual(batch.reading_rate("L1"), {"ritmo_processos_hora": 90.0, "tempo_efetivo_processo_ms": 40_000})
            self.assertIsNone(batch.reading_rate("L3")["tempo_efetivo_processo_ms"])
