"""
API Vercel — Conferência de cartões LivePDV ↔ Zoop

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
import hashlib
import traceback
import sys
from datetime import datetime, date
from http.server import BaseHTTPRequestHandler

import requests
from bs4 import BeautifulSoup


def log(msg):
    """Imprime no stderr — aparece nos logs do Vercel."""
    print(f"[CONFERIR] {msg}", file=sys.stderr, flush=True)


BASE_URL = os.environ.get("LIVEPDV_BASE_URL", "https://expositores.moombox.com.br").rstrip("/")
USUARIO = os.environ.get("LIVEPDV_USUARIO", "").strip()
SENHA = os.environ.get("LIVEPDV_SENHA", "").strip()

LOJAS_VALIDAS = [1, 3, 4]  # 1=RS, 3=BS, 4=NS
TOLERANCIA_VALOR = 0.02  # tolerância de R$ 0,02 para evitar problema de arredondamento


# ==================== CLIENTE MOOMBOX ====================

class MoomboxClient:
    """Cliente que loga no Moombox e baixa relatórios."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) DashConferencia/1.0"
        })

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

    def buscar_relatorio_pagamento(self, data_ref: date):
        """
        Baixa o relatório-tipo-pagamento do dia.
        Retorna lista de dicts: {loja_id, cupom, hora, cod_aut, valor, forma_pagto}
        """
        url = f"{BASE_URL}/relatorios/relatorio-tipo-pagamento"
        params = {
            "data_inicio": data_ref.strftime("%d/%m/%Y"),
            "data_fim": data_ref.strftime("%d/%m/%Y"),
        }
        r = self.session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return _parse_relatorio_pagamento(r.text)

    def buscar_zoop_financeiro(self, data_ref: date):
        """
        Baixa o financeiro Zoop do dia (só status=succeeded).
        Retorna lista de dicts: {loja_id, zoop_id, valor, status, bandeira, tipo_pagamento, hora}
        """
        url = f"{BASE_URL}/zoop/financeiro"
        params = {
            "data_inicio": data_ref.strftime("%d/%m/%Y"),
            "data_fim": data_ref.strftime("%d/%m/%Y"),
        }
        r = self.session.get(url, params=params, timeout=30)
        r.raise_for_status()
        return _parse_zoop_financeiro(r.text)


# ==================== PARSERS HTML ====================
# ATENÇÃO: Estes parsers são placeholders. Você precisa ajustar
# os seletores depois de inspecionar o HTML real das páginas.

def _parse_relatorio_pagamento(html: str):
    """Parse do HTML do relatório-tipo-pagamento."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    # PLACEHOLDER — ajustar seletores reais
    # Procurar pela tabela principal
    table = soup.find("table", class_="table") or soup.find("table")
    if not table:
        return items

    for tr in table.find_all("tr")[1:]:  # pula header
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 6:
            continue
        try:
            items.append({
                "loja_id": _parse_loja(tds[0]),
                "cupom": tds[1],
                "hora": tds[2],
                "cod_aut": tds[3].strip() or None,
                "valor": _parse_money(tds[4]),
                "forma_pagto": tds[5],
            })
        except (ValueError, IndexError):
            continue
    return items


def _parse_zoop_financeiro(html: str):
    """Parse do HTML do zoop/financeiro."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    table = soup.find("table", class_="table") or soup.find("table")
    if not table:
        return items

    for tr in table.find_all("tr")[1:]:
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 6:
            continue
        try:
            status = tds[5].lower()
            if "succeeded" not in status:  # filtra só sucesso
                continue
            items.append({
                "loja_id": _parse_loja(tds[0]),
                "zoop_id": tds[1],
                "valor": _parse_money(tds[2]),
                "bandeira": tds[3],
                "tipo_pagamento": tds[4],
                "hora": tds[6] if len(tds) > 6 else "",
                "status": "succeeded",
            })
        except (ValueError, IndexError):
            continue
    return items


def _parse_loja(text: str) -> int:
    """Extrai loja_id de strings tipo 'Loja 1' ou '1 - RSul'."""
    text = text.strip()
    for ch in text:
        if ch.isdigit():
            n = int(ch)
            if n in LOJAS_VALIDAS:
                return n
    return 0


def _parse_money(text: str) -> float:
    """Converte 'R$ 1.234,56' em 1234.56."""
    t = text.replace("R$", "").replace(".", "").replace(",", ".").strip()
    return float(t) if t else 0.0


# ==================== LÓGICA DE CRUZAMENTO ====================

def _make_id(*parts) -> str:
    """Gera um ID estável pra identificar o problema."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def cruzar(vendas_livepdv, transacoes_zoop):
    """
    Cruza vendas LivePDV com transações Zoop.
    Retorna: {'problemas': [...], 'stats': {'conciliado': N}}
    """
    # Indexar Zoop por (loja, zoop_id)
    zoop_index = {}
    for z in transacoes_zoop:
        key = (z["loja_id"], z["zoop_id"])
        zoop_index[key] = z
    zoop_usados = set()

    problemas = []
    conciliado = 0

    # 1) Para cada venda LivePDV, tentar match
    for v in vendas_livepdv:
        loja = v["loja_id"]
        cod_aut = v.get("cod_aut")
        valor = v["valor"]

        # Sem cód de autorização → tentar match por valor+loja (sugestão)
        if not cod_aut:
            candidatos = [
                z for z in transacoes_zoop
                if z["loja_id"] == loja
                and (z["loja_id"], z["zoop_id"]) not in zoop_usados
                and abs(z["valor"] - valor) < TOLERANCIA_VALOR
            ]
            if len(candidatos) == 1:
                z = candidatos[0]
                problemas.append({
                    "id": _make_id("match", loja, v["cupom"], z["zoop_id"]),
                    "tipo": "match",
                    "loja_id": loja,
                    "cupom": v["cupom"],
                    "hora": v["hora"],
                    "valor": valor,
                    "zoop_id": z["zoop_id"],
                })
                zoop_usados.add((z["loja_id"], z["zoop_id"]))
            else:
                # 0 ou >1 candidatos — venda fantasma
                problemas.append({
                    "id": _make_id("fantasma", loja, v["cupom"]),
                    "tipo": "fantasma",
                    "loja_id": loja,
                    "cupom": v["cupom"],
                    "hora": v["hora"],
                    "valor": valor,
                    "cod_aut": cod_aut,
                })
            continue

        # Tem cód de autorização → tentar match exato no Zoop
        z = zoop_index.get((loja, cod_aut))
        if z is None:
            # Cod aut no LivePDV mas Zoop não tem
            problemas.append({
                "id": _make_id("fantasma", loja, v["cupom"]),
                "tipo": "fantasma",
                "loja_id": loja,
                "cupom": v["cupom"],
                "hora": v["hora"],
                "valor": valor,
                "cod_aut": cod_aut,
            })
        elif abs(z["valor"] - valor) >= TOLERANCIA_VALOR:
            # Valor diverge
            problemas.append({
                "id": _make_id("diverg", loja, v["cupom"], cod_aut),
                "tipo": "divergencia",
                "loja_id": loja,
                "cupom": v["cupom"],
                "hora": v["hora"],
                "zoop_id": cod_aut,
                "valor_livepdv": valor,
                "valor_zoop": z["valor"],
            })
            zoop_usados.add((loja, cod_aut))
        else:
            # MATCH PERFEITO
            conciliado += 1
            zoop_usados.add((loja, cod_aut))

    # 2) Transações Zoop que sobraram sem match → órfãos
    for z in transacoes_zoop:
        key = (z["loja_id"], z["zoop_id"])
        if key in zoop_usados:
            continue
        problemas.append({
            "id": _make_id("orfao", z["loja_id"], z["zoop_id"]),
            "tipo": "orfao",
            "loja_id": z["loja_id"],
            "cupom": None,
            "hora": z.get("hora", ""),
            "valor": z["valor"],
            "zoop_id": z["zoop_id"],
            "bandeira": z.get("bandeira"),
            "tipo_pagamento": z.get("tipo_pagamento"),
        })

    return {
        "problemas": problemas,
        "stats": {"conciliado": conciliado},
        "data_referencia": date.today().isoformat(),
        "gerado_em": datetime.now().isoformat(),
    }


# ==================== HANDLER VERCEL ====================

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self._handle()

    def do_GET(self):
        self._handle()

    def _handle(self):
        try:
            log("=== Iniciando conferência ===")
            log(f"USUARIO configurado: {'sim' if USUARIO else 'NÃO'}")
            log(f"SENHA configurada: {'sim' if SENHA else 'NÃO'}")
            log(f"BASE_URL: {BASE_URL}")

            if not USUARIO or not SENHA:
                return self._json(500, {
                    "error": "Credenciais não configuradas",
                    "detail": "Defina LIVEPDV_USUARIO e LIVEPDV_SENHA nas env vars do Vercel",
                })

            client = MoomboxClient()
            log("Tentando login no Moombox...")
            client.login()
            log("Login OK")

            data_ref = date.today()
            log(f"Buscando relatório de pagamento para {data_ref}...")
            vendas = client.buscar_relatorio_pagamento(data_ref)
            log(f"Vendas LivePDV recebidas: {len(vendas)}")

            log(f"Buscando financeiro Zoop para {data_ref}...")
            zoop = client.buscar_zoop_financeiro(data_ref)
            log(f"Transações Zoop recebidas: {len(zoop)}")

            log("Cruzando dados...")
            resultado = cruzar(vendas, zoop)
            resultado["totais"] = {
                "vendas_livepdv": len(vendas),
                "transacoes_zoop": len(zoop),
            }
            log(f"Resultado: {len(resultado['problemas'])} problemas, {resultado['stats']['conciliado']} conciliados")
            return self._json(200, resultado)

        except Exception as e:
            tb = traceback.format_exc()
            log(f"ERRO: {type(e).__name__}: {e}")
            log(f"TRACEBACK:\n{tb}")
            return self._json(500, {
                "error": str(e),
                "type": type(e).__name__,
                "traceback": tb,
            })

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
