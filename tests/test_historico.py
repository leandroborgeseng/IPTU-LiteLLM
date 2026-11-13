"""Persistência do histórico (SQLite), exclusão de processo e exportação."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import historico  # noqa: E402

try:
    import batch  # noqa: E402
except ModuleNotFoundError as error:
    batch = None


def summary(resultado, requerente="MARIA SOUZA", **extra):
    return {"resultado": resultado, "requerente": requerente, "sei": "3516200.410.00000001-2026-01",
            "sintese": "Viúvo(a), aposentadoria", "motivos": ["motivo"], **extra}


class Banco(unittest.TestCase):
    def setUp(self):
        historico.init(Path(tempfile.mkdtemp()) / "iptu.db")

    def test_versoes_so_quando_o_parecer_muda(self):
        historico.upsert("L", "P", "lote.zip", "proc", summary("em_analise"), {"r": 1}, "a1", "leitura")
        historico.upsert("L", "P", "lote.zip", "proc", summary("em_analise"), {"r": 1}, "a2", "atualizacao")
        historico.upsert("L", "P", "lote.zip", "proc", summary("enquadra"), {"r": 2}, "a3", "conferencia_manual")
        versions = historico.versions("L", "P")
        self.assertEqual([(v["versao"], v["motivo"], v["resultado"]) for v in versions],
                         [(1, "leitura", "em_analise"), (2, "conferencia_manual", "enquadra")])
        rows, _ = historico.search()
        self.assertEqual((rows[0]["versao"], rows[0]["resultado"]), (2, "enquadra"))

    def test_reprocessamento_mantem_o_ultimo_parecer(self):
        historico.upsert("L", "P", "lote.zip", "proc", summary("enquadra"), {"r": 1}, "a1", "leitura")
        historico.upsert("L", "P", "lote.zip", "proc", {"resultado": "processando"}, None, "a2", "reanalise")
        rows, _ = historico.search(completo=True)
        self.assertEqual((rows[0]["resultado"], rows[0]["parecer"]), ("processando", {"r": 1}))

    def test_busca_filtro_e_ordem(self):
        historico.upsert("L", "A", "lote", "a", summary("enquadra", "ROZELI DE OLIVEIRA", renda_familiar_centavos=162100), {"a": 1}, "s", "x")
        historico.upsert("L", "B", "lote", "b", summary("nao_enquadra", "LEOMAR MOURA", renda_familiar_centavos=400000), {"b": 1}, "s", "x")
        historico.upsert("L", "C", "lote", "c", summary("em_analise", "NEUSA SICCHIEROLLI", renda_familiar_centavos=300000), {"c": 1}, "s", "x")
        self.assertEqual([r["processo_id"] for r in historico.search(q="rozeli")[0]], ["A"])
        self.assertEqual({r["processo_id"] for r in historico.search(filtro="passaram")[0]}, {"A"})
        self.assertEqual({r["processo_id"] for r in historico.search(filtro="nao_passaram")[0]}, {"B", "C"})
        self.assertEqual([r["processo_id"] for r in historico.search(ordem="renda", direcao="asc")[0]], ["A", "C", "B"])
        _, totals = historico.search()
        self.assertEqual(totals, {"enquadra": 1, "nao_enquadra": 1, "em_analise": 1})

    def test_exclusao_registra_auditoria_sem_dados_pessoais(self):
        historico.upsert("L", "P", "lote", "proc", summary("enquadra"), {"cpf": "111"}, "s", "x")
        historico.delete("L", "P", "3516200.410.00000001-2026-01", "iptu")
        self.assertEqual(historico.search()[0], [])
        self.assertEqual(historico.versions("L", "P"), [])
        entry = historico.audit()[0]
        self.assertEqual((entry["evento"], entry["sei"], entry["usuario"]), ("excluido", "3516200.410.00000001-2026-01", "iptu"))


@unittest.skipIf(batch is None, "dependência ausente")
class ExclusaoEExportacao(unittest.TestCase):
    def setUp(self):
        historico.init(Path(tempfile.mkdtemp()) / "iptu.db")
        self.lot = Path(tempfile.mkdtemp()) / ("e" * 32)
        (self.lot / "files").mkdir(parents=True)
        self.keep, self.gone = "1" * 32, "2" * 32
        for key in (self.keep, self.gone):
            (self.lot / "files" / key).write_bytes(b"%PDF")
        (self.lot / "upload").write_bytes(b"zip")
        self.p1, self.p2 = "a" * 32, "b" * 32
        batch.save(self.lot / "manifest.json", [
            {"id": self.p1, "processo": "SEI-1", "arquivos": [{"id": self.keep, "origem": "1/a.pdf", "erro": None}]},
            {"id": self.p2, "processo": "SEI-2", "arquivos": [{"id": self.gone, "origem": "2/b.pdf", "erro": None}]}])
        for pid, key in ((self.p1, self.keep), (self.p2, self.gone)):
            (self.lot / pid).mkdir()
            batch.save(self.lot / pid / f"{key}.json", {"paginas": 1, "total": 1})
            batch.save(self.lot / pid / f"{key}-1.json", {"status": "extraido", "tipo": "outro", "obtido": {}})
            batch.save(self.lot / pid / "complete.json", {"concluido": True})
        batch.save(self.lot / "batch.json", {"id": self.lot.name, "nome": "lote.zip", "bytes": 3, "status": "concluido"})

    def test_exclui_so_o_processo_e_seus_arquivos(self):
        code, _ = batch.delete_process(self.lot, self.p2)
        self.assertEqual(code, 200)
        self.assertFalse((self.lot / self.p2).exists())
        self.assertFalse((self.lot / "files" / self.gone).exists())
        self.assertTrue((self.lot / "files" / self.keep).exists())
        self.assertFalse((self.lot / "upload").exists())  # o ZIP original continha o processo excluído
        self.assertEqual([p["id"] for p in batch.load(self.lot / "manifest.json")], [self.p1])
        self.assertEqual(batch.describe(self.lot)["recebido"], 3)

    def test_nao_exclui_durante_o_processamento(self):
        batch.save(self.lot / "batch.json", {"id": self.lot.name, "nome": "lote.zip", "bytes": 3, "status": "processando"})
        self.assertEqual(batch.delete_process(self.lot, self.p2)[0], 409)

    def test_sincroniza_e_exporta(self):
        batch.sync_process(self.lot, self.p1, "leitura")
        rows, _ = historico.search(completo=True)
        document = rows[0]["parecer"]
        self.assertEqual(document["versao_formato"], historico.EXPORT_VERSION)
        self.assertEqual(document["processo"]["nome"], "SEI-1")
        self.assertEqual(document["lote"]["nome"], "lote.zip")
        self.assertIn("criterios", document["parecer"])
        self.assertEqual(historico.stored_signature(self.lot.name, self.p1), batch.process_signature(self.lot, self.p1))


if __name__ == "__main__":
    unittest.main()
