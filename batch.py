"""Persistent pilot imports. Original paths are metadata, never filesystem destinations."""
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from threading import Event, RLock, Thread

import parecer

from PIL import Image, ImageOps


IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
DOCUMENT_EXTENSIONS = {".pdf", *IMAGE_FORMATS}
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
         "procuracao", "certidao", "laudo", "holerite", "execucao_fiscal",
         "peticao_judicial", "despacho", "capa", "carnê_iptu", "imunidade_tributaria", "outro")
FIELDS = {"exercicio": int, "reside_no_imovel": bool, "nome_requerente": str,
          "cpf_requerente": str, "cpf_conjuge": str, "inscricao_imobiliaria": str,
          "modalidade": str}
ASSESSMENT_FIELDS = {
    "modalidade_criterio": str, "data_protocolo": str, "endereco_declarado": str,
    "possui_edicula": bool, "edicula_alugada": bool, "edicula_uso_comercial": bool,
    "vinculo_imovel": str, "renda_bruta_centavos": int, "competencia_renda": str,
    "titular_renda": str, "atividade_economica_declarada": bool,
}
PAGE_SCHEMA = 4


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
    if Path(name).suffix.lower() not in DOCUMENT_EXTENSIONS | {".zip"} or not 0 < size <= MAX_UPLOAD:
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

    def process_group(original):
        parts = PurePosixPath(original).parts
        folders = parts[:-1]
        for index, folder in enumerate(folders):
            if PROCESS_ID.fullmatch(folder):
                return ("pasta", *folders[:index + 1]), folder
        if folders:
            return ("pasta", *folders), "/".join(folders)
        numbers = sei_numbers(parts[-1])
        if Path(name).suffix.lower() in DOCUMENT_EXTENSIONS and len(numbers) == 1:
            return ("raiz",), next(iter(numbers))
        label = Path(name).stem
        return ("raiz",), label

    def add(original, file_id=None, error=None, group=None):
        key, label = group or process_group(original)
        process = processes.setdefault(key, {
            "id": uuid.uuid4().hex, "processo": label, "arquivos": [],
        })
        process["arquivos"].append({"id": file_id, "origem": original, "erro": error})

    def unzip(source, prefix="", depth=0, parent_group=None):
        nonlocal total, entries
        with zipfile.ZipFile(source) as archive:
            for item in archive.infolist():
                check_reset()
                if item.is_dir():
                    continue
                entries += 1
                if entries > MAX_ENTRIES:
                    raise ValueError("limite_de_entradas")
                relative = item.filename.replace(chr(92), "/")
                original = prefix + relative
                parts = PurePosixPath(relative)
                if parts.is_absolute() or ".." in parts.parts or len(original) > 1024:
                    add(prefix + "[entrada inválida]", error="caminho_invalido", group=parent_group)
                    continue
                group = parent_group or process_group(original)
                suffix = parts.suffix.lower()
                if parts.name.startswith(".") or "__MACOSX" in parts.parts:
                    continue
                if suffix not in DOCUMENT_EXTENSIONS | {".zip"}:
                    add(original, error="formato_nao_processado", group=group)
                    continue
                if suffix == ".zip" and depth >= 3:
                    add(original, error="limite_de_zips_internos", group=group)
                    continue
                if item.file_size > MAX_EXTRACTED - total:
                    add(original, error="limite_de_extracao", group=group)
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
                        # An archive inside a process folder is an attachment to that process.
                        attachment_group = group if parent_group or len(parts.parts) > 1 else None
                        unzip(target, original + "/", depth + 1, attachment_group)
                    else:
                        add(original, key, group=group)
                        retained = True
                except (RuntimeError, zipfile.BadZipFile, NotImplementedError, ValueError) as exc:
                    add(original, error="limite_de_entradas" if str(exc) == "limite_de_entradas" else "arquivo_inacessivel", group=group)
                finally:
                    if not retained:
                        target.unlink(missing_ok=True)

    if Path(name).suffix.lower() in DOCUMENT_EXTENSIONS:
        key = uuid.uuid4().hex
        os.link(upload, files / key)
        add(name, key)
    else:
        unzip(upload)
    return list(processes.values())


def render_page(pdf, number, extension=".pdf"):
    if extension in IMAGE_FORMATS:
        if number != 1:
            return None
        try:
            with Image.open(pdf, formats=[IMAGE_FORMATS[extension]]) as photo:
                if photo.width * photo.height > 50_000_000:
                    return None
                photo.thumbnail((1800, 1800))
                normalized = ImageOps.exif_transpose(photo).convert("RGBA")
                page = Image.new("RGB", normalized.size, "white")
                page.paste(normalized, mask=normalized.getchannel("A"))
                output = io.BytesIO()
                page.save(output, format="PNG")
                return output.getvalue()
        except (OSError, ValueError, Image.DecompressionBombError):
            return None
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


def native_page_text(pdf, number):
    try:
        completed = subprocess.run(
            ["pdftotext", "-f", str(number), "-l", str(number), "-layout", str(pdf), "-"],
            capture_output=True, timeout=20,
        )
        if completed.returncode == 0:
            return {"texto_pdf": completed.stdout.decode("utf-8", errors="replace").strip()}
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {}


def cache_preview(evidence, image):
    directory = evidence.parent.parent / "previews"
    directory.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
        try:
            temporary.write(image)
            temporary.flush()
            os.replace(temporary.name, directory / (evidence.stem + ".png"))
        finally:
            Path(temporary.name).unlink(missing_ok=True)


def normalize_text(text):
    folded = unicodedata.normalize("NFKD", text.lower())
    return " ".join(re.findall(r"[a-z0-9]+", "".join(
        character for character in folded if not unicodedata.combining(character)
    )))


def validate_page_kind(result, texts):
    if result.get("tipo") != "requerimento":
        return result
    quote = normalize_text(result.get("evidencia_tipo") or "")
    has_tax = "iptu" in quote or "imposto predial e territorial urbano" in quote
    has_request = any(word.startswith(("requer", "solicit", "pedido", "declar", "formulario"))
                      for word in quote.split())
    if (has_tax and has_request and "isencao" in quote and any(
        quote in normalize_text(texts.get(key, "")) for key in ("texto_ocr", "texto_pdf")
    )):
        return {**result, "requerimento_isencao_confirmado": True}
    return {**result, "tipo": "outro", "obtido": {},
            "motivo_classificacao": "Requerimento de isenção sem trecho confirmado no texto da página."}


def page_workers():
    try:
        return max(1, min(16, int(os.environ.get("IPTU_PAGE_WORKERS", "8"))))
    except ValueError:
        return 8


def process_page(pdf, number, evidence, analyze, extension=".pdf"):
    check_reset()
    result = {"status": "pagina_inacessivel"}
    texts = {}
    timing = {}
    attempts = 0
    stage = "renderizacao_ms"
    started = time.monotonic()
    try:
        image = render_page(pdf, number, extension)
        if image is not None:
            timing[stage] = round((time.monotonic() - started) * 1000)
            check_reset()
            if os.environ.get("IPTU_CACHE_PREVIEWS") == "1":
                cache_preview(evidence, image)
            if extension == ".pdf":
                stage = "texto_pdf_ms"
                started = time.monotonic()
                texts = native_page_text(pdf, number)
                timing[stage] = round((time.monotonic() - started) * 1000)
            check_reset()
            stage = "litellm_ms"
            started = time.monotonic()
            attempts += 1
            result = analyze(image, texts)
            if result["status"] == "falha_de_conexao" or result.get("codigo") in {429, 502, 503, 504}:
                time.sleep(1)
                check_reset()
                attempts += 1
                result = analyze(image, texts)
    except (OSError, subprocess.TimeoutExpired):
        pass
    timing[stage] = round((time.monotonic() - started) * 1000)
    check_reset()
    if "texto_ocr" in result:
        texts["texto_ocr"] = result.pop("texto_ocr")
        texts["status_ocr"] = "texto_encontrado" if texts["texto_ocr"] else "sem_texto"
    else:
        texts["status_ocr"] = "falha_ocr"
    result = validate_page_kind(result, texts)
    save(evidence, {**result, **texts, "versao_extracao": PAGE_SCHEMA,
                    "tempos_ms": timing, "tentativas_ia": attempts})
    return timing, attempts, result["status"] != "extraido" or texts.get("status_ocr") == "falha_ocr"


def process_batch(path, analyze):
    state = load(path / "batch.json")
    check_reset()
    state["status"] = "processando"
    state.pop("progresso", None)
    state["desempenho"] = {
        "paginas_processadas": 0, "paginas_com_falha": 0, "novas_tentativas": 0,
        "duracao_ms": 0, "etapas": {},
    }
    batch_started = time.monotonic()
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
            extension = PurePosixPath(file["origem"]).suffix.lower()
            count_path = folder / (key + ".json")
            if extension in IMAGE_FORMATS and not count_path.exists():
                save(count_path, {"paginas": 1, "total": 1})
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
            pending = []
            for number in range(1, count + 1):
                check_reset()
                evidence = folder / f"{key}-{number}.json"
                if evidence.exists():
                    previous = load(evidence)
                    if (previous.get("versao_extracao") == PAGE_SCHEMA
                        and previous.get("status") == "extraido"
                        and previous.get("status_ocr") != "falha_ocr"
                        and (os.environ.get("IPTU_CACHE_PREVIEWS") != "1"
                             or (path / "previews" / (evidence.stem + ".png")).is_file())):
                        continue
                pending.append((number, evidence))
            workers = min(page_workers(), len(pending))
            if workers:
                progress = {"paginas_concluidas": count - len(pending),
                            "total_paginas": count, "paralelismo": workers}
                state["progresso"] = progress
                save(path / "batch.json", state)
                pool = ThreadPoolExecutor(max_workers=workers)
                try:
                    jobs = [pool.submit(process_page, pdf, number, evidence, analyze, extension)
                            for number, evidence in pending]
                    for job in as_completed(jobs):
                        timing, attempts, failed = job.result()
                        performance = state["desempenho"]
                        performance["paginas_processadas"] += 1
                        performance["paginas_com_falha"] += int(failed)
                        performance["novas_tentativas"] += max(0, attempts - 1)
                        performance["duracao_ms"] = round((time.monotonic() - batch_started) * 1000)
                        for stage, duration in timing.items():
                            stats = performance["etapas"].setdefault(stage, {"soma_ms": 0, "paginas": 0})
                            stats["soma_ms"] += duration
                            stats["paginas"] += 1
                        progress["paginas_concluidas"] += 1
                        save(path / "batch.json", state)
                finally:
                    pool.shutdown(wait=True, cancel_futures=True)
        save(folder / "complete.json", {"concluido": True})
    state.pop("progresso", None)
    state["status"] = "concluido"
    state["desempenho"]["duracao_ms"] = round((time.monotonic() - batch_started) * 1000)
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


def original_document(path, file_id):
    manifest = load(path / "manifest.json") if (path / "manifest.json").is_file() else []
    for process in manifest:
        for file in process["arquivos"]:
            if file["id"] == file_id:
                return PurePosixPath(file["origem"])
    raise FileNotFoundError


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
            if result.get("requerimento_isencao_confirmado"):
                candidates.append(row)
    process["paginas"] = documents
    process["requerimentos"] = candidates
    process["status"] = "em_revisao" if (folder / "complete.json").exists() else "processando"
    process["pendencias"] = ["Conferir os campos e anexos com as páginas de origem.", "Consulta à SIL pendente."]
    if not candidates:
        process["pendencias"].append("Nenhum requerimento de isenção de IPTU foi confirmado no texto das páginas.")
    if any(item["erro"] for item in process["arquivos"]) or any(row["status"] != "extraido" or row.get("status_ocr") == "falha_ocr" for row in documents):
        process["pendencias"].append("Há arquivos ou páginas com falha de leitura.")
    review = folder / "review.json"
    process["revisao"] = load(review) if review.exists() else None
    process.update(sei_identity(process))
    process["parecer"] = parecer.assess(process, process["revisao"])
    return process
