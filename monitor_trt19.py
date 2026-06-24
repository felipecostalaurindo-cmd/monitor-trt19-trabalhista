#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor das Turmas Trabalhistas do TRT-19 (1ª e 2ª Turma) — engine.

Réplica da mecânica do monitor das Câmaras Cíveis do TJ/AL, trocando a FONTE
(e-SAJ/cjsg -> FALCÃO, a Jurisprudência Unificada da Justiça do Trabalho) e o
DOMÍNIO (cível -> trabalhista). Python stdlib (urllib) — roda headless.

Subcomandos:
  coletar    Busca acórdãos das Turmas do TRT-19 na API pública do FALCÃO por
             janela de DATA DE JUNTADA/disponibilização e grava CSV bruto
             (com ementa + inteiro teor inline). Atribui a Turma pelo campo do doc.
  agregar    [Fase 1] Percentuais por Turma + recorte por classe processual +
             tendência; grava registro datado (resumo.json/md + slack.txt).
             (A camada de MATÉRIA/subtemas/incidentes entra na Fase 2 c/ taxonomia.)
  notificar  Posta o slack.txt no canal via Incoming Webhook (payload c/ 'channel').
  filtrar    Drill-down: lista acórdãos de uma Turma/classe/termo (p/ ler inteiro teor).

Fonte (validada 22/06/2026 — ver FONTE_FALCAO.md):
  base:   https://jurisprudencia.jt.jus.br/jurisprudencia-nacional-backend/api/no-auth
  busca:  GET /pesquisa?sessionId=...&tribunais=TRT19&orgaoJulgador=Primeira Turma,Segunda Turma
                &colecao=acordaos&dataInicio=YYYY-MM-DD&dataFim=YYYY-MM-DD&page=N&size=5
  auth:   sessão AUTO-EMITIDA — sessionId="_"+7chars base36, mandado em cookie
          SESSION_ID_COOKIE_PUJ E como query param (server só exige cookie==param).
  regras: size=5 OBRIGATÓRIO; rate limit ~50 req/janela (header x-rate-limit-remaining).
"""
import argparse
import csv
import datetime as dt
import html
import http.cookiejar  # noqa: F401 (mantido p/ paridade; usamos cookie manual)
import json
import os
import random
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://jurisprudencia.jt.jus.br/jurisprudencia-nacional-backend/api/no-auth"
APP = "https://jurisprudencia.jt.jus.br/jurisprudencia-nacional"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TRIBUNAL = "TRT19"
PAGE_SIZE = 5  # OBRIGATÓRIO: a API só aceita size=5 (outros -> 403).

# Órgãos julgadores das Turmas (valor do filtro orgaoJulgador -> rótulo curto).
ORGAOS = {
    "Primeira Turma": "1ª Turma",
    "Segunda Turma": "2ª Turma",
}
# Opcional (fora do escopo travado, mas a API expõe):
ORGAOS_EXTRA = {"Tribunal Pleno": "Tribunal Pleno"}

ORDEM_TURMAS = ["1ª Turma", "2ª Turma", "Tribunal Pleno"]

CAMPOS = [
    "orgao_julgador", "turma", "numero", "classe_sigla", "classe", "relator",
    "tribunal", "data_julgamento", "data_juntada", "id_documento",
    "possui_ementa", "ementa", "inteiro_teor", "referencia_legislativa", "url",
]

INTEIRO_TEOR_MAX = 6000  # trunca o inteiro teor no CSV (classificação usa ementa + começo do teor)


# ----------------------------------------------------------------------------- #
# Cliente HTTP (stdlib) — sessão auto-emitida, cookies manuais, rate-limit aware
# ----------------------------------------------------------------------------- #
def _novo_sid():
    """Replica obterSessionId() do FALCÃO: '_' + 7 chars base36 aleatórios."""
    alfabeto = string.ascii_lowercase + string.digits
    return "_" + "".join(random.choice(alfabeto) for _ in range(7))


class _Cliente:
    def __init__(self, sid=None, verbose=True):
        self.sid = sid or _novo_sid()
        self.verbose = verbose
        self.cookies = {"SESSION_ID_COOKIE_PUJ": self.sid}
        self.rate_remaining = None

    def _headers(self):
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        return {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "pt-BR,pt;q=0.9",
            "Referer": f"{APP}/pesquisa",
            "Origin": "https://jurisprudencia.jt.jus.br",
            "Cookie": cookie,
        }

    def _absorver_cookies(self, headers):
        for sc in headers.get_all("Set-Cookie") or []:
            par = sc.split(";", 1)[0].strip()
            if "=" in par:
                k, v = par.split("=", 1)
                self.cookies[k] = v

    def get(self, path, params, timeout=60, tentativas=3):
        url = f"{API}{path}?" + urllib.parse.urlencode(params, encoding="utf-8")
        req = urllib.request.Request(url, headers=self._headers())
        ultimo_erro = None
        for n in range(tentativas):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    self._absorver_cookies(r.headers)
                    rem = r.headers.get("x-rate-limit-remaining")
                    if rem is not None:
                        self.rate_remaining = int(rem)
                    raw = r.read().decode("utf-8", errors="replace")
                    self._talvez_pausar_rate()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                corpo = e.read().decode("utf-8", errors="replace")
                ultimo_erro = f"HTTP {e.code}: {corpo[:200]}"
                # 403 do guard anti-abuso ("Tentativa inválida"/"tamanho N") é fatal,
                # não adianta repetir — provavelmente erro de parâmetro (size != 5 etc.).
                if e.code == 403 and ("tamanho" in corpo or "inválida" in corpo or "invalida" in corpo):
                    raise RuntimeError(f"Guard do FALCÃO recusou a requisição ({ultimo_erro}). "
                                       f"Confira size=5 e sessionId==cookie.") from None
                # 429/5xx/403 transitório: backoff e tenta de novo.
                espera = 65 if e.code == 429 else (3 * (n + 1))
                if self.verbose:
                    print(f"  [retry {n+1}/{tentativas}] {ultimo_erro} — aguardando {espera}s",
                          file=sys.stderr)
                time.sleep(espera)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                ultimo_erro = str(e)
                time.sleep(3 * (n + 1))
        raise RuntimeError(f"Falha após {tentativas} tentativas em {path}: {ultimo_erro}")

    def _talvez_pausar_rate(self):
        # Janela ~50 req. Se a folga ficar baixa, espera o reset (~1 min).
        if self.rate_remaining is not None and self.rate_remaining <= 3:
            if self.verbose:
                print(f"  [rate-limit] folga={self.rate_remaining} — pausando 65s p/ reset",
                      file=sys.stderr)
            time.sleep(65)

    def _params_base(self, orgaos_valores, texto, dt_ini, dt_fim, classe=None):
        p = {
            "sessionId": self.sid,
            "latitude": "0",
            "longitude": "0",
            "texto": texto or "",
            "verTodosPrecedentes": "false",
            "tribunais": TRIBUNAL,
            "pesquisaSomenteNasEmentas": "false",
            "orgaoJulgador": ",".join(orgaos_valores),
            "colecao": "acordaos",
        }
        if classe:
            p["classeProcesso"] = classe
        if dt_ini:
            p["dataInicio"] = dt_ini  # ISO YYYY-MM-DD (filtra por JUNTADA)
        if dt_fim:
            p["dataFim"] = dt_fim
        return p

    def filtros(self, orgaos_valores, texto, dt_ini, dt_fim, classe=None):
        return self.get("/pesquisa/filtros", self._params_base(orgaos_valores, texto, dt_ini, dt_fim, classe))

    def pesquisa(self, orgaos_valores, texto, dt_ini, dt_fim, page, classe=None):
        p = self._params_base(orgaos_valores, texto, dt_ini, dt_fim, classe)
        p["page"] = page
        p["size"] = PAGE_SIZE
        return self.get("/pesquisa", p)


# ----------------------------------------------------------------------------- #
# Parse
# ----------------------------------------------------------------------------- #
def _strip_html(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _url_acordao(numero):
    return f"{APP}/pesquisa/numero/{urllib.parse.quote(numero)}?abaSelecionada=acordaos"


def _parse_doc(d):
    turma_label = (d.get("turma") or "").strip()
    return {
        "orgao_julgador": turma_label,
        "turma": ORGAOS.get(turma_label, ORGAOS_EXTRA.get(turma_label, turma_label)),
        "numero": d.get("numeroProcesso", ""),
        "classe_sigla": d.get("siglaClasseProcesso", ""),
        "classe": d.get("classeProcesso", ""),
        "relator": d.get("relator", ""),
        "tribunal": d.get("tribunal", ""),
        "data_julgamento": d.get("dataJulgamento") or "",
        "data_juntada": d.get("dataJuntada") or "",
        "id_documento": str(d.get("idDocumentoAcordao") or ""),
        "possui_ementa": d.get("possuiEmenta") or "",
        "ementa": _strip_html(d.get("ementa") or ""),
        "inteiro_teor": _strip_html(d.get("textoAcordao") or "")[:INTEIRO_TEOR_MAX],
        "referencia_legislativa": "|".join(d.get("referenciaLegislativa") or []),
        "url": _url_acordao(d.get("numeroProcesso", "")),
    }


# ----------------------------------------------------------------------------- #
# Coleta
# ----------------------------------------------------------------------------- #
# Limites do FALCÃO descobertos ao vivo: paginação profunda travada na página 40
# (= 200 docs por query). Acima disso é preciso FATIAR a busca (data, depois classe).
RESULT_CAP = 200
PAGE_CAP = 40


def _d(iso):
    return dt.date.fromisoformat(iso)


def _date_mid(d_ini, d_fim):
    a, b = _d(d_ini), _d(d_fim)
    return _iso(a + (b - a) // 2)


def _date_next(iso):
    return _iso(_d(iso) + dt.timedelta(days=1))


def _classes_presentes(cli, orgao, d_ini, d_fim, texto, verbose):
    """Valores de classe processual presentes na janela (p/ subdividir dia 'quente')."""
    try:
        f = cli.filtros([orgao], texto, d_ini, d_fim)
        fac = next((x for x in f.get("filtrosDisponiveis", [])
                    if "classe" in (x.get("nomeDoFiltro") or "").lower()), None)
        if fac:
            return [v["valor"] for v in fac.get("valoresFiltro", []) if v.get("valor")]
    except Exception as e:
        if verbose:
            print(f"  [aviso] facet de classe indisponível ({e})", file=sys.stderr)
    return []


def coletar(dt_ini, dt_fim, orgaos_valores, texto="", pausa=0.8, verbose=True):
    """Coleta acórdãos das Turmas na janela [dt_ini, dt_fim] (ISO, por juntada).
    Itera por Turma e FATIA a janela (bisecção por data; depois por classe num dia
    quente) para nenhuma sub-busca passar de 200 docs (cap de paginação do FALCÃO)."""
    cli = _Cliente(verbose=verbose)
    rows, seen = [], set()
    for orgao in orgaos_valores:
        n0 = len(rows)
        _coletar_janela(cli, orgao, dt_ini, dt_fim, texto, None, rows, seen, pausa, verbose)
        if verbose:
            print(f"  [{ORGAOS.get(orgao, orgao)}] {len(rows) - n0} acórdãos", file=sys.stderr)
    if verbose:
        print(f"  TOTAL coletado: {len(rows)} acórdãos (únicos)", file=sys.stderr)
    return rows


def _coletar_janela(cli, orgao, d_ini, d_fim, texto, classe, rows, seen, pausa, verbose, prof=0):
    """Coleta uma (Turma, janela[, classe]); fatia se passar do cap de 200."""
    data = cli.pesquisa([orgao], texto, d_ini, d_fim, 0, classe=classe)
    total = data.get("quantidadeTotal") or 0
    docs0 = data.get("documentos") or []
    tag = f"{ORGAOS.get(orgao, orgao)} {d_ini}..{d_fim}" + (f" classe={classe}" if classe else "")

    if total > RESULT_CAP:
        if d_ini != d_fim:
            mid = _date_mid(d_ini, d_fim)
            if verbose:
                print(f"  [fatiar data] {tag}: {total}>200 -> [{d_ini}..{mid}]+[{_date_next(mid)}..{d_fim}]",
                      file=sys.stderr)
            _coletar_janela(cli, orgao, d_ini, mid, texto, classe, rows, seen, pausa, verbose, prof + 1)
            time.sleep(pausa)
            _coletar_janela(cli, orgao, _date_next(mid), d_fim, texto, classe, rows, seen, pausa, verbose, prof + 1)
            return
        if classe is None:
            classes = _classes_presentes(cli, orgao, d_ini, d_fim, texto, verbose)
            if classes:
                if verbose:
                    print(f"  [fatiar classe] {tag}: {total}>200 em 1 dia -> {len(classes)} classes",
                          file=sys.stderr)
                for cl in classes:
                    time.sleep(pausa)
                    _coletar_janela(cli, orgao, d_ini, d_fim, texto, cl, rows, seen, pausa, verbose, prof + 1)
                return
        # Indivisível e > 200: coleta as 200 acessíveis e AVISA (sem truncar em silêncio).
        if verbose:
            print(f"  [LIMITE] {tag}: {total}>200 indivisível — coletando só as 200 acessíveis", file=sys.stderr)

    _absorver(rows, seen, docs0)
    page = 1
    while page * PAGE_SIZE < total and page < PAGE_CAP:
        time.sleep(pausa)
        d = cli.pesquisa([orgao], texto, d_ini, d_fim, page, classe=classe)
        ds = d.get("documentos") or []
        if not ds:
            break
        _absorver(rows, seen, ds)
        page += 1


def _absorver(rows, seen, docs):
    for d in docs:
        chave = str(d.get("idDocumentoAcordao") or "") or d.get("numeroProcesso", "")
        if chave and chave in seen:
            continue
        if chave:
            seen.add(chave)
        rows.append(_parse_doc(d))


def _gravar_csv(linhas, caminho):
    os.makedirs(os.path.dirname(os.path.abspath(caminho)), exist_ok=True)
    with open(caminho, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CAMPOS)
        w.writeheader()
        for ln in linhas:
            w.writerow({k: ln.get(k, "") for k in CAMPOS})


def _orgaos_from_args(args):
    if getattr(args, "com_pleno", False):
        return list(ORGAOS.keys()) + list(ORGAOS_EXTRA.keys())
    if getattr(args, "orgaos", None):
        # aceita rótulos curtos (1ª/2ª) ou os valores do FALCÃO
        inv = {v: k for k, v in {**ORGAOS, **ORGAOS_EXTRA}.items()}
        out = []
        for o in args.orgaos.split(","):
            o = o.strip()
            out.append(inv.get(o, o))
        return out
    return list(ORGAOS.keys())


def _iso(d):
    return d.strftime("%Y-%m-%d")


def _cmd_coletar(args):
    if args.dias:
        hoje = dt.date.today()
        dt_fim = _iso(hoje)
        dt_ini = _iso(hoje - dt.timedelta(days=args.dias))
    else:
        dt_ini, dt_fim = args.inicio, args.fim
    orgaos = _orgaos_from_args(args)
    print(f"Coletando Turmas do TRT-19 {orgaos} — juntada {dt_ini} a {dt_fim}", file=sys.stderr)
    linhas = coletar(dt_ini, dt_fim, orgaos, texto=args.texto or "", pausa=args.pausa)
    _gravar_csv(linhas, args.out)
    print(f"OK: {len(linhas)} acórdãos -> {args.out}", file=sys.stderr)


# ----------------------------------------------------------------------------- #
# Classificação (Fase 2) — determinística; resíduo -> Sonnet lê o inteiro teor
# ----------------------------------------------------------------------------- #
# DIFERENÇA vs o cível: o FALCÃO não traz assunto/CNJ, então a matéria é lida SÓ do
# TEXTO (ementa + começo do inteiro teor + referenciaLegislativa). E a matéria é
# MULTIVALORADA (uma reclamação junta várias) -> coluna 'materias' ('|'-joined) +
# incidência no agregar. 'area' guarda a matéria DOMINANTE (1º match) p/ os destaques.
CAMPOS_CLASS = CAMPOS + ["classe_curta", "area", "materias", "subtema", "subtemas",
                         "incidentes", "precisa_llm"]

TEOR_MATCH = 4000  # quanto do inteiro teor entra no casamento (ementas vazias caem aqui)


def carregar_taxonomia(caminho=None):
    if caminho is None:
        caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "taxonomia.json")
    with open(caminho, encoding="utf-8") as f:
        return json.load(f)


def _norm_refs(refs):
    """referenciaLegislativa ('art_477_clt|sumula_331_tst') -> texto casável
    ('art 477 clt sumula 331 tst'). Sinal forte p/ matéria e incidente."""
    return re.sub(r"[_|]+", " ", refs or "").lower()


def _classe_curta(row, tax):
    classe = row.get("classe", "") or ""
    if classe in tax.get("classe_norm", {}):
        return tax["classe_norm"][classe]
    sig = (row.get("classe_sigla") or "").strip()
    return sig or classe or "—"


def _materias(texto, tax):
    """Matéria MULTIVALORADA: (dominante, [todas]). 'dominante' = 1ª na ordem de
    area_keywords que casa (1º match vence); 'todas' = todas as que casam."""
    todas, dominante = [], ""
    for area, kws in tax["area_keywords"]:
        if any(kw in texto for kw in kws):
            todas.append(area)
            if not dominante:
                dominante = area
    return dominante, todas


def _achar_subtema(area, texto, tax):
    regras = tax.get("subtemas_ementa", {}).get(area)
    if not regras:
        return ""
    for label, kws in regras:
        if any(kw in texto for kw in kws):
            return label
    return "Outros"


_RE_EMENTA_INI = re.compile(r"\bEment[ae]\b", re.I)
_RE_EMENTA_FIM = re.compile(
    r"raz[õo]es\s+de\s+decidir|tese\s+de\s+julgamento|\bdispositivo\b|"
    r"é\s+como\s+vot|\bacordam\b|\bv\s*o\s*t\s*o\b", re.I)


def _extrair_ementa(inteiro_teor):
    """O FALCÃO-TRT19 deixa o campo 'ementa' VAZIO (possuiEmenta=N), mas a EMENTA vem
    EMBUTIDA no textoAcordao como seção 'I. Ementa ...' seguindo o padrão CNJ (cabeçalho
    temático em CAIXA ALTA com as matérias decididas + CASO EM EXAME + QUESTÕES EM
    DISCUSSÃO). Recorta esse bloco — a zona da matéria DECIDIDA — cortando fora as RAZÕES
    DE DECIDIR / voto / dispositivo (onde a matéria é só MENCIONADA, inflando a contagem).
    '' se a estrutura não for encontrada (cai p/ o começo do inteiro teor)."""
    if not inteiro_teor:
        return ""
    mi = _RE_EMENTA_INI.search(inteiro_teor)
    if not mi:
        return ""
    resto = inteiro_teor[mi.end():mi.end() + 6000]
    mf = _RE_EMENTA_FIM.search(resto)
    bloco = resto[:mf.start()] if mf else resto[:3000]
    return bloco.strip()


def _texto_ementa(row):
    """Texto de ementa p/ classificar: campo 'ementa' se vier preenchido (raro no TRT19),
    senão a seção de ementa extraída do inteiro teor."""
    em = (row.get("ementa") or "").strip()
    if em:
        return em
    return _extrair_ementa(row.get("inteiro_teor") or "")


def _nucleo_ementa(ementa_txt, inteiro_teor=""):
    """Núcleo do litígio p/ casar incidentes sem pegar só o dispositivo:
      1) ementa com 'CASO EM EXAME' (CNJ): recorta CASO..RAZÕES/TESE/DISPOSITIVO;
      2) ementa sem padrão: ementa inteira;
      3) sem ementa: começo do inteiro teor (relatório + fundamentação)."""
    base = ementa_txt or ""
    if not base.strip():
        return (inteiro_teor or "")[:TEOR_MATCH]
    mi = re.search(r"caso\s+em\s+exame", base, re.I)
    if mi:
        ini = mi.start()
        mf = re.search(r"raz[õo]es\s+de\s+decidir|tese\s+de\s+julgamento|\bdispositivo\b",
                       base[ini:], re.I)
        fim = ini + mf.start() if mf else len(base)
        nucleo = base[ini:fim].strip()
        if len(nucleo) >= 30:
            return nucleo
    return base


def _achar_incidentes(texto_nucleo, refs_norm, tax):
    defs = tax.get("incidentes_processuais", [])
    if not defs:
        return ""
    t = (texto_nucleo + " " + refs_norm).lower()
    achados = [label for label, kws in defs if any(kw in t for kw in kws)]
    return "|".join(achados)


def classificar_linha(row, tax):
    teor = row.get("inteiro_teor", "") or ""
    refs_norm = _norm_refs(row.get("referencia_legislativa", ""))
    ementa_txt = _texto_ementa(row)
    # Matéria/subtema: prefere a EMENTA (zona da matéria decidida) — alta precisão.
    # Sem ementa estruturada, cai p/ o começo do inteiro teor (recall, menor precisão).
    if ementa_txt:
        texto_mat = (ementa_txt + " " + refs_norm).lower()
    else:
        texto_mat = (teor[:TEOR_MATCH] + " " + refs_norm).lower()
    dominante, todas = _materias(texto_mat, tax)
    # Sinal estrutural: Agravo de Petição (AP/AIAP) só cabe na EXECUÇÃO -> injeta a
    # matéria pela classe (alta precisão), evitando depender de refs genéricas (art. 769).
    if (row.get("classe_sigla") or "").upper() in ("AP", "AIAP") and "Execução trabalhista" not in todas:
        todas.append("Execução trabalhista")
        dominante = dominante or "Execução trabalhista"
    # Subtema POR matéria (mundo multivalorado): mapa 'matéria -> recorte', só p/ as
    # matérias com regras de subtema. Alimenta os destaques pela incidência da matéria.
    subtemas_map = {}
    for area in todas:
        lab = _achar_subtema(area, texto_mat, tax)
        if lab:
            subtemas_map[area] = lab
    # Incidentes: núcleo do litígio (ementa CASO/QUESTÕES, ou começo do teor) + refs.
    nucleo = _nucleo_ementa(ementa_txt, teor).lower()
    incidentes = _achar_incidentes(nucleo, refs_norm, tax)
    # Resíduo p/ Sonnet = ÓRFÃO REAL (nem matéria nem incidente). Acórdão só processual
    # (ex.: ED rejeitado, gratuidade) tem incidente e NÃO é resíduo.
    return {
        "classe_curta": _classe_curta(row, tax),
        "area": dominante,
        "materias": "|".join(todas),
        "subtema": subtemas_map.get(dominante, ""),
        "subtemas": "|".join(f"{a}::{l}" for a, l in subtemas_map.items()),
        "incidentes": incidentes,
        "precisa_llm": "1" if (not todas and not incidentes) else "",
    }


def _cmd_classificar(args):
    tax = carregar_taxonomia(args.taxonomia)
    rows = list(csv.DictReader(open(args.inp, encoding="utf-8")))
    n_llm = 0
    for r in rows:
        r.update(classificar_linha(r, tax))
        # O FALCÃO-TRT19 deixa 'ementa' VAZIO; grava a ementa CNJ extraída do inteiro teor
        # (texto da matéria DECIDIDA) p/ o drill-down e a busca por --texto a terem o que ler.
        if not (r.get("ementa") or "").strip():
            r["ementa"] = _texto_ementa(r)
        if r["precisa_llm"]:
            n_llm += 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_CLASS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CAMPOS_CLASS})
    pct = (100.0 * n_llm / len(rows)) if rows else 0
    print(f"OK: {len(rows)} classificados | resíduo sem matéria casada (p/ Sonnet ler o "
          f"inteiro teor): {n_llm} ({pct:.1f}%) -> {args.out}", file=sys.stderr)


# ----------------------------------------------------------------------------- #
# Agregação (Fase 2: matéria trabalhista [incidência] + classe + subtemas + incidentes)
# ----------------------------------------------------------------------------- #
import collections


def _milhar(n):
    return f"{n:,}".replace(",", ".")


def _dist(rows, campo, top=None):
    n = len(rows)
    cnt = collections.Counter((r.get(campo) or "—") for r in rows)
    itens = [{"rotulo": k, "n": v, "pct": round(100.0 * v / n, 1)} for k, v in cnt.most_common()]
    return itens[:top] if top else itens


def _ordena_turmas(nomes):
    def chave(nm):
        return (ORDEM_TURMAS.index(nm) if nm in ORDEM_TURMAS else 99, nm)
    return sorted(nomes, key=chave)


def _incidencia(rows, campo):
    """Eixo MULTIVALORADO ('|'-joined): % de acórdãos em que cada rótulo aparece.
    Um acórdão conta em vários — NÃO soma 100%. Ordenado por frequência."""
    n = len(rows)
    if not n:
        return []
    cnt = collections.Counter()
    for r in rows:
        for lab in (r.get(campo) or "").split("|"):
            if lab:
                cnt[lab] += 1
    return [{"rotulo": k, "n": v, "pct": round(100.0 * v / n, 1)} for k, v in cnt.most_common()]


def _parse_subtemas(s):
    """'Adicionais...::Insalubridade|Verbas...::Aviso prévio' -> {matéria: recorte}."""
    mp = {}
    for part in (s or "").split("|"):
        if "::" in part:
            a, lab = part.split("::", 1)
            mp[a] = lab
    return mp


def _destaques(rows, tax):
    """Para cada matéria com 'subtemas_ementa', recorta TODOS os acórdãos que a TOCAM
    (incidência), pelo recorte daquela matéria na coluna 'subtemas'. Assim a base do
    destaque bate com a incidência da matéria no panorama. pct = % dentro do recorte."""
    out = []
    total = len(rows)
    for area in tax.get("subtemas_ementa", {}):
        labels = []
        for r in rows:
            mp = _parse_subtemas(r.get("subtemas"))
            if area in mp:
                labels.append(mp[area])
        if not labels:
            continue
        cnt = collections.Counter(labels)
        nn = len(labels)
        subt = [{"rotulo": k, "n": v, "pct": round(100.0 * v / nn, 1)} for k, v in cnt.most_common()]
        out.append({
            "area": area,
            "total": nn,
            "pct_pauta": round(100.0 * nn / total, 1) if total else 0.0,
            "subtemas": subt,
        })
    return out


def _carregar_tendencia(base_dir, rotulo_atual):
    """Registro datado anterior -> (rotulo, {classes, materias, incidentes, destaques})."""
    vazio = {"classes": None, "materias": None, "incidentes": None, "destaques": None}
    if not base_dir or not os.path.isdir(base_dir):
        return None, vazio
    pat = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    cands = sorted([d for d in os.listdir(base_dir)
                    if pat.match(d) and d != rotulo_atual
                    and os.path.isfile(os.path.join(base_dir, d, "resumo.json"))])
    if not cands:
        return None, vazio
    anterior = cands[-1]
    try:
        prev = json.load(open(os.path.join(base_dir, anterior, "resumo.json"), encoding="utf-8"))
    except Exception:
        return None, vazio
    out = {
        "classes": {i["rotulo"]: i["pct"] for i in prev.get("geral", {}).get("classes", [])},
        "materias": {i["rotulo"]: i["pct"] for i in prev.get("geral", {}).get("materias", [])},
        "incidentes": {i["rotulo"]: i["pct"] for i in prev.get("incidentes_proc", [])},
        "destaques": {d["area"]: {i["rotulo"]: i["pct"] for i in d.get("subtemas", [])}
                      for d in prev.get("destaques", [])},
    }
    return anterior, out


def agregar(rows, rotulo, janela, base_dir=None, gerado_em=None, tax=None):
    classificado = bool(rows) and ("materias" in rows[0])
    turmas = _ordena_turmas({(r.get("turma") or "—") for r in rows})
    por_turma = {}
    for t in turmas:
        sub = [r for r in rows if (r.get("turma") or "—") == t]
        bloco = {"total": len(sub), "classes": _dist(sub, "classe", top=20)}
        if classificado:
            bloco["materias"] = _incidencia(sub, "materias")
            bloco["incidentes"] = _incidencia(sub, "incidentes")
        por_turma[t] = bloco
    geral = {"classes": _dist(rows, "classe", top=25)}
    resumo = {
        "rotulo": rotulo,
        "gerado_em": gerado_em or dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "janela": janela,
        "total": len(rows),
        "orgaos": turmas,
        "classificado": classificado,
        "por_turma": por_turma,
    }
    if classificado:
        geral["materias"] = _incidencia(rows, "materias")
        resumo["destaques"] = _destaques(rows, tax) if tax else []
        resumo["incidentes_proc"] = _incidencia(rows, "incidentes")
        n_resid = sum(1 for r in rows if (r.get("precisa_llm") or ""))
        resumo["residuo_sem_materia"] = {
            "n": n_resid, "pct": round(100.0 * n_resid / len(rows), 1) if rows else 0.0}
        resumo["_nota"] = ("Fase 2: matéria trabalhista (incidência, multivalorada) + classe "
                           "+ subtemas + incidentes processuais.")
    else:
        resumo["_nota"] = ("Fase 1: por Turma + classe processual. Rode 'classificar' antes do "
                           "'agregar' p/ a lente de matéria (Fase 2).")
    resumo["geral"] = geral
    anterior, prev = _carregar_tendencia(base_dir, rotulo)
    resumo["tendencia_vs"] = anterior
    return resumo, prev


def _delta_pp(pct, prev_map, rotulo):
    if prev_map and rotulo in prev_map:
        d = round(pct - prev_map[rotulo], 1)
        if abs(d) >= 0.1:
            return f"  ({'+' if d > 0 else ''}{d} p.p.)"
    return ""


def _tabela_txt(itens, base_label="classe"):
    linhas = [f"{'%':>5}  {'n':>4}  {base_label}"]
    for it in itens:
        linhas.append(f"{it['pct']:>5.1f}  {it['n']:>4}  {it['rotulo']}")
    return "\n".join(linhas)


def _tabela_pp(itens, prev_map, base_label):
    linhas = [f"{'%':>5}  {'n':>4}  {base_label}"]
    for it in itens:
        linhas.append(f"{it['pct']:>5.1f}  {it['n']:>4}  {it['rotulo']}"
                      f"{_delta_pp(it['pct'], prev_map, it['rotulo'])}")
    return "\n".join(linhas)


def montar_slack(resumo, prev=None):
    prev = prev or {}
    j = resumo["janela"]
    classificado = resumo.get("classificado")
    out = []
    out.append("*Monitor — Turmas Trabalhistas do TRT-19*")
    out.append(f"Acórdãos disponibilizados de *{j['inicio']}* a *{j['fim']}* (por data de juntada)")
    out.append(f"Total: *{_milhar(resumo['total'])}* acórdãos · {len(resumo['orgaos'])} órgão(s)"
               + (f"  · tendência vs {resumo['tendencia_vs']}" if resumo.get("tendencia_vs") else ""))

    if classificado:
        out.append("\n*Panorama geral — por matéria*  (incidência: % dos acórdãos; "
                   "um acórdão pode ter várias, não soma 100%)")
        out.append("```\n" + _tabela_pp(resumo["geral"].get("materias", []), prev.get("materias"), "matéria") + "\n```")
        for d in resumo.get("destaques", []):
            pv = (prev.get("destaques") or {}).get(d["area"], {})
            out.append(f"\n*Destaque — dentro de {d['area']}*  "
                       f"({_milhar(d['total'])} acórdãos · {d['pct_pauta']}% da pauta)")
            out.append("```\n" + _tabela_pp(d["subtemas"], pv, "recorte (lido no acórdão)") + "\n```")
        inc = resumo.get("incidentes_proc") or []
        if inc:
            out.append("\n*Pulso processual — incidentes*  (% dos acórdãos; "
                       "um pode ter vários, não soma 100%)")
            out.append("```\n" + _tabela_pp(inc, prev.get("incidentes"), "incidente") + "\n```")

    out.append("\n*Panorama geral — por classe processual*")
    out.append("```\n" + _tabela_pp(resumo["geral"]["classes"], prev.get("classes"), "classe") + "\n```")

    for t in resumo["orgaos"]:
        c = resumo["por_turma"][t]
        out.append(f"\n*{t}* — {_milhar(c['total'])} acórdãos")
        if classificado and c.get("materias"):
            out.append("_matérias (incidência, top 12):_")
            out.append("```\n" + _tabela_txt(c["materias"][:12], "matéria") + "\n```")
        out.append("_classes:_")
        out.append("```\n" + _tabela_txt(c["classes"], "classe") + "\n```")
        ci = c.get("incidentes") or []
        if classificado and ci:
            out.append("_incidentes (top 8):_")
            out.append("```\n" + _tabela_txt(ci[:8], "incidente") + "\n```")

    if classificado:
        r = resumo.get("residuo_sem_materia") or {}
        out.append(f"\n_Fase 2 (matéria + classe + subtemas + incidentes). Resíduo sem matéria "
                   f"casada: {r.get('n', '?')} ({r.get('pct', '?')}%) — Sonnet lê o inteiro teor._")
    else:
        out.append("\n_Fase 1 (coleta + classe). A lente de MATÉRIA trabalhista entra na Fase 2._")
    return "\n".join(out)


def _delta_md(pct, prev_map, rotulo):
    if prev_map and rotulo in prev_map:
        dd = round(pct - prev_map[rotulo], 1)
        return f"{'+' if dd > 0 else ''}{dd}" if abs(dd) >= 0.1 else "—"
    return ""


def montar_md(resumo, prev=None):
    prev = prev or {}
    j = resumo["janela"]
    classificado = resumo.get("classificado")
    md = [f"# Monitor Turmas Trabalhistas TRT-19 — {resumo['rotulo']}", "",
          f"- **Janela:** {j['inicio']} a {j['fim']} (por data de juntada/disponibilização)",
          f"- **Total:** {_milhar(resumo['total'])} acórdãos · {len(resumo['orgaos'])} órgão(s)",
          f"- **Gerado em:** {resumo['gerado_em']}"]
    if resumo.get("tendencia_vs"):
        md.append(f"- **Tendência comparada a:** {resumo['tendencia_vs']}")
    if classificado:
        r = resumo.get("residuo_sem_materia") or {}
        md.append(f"- **Resíduo sem matéria casada (p/ Sonnet):** {r.get('n', '?')} ({r.get('pct', '?')}%)")

        md += ["", "## Panorama geral — por matéria",
               "", "*Incidência: % dos acórdãos que tocam a matéria; um acórdão pode ter várias — "
               "não soma 100%. Lido na ementa + inteiro teor + referências legais.*",
               "", "| % | n | matéria | Δ p.p. |", "|--:|--:|---|--:|"]
        for it in resumo["geral"].get("materias", []):
            md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} | {_delta_md(it['pct'], prev.get('materias'), it['rotulo'])} |")

        for d in resumo.get("destaques", []):
            pv = (prev.get("destaques") or {}).get(d["area"], {})
            md += ["", f"## Destaque — dentro de {d['area']} ({_milhar(d['total'])} acórdãos · {d['pct_pauta']}% da pauta)",
                   "", "| % | n | recorte (lido no acórdão) | Δ p.p. |", "|--:|--:|---|--:|"]
            for it in d["subtemas"]:
                md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} | {_delta_md(it['pct'], pv, it['rotulo'])} |")

        inc = resumo.get("incidentes_proc") or []
        if inc:
            md += ["", "## Pulso processual — incidentes (transversal)",
                   "", "*% dos acórdãos; um pode ter vários — não soma 100%. Lido no núcleo do litígio.*",
                   "", "| % | n | incidente | Δ p.p. |", "|--:|--:|---|--:|"]
            for it in inc:
                md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} | {_delta_md(it['pct'], prev.get('incidentes'), it['rotulo'])} |")

    md += ["", "## Panorama geral — por classe processual", "", "| % | n | classe | Δ p.p. |", "|--:|--:|---|--:|"]
    for it in resumo["geral"]["classes"]:
        md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} | {_delta_md(it['pct'], prev.get('classes'), it['rotulo'])} |")

    md += ["", "## Por Turma"]
    for t in resumo["orgaos"]:
        c = resumo["por_turma"][t]
        md += ["", f"### {t} — {_milhar(c['total'])} acórdãos"]
        if classificado and c.get("materias"):
            md += ["", "_matérias (incidência, % dos acórdãos da Turma):_",
                   "", "| % | n | matéria |", "|--:|--:|---|"]
            for it in c["materias"]:
                md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} |")
        md += ["", "_classes:_", "", "| % | n | classe |", "|--:|--:|---|"]
        for it in c["classes"]:
            md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} |")
        ci = c.get("incidentes") or []
        if classificado and ci:
            md += ["", "_incidentes processuais (% dos acórdãos da Turma):_",
                   "", "| % | n | incidente |", "|--:|--:|---|"]
            for it in ci[:12]:
                md.append(f"| {it['pct']:.1f} | {it['n']} | {it['rotulo']} |")

    rodape = ("_Fase 2: matéria (incidência) + classe + subtemas + incidentes._" if classificado
              else "_Fase 1: coleta + classe processual. Rode 'classificar' p/ a matéria (Fase 2)._")
    md += ["", rodape]
    return "\n".join(md)


def _cmd_agregar(args):
    rows = list(csv.DictReader(open(args.inp, encoding="utf-8")))
    tax = None
    try:
        tax = carregar_taxonomia(getattr(args, "taxonomia", None))
    except Exception as e:
        print(f"  [aviso] taxonomia não carregada ({e}) — destaques por subtema ficam vazios",
              file=sys.stderr)
    janela = {"inicio": args.inicio, "fim": args.fim, "criterio": "data_juntada"}
    base_dir = args.base_dir or os.path.dirname(os.path.abspath(args.saida_dir))
    resumo, prev = agregar(rows, args.rotulo, janela, base_dir=base_dir,
                           gerado_em=args.gerado_em, tax=tax)
    os.makedirs(args.saida_dir, exist_ok=True)
    json.dump(resumo, open(os.path.join(args.saida_dir, "resumo.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    open(os.path.join(args.saida_dir, "resumo.md"), "w", encoding="utf-8").write(montar_md(resumo, prev))
    open(os.path.join(args.saida_dir, "slack.txt"), "w", encoding="utf-8").write(montar_slack(resumo, prev))
    print(f"OK: resumo.json / resumo.md / slack.txt -> {args.saida_dir}", file=sys.stderr)
    fase = "Fase 2 (matéria+classe)" if resumo.get("classificado") else "Fase 1 (só classe)"
    print(f"   {fase} | total={resumo['total']} | turmas={len(resumo['orgaos'])}"
          + (f" | tendência vs {resumo['tendencia_vs']}" if resumo.get('tendencia_vs') else ""), file=sys.stderr)


# ----------------------------------------------------------------------------- #
# Notificar Slack
# ----------------------------------------------------------------------------- #
def _cmd_notificar(args):
    caminho = args.slack_txt or os.path.join(args.saida_dir, "slack.txt")
    texto = open(caminho, encoding="utf-8").read()
    cfg = {}
    if args.config and os.path.isfile(args.config):
        cfg = json.load(open(args.config, encoding="utf-8"))
    webhook = args.webhook or os.environ.get("SLACK_WEBHOOK_TRT19") or cfg.get("slack_webhook")
    canal = args.canal or cfg.get("slack_canal")
    if not webhook:
        print("ERRO: webhook não informado (use --webhook, $SLACK_WEBHOOK_TRT19 ou --config)", file=sys.stderr)
        sys.exit(2)
    corpo = {"text": texto, "unfurl_links": False,
             "username": cfg.get("slack_username", "Monitor TRT-19"),
             "icon_emoji": cfg.get("slack_icon", ":balance_scale:")}
    if canal:
        corpo["channel"] = canal
    payload = json.dumps(corpo).encode("utf-8")
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        print(f"Slack OK: {r.status} {r.read().decode()}", file=sys.stderr)
    except urllib.error.HTTPError as e:
        print(f"Slack ERRO {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


# ----------------------------------------------------------------------------- #
# Drill-down
# ----------------------------------------------------------------------------- #
def _cmd_filtrar(args):
    rows = list(csv.DictReader(open(args.inp, encoding="utf-8")))

    def casa(r):
        ok = True
        if args.turma:
            ok = ok and args.turma.lower() in (r.get("turma", "") + " " + r.get("orgao_julgador", "")).lower()
        if args.classe:
            ok = ok and args.classe.lower() in (r.get("classe", "") + " " + r.get("classe_sigla", "")
                                                + " " + r.get("classe_curta", "")).lower()
        if getattr(args, "materia", None):
            ok = ok and args.materia.lower() in (r.get("materias", "") + " " + r.get("area", "")).lower()
        if getattr(args, "subtema", None):
            ok = ok and args.subtema.lower() in (r.get("subtema", "")).lower()
        if getattr(args, "incidente", None):
            ok = ok and args.incidente.lower() in (r.get("incidentes", "")).lower()
        if args.relator:
            ok = ok and args.relator.lower() in (r.get("relator", "")).lower()
        if args.texto:
            ok = ok and args.texto.lower() in (r.get("ementa", "") + " " + r.get("inteiro_teor", "")).lower()
        return ok

    sel = [r for r in rows if casa(r)]
    campos_out = CAMPOS_CLASS if (rows and "materias" in rows[0]) else CAMPOS
    if args.formato == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=campos_out)
        w.writeheader()
        for r in sel:
            w.writerow({k: r.get(k, "") for k in campos_out})
    else:
        print(f"{len(sel)} acórdão(s):\n")
        for r in sel:
            print(f"- {r['numero']}  [{r.get('classe_curta') or r.get('classe_sigla') or r.get('classe')}] {r.get('turma')}")
            if r.get("materias"):
                print(f"    matéria(s): {r['materias'].replace('|', ', ')}"
                      + (f"  · {r['subtema']}" if r.get("subtema") else ""))
            if r.get("incidentes"):
                print(f"    incidentes: {r['incidentes'].replace('|', ', ')}")
            print(f"    Rel. {r.get('relator', '')} · julg. {r.get('data_julgamento', '')} · junt. {r.get('data_juntada', '')}")
            print(f"    {r.get('url', '')}")
            if getattr(args, "com_ementa", False):
                em = " ".join(_texto_ementa(r).split())
                if em:
                    lim = getattr(args, "max_ementa", 1200)
                    print(f"    ementa: {em[:lim]}{'…' if len(em) > lim else ''}")
    print(f"\n({len(sel)} de {len(rows)})", file=sys.stderr)


def _cmd_publicar(args):
    """Copia o registro datado p/ o clone local do repo (registros/<rotulo>/) — o que o
    Claude da nuvem (Slack @Claude / routine) lê para o drill-down. Com --push, commita+envia."""
    import shutil
    destino = os.path.join(args.repo_dir, "registros", args.rotulo)
    os.makedirs(destino, exist_ok=True)
    copiados = []
    # classificado.csv -> versão ENXUTA (sem 'inteiro_teor', que é ~90% do peso) p/ o repo
    # ficar leve. A coluna 'ementa' (populada no 'classificar') basta p/ o drill-down e p/ a
    # busca por --texto; o link leva ao inteiro teor quando preciso.
    src_csv = os.path.join(args.saida_dir, "classificado.csv")
    if os.path.exists(src_csv):
        campos = [c for c in CAMPOS_CLASS if c != "inteiro_teor"]
        with open(src_csv, encoding="utf-8") as fin, \
             open(os.path.join(destino, "classificado.csv"), "w", newline="", encoding="utf-8") as fout:
            w = csv.DictWriter(fout, fieldnames=campos)
            w.writeheader()
            for r in csv.DictReader(fin):
                w.writerow({k: r.get(k, "") for k in campos})
        copiados.append("classificado.csv (enxuto)")
    for nome in ("resumo.json", "resumo.md", "slack.txt"):
        src = os.path.join(args.saida_dir, nome)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(destino, nome))
            copiados.append(nome)
    print(f"OK: {len(copiados)} arquivo(s) -> {destino} ({', '.join(copiados)})", file=sys.stderr)
    if not args.push:
        return
    import subprocess

    def git(*a):
        return subprocess.run(["git", "-C", args.repo_dir, *a], capture_output=True, text=True)

    git("add", os.path.join("registros", args.rotulo))
    r = git("commit", "-m", f"registros: rodada {args.rotulo}")
    saida = r.stdout + r.stderr
    if r.returncode != 0 and "nothing to commit" in saida:
        print("git: nada a commitar (registro já publicado)", file=sys.stderr)
        return
    if r.returncode != 0:
        print(f"git commit falhou: {saida.strip()}", file=sys.stderr)
        return
    rp = git("push")
    if rp.returncode != 0:
        print(f"git push falhou: {(rp.stdout + rp.stderr).strip()}", file=sys.stderr)
    else:
        print(f"git: publicado e enviado ({args.rotulo})", file=sys.stderr)


# ----------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Monitor Turmas Trabalhistas TRT-19 (FALCÃO)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("coletar", help="Busca acórdãos das Turmas no FALCÃO e grava CSV bruto")
    c.add_argument("--dias", type=int, help="Janela móvel: últimos N dias (por data de juntada)")
    c.add_argument("--inicio", help="Data início ISO YYYY-MM-DD (com --fim)")
    c.add_argument("--fim", help="Data fim ISO YYYY-MM-DD (com --inicio)")
    c.add_argument("--orgaos", help="CSV de Turmas (ex.: '1ª Turma,2ª Turma' ou 'Primeira Turma')")
    c.add_argument("--com-pleno", action="store_true", help="Incluir Tribunal Pleno")
    c.add_argument("--texto", help="Termo livre opcional (default: vazio = tudo da janela)")
    c.add_argument("--pausa", type=float, default=0.8, help="Pausa entre páginas (s)")
    c.add_argument("--out", required=True, help="CSV de saída")
    c.set_defaults(func=_cmd_coletar)

    k = sub.add_parser("classificar", help="Lê matéria/subtema/incidentes do texto (determinístico)")
    k.add_argument("--inp", required=True, help="CSV bruto da coleta")
    k.add_argument("--out", required=True, help="CSV classificado de saída")
    k.add_argument("--taxonomia", help="Caminho do taxonomia.json (default: ao lado do script)")
    k.set_defaults(func=_cmd_classificar)

    a = sub.add_parser("agregar", help="Matéria (incidência) + classe + subtemas + incidentes + tendência + Slack")
    a.add_argument("--inp", required=True, help="CSV (classificado p/ Fase 2; bruto cai p/ Fase 1)")
    a.add_argument("--saida-dir", required=True, help="Pasta datada AAAA-MM-DD onde gravar resumo/slack")
    a.add_argument("--base-dir", help="Pasta-mãe p/ tendência (default: pai de --saida-dir)")
    a.add_argument("--rotulo", required=True, help="Rótulo da rodada (AAAA-MM-DD)")
    a.add_argument("--inicio", required=True, help="Janela início (rótulo legível)")
    a.add_argument("--fim", required=True, help="Janela fim (rótulo legível)")
    a.add_argument("--gerado-em", help="Carimbo de geração (default: agora)")
    a.add_argument("--taxonomia", help="Caminho do taxonomia.json (p/ os destaques por subtema)")
    a.set_defaults(func=_cmd_agregar)

    n = sub.add_parser("notificar", help="Posta o slack.txt no canal via webhook")
    n.add_argument("--saida-dir", help="Pasta datada (lê slack.txt dela)")
    n.add_argument("--slack-txt", help="Caminho direto do slack.txt")
    n.add_argument("--webhook", help="URL do Incoming Webhook (ou $SLACK_WEBHOOK_TRT19 / --config)")
    n.add_argument("--config", help="config.local.json (lê slack_webhook/slack_canal)")
    n.add_argument("--canal", default="#trt19-turmas", help="Canal de destino")
    n.set_defaults(func=_cmd_notificar)

    f = sub.add_parser("filtrar", help="Drill-down: lista acórdãos por Turma/classe/matéria/incidente/termo")
    f.add_argument("--inp", required=True, help="CSV (bruto ou classificado)")
    f.add_argument("--turma", help="Filtra por Turma (substring, ex.: '1ª' / 'Segunda')")
    f.add_argument("--classe", help="Filtra por classe (substring, ex.: 'Agravo de Petição' / 'AP')")
    f.add_argument("--materia", help="Filtra por matéria (substring, ex.: 'Horas extras', 'Adicionais')")
    f.add_argument("--subtema", help="Filtra por subtema lido no acórdão (ex.: 'Periculosidade')")
    f.add_argument("--incidente", help="Filtra por incidente processual (ex.: 'Gratuidade', 'Prescrição')")
    f.add_argument("--relator", help="Filtra por relator (substring)")
    f.add_argument("--texto", help="Filtra por termo na ementa/inteiro teor (substring)")
    f.add_argument("--com-ementa", action="store_true", help="Inclui o texto da ementa (extraída do inteiro teor) na saída lista")
    f.add_argument("--max-ementa", type=int, default=1200, help="Máx. de caracteres da ementa impressa (com --com-ementa)")
    f.add_argument("--formato", choices=["lista", "csv"], default="lista")
    f.set_defaults(func=_cmd_filtrar)

    p = sub.add_parser("publicar", help="Copia o registro datado p/ o repo (registros/<rotulo>/) e, com --push, commita+envia")
    p.add_argument("--saida-dir", required=True, help="Pasta datada de origem (AAAA-MM-DD)")
    p.add_argument("--rotulo", required=True, help="Rótulo da rodada (AAAA-MM-DD)")
    p.add_argument("--repo-dir", required=True, help="Caminho do clone local do repo monitor-trt19-trabalhista")
    p.add_argument("--push", action="store_true", help="git add+commit+push do registro publicado")
    p.set_defaults(func=_cmd_publicar)

    args = ap.parse_args()
    if args.cmd == "coletar" and not args.dias and not (args.inicio and args.fim):
        ap.error("use --dias OU (--inicio e --fim)")
    args.func(args)


if __name__ == "__main__":
    main()
