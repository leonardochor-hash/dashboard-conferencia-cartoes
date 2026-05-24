"""
API Vercel — Aprovar match no LivePDV.

Recebe via POST JSON:
{
  "cupom_id": "98682",          // ID do cupom (numérico, sem zeros à esquerda)
  "cod_autoriza": "8358a36...",  // valor a salvar no LivePDV
  "valor_esperado": 99.90        // só pra log/conferência
}

Faz:
1. Login no Moombox
2. GET na página do cupom pra extrair os editableKey de TODOS os pagamentos
3. Identifica qual pagamento atualizar (o que está "(não definido)" e tem valor compatível)
4. POST em /vendas/pagamentos/inline-update com o cod
5. Retorna sucesso ou erro detalhado
"""

import os
import json
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler

import requests
from bs4 import BeautifulSoup


BASE_URL = os.environ.get("LIVEPDV_BASE_URL", "https://expositores.moombox.com.br").rstrip("/")
USUARIO = os.environ.get("LIVEPDV_USUARIO", "").strip()
SENHA = os.environ.get("LIVEPDV_SENHA", "").strip()


def log(msg):
    print(f"[APROVAR] {msg}", file=sys.stderr, flush=True)


class MoomboxClient:
    """Cliente que loga no Moombox."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) DashConferencia/1.0"
        })
        self._csrf_meta = None  # csrf token do <meta>, usado em ajax

    def login(self):
        login_url = f"{BASE_URL}/user/login"
        r = self.session.get(login_url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        csrf_input = soup.find("input", {"name": "_csrf"})
        if not csrf_input:
            raise RuntimeError("Campo _csrf não encontrado na página de login")
        csrf = csrf_input.get("value", "")

        payload = {
            "_csrf": csrf,
            "login-form[login]": USUARIO,
            "login-form[password]": SENHA,
            "login-form[rememberMe]": "0",
        }
        r = self.session.post(login_url, data=payload, timeout=15, allow_redirects=True)
        r.raise_for_status()

        if "/user/login" in r.url:
            raise RuntimeError("Login no Moombox falhou")
        return True

    def buscar_cupom(self, cupom_id):
        """
        Carrega a página de visualização do cupom e extrai info dos pagamentos:
        retorna lista de dicts: {editable_key, valor, cod_atual, tipo_pagamento}
        e o csrf token mais recente (do <meta>).
        """
        url = f"{BASE_URL}/vendas/cupom/view"
        log(f"GET {url}?id={cupom_id}")
        r = self.session.get(url, params={"id": cupom_id}, timeout=15)
        r.raise_for_status()

        # Pega csrf token do <meta> (usado nas chamadas AJAX)
        soup = BeautifulSoup(r.text, "html.parser")
        meta_csrf = soup.find("meta", {"name": "csrf-token"})
        if meta_csrf:
            self._csrf_meta = meta_csrf.get("content", "")
            log(f"CSRF meta capturado: {self._csrf_meta[:20]}...")

        # Encontra os pagamentos. O widget Kartik Editable cria spans tipo:
        # id="pagamentos-0-cod_autoriza-targ", data-key="113039"
        pagamentos = []
        for span in soup.find_all(attrs={"id": re.compile(r"pagamentos-\d+-cod_autoriza-targ")}):
            m = re.match(r"pagamentos-(\d+)-cod_autoriza-targ", span.get("id", ""))
            if not m:
                continue
            index = int(m.group(1))
            key = span.get("data-key", "")
            cod_atual = span.get_text(strip=True)
            pagamentos.append({
                "index": index,
                "editable_key": key,
                "cod_atual": cod_atual,
            })

        # Tenta achar valores dos pagamentos pela tabela "Tipo Pagamentos"
        # Estratégia: procura a tabela "Tipo Pagamentos" e mapeia por índice
        valores = []
        # Heurística: vamos pegar todos os <td> com valores monetários na ordem
        # da tabela tipo-pagamentos. Como o HTML pode variar, fazemos best-effort.
        for tbl in soup.find_all("table"):
            # tabela tipo-pagamentos costuma ter "Cod Autoriza" no header
            txt = tbl.get_text()
            if "Cod Autoriza" in txt and "Tipo de pagamento" in txt:
                for tr in tbl.find("tbody").find_all("tr") if tbl.find("tbody") else []:
                    tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                    # td[2] geralmente é o Valor (depois de #, ID)
                    for td_text in tds:
                        if re.match(r"^\d+[,.]?\d*$", td_text.replace(",", ".").replace(".", "")):
                            try:
                                v = float(td_text.replace(".", "").replace(",", "."))
                                valores.append(v)
                                break
                            except ValueError:
                                pass
                break

        # Acopla valor a cada pagamento por índice (se conseguimos extrair)
        for i, p in enumerate(pagamentos):
            if i < len(valores):
                p["valor"] = valores[i]

        log(f"Cupom {cupom_id}: {len(pagamentos)} pagamentos encontrados")
        return pagamentos

    def atualizar_cod_autoriza(self, editable_key, editable_index, cod_autoriza):
        """
        Faz a chamada AJAX que o Kartik Editable faria pra salvar o cod_autoriza.
        """
        url = f"{BASE_URL}/vendas/pagamentos/inline-update"
        if not self._csrf_meta:
            raise RuntimeError("CSRF token do meta não capturado — chame buscar_cupom() antes")

        payload = {
            "_csrf": self._csrf_meta,
            "hasEditable": "1",
            "editableIndex": str(editable_index),
            "editableKey": str(editable_key),
            "editableAttribute": "cod_autoriza",
            f"Pagamentos[{editable_index}][cod_autoriza]": cod_autoriza,
        }
        headers = {
            "X-CSRF-Token": self._csrf_meta,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

        log(f"POST {url} editableKey={editable_key} cod={cod_autoriza[:20]}...")
        r = self.session.post(url, data=payload, headers=headers, timeout=15)
        log(f"POST resposta: status={r.status_code}, body[:200]={r.text[:200]}")

        if r.status_code != 200:
            raise RuntimeError(f"POST inline-update falhou: status={r.status_code}, body={r.text[:300]}")

        # Resposta esperada: {"output": "...", "message": ""}
        try:
            resp = r.json()
        except ValueError:
            raise RuntimeError(f"Resposta não é JSON: {r.text[:300]}")

        if resp.get("message"):
            raise RuntimeError(f"LivePDV rejeitou: {resp['message']}")

        return resp


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            data = json.loads(body)

            cupom_id = data.get("cupom_id")
            cod_autoriza = data.get("cod_autoriza")
            valor_esperado = data.get("valor_esperado")

            if not cupom_id or not cod_autoriza:
                return self._json(400, {"error": "cupom_id e cod_autoriza são obrigatórios"})

            log(f"=== Aprovando cupom={cupom_id} cod={cod_autoriza[:20]}... valor={valor_esperado} ===")

            if not USUARIO or not SENHA:
                return self._json(500, {"error": "Credenciais não configuradas"})

            client = MoomboxClient()
            client.login()

            # 1) Buscar pagamentos do cupom
            pagamentos = client.buscar_cupom(cupom_id)
            if not pagamentos:
                return self._json(404, {
                    "error": f"Nenhum pagamento encontrado pro cupom {cupom_id}",
                })

            # 2) Identificar qual pagamento atualizar
            # Critério: cod_atual igual a "(não definido)" ou vazio
            candidatos = [
                p for p in pagamentos
                if not p.get("cod_atual")
                or "não definido" in p.get("cod_atual", "").lower()
                or p.get("cod_atual", "").strip() == ""
            ]

            if not candidatos:
                return self._json(409, {
                    "error": "Todos os pagamentos do cupom já têm código de autorização",
                    "pagamentos": pagamentos,
                })

            # Se tem mais de um sem cod, usa o valor pra desambiguar
            if len(candidatos) > 1 and valor_esperado is not None:
                candidatos_valor = [
                    p for p in candidatos
                    if abs(p.get("valor", 0) - valor_esperado) < 0.02
                ]
                if candidatos_valor:
                    candidatos = candidatos_valor

            if len(candidatos) > 1:
                return self._json(409, {
                    "error": f"Múltiplos pagamentos sem cod_autoriza no cupom (e ambíguos por valor). Atualize manualmente.",
                    "candidatos": candidatos,
                })

            alvo = candidatos[0]
            log(f"Pagamento alvo: index={alvo['index']} key={alvo['editable_key']} valor={alvo.get('valor')}")

            # 3) Atualizar
            resp = client.atualizar_cod_autoriza(
                editable_key=alvo["editable_key"],
                editable_index=alvo["index"],
                cod_autoriza=cod_autoriza,
            )

            return self._json(200, {
                "ok": True,
                "cupom_id": cupom_id,
                "cod_autoriza": cod_autoriza,
                "editable_key": alvo["editable_key"],
                "livepdv_response": resp,
            })

        except Exception as e:
            tb = traceback.format_exc()
            log(f"ERRO: {type(e).__name__}: {e}\n{tb}")
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
