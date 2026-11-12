import base64
import binascii
import errno
import hmac
import html
import json
import os
import re
import socket
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


VISION_MODELS = ("gemma-4-12b-it", "qwen3.8-27b", "qwen3.8-27b-fast")
VISION_CASES = {"sim": True, "em_branco": None}
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def connection_failure(error, base, route):
    cause = error.reason if isinstance(error, URLError) else error
    if isinstance(cause, TimeoutError):
        detail = "tempo_limite"
    elif isinstance(cause, socket.gaierror):
        detail = "falha_dns"
    elif isinstance(cause, ssl.SSLError):
        detail = "falha_tls"
    else:
        detail = {
            errno.ECONNREFUSED: "conexao_recusada",
            errno.EHOSTUNREACH: "host_inacessivel",
            errno.ENETUNREACH: "rede_inacessivel",
            errno.ECONNRESET: "conexao_interrompida",
            errno.ETIMEDOUT: "tempo_limite",
        }.get(getattr(cause, "errno", None), "erro_de_transporte")
    return {"status": "falha_de_conexao", "detalhe": detail,
            "destino": base.netloc, "rota": route}


def gateway_request(path, body=None, timeout=5):
    url = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
    try:
        base = urlsplit(url)
        if (
            base.scheme not in {"http", "https"} or not base.hostname
            or base.username or base.password or base.query or base.fragment
            or base.path not in {"", "/v1"}
        ):
            return {"status": "configuracao_invalida"}, None
        _ = base.port
        token = os.environ["LITELLM_API_KEY"].strip()
        if not token or not token.isascii() or any(c.isspace() for c in token):
            return {"status": "segredo_invalido"}, None
    except (OSError, KeyError, ValueError):
        return {"status": "configuracao_incompleta"}, None

    endpoint = url if base.path == "/v1" else url + "/v1"
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    request = Request(endpoint + path, data=data, headers=headers)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            return {"status": "resposta_grande_demais"}, None
        return {"status": "conectado"}, json.loads(raw)
    except HTTPError as error:
        return {"status": "erro_http", "codigo": error.code}, None
    except OSError as error:
        return connection_failure(error, base, "/v1" + path), None
    except (ValueError, UnicodeError):
        return {"status": "resposta_invalida"}, None


def check_models():
    result, payload = gateway_request("/models")
    if result["status"] != "conectado":
        return result
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return {"status": "resposta_invalida"}
    result["modelos"] = sorted({
        item["id"] for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    })
    return result


def check_vision(model, case):
    if model not in VISION_MODELS or case not in VISION_CASES:
        return {"status": "parametros_invalidos"}
    expected = {"exercicio": 2027, "reside_no_imovel": VISION_CASES[case]}
    image = base64.b64encode((FIXTURES / f"{case}.png").read_bytes()).decode("ascii")
    body = {
        "model": model,
        "stream": False,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": (
                "Leia apenas esta imagem. Retorne somente um objeto JSON com as chaves "
                "exercicio (ano inteiro ou null) e reside_no_imovel "
                "(true se só a caixa Sim estiver marcada, false se só a caixa Não "
                "estiver marcada, null se ambas ou nenhuma caixa estiver marcada ou "
                "se a marcação for ilegível)."
            )},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
        ]}],
    }
    started = time.monotonic()
    result, payload = gateway_request("/chat/completions", body, timeout=40)
    result.update(modelo=model, caso=case, esperado=expected,
                  tempo_ms=round((time.monotonic() - started) * 1000))
    if result["status"] != "conectado":
        return result
    try:
        choice = payload["choices"][0]
        result["motivo_termino"] = choice.get("finish_reason")
        if result["motivo_termino"] == "length":
            result["status"] = "resposta_truncada"
            return result
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            result["status"] = "resposta_sem_conteudo"
            return result
        content = content.strip()
        fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", content, re.DOTALL | re.IGNORECASE)
        result["formato"] = "bloco_markdown" if fenced else "texto"
        try:
            value = json.loads(fenced.group(1) if fenced else content)
        except ValueError:
            result["status"] = "json_invalido"
            key = os.environ.get("LITELLM_API_KEY", "").strip()
            if key:
                content = content.replace(key, "[chave omitida]")
            result["previa"] = content[:240]
            return result
        if not fenced:
            result["formato"] = "json_direto"
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        result["status"] = "json_invalido"
        return result
    if (
        not isinstance(value, dict) or value.keys() != expected.keys()
        or (value["exercicio"] is not None and type(value["exercicio"]) is not int)
        or (value["reside_no_imovel"] is not None and type(value["reside_no_imovel"]) is not bool)
    ):
        result["status"] = "esquema_invalido"
        return result
    result["obtido"] = value
    result["status"] = "aprovado" if value == expected else "divergente"
    return result


class Handler(BaseHTTPRequestHandler):
    def send_page(self, result=None):
        output = ""
        if result is not None:
            content = html.escape(json.dumps(result, ensure_ascii=False, indent=2))
            output = f'<h2>Resultado da verificação</h2><pre>{content}</pre>'
        model_options = "".join(f'<option>{model}</option>' for model in VISION_MODELS)
        body = f"""<!doctype html>
<html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>IPTU · Conexão LiteLLM</title>
<style>
body {{ font: 1rem system-ui; background: #f4f7fb; color: #142236; margin: 0; }}
main {{ max-width: 46rem; margin: 8vh auto; padding: 2rem; background: white; }}
button {{ background: #174f91; color: white; border: 0; border-radius: .4rem; padding: .8rem 1rem; cursor: pointer; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #eef3f8; padding: 1rem; }}
</style>
<main><h1>Conexão com o LiteLLM</h1>
<p>Verifique o acesso pela rede da Prefeitura e liste os modelos disponíveis para o worker.</p>
<form method="post" action="/verificar"><button>Testar conexão</button></form>
<h2>Teste de visão com formulário sintético</h2>
<form method="post" action="/visao">
<label>Modelo <select name="modelo">{model_options}</select></label>
<label>Marcação <select name="caso">
<option value="sim">Sim marcado</option><option value="em_branco">Em branco</option>
</select></label>
<button>Testar imagem</button>
</form>{output}</main></html>"""
        body = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def require_login(self):
        scheme, _, credential = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                supplied = base64.b64decode(credential, validate=True)
            except (ValueError, binascii.Error):
                supplied = b""
            user = os.environ.get("IPTU_ADMIN_USER", "iptu")
            password = os.environ.get("IPTU_ADMIN_PASSWORD", "")
            if password and hmac.compare_digest(supplied, f"{user}:{password}".encode()):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="IPTU", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(204)
            self.end_headers()
        elif self.require_login():
            if self.path == "/":
                self.send_page()
            else:
                self.send_error(404)

    def do_POST(self):
        if not self.require_login():
            return
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
            self.send_error(403)
        elif self.path == "/verificar":
            self.send_page(check_models())
        elif self.path == "/visao":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024:
                    raise ValueError
                fields = parse_qs(self.rfile.read(length).decode(), max_num_fields=2)
                model, = fields["modelo"]
                case, = fields["caso"]
            except (KeyError, ValueError):
                self.send_error(400)
                return
            self.send_page(check_vision(model, case))
        else:
            self.send_error(404)


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        result = check_models()
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result["status"] == "conectado" else 1)
    if sys.argv[1:] == ["vision"]:
        passed = True
        for model in VISION_MODELS:
            for case in VISION_CASES:
                result = check_vision(model, case)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                passed = passed and result["status"] == "aprovado"
        sys.exit(0 if passed else 1)
    password = os.environ.get("IPTU_ADMIN_PASSWORD", "")
    if len(password) < 16 or password == os.environ.get("LITELLM_API_KEY"):
        sys.exit("Configure uma senha de painel própria com pelo menos 16 caracteres.")
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
