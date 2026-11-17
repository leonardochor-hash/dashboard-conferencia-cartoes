"""
API Vercel — Autocorreção de confiança alta.

POST /api/autocorrigir?data=YYYY-MM-DD&dry_run=1

1. Chama /api/conferir deste mesmo site.
2. Separa as sugestões ELEGÍVEIS: match / cod_errado / codigo_duplicado com
   confiança "alta" e um único candidato na Zoop.
3. dry_run=1: só devolve o que seria feito (não grava nada).
   Sem dry_run: aplica cada uma via /api/aprovar (preencher ou substituir) e
   confere de novo no fim.

Nunca apaga pagamento. Divergência, duplicado, órfão e fantasma ficam manuais.
O código gravado é sempre o ID da transação Zoop (zoop_id), igual ao botão
"Aceitar correção" do painel.
"""

import json
import os
import sys
import time
import traceback
import hmac
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

TIPOS_AUTO = ("match", "cod_errado", "codigo_duplicado")
MAX_CORRECOES = int(os.environ.get("AUTOCORRIGIR_MAX", "30"))
ORCAMENTO_SEG = 45  # a função tem 60s; deixa folga para a resposta


def log(msg):
    print(f"[AUTO] {msg}", file=sys.stderr, flush=True)


def _token_ok(handler, qs):
    esperado = os.environ.get("PAINEL_TOKEN", "").strip()
    if not esperado:
        return True
    recebido = handler.headers.get("X-Painel-Token") or (qs.get("token") or [""])[0]
    return hmac.compare_digest(str(recebido), esperado)


def elegivel(p):
    if p.get("tipo") not in TIPOS_AUTO or not p.get("cupom"):
        return False
    if p.get("confianca") != "alta" or (p.get("outros_candidatos") or 0) > 0:
        return False
    if p["tipo"] == "match":
        return bool(p.get("zoop_id"))
    return bool(p.get("cod_certo") and p.get("cod_errado"))


def payload_de(p):
    base = {
        "cupom_id": str(p["cupom"]).lstrip("0"),
        "valor_esperado": p.get("valor"),
    }
    if p["tipo"] == "match":
        base.update(modo="preencher", cod_autoriza=p["zoop_id"])
    else:
        base.update(modo="substituir", cod_autoriza=p["cod_certo"], cod_atual=p["cod_errado"])
    return base


def item(p, extra=None):
    d = {
        "id": p.get("id"), "tipo": p.get("tipo"), "loja_id": p.get("loja_id"),
        "cupom": p.get("cupom"), "valor": p.get("valor"), "hora": p.get("hora"),
    }
    if extra:
        d.update(extra)
    return d


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        inicio = time.time()
        try:
            qs = parse_qs(urlparse(self.path).query)
            if not _token_ok(self, qs):
                return self._json(401, {"error": "nao autorizado"})

            dry = (qs.get("dry_run") or ["0"])[0] in ("1", "true", "sim")
            data = (qs.get("data") or [""])[0]
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
            base = f"https://{host}"
            hdr = {"X-Painel-Token": os.environ.get("PAINEL_TOKEN", "").strip()}
            q = f"?data={data}" if data else ""

            r = requests.post(f"{base}/api/conferir{q}", headers=hdr, timeout=40)
            conf = r.json()
            if r.status_code != 200:
                return self._json(502, {"error": "falha ao conferir", "detalhe": conf})

            probs = conf.get("problemas") or []
            eleg = [p for p in probs if elegivel(p)]
            manuais = [item(p) for p in probs if not elegivel(p)]
            aplicadas, falhas, puladas = [], [], []

            if dry:
                aplicadas = [item(p, {"payload": payload_de(p)}) for p in eleg]
            else:
                for i, p in enumerate(eleg):
                    if i >= MAX_CORRECOES or time.time() - inicio > ORCAMENTO_SEG:
                        puladas.append(item(p, {"motivo": "limite de tempo/quantidade; rode de novo"}))
                        continue
                    pl = payload_de(p)
                    try:
                        ra = requests.post(f"{base}/api/aprovar", json=pl,
                                           headers=hdr, timeout=25)
                        resp = ra.json() if ra.content else {}
                    except Exception as e:  # noqa: BLE001
                        ra, resp = None, {"error": str(e)}
                    if ra is not None and ra.status_code == 200 and resp.get("ok"):
                        aplicadas.append(item(p, {"payload": pl}))
                    else:
                        falhas.append(item(p, {"payload": pl, "resposta": resp}))

            depois = None
            if not dry and aplicadas and time.time() - inicio < 50:
                try:
                    rd = requests.post(f"{base}/api/conferir{q}", headers=hdr, timeout=10)
                    if rd.status_code == 200:
                        dd = rd.json()
                        depois = {"stats": dd.get("stats"), "problemas": dd.get("problemas")}
                except Exception:  # noqa: BLE001
                    depois = None

            return self._json(200, {
                "dry_run": dry,
                "data_referencia": conf.get("data_referencia"),
                "conferencia": {
                    "stats": conf.get("stats"), "totais": conf.get("totais"),
                    "problemas": probs, "alertas": conf.get("alertas"),
                },
                "aplicadas": aplicadas, "falhas": falhas, "manuais": manuais,
                "puladas": puladas, "depois": depois,
                "resumo": {
                    "elegiveis": len(eleg), "aplicadas": len(aplicadas),
                    "falhas": len(falhas), "manuais": len(manuais),
                    "puladas": len(puladas),
                    "segundos": round(time.time() - inicio, 1),
                },
            })
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            log(tb)
            return self._json(500, {"error": str(e), "traceback": tb})

    def _json(self, status, payload):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
