"""
API Vercel — AJUSTE DE TROCAS (debito de quem devolveu, credito de quem vendeu com o vale).

Esta função:
1. Loga no Moombox/LivePDV usando credenciais das env vars
2. Baixa o relatório-tipo-pagamento (vendas com cód autorização)
3. Baixa o financeiro Zoop (transações succeeded)
4. Cruza os dados por loja + cód autorização + valor
5. Retorna SÓ os problemas (não retorna o que conciliou ok)

Variáveis de ambiente esperadas (Vercel → Settings → Environment Variables):
- LIVEPDV_USUARIO
- LIVEPDV_SENHA
- LIVEPDV_BASE_URL (default: https://expositores.moombox.com.br)
"""

import os
import json
import re
import hashlib
import traceback
import sys
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse as _urlparse

import requests
from bs4 import BeautifulSoup


def log(msg):
    """Imprime no stderr — aparece nos logs do Vercel."""
    print(f"[CONFERIR] {msg}", file=sys.stderr, flush=True)


_raw_base = os.environ.get("LIVEPDV_BASE_URL", "https://expositores.moombox.com.br").strip()
if "://" not in _raw_base:
    _raw_base = "https://" + _raw_base
_parsed_base = _urlparse(_raw_base)
# Usa só esquema+host — ignora qualquer path (ex.: env var com /user/login no final)
BASE_URL = f"{_parsed_base.scheme}://{_parsed_base.netloc}"
USUARIO = os.environ.get("LIVEPDV_USUARIO", "").strip()
SENHA = os.environ.get("LIVEPDV_SENHA", "").strip()

LOJAS_VALIDAS = [1, 3, 4]  # 1=RS, 3=BS, 4=NS
TOLERANCIA_VALOR = 2.00  # tolerância de R$ 2,00 — diferenças menores são consideradas OK


# ==================== CLIENTE MOOMBOX ====================

class MoomboxClient:
    """Cliente que loga no Moombox e baixa relatórios."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) DashConferencia/1.0"
        })
        self.avisos = []

    def login(self):
        """Faz login no LivePDV. Retorna True se ok."""
        login_url = f"{BASE_URL}/user/login"
        log(f"GET {login_url}")

        # GET pra pegar CSRF token
        r = self.session.get(login_url, timeout=15)
        log(f"GET resposta: status={r.status_code}, url_final={r.url}")
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        csrf_input = soup.find("input", {"name": "_csrf"})
        if not csrf_input:
            # Mostra os primeiros 500 chars pra debug
            preview = r.text[:500].replace("\n", " ")
            raise RuntimeError(f"Campo _csrf não encontrado. Preview HTML: {preview}")
        csrf = csrf_input.get("value", "")
        log(f"CSRF token capturado: {csrf[:20]}...")

        # POST com credenciais — nomes reais do form
        payload = {
            "_csrf": csrf,
            "login-form[login]": USUARIO,
            "login-form[password]": SENHA,
            "login-form[rememberMe]": "0",
        }
        log(f"POST {login_url} com usuario={USUARIO!r}")
        r = self.session.post(login_url, data=payload, timeout=15, allow_redirects=True)
        log(f"POST resposta: status={r.status_code}, url_final={r.url}")

        # Coleta info pra diagnóstico
        url_final = r.url
        body_lower = r.text.lower()
        tem_login_form = 'name="loginform' in body_lower or "loginform[username]" in body_lower
        tem_logout = "logout" in body_lower or "sair" in body_lower
        tem_erro_senha = "incorret" in body_lower or "inválid" in body_lower or "invalid" in body_lower
        preview = r.text[:300].replace("\n", " ")

        log(f"Análise: url_final={url_final}, tem_form_login={tem_login_form}, "
            f"tem_logout={tem_logout}, tem_erro_senha={tem_erro_senha}")

        # Se ainda mostra o formulário de login OU se URL ainda é /login, falhou
        if "/user/login" in url_final or (tem_login_form and not tem_logout):
            raise RuntimeError(
                f"Login falhou. status={r.status_code}, url_final={url_final}, "
                f"tem_form_login={tem_login_form}, tem_logout={tem_logout}, "
                f"erro_senha_msg={tem_erro_senha}. Preview: {preview}"
            )

        log("LOGIN OK")
        return True

    def _baixar_grid(self, url, params, toggle, parser, nome):
        """
        Baixa um GridView do Moombox de forma resiliente.
        1) tenta "Ver todos" (toggle=all) 2x;
        2) se o Moombox der 5xx, cai para paginacao normal;
        3) pagina que der 5xx e reprocessada linha a linha (per-page=1),
           pulando so a(s) linha(s) que quebram o Moombox (vira aviso).
        """
        import time
        ultimo = None
        for tentativa in range(2):
            try:
                r = self.session.get(url, params={**params, toggle: "all"}, timeout=30)
                log(f"GET {nome} (all) status={r.status_code}")
                if r.status_code < 500:
                    r.raise_for_status()
                    return parser(r.text)
                ultimo = r.status_code
            except requests.exceptions.Timeout:
                ultimo = "timeout"
            time.sleep(1)
        self.avisos.append(f"{nome}: Moombox deu erro ({ultimo}) no 'ver todos'; baixado pagina a pagina.")

        t0 = time.time()
        def pegar(page, per_page=None):
            q = {**params, "page": page}
            if per_page:
                q["per-page"] = per_page
            r = self.session.get(url, params=q, timeout=30)
            return r

        r = pegar(1)
        log(f"GET {nome} page=1 status={r.status_code}")
        tamanho = 20
        if r.status_code >= 500:
            # ate a 1a pagina quebra: tenta linha a linha direto
            html1 = None
        else:
            r.raise_for_status()
            html1 = r.text
        total_pags = 1
        if html1:
            soup = BeautifulSoup(html1, "html.parser")
            pags = [int(a.get("data-page")) for a in soup.select("ul.pagination a[data-page]") if str(a.get("data-page")).isdigit()]
            total_pags = (max(pags) + 1) if pags else 1
            m = re.search(r"(\d+)\s*-\s*(\d+)\D+?(\d+)", (soup.select_one(".summary").get_text(" ") if soup.select_one(".summary") else ""))
            if m:
                tamanho = int(m.group(2)) - int(m.group(1)) + 1 or 20
                total_itens = int(m.group(3))
                total_pags = max(total_pags, -(-total_itens // max(tamanho, 1)))
        else:
            total_pags = 50  # desconhecido: vai ate esvaziar/repetir

        itens, vistos = [], set()
        def add(lista):
            novos = 0
            for it in lista:
                k = json.dumps(it, sort_keys=True, default=str)
                if k not in vistos:
                    vistos.add(k); itens.append(it); novos += 1
            return novos

        for page in range(1, total_pags + 1):
            if time.time() - t0 > 45:
                self.avisos.append(f"{nome}: tempo esgotado na pagina {page}; resultado parcial.")
                break
            if page == 1 and html1 is not None:
                add(parser(html1)); continue
            r = pegar(page) if page > 1 or html1 is None else None
            if r is not None and r.status_code < 500:
                r.raise_for_status()
                if add(parser(r.text)) == 0 and page > 1:
                    break  # Yii repete a ultima pagina -> acabou
                continue
            # pagina quebrada: linha a linha
            base = (page - 1) * tamanho
            ruins = []
            for i in range(1, tamanho + 1):
                if time.time() - t0 > 45:
                    break
                rr = pegar(base + i, 1)
                if rr.status_code >= 500:
                    ruins.append(base + i); continue
                rr.raise_for_status()
                if add(parser(rr.text)) == 0:
                    break
            if ruins:
                self.avisos.append(f"{nome}: o Moombox nao abre a(s) linha(s) {', '.join(map(str, ruins))} do relatorio (erro interno deles) — essas vendas NAO foram conferidas.")
        if not itens and ultimo:
            raise RuntimeError(f"{nome}: Moombox devolveu erro {ultimo} e nenhuma pagina pode ser lida.")
        return itens

    def buscar_relatorio_pagamento(self, data_ref: date):
        """Baixa o relatorio-tipo-pagamento do dia (resiliente a 500 do Moombox)."""
        url = f"{BASE_URL}/relatorios/relatorio-tipo-pagamento/index"
        data_str = data_ref.strftime("%d/%m/%Y")
        params = {"RelatorioTipoPagamentoForm[data]": f"{data_str} - {data_str}"}
        return self._baixar_grid(url, params, "_togd9a55727", _parse_relatorio_pagamento, "Relatorio PDV")

    def buscar_zoop_financeiro(self, data_ref: date):
        """Baixa o financeiro Zoop do dia (resiliente a 500 do Moombox)."""
        url = f"{BASE_URL}/zoop/financeiro/index"
        data_str = data_ref.strftime("%d/%m/%Y")
        params = {"TransacaoPosSearch[data]": f"{data_str} - {data_str}"}
        return self._baixar_grid(url, params, "_tog1149016d", _parse_zoop_financeiro, "Zoop financeiro")



def _parse_money(text: str) -> float:
    t = (text or "").replace("R$", "").replace(".", "").replace(",", ".").strip()
    try:
        return float(t) if t else 0.0
    except ValueError:
        return 0.0


# ==================== TROCAS ====================
# Venda paga com "Troca" (cupom +) consome uma devolucao (cupom -).
# Quem devolveu perde -> DEBITO; quem vendeu com o vale recebe -> CREDITO.
# Valor = parte da troca de cada marca (coluna "Valor pago") x LIQUIDO.

LIQUIDO = float(os.environ.get("TROCA_PCT_LIQUIDO", "0.77"))  # 1 - 23% comissao
TOL = 0.05          # diferenca de centavos entre devolucao e venda
JANELA_DIAS = 30    # devolucao pode ter sido feita ate 30 dias antes


def _num(cell):
    raw = cell.get("raw")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _parse_money(cell.get("text", "0") or "0")


def _parse_trocas(html: str):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_=lambda c: c and "kv-grid-table" in c) or soup.find("table")
    if not table:
        return []
    out = []
    for tr in (table.find("tbody") or table).find_all("tr"):
        cells = {}
        for td in tr.find_all("td"):
            seq = td.get("data-col-seq")
            if seq is not None:
                cells[seq] = {"raw": td.get("data-raw-value"), "text": td.get_text(" ", strip=True)}
        cupom = cells.get("3", {}).get("text", "")
        if not cupom.isdigit():
            continue
        if "troca" not in cells.get("8", {}).get("text", "").lower():
            continue
        m = re.match(r"^(\d+)\s*-\s*(.+)$", cells.get("4", {}).get("text", ""))
        e = re.match(r"^(\d+)\s*-\s*(.+)$", cells.get("5", {}).get("text", ""))
        if not m or not e:
            continue
        data_raw = cells.get("1", {}).get("raw") or cells.get("1", {}).get("text", "")
        dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", data_raw) or re.search(r"(\d{2})/(\d{2})/(\d{4})", data_raw)
        if dm and "/" in dm.group(0):
            data_iso = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        else:
            data_iso = dm.group(0) if dm else ""
        out.append({
            "data": data_iso,
            "cupom": cupom,
            "loja_id": int(m.group(1)),
            "loja_nome": m.group(2).strip(),
            "expositor_id": int(e.group(1)),
            "expositor": e.group(2).strip(),
            "total_cupom": _num(cells.get("10", {})),
            "valor": _num(cells.get("11", {})),
        })
    return out


def _r2(x):
    return round(x + 1e-9, 2)


def montar_trocas(linhas, dia_iso):
    """Pareia devolucoes x vendas-com-troca por loja e gera os lancamentos do dia."""
    cupons = {}
    for l in linhas:
        k = (l["loja_id"], l["cupom"])
        c = cupons.setdefault(k, {"loja_id": l["loja_id"], "loja_nome": l["loja_nome"], "cupom": l["cupom"],
                                  "data": l["data"], "marcas": {}, "total": 0.0})
        mk = c["marcas"].setdefault(l["expositor_id"], {"expositor_id": l["expositor_id"], "expositor": l["expositor"], "valor": 0.0})
        mk["valor"] += l["valor"]
        c["total"] += l["valor"]
    for c in cupons.values():
        c["total"] = _r2(c["total"])
        c["marcas"] = [dict(m, valor=_r2(m["valor"])) for m in c["marcas"].values() if abs(m["valor"]) >= 0.005]

    devol = sorted([c for c in cupons.values() if c["total"] < 0], key=lambda c: (c["data"], c["cupom"]))
    vendas = sorted([c for c in cupons.values() if c["total"] > 0], key=lambda c: (c["data"], c["cupom"]))
    usados = set()
    pares, sem_devolucao = [], []

    for v in vendas:
        cands = [d for d in devol if d["loja_id"] == v["loja_id"] and (d["loja_id"], d["cupom"]) not in usados
                 and abs(-d["total"] - v["total"]) <= TOL and (d["data"], d["cupom"]) <= (v["data"], v["cupom"])]
        if not cands:
            # combinacao de 2 devolucoes que somam o valor da venda
            livres = [d for d in devol if d["loja_id"] == v["loja_id"] and (d["loja_id"], d["cupom"]) not in usados
                      and (d["data"], d["cupom"]) <= (v["data"], v["cupom"])]
            combo = None
            for i in range(len(livres)):
                for j in range(i + 1, len(livres)):
                    if abs(-(livres[i]["total"] + livres[j]["total"]) - v["total"]) <= TOL:
                        combo = [livres[i], livres[j]]
            if combo:
                cands_sel = combo
            else:
                if v["data"] == dia_iso:
                    sem_devolucao.append(v)
                continue
        else:
            # a devolucao mais proxima (ultima antes da venda)
            cands_sel = [cands[-1]]
        for d in cands_sel:
            usados.add((d["loja_id"], d["cupom"]))
        if v["data"] == dia_iso:
            pares.append((v, cands_sel, len(cands) > 1))

    lojas = {}
    for v, ds, ambiguo in pares:
        saldo = {}
        nomes = {}
        for d in ds:
            for m in d["marcas"]:
                saldo[m["expositor_id"]] = saldo.get(m["expositor_id"], 0.0) + m["valor"]
                nomes[m["expositor_id"]] = m["expositor"]
        for m in v["marcas"]:
            saldo[m["expositor_id"]] = saldo.get(m["expositor_id"], 0.0) + m["valor"]
            nomes[m["expositor_id"]] = m["expositor"]
        lanc = []
        for eid, bruto in saldo.items():
            bruto = _r2(bruto)
            if abs(bruto) < 0.01:
                continue
            liq = _r2(abs(bruto) * LIQUIDO)
            tipo = "credito" if bruto > 0 else "debito"
            lid = hashlib.md5(f"troca|{v['loja_id']}|{v['cupom']}|{eid}|{tipo}".encode()).hexdigest()[:12]
            lanc.append({"id": lid, "tipo": tipo, "expositor_id": eid, "expositor": nomes[eid],
                         "bruto": abs(bruto), "valor": liq,
                         "cupom": v["cupom"] if tipo == "credito" else ds[0]["cupom"],
                         "observacao": f"Ajuste troca: devolucao {'+'.join(d['cupom'] for d in ds)} -> venda {v['cupom']}"})
        lanc.sort(key=lambda x: (x["tipo"] != "debito", -x["valor"]))
        L = lojas.setdefault(v["loja_id"], {"loja_id": v["loja_id"], "nome": v["loja_nome"], "trocas": [], "alertas": []})
        L["trocas"].append({
            "id": hashlib.md5(f"troca|{v['loja_id']}|{v['cupom']}".encode()).hexdigest()[:12],
            "venda": v, "devolucoes": ds, "ambiguo": ambiguo,
            "mesma_marca": not lanc, "lancamentos": lanc,
            "diferenca": _r2(v["total"] + sum(d["total"] for d in ds)),
        })
    for v in sem_devolucao:
        L = lojas.setdefault(v["loja_id"], {"loja_id": v["loja_id"], "nome": v["loja_nome"], "trocas": [], "alertas": []})
        L["alertas"].append({"tipo": "sem_devolucao", "venda": v,
                             "motivo": f"Venda paga com troca sem devolucao de {v['total']:.2f} nos ultimos {JANELA_DIAS} dias"})
    # devolucoes do dia que ninguem usou ainda: so informativo (cliente pode usar o vale depois)
    for d in devol:
        if d["data"] == dia_iso and (d["loja_id"], d["cupom"]) not in usados:
            L = lojas.setdefault(d["loja_id"], {"loja_id": d["loja_id"], "nome": d["loja_nome"], "trocas": [], "alertas": []})
            L["alertas"].append({"tipo": "vale_aberto", "venda": d,
                                 "motivo": "Devolucao sem venda paga com troca ainda (vale em aberto) — nada a lancar por enquanto"})
    # conferencia do dia: soma de tudo que foi pago/devolvido em troca no dia deve ser 0 por loja
    for l in linhas:
        if l["data"] == dia_iso:
            L = lojas.setdefault(l["loja_id"], {"loja_id": l["loja_id"], "nome": l["loja_nome"], "trocas": [], "alertas": []})
            L["total_dia"] = L.get("total_dia", 0.0) + l["valor"]
    for L in lojas.values():
        L["total_dia"] = _r2(L.get("total_dia", 0.0))
        L["fecha_zero"] = abs(L["total_dia"]) <= TOL
    return sorted(lojas.values(), key=lambda l: l["loja_id"])


# ==================== HANDLER VERCEL ====================

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        try:
            if not USUARIO or not SENHA:
                return self._json(500, {"error": "Credenciais nao configuradas"})
            from urllib.parse import urlparse, parse_qs
            from datetime import timedelta
            qs = parse_qs(urlparse(self.path).query)
            try:
                dia = date.fromisoformat(qs["data"][0])
            except (KeyError, IndexError, ValueError):
                dia = (datetime.now() - timedelta(days=1)).date()
            ini = dia - timedelta(days=JANELA_DIAS)
            client = MoomboxClient()
            client.login()
            url = f"{BASE_URL}/relatorios/relatorio-tipo-pagamento/index"
            params = {
                "RelatorioTipoPagamentoForm[data]": f"{ini.strftime('%d/%m/%Y')} - {dia.strftime('%d/%m/%Y')}",
                "RelatorioTipoPagamentoForm[pagamento_nome]": "troca",
            }
            linhas = client._baixar_grid(url, params, "_togd9a55727", _parse_trocas, "Trocas")
            lojas = montar_trocas(linhas, dia.isoformat())
            return self._json(200, {
                "data_referencia": dia.isoformat(),
                "janela": [ini.isoformat(), dia.isoformat()],
                "pct_liquido": LIQUIDO,
                "linhas": len(linhas),
                "lojas": lojas,
                "total_dia": _r2(sum(l["total_dia"] for l in lojas)),
                "avisos": client.avisos,
            })
        except Exception as e:
            tb = traceback.format_exc()
            log(f"ERRO trocas: {type(e).__name__}: {e}\n{tb}")
            return self._json(500, {"error": str(e), "type": type(e).__name__})

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
