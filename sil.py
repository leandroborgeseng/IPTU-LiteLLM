"""Consulta somente leitura às APIs da SIL Tecnologia (Franca).

Credenciais vêm do ambiente e nunca são gravadas nem registradas em log:
SIL_BASE_URL, SIL_CLIENT_ID, SIL_CLIENT_SECRET e SIL_SCOPE.
Documentação: https://interface.siltecnologia.com.br/apidocs/franca/
"""

import csv
import json
import os
import re
import time
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path
from threading import Lock

DEFAULT_BASE = "https://interface-sandbox.siltecnologia.com.br/v1"
_TOKEN = {"valor": None, "expira": 0.0}
_LOCK = Lock()


class SilError(Exception):
    pass


def configured():
    return bool(os.environ.get("SIL_CLIENT_ID") and os.environ.get("SIL_CLIENT_SECRET"))


def base_url():
    return os.environ.get("SIL_BASE_URL", DEFAULT_BASE).rstrip("/")


def _call(method, path, *, query=None, form=None, token=None, timeout=15):
    url = base_url() + path + ("?" + urllib.parse.urlencode(query) if query else "")
    data = urllib.parse.urlencode(form).encode() if form else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if form:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read() or b"null")
        except ValueError:
            body = None
        return error.code, body
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        raise SilError(f"falha_de_conexao: {type(error).__name__}") from None


def token():
    with _LOCK:
        if _TOKEN["valor"] and time.time() < _TOKEN["expira"] - 30:
            return _TOKEN["valor"]
        code, body = _call("POST", "/api/authenticate", form={
            "client_id": os.environ.get("SIL_CLIENT_ID", ""),
            "client_secret": os.environ.get("SIL_CLIENT_SECRET", ""),
            "scope": os.environ.get("SIL_SCOPE", "pmfranca"),
            "grant_type": "client_credentials",
        })
        if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
            raise SilError(f"autenticacao_recusada: HTTP {code}")
        seconds = int(re.sub(r"\D", "", str(body.get("expires_in", "600"))) or 600)
        _TOKEN.update(valor=body["access_token"], expira=time.time() + seconds)
        return _TOKEN["valor"]


def _get(path, query):
    code, body = _call("GET", path, query=query, token=token())
    if code == 401:
        _TOKEN["valor"] = None
        code, body = _call("GET", path, query=query, token=token())
    return code, body


def property_by_registration(registration):
    """Imóvel pela inscrição física (ex.: 01.3.11.12.005.15.00). None se não localizado."""
    code, body = _get("/franca/imoveis", {"InscrFisico": registration})
    if code == 404:
        return None
    if code != 200 or not isinstance(body, dict):
        raise SilError(f"imoveis: HTTP {code}")
    return body.get("data") or None


def company_by_document(document):
    """Cadastro mobiliário pelo CPF/CNPJ ou CCM. None se não houver inscrição."""
    code, body = _get("/franca/empresas", {"Documento": document})
    if code == 404:
        return None
    if code != 200 or not isinstance(body, dict):
        raise SilError(f"empresas: HTTP {code}")
    return body.get("data", body) or None


def lookup(registration, cpfs):
    """Consulta o que o parecer usa. Cada parte falha de forma independente."""
    result = {"consultado_em": int(time.time()), "base": base_url()}
    if registration:
        try:
            result["imovel"] = property_by_registration(registration)
        except SilError as error:
            result["erro_imovel"] = str(error)
    result["mobiliario"] = {}
    for cpf in cpfs:
        try:
            result["mobiliario"][cpf] = company_by_document(cpf)
        except SilError as error:
            result.setdefault("erro_mobiliario", {})[cpf] = str(error)
    return result


def check():
    """Diagnóstico: `python app.py sil`. Não imprime o token."""
    if not configured():
        return {"status": "nao_configurado", "base": base_url()}
    try:
        token()
        return {"status": "autenticado", "base": base_url()}
    except SilError as error:
        return {"status": "falha", "base": base_url(), "motivo": str(error)}

def normalize_cpf(value):
    cpf = re.sub(r"[.\s-]", "", str(value or ""))
    if not re.fullmatch(r"[0-9]{11}", cpf) or len(set(cpf)) == 1:
        return None
    for size in (9, 10):
        remainder = sum(int(cpf[i]) * (size + 1 - i) for i in range(size)) % 11
        if int(cpf[size]) != (0 if remainder < 2 else 11 - remainder):
            return None
    return cpf


def normalize_registration(value):
    return re.sub(r"[.\s/-]", "", str(value or ""))


def import_properties(source, database, *, complete=False):
    """Substitui a carga somente depois de validar todo o arquivo."""
    database.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=database.parent) as directory:
        staging = Path(directory) / "cadastro.db"
        with closing(sqlite3.connect(staging)) as db, db, source.open(encoding="utf-8-sig", newline="") as stream:
            sample = stream.read(4096)
            stream.seek(0)
            delimiter = ";" if sample and ";" in sample.splitlines()[0] else ","
            reader = csv.DictReader(stream, delimiter=delimiter)
            headers = reader.fieldnames or []
            if not {"cpf", "inscricao_imobiliaria"} <= set(headers) or len(headers) != len(set(headers)):
                raise ValueError("O CSV deve conter cpf e inscricao_imobiliaria.")
            db.execute("CREATE TABLE vinculos (cpf TEXT, inscricao TEXT, PRIMARY KEY(cpf, inscricao)) WITHOUT ROWID")
            db.execute("CREATE TABLE carga (completa INTEGER, carregada_em INTEGER)")
            for row in reader:
                cpf = normalize_cpf(row["cpf"])
                registration = normalize_registration(row["inscricao_imobiliaria"])
                if None in row or not cpf or not re.fullmatch(r"[0-9]{5,30}", registration):
                    raise ValueError(f"CPF ou inscrição inválidos na linha {reader.line_num}.")
                db.execute("INSERT OR IGNORE INTO vinculos VALUES (?, ?)", (cpf, registration))
            count = db.execute("SELECT count(*) FROM vinculos").fetchone()[0]
            if not count:
                raise ValueError("A carga não contém vínculos.")
            db.execute("INSERT INTO carga VALUES (?, ?)", (int(complete), int(time.time())))
        staging.chmod(0o600)
        os.replace(staging, database)
    return count


def local_properties(database, cpfs):
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        complete, timestamp = db.execute("SELECT completa, carregada_em FROM carga").fetchone()
        results = {}
        for value in cpfs:
            cpf = normalize_cpf(value)
            if cpf and cpf not in results:
                results[cpf] = [row[0] for row in db.execute(
                    "SELECT inscricao FROM vinculos WHERE cpf=? ORDER BY inscricao", (cpf,)
                )]
    return {"fonte": "carga_local_sil", "cobertura_completa": bool(complete),
            "carregada_em": timestamp, "por_cpf": results}
