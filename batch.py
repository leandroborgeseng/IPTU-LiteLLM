"""Persistent pilot imports. Original paths are metadata, never filesystem destinations."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from threading import Event, RLock, Thread

import parecer

ROOT = Path(os.environ.get("IPTU_DATA_DIR", "/data"))
CHUNK = 8 * 1024**2
MAX_UPLOAD = 48 * 1024**3
MAX_EXTRACTED = 150 * 1024**3
MAX_ENTRIES = 100_000
PROCESS_ID = re.compile(r"(?<![0-9])[0-9]{7}\.[0-9]{3}\.[0-9]{8}-[0-9]{4}-[0-9]{2}(?![0-9])")
ID = re.compile(r"[a-f0-9]{32}")
WAKE = Event()
LOCK = RLock()
KINDS = ("requerimento", "identidade", "cpf", "residencia", "beneficio", "posse",
         "procuracao", "certidao", "laudo", "holerite", "outro")
FIELDS = {"exercicio": int, "reside_no_imovel": bool, "nome_requerente": str,
          "cpf_requerente": str, "cpf_conjuge": str, "inscricao_imobiliaria": str,
          "modalidade": str}
ASSESSMENT_FIELDS = {
    "modalidade_criterio": str, "data_protocolo": str, "endereco_declarado": str,
    "possui_edicula": bool, "edicula_alugada": bool, "edicula_uso_comercial": bool,
    "vinculo_imovel": str, "renda_bruta_centavos": int, "competencia_renda": str,
    "titular_renda": str, "atividade_economica_declarada": bool,
}
PAGE_SCHEMA = 2


class ResetRequested(Exception):
    pass


def reset_pending():
    return (ROOT / ".limpeza_pendente").exists()


def check_reset():
    if reset_pending():
        raise ResetRequested


def request_reset():
    with LOCK:
        (ROOT / ".limpeza_pendente").touch()
        WAKE.set()


def clear_batches():
    with LOCK:
        for path in ROOT.iterdir():
            if ID.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        (ROOT / ".limpeza_pendente").unlink(missing_ok=True)


def save(path, value):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        try:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
            os.replace(f.name, path)
        finally:
            Path(f.name).unlink(missing_ok=True)


def load(path):
    return json.loads(path.read_text())


def batch_dir(key):
    if not ID.fullmatch(key):
        raise FileNotFoundError
    path = ROOT / key
    if not (path / "batch.json").is_file():
        raise FileNotFoundError
    return path


def create(name, size):
    if not isinstance(name, str) or not 0 < len(name) <= 255 or type(size) is not int:
        raise ValueError
    if Path(name).suffix.lower() not in {".pdf", ".zip"} or not 0 < size <= MAX_UPLOAD:
        raise ValueError
    if size > shutil.disk_usage(ROOT).free:
        raise OSError("espaco_insuficiente")
    path = ROOT / uuid.uuid4().hex
    path.mkdir(mode=0o700)
    (path / "upload").touch(mode=0o600)
    value = {"id": path.name, "nome": name, "bytes": size, "status": "recebendo", "criado": time.time()}
    save(path / "batch.json", value)
    return value


def describe(path):
    value = load(path / "batch.json")
    value["recebido"] = (path / "upload").stat().st_size
    return value


def append(path, start, data):
    with LOCK:
        check_reset()
        value = describe(path)
        if value["status"] != "recebendo" or start != value["recebido"]:
            return 409, value
        if not data or start + len(data) > value["bytes"]:
            return 400, {"status": "parte_invalida"}
        with (path / "upload").open("ab") as target:
            try:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            except OSError:
                target.truncate(start)
                raise
        return 200, describe(path)


def queue(path):
    with LOCK:
        check_reset()
        value = describe(path)
        if value["status"] == "recebendo" and value["bytes"] == value["recebido"]:
            value["status"] = "na_fila"
            save(path / "batch.json", value)
            WAKE.set()
        return value


def copy_member(source, target, limit):
    with target.open("wb") as out:
        written = 0
        while chunk := source.read(1024**2):
            check_reset()
            written += len(chunk)
            if written > limit:
                raise ValueError("limite_de_extracao")
            out.write(chunk)
        return written


def sei_numbers(origin):
    return set(PROCESS_ID.findall(origin))


def sei_identity(process):
    found = []
    for item in process["arquivos"]:
        parts = PurePosixPath(item["origem"]).parts
        for index, part in enumerate(parts):
            for number in sorted(sei_numbers(part)):
                found.append({"numero": number, "origem": item["origem"],
                              "fonte": "nome_do_arquivo" if index == len(parts) - 1 else "caminho"})
    values = {source["numero"] for source in found}
    return {"numero_processo_sei": next(iter(values)) if len(values) == 1 else None,
            "sei_divergente": len(values) > 1, "fontes_numero_sei": found}


def inventory(path, name):
    files = path / "files"
    files.mkdir(exist_ok=True)
    for partial in files.iterdir():
        partial.unlink()
    upload = path / "upload"
    processes = {}
    total = entries = 0

    def add(original, file_id=None, error=None):
        parts = PurePosixPath(original).parts
        numbers = sei_numbers(original)
        external = next(iter(numbers), None) if len(numbers) == 1 else None
        key = external or (parts[0] if len(parts) > 1 else Path(name).stem)
        if len(numbers) > 1:
            key = "Divergência SEI · " + " / ".join(sorted(numbers))
        process = processes.setdefault(key, {"id": uuid.uuid4().hex, "processo": key, "arquivos": []})
        process["arquivos"].append({"id": file_id, "origem": original, "erro": error})

    def unzip(source, prefix="", depth=0):
        nonlocal total, entries
        with zipfile.ZipFile(source) as archive:
            for item in archive.infolist():
                check_reset()
                if item.is_dir():
                    continue
                entries += 1
                if entries > MAX_ENTRIES:
                    raise ValueError("limite_de_entradas")
                original = prefix + item.filename
                parts = PurePosixPath(item.filename.replace(chr(92), "/"))
                if parts.is_absolute() or ".." in parts.parts or len(original) > 1024:
                    add(prefix + "[entrada inválida]", error="caminho_invalido")
                    continue
                suffix = parts.suffix.lower()
                if parts.name.startswith(".") or "__MACOSX" in parts.parts:
                    continue
                if suffix not in {".pdf", ".zip"}:
                    add(original, error="formato_nao_processado")
                    continue
                if suffix == ".zip" and depth >= 3:
                    add(original, error="limite_de_zips_internos")
                    continue
                if item.file_size > MAX_EXTRACTED - total:
                    add(original, error="limite_de_extracao")
                    continue
                if item.file_size > shutil.disk_usage(path).free:
                    raise OSError("espaco_insuficiente")
                key = uuid.uuid4().hex
                target = files / key
                retained = False
                try:
                    with archive.open(item) as member:
                        total += copy_member(member, target, MAX_EXTRACTED - total)
                    if suffix == ".zip":
                        unzip(target, original + "/", depth + 1)
                    else:
                        add(original, key)
                        retained = True
                except (RuntimeError, zipfile.BadZipFile, NotImplementedError, ValueError) as exc:
                    add(original, error="limite_de_entradas" if str(exc) == "limite_de_entradas" else "arquivo_inacessivel")
                finally:
                    if not retained:
                        target.unlink(missing_ok=True)

    if Path(name).suffix.lower() == ".pdf":
        key = uuid.uuid4().hex
        os.link(upload, files / key)
        add(name, key)
    else:
        unzip(upload)
    return list(processes.values())


def render_page(pdf, number):
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "pagina.png"
        completed = subprocess.run(
            ["pdftoppm", "-f", str(number), "-l", str(number), "-singlefile",
             "-scale-to", "1800", "-gray", "-png", str(pdf), str(png.with_suffix(""))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
        if completed.returncode or not png.exists():
            return None
        return png.read_bytes()


def process_batch(path, analyze):
    state = load(path / "batch.json")
    check_reset()
    state["status"] = "processando"
    state.pop("progresso", None)
    save(path / "batch.json", state)
    manifest = path / "manifest.json"
    if not manifest.exists():
        save(manifest, inventory(path, state["nome"]))
    processes = load(manifest)
    for process in processes:
        folder = path / process["id"]
        folder.mkdir(exist_ok=True)
        for file in process["arquivos"]:
            check_reset()
            if not file["id"]:
                continue
            key = file["id"]
            pdf = path / "files" / key
            count_path = folder / (key + ".json")
            if not count_path.exists():
                try:
                    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True,
                                          timeout=15, text=True, env={**os.environ, "LC_ALL": "C"})
                    match = re.search(r"^Pages:\s+(\d+)$", info.stdout, re.MULTILINE)
                    count = int(match[1]) if not info.returncode and match else 0
                except (OSError, subprocess.TimeoutExpired, UnicodeError):
                    count = 0
                save(count_path, {"paginas": min(count, 1000), "total": count})
            count = load(count_path)["paginas"]
            for number in range(1, count + 1):
                check_reset()
                evidence = folder / f"{key}-{number}.json"
                if evidence.exists():
                    previous = load(evidence)
                    if previous.get("versao_extracao") == PAGE_SCHEMA and previous.get("status") == "extraido":
                        continue
                state["progresso"] = {"pagina": number, "total_paginas": count, "etapa": "renderizando"}
                save(path / "batch.json", state)
                result = {"status": "pagina_inacessivel"}
                try:
                    image = render_page(pdf, number)
                    if image is not None:
                        check_reset()
                        state["progresso"]["etapa"] = "consultando_ia"
                        save(path / "batch.json", state)
                        result = analyze(image)
                        if result["status"] == "falha_de_conexao" or result.get("codigo") in {429, 502, 503, 504}:
                            check_reset()
                            state["progresso"]["etapa"] = "nova_tentativa_ia"
                            save(path / "batch.json", state)
                            time.sleep(1)
                            result = analyze(image)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                check_reset()
                save(evidence, {**result, "versao_extracao": PAGE_SCHEMA})
        save(folder / "complete.json", {"concluido": True})
    state.pop("progresso", None)
    state["status"] = "concluido"
    save(path / "batch.json", state)


def run_worker(analyze):
    while True:
        WAKE.clear()
        if reset_pending():
            try:
                clear_batches()
            except OSError as error:
                print(f"Falha ao limpar lotes: {type(error).__name__}", flush=True)
                WAKE.wait(3)
            continue
        for meta in ROOT.glob("*/batch.json"):
            if reset_pending():
                break
            try:
                state = load(meta)
            except (OSError, ValueError) as error:
                print(f"Falha ao carregar lote {meta.parent.name}: {type(error).__name__}", flush=True)
                continue
            if state["status"] not in {"na_fila", "processando"}:
                continue
            try:
                process_batch(meta.parent, analyze)
            except ResetRequested:
                break
            except Exception as exc:
                print(f"Falha no lote {meta.parent.name}: {type(exc).__name__}", flush=True)
                state["status"] = "falha"
                state["motivo"] = (
                    "zip_invalido" if isinstance(exc, zipfile.BadZipFile)
                    else str(exc) if str(exc) in {"espaco_insuficiente", "limite_de_entradas"}
                    else "falha_de_importacao"
                )
                try:
                    save(meta, state)
                except OSError as error:
                    print(f"Falha ao salvar status do lote {meta.parent.name}: {type(error).__name__}", flush=True)
        if not reset_pending():
            WAKE.wait(3)


def start_worker(analyze):
    ROOT.mkdir(parents=True, exist_ok=True)
    Thread(target=run_worker, args=(analyze,), daemon=True).start()


def process_detail(path, process_id):
    processes = load(path / "manifest.json") if (path / "manifest.json").exists() else []
    process = next((item for item in processes if item["id"] == process_id), None)
    if process is None:
        raise FileNotFoundError
    folder = path / process_id
    candidates = []
    documents = []
    for item in process["arquivos"]:
        key = item["id"]
        if not key:
            continue
        meta = folder / (key + ".json")
        if not meta.exists():
            continue
        counts = load(meta)
        if not counts["paginas"]:
            item["erro"] = "pdf_inacessivel"
        if counts["total"] > counts["paginas"]:
            item["erro"] = "limite_de_paginas"
        for number in range(1, counts["paginas"] + 1):
            evidence = folder / f"{key}-{number}.json"
            if not evidence.exists():
                continue
            result = load(evidence)
            row = {"arquivo": key, "origem": item["origem"], "pagina": number, **result}
            documents.append(row)
            if result.get("tipo") == "requerimento":
                candidates.append(row)
    process["paginas"] = documents
    process["requerimentos"] = candidates
    process["status"] = "em_revisao" if (folder / "complete.json").exists() else "processando"
    process["pendencias"] = ["Conferir os campos e anexos com as páginas de origem.", "Consulta à SIL pendente."]
    if not candidates:
        process["pendencias"].append("Nenhuma página foi classificada como requerimento.")
    if any(item["erro"] for item in process["arquivos"]) or any(row["status"] != "extraido" for row in documents):
        process["pendencias"].append("Há arquivos ou páginas com falha de leitura.")
    review = folder / "review.json"
    process["revisao"] = load(review) if review.exists() else None
    process.update(sei_identity(process))
    process["parecer"] = parecer.assess(process, process["revisao"])
    return process
