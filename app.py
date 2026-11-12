import base64
import binascii
import errno
import hmac
import html
import json
import os
import socket
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def connection_failure(error, base):
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
            "destino": base.netloc, "rota": "/v1/models"}


def check_models():
    url = os.environ.get("LITELLM_BASE_URL", "").rstrip("/")
    try:
        base = urlsplit(url)
        if (
            base.scheme not in {"http", "https"} or not base.hostname
            or base.username or base.password or base.query or base.fragment
            or base.path not in {"", "/v1"}
        ):
            return {"status": "configuracao_invalida"}
        _ = base.port
        token = os.environ["LITELLM_API_KEY"].strip()
        if not token or not token.isascii() or any(c.isspace() for c in token):
            return {"status": "segredo_invalido"}
    except (OSError, KeyError, ValueError):
        return {"status": "configuracao_incompleta"}

    endpoint = url if base.path == "/v1" else url + "/v1"
    request = Request(endpoint + "/models", headers={"Authorization": f"Bearer {token}"})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            return {"status": "resposta_grande_demais"}
        payload = json.loads(raw)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return {"status": "resposta_invalida"}
        models = sorted({
            item["id"] for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        })
        return {"status": "conectado", "modelos": models}
    except HTTPError as error:
        return {"status": "erro_http", "codigo": error.code}
    except OSError as error:
        return connection_failure(error, base)
    except (ValueError, UnicodeError):
        return {"status": "resposta_invalida"}


class Handler(BaseHTTPRequestHandler):
    def send_page(self, result=None):
        output = ""
        if result is not None:
            content = html.escape(json.dumps(result, ensure_ascii=False, indent=2))
            output = f'<h2>Resultado da verificação</h2><pre>{content}</pre>'
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
<form method="post" action="/verificar"><button>Testar conexão</button></form>{output}</main></html>"""
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
        if self.path != "/verificar":
            self.send_error(404)
        elif self.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
            self.send_error(403)
        else:
            self.send_page(check_models())


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        result = check_models()
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(0 if result["status"] == "conectado" else 1)
    password = os.environ.get("IPTU_ADMIN_PASSWORD", "")
    if len(password) < 16 or password == os.environ.get("LITELLM_API_KEY"):
        sys.exit("Configure uma senha de painel própria com pelo menos 16 caracteres.")
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
