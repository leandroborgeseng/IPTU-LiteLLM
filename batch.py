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
from threading import BoundedSemaphore, Event, Lock, RLock, Thread

import parecer
import sil

from PIL import Image, ImageOps


IMAGE_FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}
HTML_EXTENSIONS = {"", ".html", ".htm"}
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


def available_cpus():
    """Núcleos realmente disponíveis ao container (cota do cgroup), não os do host."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return max(1, int(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


# Renderização é CPU pesada: limitada aos núcleos do container e com prioridade baixa,
# para o servidor web continuar respondendo (envio de lotes, painel) durante o processamento.
RENDER_SLOTS = BoundedSemaphore(int(os.environ.get("IPTU_RENDER_WORKERS", "0") or 0)
                                or max(1, available_cpus() - 1))
NICE = ["nice", "-n", "15"] if shutil.which("nice") else []
UPLOAD_LOCKS = {}
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
    "numero_beneficio": str, "especie_beneficio": str, "usufrutuario": bool,
    "imovel_ponto_comercial": bool, "edicula_habitada": bool,
    "edicula_ocupante_familiar": bool, "possui_outro_imovel": bool,
    "estado_civil": str, "nome_conjuge": str, "conjuge_informado": bool,
    "conjuge_renda_centavos": int, "outras_rendas": bool, "outras_rendas_centavos": int,
    "renda_atividade_centavos": int, "trabalha_registrado": bool, "data_requerimento": str,
    "beneficio_aposentadoria_numero": str, "beneficio_aposentadoria_centavos": int,
    "beneficio_pensao_numero": str, "beneficio_pensao_centavos": int,
    "beneficio_bpc_numero": str, "beneficio_bpc_centavos": int,
    "nome_documento": str, "cpf_documento": str, "filiacao_documento": str,
    "nome_ocupante_edicula": str, "cpf_ocupante_edicula": str, "conjuge_beneficio_numero": str,
}
INCOME_FIELDS = {"renda_bruta_centavos", "competencia_renda", "titular_renda",
                 "numero_beneficio", "especie_beneficio"}
ID_FIELDS = {"nome_documento", "cpf_documento", "filiacao_documento"}
PAGE_SCHEMA = 8


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


def append(path, start, data, timing=None):
    """Grava uma parte do envio. Usa uma trava por lote: o fsync não bloqueia o resto do servidor."""
    check_reset()
    with LOCK:
        lock = UPLOAD_LOCKS.setdefault(path.name, Lock())
    with lock:
        value = describe(path)
        if value["status"] != "recebendo" or start != value["recebido"]:
            return 409, value
        if not data or start + len(data) > value["bytes"]:
            return 400, {"status": "parte_invalida"}
        began = time.monotonic()
        with (path / "upload").open("ab") as target:
            try:
                target.write(data)
                target.flush()
                written = time.monotonic()
                os.fsync(target.fileno())
            except OSError:
                target.truncate(start)
                raise
        if timing is not None:
            timing.update(escrita_s=written - began, fsync_s=time.monotonic() - written)
        return 200, describe(path)


def queue(path):
    with LOCK:
        UPLOAD_LOCKS.pop(path.name, None)
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
    document_dirs = set()
    total = entries = 0

    def process_group(original):
        parts = PurePosixPath(original).parts
        folders = parts[:-1]
        for index, folder in enumerate(folders):
            if PROCESS_ID.fullmatch(folder):
                return ("pasta", *folders[:index + 1]), folder
        if folders:
            # Subpastas (ex.: "anexos") pertencem à pasta de processo que já contém documentos.
            for size in range(2, len(folders)):
                if folders[:size] in document_dirs:
                    folders = folders[:size]
                    break
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
                if suffix in HTML_EXTENSIONS and item.file_size <= 5 * 1024**2:
                    with archive.open(item) as member:
                        head = member.read(512).lstrip().lower()
                    if head.startswith((b"<!doctype html", b"<html")):
                        key = uuid.uuid4().hex
                        with archive.open(item) as member:
                            total += copy_member(member, files / key, MAX_EXTRACTED - total)
                        add(original, key, group=group)
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
        with zipfile.ZipFile(upload) as archive:
            document_dirs.update(
                PurePosixPath(item.filename.replace(chr(92), "/")).parts[:-1]
                for item in archive.infolist()
                if not item.is_dir() and PurePosixPath(item.filename).suffix.lower() in DOCUMENT_EXTENSIONS)
        unzip(upload)
    return list(processes.values())


def render_page(pdf, number, extension=".pdf", size=1800, gray=True):
    with RENDER_SLOTS:
        return _render_page(pdf, number, extension, size, gray)


def _render_page(pdf, number, extension, size, gray):
    if extension in IMAGE_FORMATS:
        if number != 1:
            return None
        try:
            with Image.open(pdf, formats=[IMAGE_FORMATS[extension]]) as photo:
                if photo.width * photo.height > 50_000_000:
                    return None
                photo.thumbnail((size, size))
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
            [*NICE, "pdftoppm", "-f", str(number), "-l", str(number), "-singlefile",
             "-scale-to", str(size), *(["-gray"] if gray else []), "-png", str(pdf), str(png.with_suffix(""))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
        if completed.returncode or not png.exists():
            return None
        return png.read_bytes()


def html_text(path):
    """Texto de um documento HTML exportado do SEI (despachos, ofícios)."""
    import html as html_lib
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="strict") if raw.startswith(b"\xef\xbb\xbf") else None
    if text is None:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    text = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def native_page_text(pdf, number):
    try:
        completed = subprocess.run(
            [*NICE, "pdftotext", "-f", str(number), "-l", str(number), "-layout", str(pdf), "-"],
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
    return {**result, "tipo": "outro", "obtido": {}, "tipo_sugerido": "requerimento",
            "obtido_nao_confirmado": result.get("obtido", {}),
            "motivo_classificacao": "Requerimento de isenção sem trecho confirmado no texto da página."}


def page_workers():
    try:
        return max(1, min(64, int(os.environ.get("IPTU_PAGE_WORKERS", "16"))))
    except ValueError:
        return 16


def process_page(pdf, number, evidence, analyze, extension=".pdf"):
    check_reset()
    if extension in HTML_EXTENSIONS:
        # Documento nativo do SEI: o texto já é exato, sem OCR nem modelo.
        text = html_text(pdf)
        result = {"status": "extraido", "tipo": "despacho", "obtido": {}, "texto_pdf": text,
                  "texto_ocr": "", "status_ocr": "texto_nativo", "evidencia_tipo": text[:160],
                  "versao_extracao": PAGE_SCHEMA, "tempos_ms": {}, "tentativas_ia": 0}
        save(evidence, result)
        return {}, 0, False
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
            # Alta resolução colorida só é gerada se a página precisar de leitura detalhada.
            hires = lambda: render_page(pdf, number, extension, size=2600, gray=False)  # noqa: E731
            result = analyze(image, texts, hires=hires)
            if result["status"] == "falha_de_conexao" or result.get("codigo") in {429, 502, 503, 504}:
                time.sleep(1)
                check_reset()
                attempts += 1
                result = analyze(image, texts, hires=hires)
    except ResetRequested:
        raise
    except (OSError, subprocess.TimeoutExpired):
        pass
    except Exception as error:  # um erro numa página não pode derrubar o lote inteiro
        print(f"Falha na página {number} de {pdf.name}: {type(error).__name__}", flush=True)
        result = {"status": "falha_de_leitura", "erro": type(error).__name__}
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


PAGE_RESULT = re.compile(r"[a-f0-9]{32}-[0-9]+\.json")


def request_rerun(path, process_id=None):
    """Agenda nova leitura. Lote: páginas com falha ou de versão antiga. Processo: todas as páginas.

    Conferências manuais (review.json) são mantidas. Se o lote estiver em processamento,
    a nova rodada começa assim que a atual terminar.
    """
    with LOCK:
        check_reset()
        state = describe(path)
        if state["status"] == "recebendo":
            return 409, {"status": "envio_incompleto"}
        manifest = load(path / "manifest.json") if (path / "manifest.json").exists() else []
        targets = [item for item in manifest if process_id is None or item["id"] == process_id]
        if process_id and not targets:
            raise FileNotFoundError
        for item in targets:
            folder = path / item["id"]
            (folder / "complete.json").unlink(missing_ok=True)
            if process_id and folder.is_dir():
                for page in folder.iterdir():
                    if PAGE_RESULT.fullmatch(page.name):
                        page.unlink()
                (folder / "sil.json").unlink(missing_ok=True)
        if state["status"] in {"concluido", "falha"} or not manifest:
            state["status"] = "na_fila"
            state.pop("motivo", None)
            state.pop("recebido", None)
            save(path / "batch.json", state)
        else:
            (path / ".rodar_novamente").touch()
        WAKE.set()
        return 202, describe(path)


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
    # 1) Conta as páginas de todos os documentos. 2) Uma única fila paralela atende
    # todos os processos do lote, na ordem dos processos; cada processo é marcado
    # como concluído assim que suas páginas terminam, e o parecer já fica disponível.
    jobs_by_process = {}
    total_pages = 0
    for process in processes:
        folder = path / process["id"]
        folder.mkdir(exist_ok=True)
        pending = []
        for file in process["arquivos"]:
            check_reset()
            if not file["id"]:
                continue
            key = file["id"]
            pdf = path / "files" / key
            extension = PurePosixPath(file["origem"]).suffix.lower()
            count_path = folder / (key + ".json")
            if (extension in IMAGE_FORMATS or extension in HTML_EXTENSIONS) and not count_path.exists():
                save(count_path, {"paginas": 1, "total": 1})
            if not count_path.exists():
                try:
                    info = subprocess.run([*NICE, "pdfinfo", str(pdf)], capture_output=True,
                                          timeout=15, text=True, env={**os.environ, "LC_ALL": "C"})
                    match = re.search(r"^Pages:\s+(\d+)$", info.stdout, re.MULTILINE)
                    count = int(match[1]) if not info.returncode and match else 0
                except (OSError, subprocess.TimeoutExpired, UnicodeError):
                    count = 0
                save(count_path, {"paginas": min(count, 1000), "total": count})
            count = load(count_path)["paginas"]
            total_pages += count
            for number in range(1, count + 1):
                evidence = folder / f"{key}-{number}.json"
                if evidence.exists():
                    previous = load(evidence)
                    if (previous.get("versao_extracao") == PAGE_SCHEMA
                        and previous.get("status") == "extraido"
                        and previous.get("status_ocr") != "falha_ocr"
                        and (os.environ.get("IPTU_CACHE_PREVIEWS") != "1"
                             or (path / "previews" / (evidence.stem + ".png")).is_file())):
                        continue
                pending.append((pdf, number, evidence, extension))
        jobs_by_process[process["id"]] = pending
        if not pending:
            save(folder / "complete.json", {"concluido": True})
    remaining = {key: len(items) for key, items in jobs_by_process.items()}
    queued = sum(remaining.values())
    workers = min(page_workers(), queued)
    if workers:
        progress = {"paginas_concluidas": total_pages - queued, "total_paginas": total_pages,
                    "paralelismo": workers}
        state["progresso"] = progress
        save(path / "batch.json", state)
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            jobs = {pool.submit(process_page, pdf, number, evidence, analyze, extension): key
                    for key, items in jobs_by_process.items()
                    for pdf, number, evidence, extension in items}
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
                key = jobs[job]
                remaining[key] -= 1
                if not remaining[key]:
                    save(path / key / "complete.json", {"concluido": True})
                save(path / "batch.json", state)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    state.pop("progresso", None)
    state["status"] = "concluido"
    state["desempenho"]["duracao_ms"] = round((time.monotonic() - batch_started) * 1000)
    rerun = path / ".rodar_novamente"
    if rerun.exists():  # reanálise pedida durante esta rodada
        rerun.unlink()
        state["status"] = "na_fila"
    save(path / "batch.json", state)


def run_worker(analyze):
    print(f"Worker: {available_cpus()} CPU(s) no container; renderização em até "
          f"{RENDER_SLOTS._initial_value} página(s) por vez; {page_workers()} página(s) em leitura paralela.",
          flush=True)
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
    # Outras páginas do mesmo formulário (cônjuge, atividades, assinatura) fazem parte do
    # requerimento, antes ou depois dele e mesmo com páginas em branco no meio.
    if candidates:
        for row in documents:
            if row in candidates or row.get("status") != "extraido":
                continue
            text = normalize_text(" ".join(row.get(key) or "" for key in ("texto_ocr", "texto_pdf")))
            form_page = ("conjuge ou companheiro" in text
                         and ("socio de empresa" in text or "trabalha registrado" in text))
            if row.get("tipo_sugerido") == "requerimento" or form_page:
                row.update(tipo="requerimento", continuacao_requerimento=True,
                           obtido=row.get("obtido_nao_confirmado") or row.get("obtido_bruto") or {})
                row.pop("motivo_classificacao", None)
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
    process["cadastro_sil"] = registry_lookup(folder, process) if process["status"] == "em_revisao" else None
    process["parecer"] = parecer.assess(process, process["revisao"], process["cadastro_sil"])
    return process


def registry_lookup(folder, process):
    """Consulta a SIL uma vez por processo e guarda o resultado (24 h; erros, 10 min)."""
    if not sil.configured():
        return None
    def values(field):
        return sorted({row["obtido"][field] for row in process["requerimentos"]
                       if isinstance(row.get("obtido", {}).get(field), str)})
    registrations = values("inscricao_imobiliaria")
    cpfs = values("cpf_requerente") + values("cpf_conjuge")
    key = [registrations, cpfs]
    cache = folder / "sil.json"
    if cache.exists():
        saved = load(cache)
        age = time.time() - saved["dados"].get("consultado_em", 0)
        failed = any(name.startswith("erro") for name in saved["dados"])
        if saved.get("chave") == key and age < (600 if failed else 86400):
            return saved["dados"]
    data = sil.lookup(registrations[0] if len(registrations) == 1 else None, cpfs)
    save(cache, {"chave": key, "dados": data})
    return data


def batch_summary(path):
    """Uma linha por processo com o resultado do parecer, para a lista e a exportação."""
    manifest = path / "manifest.json"
    rows = []
    for item in load(manifest) if manifest.exists() else []:
        row = {"id": item["id"], "processo": item["processo"],
               "concluido": (path / item["id"] / "complete.json").exists()}
        if row["concluido"]:
            try:
                report = process_detail(path, item["id"])["parecer"]
            except (OSError, ValueError, KeyError):
                row["resultado"] = "erro"
                rows.append(row)
                continue
            summary = report["resumo"]
            row.update(sei=report["numero_processo_sei"], resultado=report["resultado"],
                       requerente=summary["requerente"], cpf=summary["cpf"], sintese=summary.get("sintese"),
                       inscricao=summary["inscricao_imobiliaria"], modalidade=summary["modalidade"],
                       renda_familiar_centavos=summary["renda_familiar_centavos"],
                       motivos=report["motivos_indeferimento"] or report["pendencias"]
                       or [row["motivo"] for row in report["criterios"] if row["status"] == "fora_do_fluxo"]
                       or report["conferencias_cadastrais"])
        else:
            row["resultado"] = "processando"
        rows.append(row)
    return rows
