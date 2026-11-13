"""Persistent pilot imports. Original paths are metadata, never filesystem destinations."""
import hashlib
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

import historico
import leitura
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
IMPORT_LOCKS = {}  # um lote é separado em processos uma única vez, por uma única thread
PAGE_SLOTS = None  # vagas de leitura compartilhadas por todos os lotes em andamento


def lot_lock(name):
    with LOCK:
        return IMPORT_LOCKS.setdefault(name, Lock())


def max_parallel_lots():
    try:
        return max(1, min(16, int(os.environ.get("IPTU_LOTES_SIMULTANEOS", "3"))))
    except ValueError:
        return 3


def import_lot(path):
    """Separa o lote em processos (manifest) assim que o envio termina, sem esperar a vez de leitura."""
    with lot_lock(path.name):
        manifest = path / "manifest.json"
        if manifest.exists():
            return load(manifest)
        state = load(path / "batch.json")
        state["etapa"] = "separando"
        save(path / "batch.json", state)
        processes = inventory(path, state["nome"])
        save(manifest, processes)
        # Os arquivos já foram extraídos: o ZIP enviado (≈1 GB por lote) deixa de ser necessário.
        if (path / "upload").exists() and Path(state["nome"]).suffix.lower() == ".zip":
            (path / "upload").unlink(missing_ok=True)
        state = load(path / "batch.json")
        state.pop("etapa", None)
        state["processos"] = len(processes)
        state["documentos"] = sum(1 for item in processes for doc in item["arquivos"] if doc["id"])
        save(path / "batch.json", state)
        return processes
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
    "nomes_citados": str, "usufruto_citado": bool,
}
DEED_FIELDS = {"nomes_citados", "usufruto_citado"}
INCOME_FIELDS = {"renda_bruta_centavos", "competencia_renda", "titular_renda",
                 "numero_beneficio", "especie_beneficio"}
ID_FIELDS = {"nome_documento", "cpf_documento", "filiacao_documento"}
PAGE_SCHEMA = 10


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
    upload = path / "upload"
    value["recebido"] = upload.stat().st_size if upload.exists() else value.get("bytes", 0)
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


def render_page(pdf, number, extension=".pdf", size=1800, gray=True, jpeg=True):
    """Renderiza uma página. JPEG por padrão: a compressão PNG custava ~1,4 s por página digitalizada."""
    with RENDER_SLOTS:
        return _render_page(pdf, number, extension, size, gray, jpeg)


def _render_page(pdf, number, extension, size, gray, jpeg=True):
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
                if jpeg:
                    page.save(output, format="JPEG", quality=85)
                else:
                    page.save(output, format="PNG")
                return output.getvalue()
        except (OSError, ValueError, Image.DecompressionBombError):
            return None
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / ("pagina.jpg" if jpeg else "pagina.png")
        completed = subprocess.run(
            [*NICE, "pdftoppm", "-f", str(number), "-l", str(number), "-singlefile",
             "-scale-to", str(size), *(["-gray"] if gray else []),
             *(["-jpeg", "-jpegopt", "quality=88"] if jpeg else ["-png"]), str(pdf), str(png.with_suffix(""))],
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
    page = " ".join(texts.get(key, "") or "" for key in ("texto_ocr", "texto_pdf"))
    if result.get("status") == "extraido" and leitura.is_form_page(page):
        # O texto da própria página identifica o formulário de isenção da Prefeitura.
        return {**result, "tipo": "requerimento", "requerimento_isencao_confirmado": True}
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
        return max(1, min(128, int(os.environ.get("IPTU_PAGE_WORKERS", "32"))))
    except ValueError:
        return 32


TRANSIENT_STATUSES = {"falha_de_conexao", "erro_http", "falha_de_leitura"}


def process_page(pdf, number, evidence, analyze, extension=".pdf", slow=False):
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
        if extension == ".pdf":
            stage = "texto_pdf_ms"
            texts = native_page_text(pdf, number)
            timing[stage] = round((time.monotonic() - started) * 1000)
            kind = leitura.text_only_kind(texts.get("texto_pdf"))
            if kind:
                # Texto digital de andamento, despacho ou lista: classificado sem chamar o modelo.
                if os.environ.get("IPTU_CACHE_PREVIEWS") == "1":
                    preview = render_page(pdf, number, extension)
                    if preview is not None:
                        cache_preview(evidence, preview)
                save(evidence, {"status": "extraido", "tipo": kind, "obtido": {}, **texts, "texto_ocr": "",
                                "status_ocr": "texto_nativo", "evidencia_tipo": texts["texto_pdf"][:160],
                                "versao_extracao": PAGE_SCHEMA, "tempos_ms": timing, "tentativas_ia": 0,
                                "sem_modelo": True})
                return timing, 0, False
            check_reset()
        stage = "renderizacao_ms"
        started = time.monotonic()
        image = render_page(pdf, number, extension, size=1280)  # só classificar: imagem menor
        if image is not None:
            timing[stage] = round((time.monotonic() - started) * 1000)
            check_reset()
            if os.environ.get("IPTU_CACHE_PREVIEWS") == "1":
                cache_preview(evidence, image)
            stage = "litellm_ms"
            started = time.monotonic()
            attempts += 1
            # Alta resolução colorida só é gerada se a página precisar de leitura detalhada.
            hires = lambda: render_page(pdf, number, extension, size=1800, gray=False, jpeg=True)  # noqa: E731
            result = analyze(image, texts, hires=hires, slow=slow)
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
                    "tempos_ms": timing, "tentativas_ia": attempts + max(0, result.get("tentativas_llm", 1) - 1)})
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


def jobs_job_args(jobs, retry):
    return [jobs[job] for job in retry]


def second_pass(path, state, progress, failed, analyze, workers):
    """Relê, com prazo longo, as páginas que falharam por instabilidade do servidor de IA."""
    pending = []
    for key, pdf, number, evidence, extension in failed:
        try:
            if load(evidence).get("status") in TRANSIENT_STATUSES:
                pending.append((key, pdf, number, evidence, extension))
        except (OSError, ValueError):
            pending.append((key, pdf, number, evidence, extension))
    if not pending:
        return
    progress["segunda_passada"] = len(pending)
    save(path / "batch.json", state)
    def slotted(*job):
        with PAGE_SLOTS:
            return process_page(*job, slow=True)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending)))) as pool:
        for job in as_completed([pool.submit(slotted, pdf, number, evidence, analyze, extension)
                                 for _, pdf, number, evidence, extension in pending]):
            job.result()
            progress["segunda_passada"] -= 1
            progress["atualizado_em"] = time.time()
            save(path / "batch.json", state)
    progress.pop("segunda_passada", None)
    for key in {item[0] for item in pending}:  # o parecer do processo é refeito com as páginas relidas
        save(path / key / "complete.json", {"concluido": True})


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
    processes = import_lot(path)
    state["processos"] = len(processes)
    state["documentos"] = sum(1 for item in processes for doc in item["arquivos"] if doc["id"])
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
                    "paralelismo": workers, "atualizado_em": time.time()}
        state["progresso"] = progress
        save(path / "batch.json", state)
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            def slotted(*job):
                with PAGE_SLOTS:
                    return process_page(*job)
            jobs = {pool.submit(slotted, pdf, number, evidence, analyze, extension): (key, pdf, number, evidence, extension)
                    for key, items in jobs_by_process.items()
                    for pdf, number, evidence, extension in items}
            retry = []
            for job in as_completed(jobs):
                timing, attempts, failed = job.result()
                if failed:
                    retry.append(job)
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
                progress["atualizado_em"] = time.time()  # mostra no painel se a leitura parou
                key = jobs[job][0]
                remaining[key] -= 1
                if not remaining[key]:
                    save(path / key / "complete.json", {"concluido": True})
                save(path / "batch.json", state)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        second_pass(path, state, progress, jobs_job_args(jobs, retry), analyze, workers)
    state.pop("progresso", None)
    state["status"] = "concluido"
    state["desempenho"]["duracao_ms"] = round((time.monotonic() - batch_started) * 1000)
    rerun = path / ".rodar_novamente"
    if rerun.exists():  # reanálise pedida durante esta rodada
        rerun.unlink()
        state["status"] = "na_fila"
    save(path / "batch.json", state)


def failure_reason(exc):
    return ("zip_invalido" if isinstance(exc, zipfile.BadZipFile)
            else str(exc) if str(exc) in {"espaco_insuficiente", "limite_de_entradas", "limite_de_extracao"}
            else "falha_de_importacao")


def mark_failed(meta, exc):
    print(f"Falha no lote {meta.parent.name}: {type(exc).__name__}: {exc}", flush=True)
    try:
        state = load(meta)
        state["status"] = "falha"
        state["motivo"] = failure_reason(exc)
        state.pop("etapa", None)
        save(meta, state)
    except OSError as error:
        print(f"Falha ao salvar status do lote {meta.parent.name}: {type(error).__name__}", flush=True)


def queued_lots(statuses):
    lots = []
    for meta in ROOT.glob("*/batch.json"):
        try:
            state = load(meta)
        except (OSError, ValueError):
            continue
        if state.get("status") in statuses:
            lots.append((state.get("criado", 0), meta))
    return [meta for _, meta in sorted(lots)]


def run_importer():
    """Separa em processos os lotes que chegam, em paralelo à leitura dos anteriores."""
    while True:
        if not reset_pending():
            for meta in queued_lots({"na_fila"}):
                if reset_pending():
                    break
                if not (meta.parent / "manifest.json").exists():
                    try:
                        import_lot(meta.parent)
                    except ResetRequested:
                        break
                    except Exception as exc:
                        mark_failed(meta, exc)
        time.sleep(2)


def run_worker(analyze):
    global PAGE_SLOTS
    PAGE_SLOTS = BoundedSemaphore(page_workers())
    print(f"Worker: {available_cpus()} CPU(s) no container; renderização em até "
          f"{RENDER_SLOTS._initial_value} página(s) por vez; {page_workers()} página(s) em leitura paralela; "
          f"até {max_parallel_lots()} lote(s) ao mesmo tempo.", flush=True)
    Thread(target=run_importer, daemon=True).start()
    active = {}

    def run_lot(meta):
        try:
            process_batch(meta.parent, analyze)
        except ResetRequested:
            pass
        except Exception as exc:
            mark_failed(meta, exc)

    while True:
        WAKE.clear()
        active = {name: thread for name, thread in active.items() if thread.is_alive()}
        if reset_pending():
            if not active:
                try:
                    clear_batches()
                except OSError as error:
                    print(f"Falha ao limpar lotes: {type(error).__name__}", flush=True)
            WAKE.wait(3)
            continue
        for meta in queued_lots({"na_fila", "processando"}):
            if len(active) >= max_parallel_lots():
                break
            if meta.parent.name not in active:
                thread = Thread(target=run_lot, args=(meta,), daemon=True)
                active[meta.parent.name] = thread
                thread.start()
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


def diagnostics(path):
    """Resumo do lote: páginas por situação, leitura detalhada, divergências e tempos médios."""
    report = {"paginas": 0, "por_situacao": {}, "leitura_detalhada": {}, "divergencias": 0,
              "campos_descartados": 0, "novas_tentativas": 0, "tempo_medio_ms": {}, "exemplos_de_falha": []}
    sums = {}
    manifest = load(path / "manifest.json") if (path / "manifest.json").exists() else []
    for process in manifest:
        folder = path / process["id"]
        if not folder.is_dir():
            continue
        origin = {item["id"]: item["origem"] for item in process["arquivos"] if item["id"]}
        for page in sorted(folder.iterdir()):
            if not PAGE_RESULT.fullmatch(page.name):
                continue
            try:
                row = load(page)
            except (OSError, ValueError):
                continue
            report["paginas"] += 1
            status = row.get("status") or "desconhecido"
            if status == "erro_http":
                status = f"erro_http_{row.get('codigo')}"
            elif status == "falha_de_conexao" and row.get("detalhe"):
                status = f"falha_de_conexao ({row['detalhe']})"
            report["por_situacao"][status] = report["por_situacao"].get(status, 0) + 1
            detail = (row.get("leitura_detalhada") or {}).get("status")
            if detail:
                report["leitura_detalhada"][detail] = report["leitura_detalhada"].get(detail, 0) + 1
            report["divergencias"] += len(row.get("divergencias") or [])
            report["campos_descartados"] += len(row.get("campos_descartados") or [])
            report["novas_tentativas"] += max(0, (row.get("tentativas_ia") or 1) - 1)
            tokens = row.get("tokens") or {}
            if tokens.get("chamadas"):
                report.setdefault("tokens", {"entrada": 0, "saida": 0, "chamadas": 0})
                for key in ("entrada", "saida", "chamadas"):
                    report["tokens"][key] += tokens.get(key) or 0
            for stage, ms in (row.get("tempos_ms") or {}).items():
                total, count = sums.get(stage, (0, 0))
                sums[stage] = (total + ms, count + 1)
            if status != "extraido" and len(report["exemplos_de_falha"]) < 10:
                key, number = page.stem.rsplit("-", 1)
                report["exemplos_de_falha"].append({"processo": process["processo"], "arquivo": origin.get(key, key),
                                                    "pagina": int(number), "situacao": status})
    report["tempo_medio_ms"] = {stage: round(total / count) for stage, (total, count) in sums.items() if count}
    if report.get("tokens", {}).get("chamadas"):
        calls = report["tokens"]["chamadas"]
        report["tokens"]["saida_por_chamada"] = round(report["tokens"]["saida"] / calls)
        report["tokens"]["entrada_por_chamada"] = round(report["tokens"]["entrada"] / calls)
    state = load(path / "batch.json")
    report["lote"] = {key: state.get(key) for key in ("status", "progresso", "desempenho")}
    report["configuracao"] = {"paginas_em_paralelo": page_workers(), "renderizacao_em_paralelo": RENDER_SLOTS._initial_value,
                              "cpus": available_cpus()}
    return report


# ---------- Persistência: arquivos do lote → banco de histórico ----------

RULES_VERSION = hashlib.sha256(Path(parecer.__file__).read_bytes()).hexdigest()[:12]


def process_signature(path, process_id):
    """Muda quando o parecer pode ter mudado: leitura concluída, conferência, consulta SIL ou regras."""
    folder = path / process_id
    stamps = [(folder / name).stat().st_mtime_ns if (folder / name).exists() else None
              for name in ("complete.json", "review.json", "sil.json")]
    return historico.signature(stamps, RULES_VERSION)


def export_document(path, detail):
    """Documento JSON do processo para outros sistemas (formato versionado)."""
    state = load(path / "batch.json")
    report = detail["parecer"]
    return {
        "versao_formato": historico.EXPORT_VERSION,
        "gerado_em": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "versao_regras": RULES_VERSION,
        "lote": {"id": path.name, "nome": state.get("nome"), "criado_em": state.get("criado")},
        "processo": {"id": detail["id"], "nome": detail["processo"], "numero_sei": detail.get("numero_processo_sei"),
                     "arquivos": [{"origem": item["origem"], "erro": item["erro"]} for item in detail["arquivos"]]},
        "resultado": report["resultado"],
        "decisao_final": final_decision(detail) if detail["status"] == "em_revisao" else None,
        "parecer": report,
        "conferencia_manual": detail.get("revisao"),
        "cadastro_sil": detail.get("cadastro_sil"),
    }


AI_DECISION = {"enquadra": "deferido", "enquadra_conferir_cadastro": "deferido", "nao_enquadra": "indeferido",
               "em_analise": "pendente", "fora_do_fluxo": "fora_do_fluxo"}


def final_decision(detail):
    """Decisão final: a do servidor, se registrada; senão a proposta da IA. Indica a origem."""
    report = detail["parecer"]
    review = detail.get("revisao") or {}
    human = review.get("decisao") or None
    return {
        "decisao": human["resultado"] if human else AI_DECISION.get(report["resultado"], "pendente"),
        "decisao_ia": AI_DECISION.get(report["resultado"], "pendente"),
        "origem_decisao": "revisao_humana" if review else "ia",
        "revisado_por": (human or {}).get("usuario") or review.get("usuario"),
        "decidido_em": (human or {}).get("registrada_em") or review.get("registrada_em"),
        "justificativa": (human or {}).get("justificativa"),
        "decisao_humana_alterou_ia": bool(human) and human["resultado"] != AI_DECISION.get(report["resultado"]),
    }


def summary_of(detail):
    report = detail["parecer"]
    if detail["status"] != "em_revisao":
        return {"resultado": "processando", "sei": detail.get("numero_processo_sei")}
    resumo = report["resumo"]
    work = sum(sum((row.get("tempos_ms") or {}).values()) for row in detail["paginas"])
    return {**final_decision(detail),
            "sei": report["numero_processo_sei"], "resultado": report["resultado"], "tempo_ms": work or None,
            "requerente": resumo.get("requerente"), "cpf": resumo.get("cpf"),
            "inscricao": resumo.get("inscricao_imobiliaria"), "modalidade": resumo.get("modalidade"),
            "renda_familiar_centavos": resumo.get("renda_familiar_centavos"), "sintese": resumo.get("sintese"),
            "exercicio": report.get("exercicio"),
            "cpf_conjuge": next(iter({item["valor"] for item in report["fatos_do_requerimento"].get("cpf_conjuge", [])}), None),
            "motivos": report["motivos_indeferimento"] or report["pendencias"]
            or [row["motivo"] for row in report["criterios"] if row["status"] == "fora_do_fluxo"]
            or report["conferencias_cadastrais"]}


def sync_process(path, process_id, motivo="atualizacao"):
    manifest = load(path / "manifest.json")
    item = next((entry for entry in manifest if entry["id"] == process_id), None)
    if item is None:
        return None
    detail = process_detail(path, process_id)
    summary = summary_of(detail)
    complete = detail["status"] == "em_revisao"
    historico.upsert(path.name, process_id, load(path / "batch.json").get("nome"), item["processo"], summary,
                     export_document(path, detail) if complete else None,
                     process_signature(path, process_id), motivo)
    return detail


def sync_all():
    """Grava no banco os processos novos ou alterados de todos os lotes."""
    stored = historico.signatures()
    for meta in ROOT.glob("*/batch.json"):
        path = meta.parent
        manifest_path = path / "manifest.json"
        if not ID.fullmatch(path.name) or not manifest_path.exists() or reset_pending():
            continue
        try:
            manifest = load(manifest_path)
        except (OSError, ValueError):
            continue
        for item in manifest:
            try:
                if stored.get((path.name, item["id"])) != process_signature(path, item["id"]):
                    sync_process(path, item["id"], "leitura" if (path.name, item["id"]) not in stored else "atualizacao")
            except (OSError, ValueError, KeyError) as error:
                print(f"Falha ao gravar histórico {path.name[:8]}/{item['id'][:8]}: {type(error).__name__}", flush=True)


def delete_process(path, process_id):
    """Exclui documentos, leituras e o processo do lote. Recusa enquanto o lote é processado."""
    with LOCK:
        check_reset()
        state = describe(path)
        if state["status"] in {"na_fila", "processando", "recebendo"}:
            return 409, {"status": "lote_em_processamento"}
        manifest = load(path / "manifest.json")
        item = next((entry for entry in manifest if entry["id"] == process_id), None)
        if item is None:
            raise FileNotFoundError
        others = [entry for entry in manifest if entry["id"] != process_id]
        shared = {doc["id"] for entry in others for doc in entry["arquivos"] if doc["id"]}
        for doc in item["arquivos"]:
            if doc["id"] and doc["id"] not in shared:
                (path / "files" / doc["id"]).unlink(missing_ok=True)
                for preview in (path / "previews").glob(doc["id"] + "-*.png") if (path / "previews").is_dir() else []:
                    preview.unlink()
        shutil.rmtree(path / process_id, ignore_errors=True)
        # O ZIP original contém o processo excluído; os arquivos extraídos já bastam.
        (path / "upload").unlink(missing_ok=True)
        if others:
            save(path / "manifest.json", others)
        else:
            shutil.rmtree(path, ignore_errors=True)
        return 200, {"status": "excluido", "processo": item["processo"]}


def throughput(lote_id=""):
    """Processos por hora de leitura, a partir da duração registrada de cada lote."""
    processes = seconds = 0
    for meta in ROOT.glob("*/batch.json"):
        if lote_id and meta.parent.name != lote_id:
            continue
        try:
            state = load(meta)
            manifest = load(meta.parent / "manifest.json") if (meta.parent / "manifest.json").exists() else []
        except (OSError, ValueError):
            continue
        duration = (state.get("desempenho") or {}).get("duracao_ms") or 0
        if state.get("status") == "concluido" and duration > 0 and manifest:
            processes += len(manifest)
            seconds += duration / 1000
    return round(processes / (seconds / 3600), 1) if seconds else None


def lots_overview():
    """Estado de cada lote enviado, para o painel mostrar onde cada envio está."""
    rows = []
    for meta in ROOT.glob("*/batch.json"):
        try:
            state = describe(meta.parent)
        except (OSError, ValueError):
            continue
        rows.append({key: state.get(key) for key in ("id", "nome", "status", "etapa", "motivo", "bytes", "recebido",
                                                       "criado", "processos", "documentos", "progresso")}
                    | {"duracao_ms": (state.get("desempenho") or {}).get("duracao_ms")})
    rows.sort(key=lambda row: row.get("criado") or 0, reverse=True)
    return {"lotes": rows, "lotes_simultaneos": max_parallel_lots(), "paginas_em_paralelo": page_workers()}
