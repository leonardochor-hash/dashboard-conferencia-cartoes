"""
API Vercel — VENDAS x REPASSE por marca (venda bruta, liquida pela comissao do contrato, repasses Zoop).

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


# ==================== VENDAS x REPASSE ====================
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

COMISSAO_PADRAO = 23.0


def _norm(t):
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _num(cell):
    raw = cell.get("raw")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _parse_money(cell.get("text", "0") or "0")


def _cells(tr):
    c = {}
    for td in tr.find_all("td"):
        seq = td.get("data-col-seq")
        if seq is not None:
            c[seq] = {"raw": td.get("data-raw-value"), "text": td.get_text(" ", strip=True)}
    return c


def _parse_vendas(html):
    """relatorio-tipo-pagamento: TODAS as formas (inclui troca/devolucao negativa)."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_=lambda c: c and "kv-grid-table" in c) or soup.find("table")
    out = []
    if not table:
        return out
    for tr in (table.find("tbody") or table).find_all("tr"):
        c = _cells(tr)
        cupom = c.get("3", {}).get("text", "")
        if not cupom.isdigit():
            continue
        m = re.match(r"^(\d+)\s*-\s*(.+)$", c.get("4", {}).get("text", ""))
        e = re.match(r"^(\d+)\s*-\s*(.+)$", c.get("5", {}).get("text", ""))
        if not m or not e:
            continue
        draw = c.get("1", {}).get("raw") or c.get("1", {}).get("text", "")
        dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", draw)
        if not dm:
            dm2 = re.search(r"(\d{2})/(\d{2})/(\d{4})", draw)
            data = f"{dm2.group(3)}-{dm2.group(2)}-{dm2.group(1)}" if dm2 else ""
        else:
            data = dm.group(0)
        out.append({"cupom": cupom, "data": data, "loja_id": int(m.group(1)), "loja": m.group(2).strip(),
                     "expositor_id": int(e.group(1)), "expositor": e.group(2).strip(),
                     "pagamento": c.get("8", {}).get("text", ""), "valor": _num(c.get("11", {}))})
    return out


def _parse_repasses(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table tbody tr[data-key]"):
        c = _cells(tr)
        try:
            v = float(str(c.get("4", {}).get("text", "")).replace(",", ""))
        except ValueError:
            continue
        out.append({"zoop_id": c.get("2", {}).get("text", ""), "expositor": c.get("3", {}).get("text", ""),
                    "valor": v, "tipo": c.get("5", {}).get("text", ""), "status": c.get("6", {}).get("text", "").lower(),
                    "data": c.get("7", {}).get("text", "")})
    return out


def _parse_cadastros(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table tbody tr[data-key]"):
        c = _cells(tr)
        cid = c.get("1", {}).get("text", "")
        if not cid.isdigit():
            continue
        try:
            com = float(c.get("10", {}).get("text", "").replace(",", "."))
        except ValueError:
            com = None
        out.append({"cadastro_id": int(cid), "marca": c.get("4", {}).get("text", ""), "razao": c.get("3", {}).get("text", ""),
                    "status": c.get("9", {}).get("text", ""), "comissao": com})
    return out


def _parse_adendo(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table tbody tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 6 or not tds[0].isdigit():
            continue
        def d(s):
            m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s)
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""
        try:
            com = float(tds[3].replace(",", "."))
        except ValueError:
            continue
        out.append({"inicio": d(tds[1]), "fim": d(tds[2]) or "9999-12-31", "comissao": com,
                    "loja_id": int(tds[4]) if tds[4].isdigit() else None, "ativo": tds[5].strip().lower().startswith("s")})
    return out


def _fmt(d):
    return d.strftime("%d/%m/%Y")


def comissao_de(adendos, base, loja_id, data):
    for a in adendos:
        if a["ativo"] and a["loja_id"] == loja_id and a["inicio"] <= data <= a["fim"]:
            return a["comissao"], "contrato da loja"
    if base is not None:
        return base, "cadastro (sem adendo da loja na data)"
    return COMISSAO_PADRAO, "padrão 23% (sem cadastro)"


def _casar(nome, por_nome):
    n = _norm(nome)
    if n in por_nome:
        return por_nome[n]
    cands = [k for k in por_nome if k and (n.startswith(k + " ") or k.startswith(n + " "))]
    if len(cands) == 1:
        return por_nome[cands[0]]
    return None


def montar(vendas, repasses, cadastros, adendos_por_cad):
    cad_por_nome = {}
    for c in cadastros:
        cad_por_nome.setdefault(_norm(c["marca"]), c)

    marcas = {}
    for v in vendas:
        M = marcas.setdefault(v["expositor_id"], {"expositor_id": v["expositor_id"], "marca": v["expositor"], "lojas": set(),
                                                  "bruta": 0.0, "liquida": 0.0, "comissoes": set(), "origem_comissao": set()})
        cad = _casar(v["expositor"], cad_por_nome)
        ad = adendos_por_cad.get(cad["cadastro_id"], []) if cad else []
        pct, origem = comissao_de(ad, cad["comissao"] if cad else None, v["loja_id"], v["data"])
        M["lojas"].add(v["loja_id"])
        M["bruta"] += v["valor"]
        M["liquida"] += v["valor"] * (1 - pct / 100.0)
        M["comissoes"].add(pct)
        M["origem_comissao"].add(origem)
        M["cadastro_id"] = cad["cadastro_id"] if cad else None

    rep_por_nome = {}
    for r in repasses:
        R = rep_por_nome.setdefault(_norm(r["expositor"]), {"nome": r["expositor"], "pago": 0.0, "pendente": 0.0, "n": 0})
        if r["status"] == "succeeded":
            R["pago"] += r["valor"]
        elif r["status"] in ("pending", "processing", "scheduled"):
            R["pendente"] += r["valor"]
        R["n"] += 1

    usados = set()
    linhas = []
    for M in marcas.values():
        alvo = _norm(M["marca"])
        chave = alvo if alvo in rep_por_nome else None
        if not chave:
            cands = [k for k in rep_por_nome if k and (alvo.startswith(k + " ") or k.startswith(alvo + " "))]
            chave = cands[0] if len(cands) == 1 else None
        R = rep_por_nome.get(chave) if chave else None
        if chave:
            usados.add(chave)
        bruta, liq = round(M["bruta"], 2), round(M["liquida"], 2)
        pago = round(R["pago"], 2) if R else 0.0
        linhas.append({
            "expositor_id": M["expositor_id"], "marca": M["marca"], "lojas": sorted(M["lojas"]),
            "bruta": bruta, "comissao": sorted(M["comissoes"]), "origem_comissao": sorted(M["origem_comissao"]),
            "liquida": liq, "repasse": pago, "repasse_pendente": round(R["pendente"], 2) if R else 0.0,
            "qtd_repasses": R["n"] if R else 0, "nome_zoop": R["nome"] if R else None,
            "diferenca": round(pago - liq, 2),
        })
    so_repasse = []
    for k, R in rep_por_nome.items():
        if k not in usados:
            so_repasse.append({"marca": R["nome"], "repasse": round(R["pago"], 2), "repasse_pendente": round(R["pendente"], 2),
                               "qtd_repasses": R["n"]})
    linhas.sort(key=lambda x: -abs(x["diferenca"]))
    so_repasse.sort(key=lambda x: -x["repasse"])
    return linhas, so_repasse


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if not USUARIO or not SENHA:
                return self._json(500, {"error": "Credenciais nao configuradas"})
            from urllib.parse import urlparse, parse_qs
            qs = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            vi, vf = date.fromisoformat(qs["venda_ini"]), date.fromisoformat(qs["venda_fim"])
            ri, rf = date.fromisoformat(qs["repasse_ini"]), date.fromisoformat(qs["repasse_fim"])
            if (vf - vi).days > 62 or (rf - ri).days > 62:
                return self._json(400, {"error": "Periodo maximo de 62 dias por consulta"})
            client = MoomboxClient()
            client.login()
            vendas = client._baixar_grid(f"{BASE_URL}/relatorios/relatorio-tipo-pagamento/index",
                                         {"RelatorioTipoPagamentoForm[data]": f"{_fmt(vi)} - {_fmt(vf)}"},
                                         "_togd9a55727", _parse_vendas, "Vendas")
            repasses, vistos = [], set()
            for page in range(1, 200):
                r = client.session.get(f"{BASE_URL}/zoop/relatorio-transferencia-zoop",
                                       params={"ZoopTransferenciasSearch[data]": f"{_fmt(ri)} - {_fmt(rf)}", "page": page}, timeout=30)
                r.raise_for_status()
                novos = [x for x in _parse_repasses(r.text) if x["zoop_id"] not in vistos]
                if not novos:
                    break
                for x in novos:
                    vistos.add(x["zoop_id"])
                repasses += novos
            cadastros = client._baixar_grid(f"{BASE_URL}/boxstore/cadastro", {}, "_tog442d68b1", _parse_cadastros, "Cadastros")
            # adendos (comissao por loja) so das marcas que venderam no periodo
            cad_por_nome = {}
            for c in cadastros:
                cad_por_nome.setdefault(_norm(c["marca"]), c)
            ids = set()
            for nome in {v["expositor"] for v in vendas}:
                c = _casar(nome, cad_por_nome)
                if c:
                    ids.add(c["cadastro_id"])

            def pega(cid):
                try:
                    r = client.session.get(f"{BASE_URL}/boxstore/adendo-info", params={"id": cid}, timeout=20)
                    return cid, _parse_adendo(r.text) if r.status_code == 200 else []
                except Exception:
                    return cid, None
            with ThreadPoolExecutor(max_workers=12) as ex:
                res = list(ex.map(pega, sorted(ids)))
            adendos = {cid: (a or []) for cid, a in res}
            falhas = [cid for cid, a in res if a is None]
            if falhas:
                client.avisos.append(f"Nao consegui ler o contrato (comissao por loja) de {len(falhas)} marca(s); usei a comissao do cadastro.")

            linhas, so_repasse = montar(vendas, repasses, cadastros, adendos)
            sem_cad = [l["marca"] for l in linhas if any("sem cadastro" in o for o in l["origem_comissao"])]
            if sem_cad:
                client.avisos.append(f"{len(sem_cad)} marca(s) sem cadastro de box encontrado pelo nome — usei 23%: " + ", ".join(sem_cad[:15]))
            return self._json(200, {
                "venda": [vi.isoformat(), vf.isoformat()], "repasse": [ri.isoformat(), rf.isoformat()],
                "linhas_venda": len(vendas), "qtd_repasses": len(repasses),
                "marcas": linhas, "so_repasse": so_repasse, "avisos": client.avisos,
            })
        except Exception as e:
            log(f"ERRO repasse: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            return self._json(500, {"error": str(e), "type": type(e).__name__})

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
