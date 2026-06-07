# Dashboard de Conferência de Cartões

Conferência diária LivePDV ↔ Zoop. Mostra só os problemas e sugere matches.

## Arquitetura

```
Browser → index.html (login + UI)
            ↓ POST /api/conferir
         api/conferir.py (serverless Vercel)
            ↓ login + scraping
         Moombox/LivePDV
            ↓ HTML
         Parse + cruzamento
            ↓ JSON com só os problemas
         Browser renderiza
```

## Deploy

### 1. Subir no GitHub
- Crie um repositório novo (privado)
- Suba os arquivos: `index.html`, `api/conferir.py`, `requirements.txt`, `vercel.json`, `README.md`

### 2. Conectar no Vercel
- vercel.com → Add New → Project → Import do seu GitHub
- Em "Environment Variables", adicione:
  - `LIVEPDV_USUARIO` = seu email do Moombox
  - `LIVEPDV_SENHA` = sua senha do Moombox
  - `LIVEPDV_BASE_URL` = `https://expositores.moombox.com.br` (opcional, é o default)
- Deploy

### 3. Mudar a senha do dashboard
No `index.html`, linha que diz:
```js
const DASHBOARD_PASSWORD = 'cartoes2026';
```
Trocar por uma senha forte. Subir de novo.

## ⚠️ IMPORTANTE: Ajustar os parsers HTML

Os parsers em `api/conferir.py` (`_parse_relatorio_pagamento` e `_parse_zoop_financeiro`) são **placeholders**. Eles assumem que as tabelas têm colunas em ordem específica, mas você precisa:

1. Acessar as 2 páginas do Moombox no navegador
2. Inspecionar o HTML (F12 → Elements)
3. Ajustar os seletores e índices de colunas no Python

### Como ajustar

No `_parse_relatorio_pagamento`, ajustar os índices das colunas:
- `tds[0]` → coluna da loja
- `tds[1]` → cupom
- `tds[2]` → hora
- `tds[3]` → código de autorização
- `tds[4]` → valor
- `tds[5]` → forma de pagamento

Idem pro `_parse_zoop_financeiro`.

Se a tabela tiver uma classe CSS específica (ex: `table-bordered`), trocar:
```python
table = soup.find("table", class_="table") or soup.find("table")
```
por:
```python
table = soup.find("table", class_="table-bordered")
```

## Como funciona o cruzamento

1. **Match perfeito** (não vai pro dashboard): mesma loja + mesmo cód autorização + valor bate
2. **Sugestão de match** (amarelo): venda LivePDV sem cód aut + uma única transação Zoop com mesmo valor/loja
3. **Divergência de valor** (vermelho): cód aut bate mas valor não
4. **Cartão órfão** (vermelho): transação Zoop sem venda LivePDV correspondente
5. **Venda fantasma** (azul): venda LivePDV com cód aut mas Zoop não tem a transação

Tolerância de valor: R$ 0,02 (ajustável em `TOLERANCIA_VALOR`).

## Storage

- **Login do dashboard**: `sessionStorage` (perde quando fecha aba)
- **Itens marcados como resolvidos**: `localStorage` por dia (reseta automaticamente no dia seguinte)

## Limitações conhecidas

- Vercel free tier: timeout de 10s. Plano Hobby aumenta pra 60s. Se o Moombox demorar muito, pode dar erro.
- Senha do Moombox fica nas env vars do Vercel — quem tiver acesso ao painel do Vercel vê.
- Scraping pode quebrar se o Moombox mudar o HTML. Quando isso acontecer, ajustar os parsers.
- A senha do dashboard (`DASHBOARD_PASSWORD`) está hardcoded no JS — qualquer um que abrir o "Ver código fonte" vê. É uma barreira fraca, não uma proteção real. Para algo mais sério, mover validação pro backend.

## Novos tipos de problema (correção semi-automática)

Além de match/divergência/órfão/fantasma, a engine agora identifica e sugere correção:

- **Cód. errado** (`cod_errado`): cupom com código de autorização que não existe na
  Zoop, pareado por loja+valor com uma transação Zoop órfã. Sugere TROCAR o código.
  Botão "Aceitar correção" → `POST /api/aprovar` com `modo: "substituir"`,
  `cod_atual` = código errado, `cod_autoriza` = ID Zoop correto.
- **Cartão duplicado** (`duplicado`): mesmo valor lançado 2+ vezes no PDV na mesma
  loja, mas só há 1 transação Zoop nesse valor. Sugere APAGAR o pagamento de cartão
  do cupom duplicado. Botão "Aceitar (apagar cartão)" -> `POST /api/aprovar` com
  `modo: "apagar"` + `valor_esperado`. ATENÇÃO: o caminho de delete (`DELETE_PATH`)
  é uma suposição da convenção Yii2 — confirme inspecionando o Moombox.

O caso "cupom com a referência de cartão de OUTRO valor" (ex: cupom R$599 apontando
para uma transação de valor diferente) também cai em `cod_errado`: a engine acha a
transação Zoop do MESMO valor do cupom e sugere trocar a referência. Se não achar
nenhuma do valor certo, vira `divergencia` (investigar manual).

Cada sugestão traz `confianca`:
- **alta**: 1 único candidato Zoop no valor → seguro aceitar.
- **media**: mais de um candidato no mesmo valor → confira antes de aceitar.

### Fluxo semi-automático
O sistema sugere a correção; você clica em **Aceitar correção** e a alteração é
gravada no LivePDV via `inline-update`. Nada é alterado sem o seu clique.

### Modos do /api/aprovar
- `modo: "preencher"` (default) — preenche cód em pagamento que está sem código (match).
- `modo: "substituir"` — troca o cód errado pelo correto (cod_errado); exige `cod_atual`.
- `modo: "apagar"` — apaga o pagamento de cartão do cupom (duplicado); exige `valor_esperado`.
