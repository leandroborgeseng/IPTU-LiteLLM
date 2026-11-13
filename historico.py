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
# Decisão final: a da IA, ou a registrada por um servidor (que prevalece).
DECISOES = ("deferido", "indeferido", "pendente", "fora_do_fluxo")
FILTERS = {  # filtro -> (coluna, valores)
    "aceitos": ("decisao", ("deferido",)),
    "nao_aceitos": ("decisao", ("indeferido",)),
    "revisao_manual": ("decisao", ("pendente",)),
    "fora_do_fluxo": ("decisao", ("fora_do_fluxo",)),
    "processando": ("resultado", ("processando",)),
    "revisao_humana": ("origem_decisao", ("revisao_humana",)),
    "somente_ia": ("origem_decisao", ("ia",)),
    # nomes anteriores, mantidos para links e integrações já existentes
    "passaram": ("resultado", PASSARAM),
    "nao_passaram": ("resultado", NAO_PASSARAM),
}
SORTS = {"decisao": "decisao", "atualizado": "atualizado_em", "criado": "criado_em", "processo": "processo",
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
        columns = {row["name"] for row in db.execute("PRAGMA table_info(processos)")}
        for name, kind in (("tempo_ms", "INTEGER"), ("analisado_em", "REAL"), ("decisao", "TEXT"),
                           ("origem_decisao", "TEXT"), ("revisado_por", "TEXT"), ("decidido_em", "REAL"),
                           ("cpf_digitos", "TEXT"), ("conjuge_digitos", "TEXT"), ("inscricao_digitos", "TEXT"),
                           ("sei_digitos", "TEXT"), ("exercicio", "INTEGER"), ("paginas", "INTEGER"),
                           ("tokens_entrada", "INTEGER"), ("tokens_saida", "INTEGER")):
            if name not in columns:
                db.execute(f"ALTER TABLE processos ADD COLUMN {name} {kind}")
        db.execute("CREATE INDEX IF NOT EXISTS processos_analisado ON processos(analisado_em)")
        for column in ("cpf_digitos", "conjuge_digitos", "inscricao_digitos", "sei_digitos"):
            db.execute(f"CREATE INDEX IF NOT EXISTS processos_{column} ON processos({column}, exercicio)")


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
            INSERT INTO processos (lote_id, processo_id, lote_nome, processo, sei, requerente, cpf, inscricao,
                modalidade, renda_centavos, resultado, sintese, motivos, parecer, assinatura, hash, versao,
                criado_em, atualizado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        if body:
            digits = lambda value: "".join(ch for ch in str(value or "") if ch.isdigit()) or None  # noqa: E731
            db.execute("UPDATE processos SET analisado_em=COALESCE(analisado_em, ?), tempo_ms=?, decisao=?, "
                       "origem_decisao=?, revisado_por=?, decidido_em=?, cpf_digitos=?, conjuge_digitos=?, "
                       "inscricao_digitos=?, sei_digitos=?, exercicio=?, paginas=?, tokens_entrada=?, tokens_saida=? "
                       "WHERE lote_id=? AND processo_id=?",
                       (now, summary.get("tempo_ms"), summary.get("decisao"), summary.get("origem_decisao"),
                        summary.get("revisado_por"), summary.get("decidido_em"), digits(summary.get("cpf")),
                        digits(summary.get("cpf_conjuge")), digits(summary.get("inscricao")),
                        digits(summary.get("sei") or processo), summary.get("exercicio"), summary.get("paginas"),
                        summary.get("tokens_entrada"), summary.get("tokens_saida"), lote_id, processo_id))


# Possível duplicidade no mesmo exercício: mesmo SEI (reenvio), mesmo CPF do requerente, CPF do requerente
# igual ao do cônjuge declarado em outro processo, ou mesma inscrição imobiliária.
_OTHER = "o.rowid != processos.rowid AND o.exercicio IS processos.exercicio"
DUPLICATE_SQL = (
    "(EXISTS (SELECT 1 FROM processos o WHERE " + _OTHER + " AND o.sei_digitos = processos.sei_digitos)"
    " OR EXISTS (SELECT 1 FROM processos o WHERE " + _OTHER + " AND o.cpf_digitos = processos.cpf_digitos)"
    " OR EXISTS (SELECT 1 FROM processos o WHERE " + _OTHER + " AND (o.conjuge_digitos = processos.cpf_digitos"
    " OR o.cpf_digitos = processos.conjuge_digitos))"
    " OR EXISTS (SELECT 1 FROM processos o WHERE " + _OTHER + " AND o.inscricao_digitos = processos.inscricao_digitos))")
DUPLICATE_REASONS = {
    "sei": "mesmo número SEI enviado em outro lote",
    "cpf": "mesmo CPF do requerente em outro processo",
    "conjuge": "CPF do requerente aparece como cônjuge em outro processo (ou vice-versa)",
    "inscricao": "mesma inscrição imobiliária em outro processo",
}


def duplicates(db, rows):
    """Para cada linha, os processos relacionados e o motivo da possível duplicidade."""
    found = {}
    for row in rows:
        key = (row["lote_id"], row["processo_id"])
        me = db.execute("SELECT rowid, sei_digitos, cpf_digitos, conjuge_digitos, inscricao_digitos, exercicio "
                        "FROM processos WHERE lote_id=? AND processo_id=?", key).fetchone()
        if me is None:
            continue
        checks = (("sei", "o.sei_digitos = ?", (me["sei_digitos"],)),
                  ("cpf", "o.cpf_digitos = ?", (me["cpf_digitos"],)),
                  ("conjuge", "(o.conjuge_digitos = ? OR o.cpf_digitos = ?)", (me["cpf_digitos"], me["conjuge_digitos"])),
                  ("inscricao", "o.inscricao_digitos = ?", (me["inscricao_digitos"],)))
        related = []
        for reason, condition, values in checks:
            if not any(values):
                continue
            for other in db.execute(
                    f"SELECT o.lote_id, o.processo_id, o.processo, o.sei, o.requerente, o.decisao, o.lote_nome "
                    f"FROM processos o WHERE o.rowid != ? AND o.exercicio IS ? AND {condition}",
                    (me["rowid"], me["exercicio"], *values)):
                related.append({**dict(other), "motivo": reason, "descricao": DUPLICATE_REASONS[reason]})
        if related:
            found[key] = related
    return found


def _filters(q="", filtro="", lote_id=""):
    where, args = [], []
    if q:
        where.append("(lower(coalesce(processo,'')||' '||coalesce(sei,'')||' '||coalesce(requerente,'')||' '||"
                     "coalesce(cpf,'')||' '||coalesce(inscricao,'')||' '||coalesce(sintese,'')||' '||"
                     "coalesce(motivos,'')||' '||coalesce(lote_nome,'')) LIKE ?)")
        args.append("%" + q.strip().lower() + "%")
    if filtro in FILTERS:
        column, values = FILTERS[filtro]
        where.append(f"{column} IN ({','.join('?' * len(values))})")
        args += list(values)
    if filtro == "duplicados":
        where.append(DUPLICATE_SQL)
    if lote_id:
        where.append("lote_id = ?")
        args.append(lote_id)
    return ("WHERE " + " AND ".join(where)) if where else "", args


def search(q="", filtro="", lote_id="", ordem="atualizado", direcao="desc", limite=500, completo=False, pagina=1):
    """Processos filtrados, ordenados e paginados. Retorna (linhas, totais por resultado, total filtrado)."""
    clause, args = _filters(q, filtro, lote_id)
    column = SORTS.get(ordem, "atualizado_em")
    direction = "ASC" if direcao == "asc" else "DESC"
    limit = max(1, min(int(limite), 100000))
    offset = (max(1, int(pagina)) - 1) * limit
    fields = "*" if completo else "lote_id, processo_id, lote_nome, processo, sei, requerente, cpf, inscricao, " \
                                  "modalidade, renda_centavos, resultado, sintese, motivos, versao, criado_em, " \
                                  "atualizado_em, analisado_em, tempo_ms, decisao, origem_decisao, revisado_por, decidido_em"
    sql = (f"SELECT {fields} FROM processos {clause} "
           f"ORDER BY {column} IS NULL, {column} {direction}, processo LIMIT ? OFFSET ?")
    lot_clause, lot_args = _filters(lote_id=lote_id)
    with closing(connect()) as db:
        rows = [dict(row) for row in db.execute(sql, [*args, limit, offset])]
        total = db.execute(f"SELECT count(*) FROM processos {clause}", args).fetchone()[0]
        totals = {}
        for decision, result, origin, count in db.execute(
                f"SELECT decisao, resultado, origem_decisao, count(*) FROM processos {lot_clause} "
                f"GROUP BY decisao, resultado, origem_decisao", lot_args):
            for name, (column, values) in FILTERS.items():
                value = {"decisao": decision, "resultado": result, "origem_decisao": origin}[column]
                if value in values:
                    totals[name] = totals.get(name, 0) + count
            totals["todos"] = totals.get("todos", 0) + count
        related = duplicates(db, rows)
        totals["duplicados"] = db.execute(
            f"SELECT count(*) FROM processos {lot_clause}{' AND' if lot_clause else ' WHERE'} {DUPLICATE_SQL}",
            lot_args).fetchone()[0]
    for row in rows:
        row["motivos"] = json.loads(row["motivos"] or "[]")
        row["duplicidades"] = related.get((row["lote_id"], row["processo_id"]), [])
        if completo:
            row["parecer"] = json.loads(row["parecer"]) if row.get("parecer") else None
    return rows, totals, total


def dashboard(lote_id="", dias=14):
    """Indicadores pela decisão final: totais, origem (IA ou revisão humana), tempo e série diária."""
    clause, args = _filters(lote_id=lote_id)
    since = time.time() - dias * 86400
    with closing(connect()) as db:
        counts = db.execute(f"SELECT decisao, resultado, origem_decisao, count(*) AS n FROM processos {clause} "
                            f"GROUP BY decisao, resultado, origem_decisao", args).fetchall()
        timing = db.execute(f"SELECT avg(tempo_ms) FROM processos {clause}"
                            f"{' AND' if clause else ' WHERE'} tempo_ms IS NOT NULL", args).fetchone()
        work = db.execute(f"SELECT coalesce(sum(paginas),0), coalesce(sum(tokens_entrada),0), "
                          f"coalesce(sum(tokens_saida),0), count(tokens_entrada) FROM processos {clause}", args).fetchone()
        series = db.execute(
            f"SELECT date(analisado_em, 'unixepoch', 'localtime') AS dia, decisao, count(*) AS n, "
            f"avg(tempo_ms) AS tempo FROM processos {clause}{' AND' if clause else ' WHERE'} analisado_em >= ? "
            f"GROUP BY dia, decisao ORDER BY dia", [*args, since]).fetchall()
    report = {"processados": 0, "aceitos": 0, "aceitos_sem_ressalvas": 0, "aceitos_conferir_cadastro": 0,
              "aceitos_por_ia": 0, "aceitos_com_revisao": 0, "nao_aceitos": 0, "revisao_manual": 0,
              "fora_do_fluxo": 0, "processando": 0, "com_revisao_humana": 0, "somente_ia": 0}
    names = {"deferido": "aceitos", "indeferido": "nao_aceitos", "pendente": "revisao_manual",
             "fora_do_fluxo": "fora_do_fluxo"}
    for decision, result, origin, count in counts:
        if result == "processando" and decision is None:
            report["processando"] += count
            continue
        report["processados"] += count
        if decision in names:
            report[names[decision]] += count
        report["com_revisao_humana" if origin == "revisao_humana" else "somente_ia"] += count
        if decision == "deferido":
            report["aceitos_com_revisao" if origin == "revisao_humana" else "aceitos_por_ia"] += count
            if origin != "revisao_humana" and result == "enquadra":
                report["aceitos_sem_ressalvas"] += count
            elif origin != "revisao_humana":
                report["aceitos_conferir_cadastro"] += count
    days = {}
    for day, decision, count, average in series:
        entry = days.setdefault(day, {"dia": day, "aceitos": 0, "nao_aceitos": 0, "revisao_manual": 0,
                                      "fora_do_fluxo": 0, "_t": 0.0, "_n": 0})
        if decision in names:
            entry[names[decision]] += count
        if average is not None:
            entry["_t"] += average * count
            entry["_n"] += count
    report["serie"] = []
    for entry in sorted(days.values(), key=lambda item: item["dia"]):
        entry["tempo_medio_ms"] = round(entry.pop("_t") / entry["_n"]) if entry["_n"] else None
        entry.pop("_n")
        report["serie"].append(entry)
    report["tempo_medio_ms"] = round(timing[0]) if timing[0] is not None else None
    report["paginas"], report["tokens_entrada"], report["tokens_saida"], counted = work
    report["tokens_por_processo"] = round((report["tokens_entrada"] + report["tokens_saida"]) / counted) if counted else None
    report["dias"] = dias
    report["envios"] = by_lot(lote_id)
    return report


def by_lot(lote_id="", limite=1000):
    """Envios (lotes), em ordem de envio: decisões, tempo e tokens por cidadão em cada um."""
    clause, args = _filters(lote_id=lote_id)
    names = {"deferido": "aceitos", "indeferido": "nao_aceitos", "pendente": "revisao_manual", "fora_do_fluxo": "fora_do_fluxo"}
    with closing(connect()) as db:
        lots = db.execute(f"SELECT lote_id, max(lote_nome) AS nome, min(criado_em) AS criado FROM processos {clause} "
                          f"GROUP BY lote_id ORDER BY criado DESC LIMIT ?", [*args, limite]).fetchall()
        result = []
        for lot in reversed(lots):
            entry = {"lote_id": lot["lote_id"], "nome": lot["nome"], "criado_em": lot["criado"], "aceitos": 0,
                     "nao_aceitos": 0, "revisao_manual": 0, "fora_do_fluxo": 0, "processando": 0}
            for decision, result_code, count in db.execute(
                    "SELECT decisao, resultado, count(*) FROM processos WHERE lote_id=? GROUP BY decisao, resultado",
                    (lot["lote_id"],)):
                if result_code == "processando" and decision is None:
                    entry["processando"] += count
                elif decision in names:
                    entry[names[decision]] += count
            average = db.execute("SELECT avg(tempo_ms) FROM processos WHERE lote_id=? AND tempo_ms IS NOT NULL",
                                 (lot["lote_id"],)).fetchone()[0]
            entry["tempo_medio_ms"] = round(average) if average is not None else None
            tokens, counted = db.execute(
                "SELECT coalesce(sum(coalesce(tokens_entrada,0) + coalesce(tokens_saida,0)),0), "
                "count(tokens_entrada) FROM processos WHERE lote_id=? AND tokens_entrada IS NOT NULL",
                (lot["lote_id"],)).fetchone()
            entry["tokens"] = tokens if counted else None
            entry["tokens_por_processo"] = round(tokens / counted) if counted else None
            result.append(entry)
    return result


def find_by_sei(sei):
    """Processo mais recente com esse número SEI (aceita com / ou -), com o documento completo."""
    key = "".join(ch for ch in sei if ch.isdigit())
    if not key:
        return None
    with closing(connect()) as db:
        for row in db.execute("SELECT lote_id, processo_id, sei, processo FROM processos "
                              "WHERE parecer IS NOT NULL ORDER BY atualizado_em DESC"):
            if any(value and "".join(ch for ch in value if ch.isdigit()) == key for value in (row["sei"], row["processo"])):
                found = dict(db.execute("SELECT * FROM processos WHERE lote_id=? AND processo_id=?",
                                        (row["lote_id"], row["processo_id"])).fetchone())
                found["parecer"] = json.loads(found["parecer"])
                found["duplicidades"] = duplicates(db, [found]).get((found["lote_id"], found["processo_id"]), [])
                return found
    return None


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


def _iso(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(seconds)) if seconds else None


def integration_view(row):
    """Resposta da API de integração: aceite, dados do aceite, datas e origem da decisão."""
    document = row["parecer"] if isinstance(row.get("parecer"), dict) else (json.loads(row["parecer"]) if row.get("parecer") else {})
    report = document.get("parecer") or {}
    resumo = report.get("resumo") or {}
    final = document.get("decisao_final") or {}
    decision = row.get("decisao") or final.get("decisao")
    return {
        "versao_formato": EXPORT_VERSION,
        "numero_sei": row.get("sei") or (document.get("processo") or {}).get("numero_sei"),
        "processo": row.get("processo"),
        "aceito": True if decision == "deferido" else False if decision == "indeferido" else None,
        "decisao": decision or ("processando" if row.get("resultado") == "processando" else None),
        "decisao_ia": final.get("decisao_ia"),
        "origem_decisao": row.get("origem_decisao") or "ia",
        "revisao_humana": (row.get("origem_decisao") == "revisao_humana"),
        "revisado_por": row.get("revisado_por"),
        "justificativa": final.get("justificativa"),
        "processado_em": _iso(row.get("analisado_em")),
        "decidido_em": _iso(row.get("decidido_em")) or _iso(row.get("analisado_em")),
        "atualizado_em": _iso(row.get("atualizado_em")),
        "dados_do_aceite": {
            "requerente": resumo.get("requerente"), "cpf": resumo.get("cpf"),
            "inscricao_imobiliaria": resumo.get("inscricao_imobiliaria"), "modalidade": resumo.get("modalidade"),
            "renda_familiar_centavos": resumo.get("renda_familiar_centavos"), "exercicio": report.get("exercicio"),
            "sintese": resumo.get("sintese"),
            "fundamentos": [{"criterio": item["criterio"], "situacao": item["status"], "motivo": item["motivo"]}
                            for item in report.get("criterios") or []],
        },
        "parecer_texto": report.get("parecer_texto"),
        "possiveis_duplicidades": [{"numero_sei": item.get("sei") or item.get("processo"), "motivo": item["descricao"],
                                    "decisao": item.get("decisao")} for item in row.get("duplicidades") or []],
    }


def changed_since(since, decisao="", pagina=1, por_pagina=100):
    where, args = ["parecer IS NOT NULL", "coalesce(atualizado_em, criado_em) >= ?"], [since]
    if decisao in DECISOES:
        where.append("decisao = ?")
        args.append(decisao)
    clause = "WHERE " + " AND ".join(where)
    with closing(connect()) as db:
        total = db.execute(f"SELECT count(*) FROM processos {clause}", args).fetchone()[0]
        rows = [dict(row) for row in db.execute(
            f"SELECT * FROM processos {clause} ORDER BY coalesce(atualizado_em, criado_em), processo LIMIT ? OFFSET ?",
            [*args, por_pagina, (pagina - 1) * por_pagina])]
        related = duplicates(db, rows)
        for row in rows:
            row["duplicidades"] = related.get((row["lote_id"], row["processo_id"]), [])
    return [integration_view(row) for row in rows], total


def reset_all(usuario=None):
    """Apaga o histórico de pareceres (processos e versões). A auditoria registra o evento."""
    with _WRITE, closing(connect()) as db, db:
        removed = db.execute("SELECT count(*) FROM processos").fetchone()[0]
        db.execute("DELETE FROM processos")
        db.execute("DELETE FROM versoes")
        db.execute("INSERT INTO auditoria VALUES (?,?,?,?,?,?)",
                   (time.time(), f"sistema_zerado ({removed} processos)", None, None, None, usuario))
    with _WRITE, closing(connect()) as db:
        db.execute("VACUUM")  # devolve ao disco o espaço do histórico apagado
    return removed
