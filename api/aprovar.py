"""
API Vercel — Aprovar/corrigir/apagar pagamento de cartão no LivePDV.

Recebe via POST JSON:
{
  "cupom_id": "98682",           // ID do cupom (numérico, sem zeros à esquerda)
  "cod_autoriza": "8358a36...",  // (preencher/substituir) cód correto a salvar
  "valor_esperado": 99.90,       // ajuda a desambiguar qual pagamento
  "modo": "preencher",           // "preencher" | "substituir" | "apagar"
  "cod_atual": "abc123..."       // (substituir) o cód ERRADO que está lá hoje
}

Modos:
- "preencher"  -> acha o pagamento com cód "(não definido)"/vazio e grava o cod.
                 (caso "match" — cartão sem código de autorização)
- "substituir" -> acha o pagamento cujo cód atual é o ERRADO (cod_atual) e troca
                 pelo correto. (caso "cod_errado")
- "apagar"     -> APAGA a linha de pagamento de cartão do cupom (caso "duplicado":
                 o mesmo cartão foi lançado em dois cupons; remove o duplicado).
                 Usa valor_esperado pra achar a linha. Se houver mais de uma linha
                 de mesmo valor no cupom, retorna erro (apague manualmente).

Faz: login -> abre o cupom -> identifica o pagamento -> grava/apaga -> responde.
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

# ATENÇÃO: caminho do delete é uma SUPOSIÇÃO baseada na convenção do Yii2.
# Confirme inspecionando o botão de excluir pagamento no Moombox e ajuste aqui.
DELETE_PATH = os.environ.get("LIVEPDV_DELETE_PATH", "/vendas/pagamentos/delete")


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
        Carrega a página do cupom e extrai info dos pagamentos.
        Retorna lista de {index, editable_key, cod_atual, valor, csrf_form}.
        """
        url = f"{BASE_URL}/vendas/cupom/view"
        log(f"GET {url}?id={cupom_id}")
        r = self.session.get(url, params={"id": cupom_id}, timeout=15)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        meta_csrf = soup.find("meta", {"name": "csrf-token"})
        if meta_csrf:
            self._csrf_meta = meta_csrf.get("content", "")
            log(f"CSRF meta capturado: {self._csrf_meta[:20]}...")

        pagamentos = []
        for btn in soup.find_all(attrs={"id": re.compile(r"pagamentos-\d+-cod_autoriza-targ")}):
            m = re.match(r"pagamentos-(\d+)-cod_autoriza-targ", btn.get("id", ""))
            if not m:
                continue
            index = int(m.group(1))
            cod_atual = btn.get_text(strip=True)

            popover_id = f"pagamentos-{index}-cod_autoriza-popover"
            popover = soup.find(id=popover_id)
            editable_key = ""
            csrf_form = ""
            if popover:
                key_input = popover.find("input", {"name": "editableKey"})
                if key_input:
                    editable_key = key_input.get("value", "").strip()
                csrf_input = popover.find("input", {"name": "_csrf"})
                if csrf_input:
                    csrf_form = csrf_input.get("value", "").strip()

            if not editable_key:
                tr = btn.find_parent("tr")
                if tr:
                    editable_key = (tr.get("data-key") or "").strip()

            valor = None
            tr = btn.find_parent("tr")
            if tr:
                for td in tr.find_all("td"):
                    txt = td.get_text(strip=True)
                    if re.match(r"^\d{1,3}(?:\.\d{3})*,\d{2,4}$|^\d+,\d{2,4}$|^\d+\.\d{2,4}$", txt):
                        try:
                            valor = float(txt.replace(".", "").replace(",", "."))
                            break
                        except ValueError:
                            pass

            pagamentos.append({
                "index": index,
                "editable_key": editable_key,
                "cod_atual": cod_atual,
                "valor": valor,
                "csrf_form": csrf_form,
            })

        log(f"Cupom {cupom_id}: {len(pagamentos)} pagamentos, keys={[p['editable_key'] for p in pagamentos]}")
        return pagamentos

    def atualizar_cod_autoriza(self, editable_key, editable_index, cod_autoriza):
        """Chamada AJAX do Kartik Editable pra salvar o cod_autoriza."""
        if not editable_key or not str(editable_key).strip():
            raise RuntimeError("editable_key vazio — não consegui descobrir o ID do pagamento")
        if not self._csrf_meta:
            raise RuntimeError("CSRF token do meta não capturado — chame buscar_cupom() antes")

        url = f"{BASE_URL}/vendas/pagamentos/inline-update"
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

        log(f"POST inline-update editableKey={editable_key} cod={cod_autoriza[:20]}...")
        r = self.session.post(url, data=payload, headers=headers, timeout=15)
        log(f"resposta: status={r.status_code}, body[:200]={r.text[:200]}")

        if r.status_code != 200:
            raise RuntimeError(f"POST inline-update falhou: status={r.status_code}, body={r.text[:300]}")
        try:
            resp = r.json()
        except ValueError:
            raise RuntimeError(f"Resposta não é JSON: {r.text[:300]}")
        if resp.get("message"):
            raise RuntimeError(f"LivePDV rejeitou: {resp['message']}")
        return resp

    def apagar_pagamento(self, editable_key):
        """
        APAGA a linha de pagamento do cupom (caso duplicado).
        Convenção Yii2: POST em /vendas/pagamentos/delete?id=<id> com _csrf.
        CONFIRME o caminho real (DELETE_PATH) inspecionando o Moombox.
        """
        if not editable_key or not str(editable_key).strip():
            raise RuntimeError("editable_key vazio — não consegui identificar o pagamento a apagar")
        if not self._csrf_meta:
            raise RuntimeError("CSRF token do meta não capturado — chame buscar_cupom() antes")

        url = f"{BASE_URL}{DELETE_PATH}"
        headers = {
            "X-CSRF-Token": self._csrf_meta,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        log(f"POST delete pagamento id={editable_key} url={url}")
        r = self.session.post(url, params={"id": editable_key},
                              data={"_csrf": self._csrf_meta},
                              headers=headers, timeout=15)
        log(f"resposta delete: status={r.status_code}, body[:200]={r.text[:200]}")

        # Yii2 delete costuma responder 200/204 (ajax) ou 302 (redirect pós-delete)
        if r.status_code not in (200, 204, 302):
            raise RuntimeError(f"Delete falhou: status={r.status_code}, body={r.text[:300]}")
        return {"status": r.status_code}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            data = json.loads(body)

            cupom_id = data.get("cupom_id")
            cod_autoriza = data.get("cod_autoriza")
            valor_esperado = data.get("valor_esperado")
            modo = (data.get("modo") or "preencher").lower()
            cod_atual_errado = (data.get("cod_atual") or "").strip()

            if not cupom_id:
                return self._json(400, {"error": "cupom_id é obrigatório"})
            if modo not in ("preencher", "substituir", "apagar"):
                return self._json(400, {"error": f"modo inválido: {modo}"})
            if modo in ("preencher", "substituir") and not cod_autoriza:
                return self._json(400, {"error": "cod_autoriza é obrigatório nesse modo"})
            if modo == "substituir" and not cod_atual_errado:
                return self._json(400, {"error": "modo 'substituir' exige cod_atual (o código errado)"})
            if modo == "apagar" and valor_esperado is None:
                return self._json(400, {"error": "modo 'apagar' exige valor_esperado pra achar a linha"})

            log(f"=== cupom={cupom_id} modo={modo} valor={valor_esperado} ===")

            if not USUARIO or not SENHA:
                return self._json(500, {"error": "Credenciais não configuradas"})

            client = MoomboxClient()
            client.login()

            pagamentos = client.buscar_cupom(cupom_id)
            if not pagamentos:
                return self._json(404, {"error": f"Nenhum pagamento encontrado pro cupom {cupom_id}"})

            def _vazio(cod):
                c = (cod or "").lower().strip()
                return c == "" or "não definido" in c or "nao definido" in c

            # ---------- MODO APAGAR (duplicado) ----------
            if modo == "apagar":
                candidatos = [
                    p for p in pagamentos
                    if p.get("valor") is not None
                    and abs((p.get("valor") or 0) - valor_esperado) < 0.02
                ]
                if not candidatos:
                    return self._json(409, {
                        "error": f"Não achei pagamento de R$ {valor_esperado} no cupom {cupom_id}.",
                        "pagamentos": pagamentos,
                    })
                if len(candidatos) > 1:
                    return self._json(409, {
                        "error": "Há mais de um pagamento com esse valor no cupom — apague manualmente pra não remover o errado.",
                        "candidatos": candidatos,
                    })
                alvo = candidatos[0]
                log(f"Apagando pagamento: index={alvo['index']} key={alvo['editable_key']} valor={alvo.get('valor')}")
                resp = client.apagar_pagamento(alvo["editable_key"])
                return self._json(200, {
                    "ok": True, "cupom_id": cupom_id, "modo": "apagar",
                    "editable_key": alvo["editable_key"], "livepdv_response": resp,
                })

            # ---------- MODOS PREENCHER / SUBSTITUIR ----------
            if modo == "substituir":
                alvo_pref = cod_atual_errado[:12].lower()
                candidatos = [
                    p for p in pagamentos
                    if p.get("cod_atual") and not _vazio(p.get("cod_atual"))
                    and p.get("cod_atual", "").lower().replace(" ", "").startswith(alvo_pref)
                ]
                if not candidatos and valor_esperado is not None:
                    candidatos = [
                        p for p in pagamentos
                        if not _vazio(p.get("cod_atual"))
                        and abs((p.get("valor") or 0) - valor_esperado) < 0.02
                    ]
                if not candidatos:
                    return self._json(409, {
                        "error": "Não achei o pagamento com o código errado informado neste cupom.",
                        "cod_atual_procurado": cod_atual_errado,
                        "pagamentos": pagamentos,
                    })
            else:
                candidatos = [p for p in pagamentos if _vazio(p.get("cod_atual"))]
                if not candidatos:
                    return self._json(409, {
                        "error": "Todos os pagamentos do cupom já têm código de autorização",
                        "pagamentos": pagamentos,
                    })

            if len(candidatos) > 1 and valor_esperado is not None:
                candidatos_valor = [
                    p for p in candidatos
                    if abs((p.get("valor") or 0) - valor_esperado) < 0.02
                ]
                if candidatos_valor:
                    candidatos = candidatos_valor

            if len(candidatos) > 1:
                return self._json(409, {
                    "error": "Múltiplos pagamentos candidatos no cupom (ambíguos por valor). Atualize manualmente.",
                    "candidatos": candidatos,
                })

            alvo = candidatos[0]
            log(f"Pagamento alvo: index={alvo['index']} key={alvo['editable_key']} valor={alvo.get('valor')}")
            resp = client.atualizar_cod_autoriza(
                editable_key=alvo["editable_key"],
                editable_index=alvo["index"],
                cod_autoriza=cod_autoriza,
            )
            return self._json(200, {
                "ok": True, "cupom_id": cupom_id, "modo": modo,
                "cod_autoriza": cod_autoriza, "editable_key": alvo["editable_key"],
                "livepdv_response": resp,
            })

        except Exception as e:
            tb = traceback.format_exc()
            log(f"ERRO: {type(e).__name__}: {e}\n{tb}")
            return self._json(500, {"error": str(e), "type": type(e).__name__, "traceback": tb})

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
