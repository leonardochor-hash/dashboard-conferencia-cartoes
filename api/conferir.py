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
        Retorna lista de dicts com itens individuais de pagamento.

        Como a coluna 'Cod Autoriza' do LivePDV pode conter VÁRIOS pagamentos
        concatenados (cod - valor - data - tipo - parcelas), expandimos cada linha
        do cupom em vários itens (um por pagamento).
        """
        url = f"{BASE_URL}/relatorios/relatorio-tipo-pagamento/index"
        data_str = data_ref.strftime("%d/%m/%Y")
        params = {
            "RelatorioTipoPagamentoForm[data]": f"{data_str} - {data_str}",
            "_togd9a55727": "all",  # "Ver todos" - trás todas as linhas
        }
        log(f"GET relatorio-tipo-pagamento ({data_str})")
        r = self.session.get(url, params=params, timeout=30)
        log(f"GET resposta: status={r.status_code}")
        r.raise_for_status()
        return _parse_relatorio_pagamento(r.text)

    def buscar_zoop_financeiro(self, data_ref: date):
        """
        Baixa o financeiro Zoop do dia (só status=succeeded).
        """
        url = f"{BASE_URL}/zoop/financeiro/index"
        data_str = data_ref.strftime("%d/%m/%Y")
        params = {
            "TransacaoPosSearch[data]": f"{data_str} - {data_str}",
            "_tog1149016d": "all",  # "Ver todos"
        }
        log(f"GET zoop/financeiro ({data_str})")
        r = self.session.get(url, params=params, timeout=30)
        log(f"GET resposta: status={r.status_code}")
        r.raise_for_status()
        return _parse_zoop_financeiro(r.text)


# ==================== PARSERS HTML ====================
# ATENÇÃO: Estes parsers são placeholders. Você precisa ajustar
# os seletores depois de inspecionar o HTML real das páginas.

def _parse_relatorio_pagamento(html: str):
    """
    Parse do HTML do relatório-tipo-pagamento (Kartik GridView do Yii2).

    Estratégia: usa `data-col-seq` pra mapear colunas e `data-raw-value` pros
    valores canônicos (mais confiável que parsear texto pt-BR).

    Mapeamento das colunas (data-col-seq):
      0=#  1=Data  2=User Id  3=Cupom  4=Loja  5=Expositor  6=Expositor pagto
      7=Meio pagto próprio  8=Pagamento  9=Cod Autoriza  10=Total cupom
      11=Valor pago  12=Participação

    A coluna 9 (Cod Autoriza) pode ter VÁRIOS pagamentos do mesmo cupom
    concatenados no formato "COD - VALOR - DATA - tipo - parcelas". Por isso
    cada linha pode gerar 1 ou mais itens de pagamento (mas mantemos 1 item
    por linha aqui — o cruzamento usa cupom+valor+cod).
    """
    import re
    soup = BeautifulSoup(html, "html.parser")
    items = []

    # Encontra a tabela do GridView
    table = soup.find("table", class_=lambda c: c and "kv-grid-table" in c)
    if not table:
        log("AVISO: tabela kv-grid-table não encontrada — tentando fallback")
        table = soup.find("table")
    if not table:
        return items

    tbody = table.find("tbody") or table

    for tr in tbody.find_all("tr"):
        # Ignora linhas vazias ou de mensagem "Nenhum resultado encontrado"
        cells = {}
        for td in tr.find_all("td"):
            seq = td.get("data-col-seq")
            if seq is None:
                continue
            raw = td.get("data-raw-value")
            txt = td.get_text(strip=True)
            cells[seq] = {"raw": raw, "text": txt}

        if not cells or "3" not in cells:  # precisa pelo menos ter Cupom
            continue

        try:
            cupom = cells["3"]["text"]
            if not cupom or not cupom.isdigit():
                continue

            # Loja vem como "3 - Barra Shopping" — extrair id e nome
            loja_txt = cells.get("4", {}).get("text", "")
            m = re.match(r"^(\d+)\s*-\s*(.+)$", loja_txt)
            if not m:
                continue
            loja_id = int(m.group(1))
            loja_nome = m.group(2).strip()

            # Valor pago: usar data-raw-value (canônico)
            valor_raw = cells.get("11", {}).get("raw")
            valor = float(valor_raw) if valor_raw else _parse_money(cells.get("11", {}).get("text", "0"))

            # Cod Autoriza: pode ser "(não definido)" ou conter ID Zoop (32 hex)
            # eventualmente seguido pelo cod autorização da bandeira (6 dígitos)
            # ou múltiplos pagamentos concatenados.
            cod_txt = cells.get("9", {}).get("text", "").strip()
            cod_aut = None
            if cod_txt and "(não definido)" not in cod_txt and "não definido" not in cod_txt.lower():
                # Tenta extrair o ID Zoop (primeiros 32 caracteres hexadecimais)
                m_zoop = re.match(r"^([a-f0-9]{32})", cod_txt)
                if m_zoop:
                    cod_aut = m_zoop.group(1)
                else:
                    # Fallback: pega o primeiro "token" alfanumérico
                    m_cod = re.match(r"^\s*([A-Za-z0-9]+)", cod_txt)
                    if m_cod:
                        cod_aut = m_cod.group(1)

            # Data (canônica via data-raw-value: "2026-05-23")
            data_raw = cells.get("1", {}).get("raw") or cells.get("1", {}).get("text", "")

            # Pagamento (tipo: "3 - Debito", "4 - PIX máquina", etc)
            pagamento = cells.get("8", {}).get("text", "")

            items.append({
                "loja_id": loja_id,
                "loja_nome": loja_nome,
                "cupom": cupom,
                "hora": data_raw,  # só data por enquanto, o relatório não dá hora
                "cod_aut": cod_aut,
                "valor": valor,
                "forma_pagto": pagamento,
            })
        except (ValueError, KeyError, AttributeError) as e:
            log(f"Erro ao parsear linha: {e}")
            continue

    log(f"Relatório-tipo-pagamento: {len(items)} itens parseados")
    return items


def _parse_zoop_financeiro(html: str):
    """
    Parse do HTML do zoop/financeiro (Kartik GridView do Yii2).

    Estrutura descoberta:
      data-col-seq="1"  → Loja (id numérico, com rowspan! forward-fill)
      data-col-seq="2"  → Cod Autorização (6 dígitos, da bandeira)
      data-col-seq="3"  → Data + hora
      data-col-seq="4"  → Valor Crédito
      data-col-seq="5"  → Valor da Operação (valor pago pelo cliente)
      data-col-seq="6"  → Tipo Pagamento (credit/debit/pix)
      data-col-seq="9"  → Bandeira
      data-col-seq="13" → ID Transação (hash 32 chars) — CHAVE PRA CRUZAR
      data-col-seq="14" → Status (succeeded/failed/canceled)
    """
    import re
    soup = BeautifulSoup(html, "html.parser")
    items = []

    table = soup.find("table", class_=lambda c: c and "kv-grid-table" in c)
    if not table:
        log("AVISO: tabela kv-grid-table não encontrada no Zoop")
        return items

    tbody = table.find("tbody") or table
    loja_atual = None  # forward-fill da loja (vem com rowspan)

    total_linhas = 0
    pulou_total = 0
    pulou_nao_succeeded = 0

    for tr in tbody.find_all("tr"):
        # Pular linhas de totalizadores de grupo (têm class kv-group-footer)
        tr_class = " ".join(tr.get("class") or [])
        if "kv-group" in tr_class and "kv-group-footer" in tr_class:
            pulou_total += 1
            continue

        cells = {}
        for td in tr.find_all("td"):
            seq = td.get("data-col-seq")
            if seq is None:
                continue
            cells[seq] = td.get_text(strip=True)

        if not cells:
            continue

        # Atualiza loja se a linha trouxer (primeira do grupo)
        if "1" in cells and cells["1"].strip().isdigit():
            loja_atual = int(cells["1"].strip())

        # Linha precisa ter ID transação (col 13)
        zoop_id = cells.get("13", "").strip()
        if not zoop_id or len(zoop_id) < 20:
            continue

        # Filtra só succeeded
        status = cells.get("14", "").strip().lower()
        if status != "succeeded":
            pulou_nao_succeeded += 1
            continue

        if loja_atual is None:
            log(f"AVISO: transação Zoop sem loja determinada: {zoop_id}")
            continue

        try:
            # Valor da Operação (col 5) = o que o cliente pagou
            valor_str = cells.get("5", "0").replace(",", ".")
            valor = float(valor_str) if valor_str else 0.0

            # Data + hora: "23/05/2026 21:06:03"
            data_txt = cells.get("3", "")
            hora = ""
            if " " in data_txt:
                hora = data_txt.split(" ", 1)[1]  # só hora

            items.append({
                "loja_id": loja_atual,
                "zoop_id": zoop_id,
                "cod_autorizacao": cells.get("2", ""),  # 6 dígitos
                "valor": valor,
                "tipo_pagamento": cells.get("6", ""),
                "bandeira": cells.get("9", ""),
                "hora": hora,
                "status": status,
            })
            total_linhas += 1
        except (ValueError, KeyError) as e:
            log(f"Erro parseando linha Zoop: {e}")
            continue

    log(f"Zoop: {total_linhas} transações succeeded, {pulou_total} totais ignorados, {pulou_nao_succeeded} não-succeeded ignoradas")
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

    REGRA: cada transação Zoop é UM pagamento. No LivePDV, o mesmo pagamento
    pode aparecer em VÁRIAS linhas (uma por expositor/marca do cupom), porque
    o sistema rateia o valor entre os produtos vendidos. Por isso precisamos
    AGRUPAR as linhas do LivePDV por (cupom, cod_aut) e SOMAR os valores
    antes de cruzar com o Zoop.

    Tipos de problema retornados:
    - "match"        : pagamento PDV sem cod_aut, mas existe Zoop compatível
    - "divergencia"  : cod_aut bate mas valor diverge
    - "fantasma"     : pagamento PDV com cod_aut mas Zoop não tem
    - "orfao"        : Zoop succeeded sem pagamento PDV correspondente
    """
    from collections import defaultdict

    # 1) Agrupar LivePDV por (loja, cupom, cod_aut) e somar valores
    grupos_pdv = defaultdict(lambda: {"valor": 0.0, "linhas": 0, "hora": "", "loja_nome": ""})
    for v in vendas_livepdv:
        key = (v["loja_id"], v["cupom"], v.get("cod_aut") or "")
        g = grupos_pdv[key]
        g["valor"] += v["valor"]
        g["linhas"] += 1
        g["hora"] = v.get("hora", g["hora"])
        g["loja_nome"] = v.get("loja_nome", g["loja_nome"])
        g["forma_pagto"] = v.get("forma_pagto", g.get("forma_pagto"))

    # 2) Indexar Zoop por (loja, zoop_id)
    zoop_index = {}
    for z in transacoes_zoop:
        zoop_index[(z["loja_id"], z["zoop_id"])] = z
    zoop_usados = set()

    problemas = []
    conciliado = 0

    # 3) Para cada grupo do LivePDV, tentar match
    for (loja, cupom, cod_aut), grupo in grupos_pdv.items():
        valor = round(grupo["valor"], 2)

        # Sem cod_aut → tentar sugerir match por valor+loja
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
                    "id": _make_id("match", loja, cupom, z["zoop_id"]),
                    "tipo": "match",
                    "loja_id": loja,
                    "cupom": cupom,
                    "hora": grupo["hora"],
                    "valor": valor,
                    "valor_zoop": z["valor"],
                    "zoop_id": z["zoop_id"],
                })
                zoop_usados.add((z["loja_id"], z["zoop_id"]))
            else:
                problemas.append({
                    "id": _make_id("fantasma", loja, cupom, "no_cod"),
                    "tipo": "fantasma",
                    "loja_id": loja,
                    "cupom": cupom,
                    "hora": grupo["hora"],
                    "valor": valor,
                    "cod_aut": None,
                })
            continue

        # Tem cod_aut → tentar match exato no Zoop
        z = zoop_index.get((loja, cod_aut))
        if z is None:
            problemas.append({
                "id": _make_id("fantasma", loja, cupom, cod_aut),
                "tipo": "fantasma",
                "loja_id": loja,
                "cupom": cupom,
                "hora": grupo["hora"],
                "valor": valor,
                "cod_aut": cod_aut,
            })
        elif abs(z["valor"] - valor) >= TOLERANCIA_VALOR:
            problemas.append({
                "id": _make_id("diverg", loja, cupom, cod_aut),
                "tipo": "divergencia",
                "loja_id": loja,
                "cupom": cupom,
                "hora": grupo["hora"],
                "zoop_id": cod_aut,
                "valor_livepdv": valor,
                "valor_zoop": z["valor"],
            })
            zoop_usados.add((loja, cod_aut))
        else:
            # MATCH PERFEITO — não vai pro dashboard
            conciliado += 1
            zoop_usados.add((loja, cod_aut))

    # 4) Transações Zoop que sobraram sem match → órfãos
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

            # Tenta ler ?data=YYYY-MM-DD da URL; senão usa data operacional
            data_ref = self._resolver_data()
            log(f"Data de referência: {data_ref}")

            client = MoomboxClient()
            log("Tentando login no Moombox...")
            client.login()
            log("Login OK")

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
            resultado["data_referencia"] = data_ref.isoformat()
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

    def _resolver_data(self):
        """
        Resolve a data de referência:
        - Se a URL tiver ?data=YYYY-MM-DD, usa essa
        - Senão, usa a "data operacional": antes das 6h, considera ontem
        """
        from urllib.parse import urlparse, parse_qs
        from datetime import timedelta

        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if "data" in qs:
            try:
                return date.fromisoformat(qs["data"][0])
            except (ValueError, IndexError):
                pass

        # Data operacional: antes das 6h → ontem
        agora = datetime.now()
        if agora.hour < 6:
            return (agora - timedelta(days=1)).date()
        return agora.date()

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
