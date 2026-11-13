"""Histórico persistente dos pareceres em SQLite (/data/iptu.db).

- processos: uma linha por processo de todos os lotes, com o parecer atual completo.
- versoes: cada parecer diferente do anterior (reanálise, conferência manual) com data.
- auditoria: exclusões (número do processo e data, sem dados pessoais).

Os documentos originais e as leituras por página continuam nos arquivos do lote em /data.
Só o painel grava no banco (o worker remoto apenas escreve arquivos); a sincronização lê os
arquivos e atualiza o banco quando algo muda.
"""
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from threading import Lock

EXPORT_VERSION = 1
PASSARAM = ("enquadra", "enquadra_conferir_cadastro")
NAO_PASSARAM = ("nao_enquadra", "em_analise")
FILTERS = {
    "passaram": PASSARAM,
    "nao_passaram": NAO_PASSARAM,
    "fora_do_fluxo": ("fora_do_fluxo",),
    "processando": ("processando",),
}
SORTS = {"atualizado": "atualizado_em", "criado": "criado_em", "processo": "processo",
         "requerente": "requerente", "resultado": "resultado", "renda": "renda_centavos", "lote": "lote_nome"}
_WRITE = Lock()
_path = None


def connect():
    connection = sqlite3.connect(_path, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def init(path):
    global _path
    _path = str(path)
    with _WRITE, closing(connect()) as db, db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS processos (
                lote_id TEXT NOT NULL, processo_id TEXT NOT NULL, lote_nome TEXT, processo TEXT,
                sei TEXT, requerente TEXT, cpf TEXT, inscricao TEXT, modalidade TEXT,
                renda_centavos INTEGER, resultado TEXT, sintese TEXT, motivos TEXT,
                parecer TEXT, assinatura TEXT, hash TEXT, versao INTEGER DEFAULT 0,
                criado_em REAL, atualizado_em REAL,
                PRIMARY KEY (lote_id, processo_id));
            CREATE INDEX IF NOT EXISTS processos_resultado ON processos(resultado);
            CREATE INDEX IF NOT EXISTS processos_atualizado ON processos(atualizado_em);
            CREATE TABLE IF NOT EXISTS versoes (
                lote_id TEXT NOT NULL, processo_id TEXT NOT NULL, versao INTEGER NOT NULL,
                registrado_em REAL, motivo TEXT, resultado TEXT, parecer TEXT,
                PRIMARY KEY (lote_id, processo_id, versao));
            CREATE TABLE IF NOT EXISTS auditoria (
                registrado_em REAL, evento TEXT, lote_id TEXT, processo_id TEXT, sei TEXT, usuario TEXT);
        """)


def signature(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def stored_signature(lote_id, processo_id):
    with closing(connect()) as db:
        row = db.execute("SELECT assinatura FROM processos WHERE lote_id=? AND processo_id=?",
                         (lote_id, processo_id)).fetchone()
    return row["assinatura"] if row else None


def upsert(lote_id, processo_id, lote_nome, processo, summary, report, assinatura, motivo):
    """Grava o estado atual; cria uma versão no histórico quando o parecer muda."""
    now = time.time()
    body = json.dumps(report, ensure_ascii=False, sort_keys=True) if report is not None else None
    digest = hashlib.sha256(body.encode()).hexdigest() if body else None
    with _WRITE, closing(connect()) as db, db:
        current = db.execute("SELECT hash, versao, criado_em FROM processos WHERE lote_id=? AND processo_id=?",
                             (lote_id, processo_id)).fetchone()
        if body is None and current:
            # Em (re)processamento: mantém o último parecer gravado e só sinaliza o estado.
            db.execute("UPDATE processos SET resultado=?, assinatura=?, lote_nome=?, processo=? "
                       "WHERE lote_id=? AND processo_id=?",
                       (summary.get("resultado"), assinatura, lote_nome, processo, lote_id, processo_id))
            return
        version = current["versao"] if current else 0
        if body and (not current or current["hash"] != digest):
            version += 1
            db.execute("INSERT INTO versoes VALUES (?,?,?,?,?,?,?)",
                       (lote_id, processo_id, version, now, motivo, summary.get("resultado"), body))
        changed = not current or current["hash"] != digest
        db.execute("""
            INSERT INTO processos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(lote_id, processo_id) DO UPDATE SET
                lote_nome=excluded.lote_nome, processo=excluded.processo, sei=excluded.sei,
                requerente=excluded.requerente, cpf=excluded.cpf, inscricao=excluded.inscricao,
                modalidade=excluded.modalidade, renda_centavos=excluded.renda_centavos,
                resultado=excluded.resultado, sintese=excluded.sintese, motivos=excluded.motivos,
                parecer=excluded.parecer, assinatura=excluded.assinatura, hash=excluded.hash,
                versao=excluded.versao,
                atualizado_em=CASE WHEN processos.hash IS excluded.hash THEN processos.atualizado_em
                                   ELSE excluded.atualizado_em END
        """, (lote_id, processo_id, lote_nome, processo, summary.get("sei"), summary.get("requerente"),
              summary.get("cpf"), summary.get("inscricao"), summary.get("modalidade"),
              summary.get("renda_familiar_centavos"), summary.get("resultado"), summary.get("sintese"),
              json.dumps(summary.get("motivos") or [], ensure_ascii=False), body, assinatura, digest, version,
              current["criado_em"] if current else now, now if changed else None))


def search(q="", filtro="", lote_id="", ordem="atualizado", direcao="desc", limite=500, completo=False):
    where, args = [], []
    if q:
        like = "%" + q.strip().lower() + "%"
        where.append("(lower(coalesce(processo,'')||' '||coalesce(sei,'')||' '||coalesce(requerente,'')||' '||"
                     "coalesce(cpf,'')||' '||coalesce(inscricao,'')||' '||coalesce(sintese,'')||' '||"
                     "coalesce(motivos,'')||' '||coalesce(lote_nome,'')) LIKE ?)")
        args.append(like)
    if filtro in FILTERS:
        values = FILTERS[filtro]
        where.append(f"resultado IN ({','.join('?' * len(values))})")
        args += list(values)
    if lote_id:
        where.append("lote_id = ?")
        args.append(lote_id)
    column = SORTS.get(ordem, "atualizado_em")
    direction = "ASC" if direcao == "asc" else "DESC"
    fields = "*" if completo else "lote_id, processo_id, lote_nome, processo, sei, requerente, cpf, inscricao, " \
                                  "modalidade, renda_centavos, resultado, sintese, motivos, versao, criado_em, atualizado_em"
    sql = (f"SELECT {fields} FROM processos {'WHERE ' + ' AND '.join(where) if where else ''} "
           f"ORDER BY {column} IS NULL, {column} {direction}, processo LIMIT ?")
    with closing(connect()) as db:
        rows = [dict(row) for row in db.execute(sql, [*args, max(1, min(int(limite), 100000))])]
        totals = {key: count for key, count in db.execute("SELECT resultado, count(*) FROM processos GROUP BY resultado")}
    for row in rows:
        row["motivos"] = json.loads(row["motivos"] or "[]")
        if completo:
            row["parecer"] = json.loads(row["parecer"]) if row.get("parecer") else None
    return rows, totals


def versions(lote_id, processo_id, with_body=False):
    with closing(connect()) as db:
        rows = db.execute("SELECT versao, registrado_em, motivo, resultado" + (", parecer" if with_body else "")
                          + " FROM versoes WHERE lote_id=? AND processo_id=? ORDER BY versao",
                          (lote_id, processo_id)).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        if with_body and row.get("parecer"):
            row["parecer"] = json.loads(row["parecer"])
    return result


def signatures():
    with closing(connect()) as db:
        return {(row["lote_id"], row["processo_id"]): row["assinatura"]
                for row in db.execute("SELECT lote_id, processo_id, assinatura FROM processos")}


def known_processes():
    with closing(connect()) as db:
        return {(row["lote_id"], row["processo_id"]) for row in db.execute("SELECT lote_id, processo_id FROM processos")}


def delete(lote_id, processo_id, sei=None, usuario=None, evento="excluido"):
    with _WRITE, closing(connect()) as db, db:
        db.execute("DELETE FROM processos WHERE lote_id=? AND processo_id=?", (lote_id, processo_id))
        db.execute("DELETE FROM versoes WHERE lote_id=? AND processo_id=?", (lote_id, processo_id))
        db.execute("INSERT INTO auditoria VALUES (?,?,?,?,?,?)", (time.time(), evento, lote_id, processo_id, sei, usuario))


def audit(limite=200):
    with closing(connect()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM auditoria ORDER BY registrado_em DESC LIMIT ?", (limite,))]
