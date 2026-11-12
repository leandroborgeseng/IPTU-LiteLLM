"""Consulta somente leitura às APIs da SIL Tecnologia (Franca).

Credenciais vêm do ambiente e nunca são gravadas nem registradas em log:
SIL_BASE_URL, SIL_CLIENT_ID, SIL_CLIENT_SECRET e SIL_SCOPE.
Documentação: https://interface.siltecnologia.com.br/apidocs/franca/
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
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
