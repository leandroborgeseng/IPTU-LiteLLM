"""Importação e montagem do processo (sem modelo): continuação do formulário e HTML do SEI."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

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
