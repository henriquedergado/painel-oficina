import hmac
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Faturamento Asaas", layout="centered")

FUSO = ZoneInfo("America/Sao_Paulo")
DIAS_SEM = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
RECEBIDOS = ["RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"]
COLS = ["id", "customer", "installment", "billingType", "value", "status",
        "paymentDate", "clientPaymentDate", "confirmedDate", "dateCreated", "deleted"]


def agora():
    return datetime.now(FUSO)


def brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def segredo(nome, padrao=None):
    try:
        return st.secrets.get(nome, padrao)
    except Exception:
        return padrao


# ---------- acesso por senha ----------
def liberar_acesso():
    senha = segredo("APP_PASSWORD", "")
    if not senha:
        st.warning("Nenhuma senha configurada (segredo APP_PASSWORD): qualquer pessoa com o link vê estes dados.")
        return
    if st.session_state.get("autenticado"):
        return
    st.title("Faturamento Asaas")
    digitada = st.text_input("Senha", type="password")
    if digitada:
        if hmac.compare_digest(digitada, senha):
            st.session_state["autenticado"] = True
            st.rerun()
        else:
            st.error("Senha incorreta.")
    st.stop()


# ---------- API do Asaas ----------
@st.cache_resource
def sessao():
    chave = segredo("ASAAS_API_KEY")
    s = requests.Session()
    s.headers.update({"access_token": chave, "Content-Type": "application/json",
                      "User-Agent": "streamlit-faturamento"})
    return s


def base_url():
    return ("https://api-sandbox.asaas.com/v3" if segredo("ASAAS_SANDBOX", False)
            else "https://api.asaas.com/v3")


def req(method, path, **kw):
    ultimo = ""
    for t in range(6):
        try:
            r = sessao().request(method, base_url() + path, timeout=60, **kw)
        except requests.exceptions.RequestException as e:
            ultimo = str(e)[:200]
            time.sleep(3 * (t + 1))
            continue
        bloqueio_html = r.status_code in (403, 502, 503, 504) and "<html" in r.text[:300].lower()
        if r.status_code == 429 or bloqueio_html:
            ultimo = f"{r.status_code} (bloqueio/instabilidade antes da API do Asaas)"
            time.sleep(5 * (t + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError(f"Sem resposta da API após várias tentativas. Último erro: {ultimo}")


def listar(path, params=None):
    params = dict(params or {})
    params["limit"] = 100
    off, out = 0, []
    while True:
        params["offset"] = off
        d = req("GET", path, params=params)
        out += d["data"]
        if not d.get("hasMore"):
            break
        off += 100
    return out


@st.cache_data(ttl=60, show_spinner="Consultando o Asaas...")
def buscar_mes(d_ini: str, d_fim: str) -> pd.DataFrame:
    """Vendas do período pela DATA DA COMPRA (quando o cliente pagou), valor bruto.
    Todas as parcelas de um parcelamento caem na data da compra e contam como 1 venda."""
    pays = listar("/payments", {"dateCreated[ge]": d_ini, "dateCreated[le]": d_fim})    # geradas no período (cartão/Pix)
    pays += listar("/payments", {"paymentDate[ge]": d_ini, "paymentDate[le]": d_fim})   # criadas antes e pagas no período
    d = pd.DataFrame(pays).reindex(columns=COLS).drop_duplicates("id")
    d = d[d["deleted"].fillna(False).astype(str).str.lower() != "true"]
    d = d[d["status"].isin(RECEBIDOS)].copy()
    for c in ["paymentDate", "clientPaymentDate", "confirmedDate", "dateCreated"]:
        d[c] = pd.to_datetime(d[c], errors="coerce")
    d["data_ref"] = (d["clientPaymentDate"].fillna(d["confirmedDate"])
                     .fillna(d["paymentDate"]).fillna(d["dateCreated"]).dt.normalize())
    d = d.dropna(subset=["data_ref"])
    d["valor"] = d["value"].astype(float)
    # id da venda: o parcelamento; sem ele, junta as parcelas de cartão do mesmo cliente no mesmo dia
    chave_cartao = d["customer"].astype(str) + "|" + d["data_ref"].dt.strftime("%Y%m%d")
    alternativa = chave_cartao.where(d["billingType"] == "CREDIT_CARD", d["id"])
    d["venda_id"] = d["installment"].astype("object").where(d["installment"].notna(), alternativa)
    return d[(d["data_ref"] >= pd.Timestamp(d_ini)) & (d["data_ref"] <= pd.Timestamp(d_fim))]


# ---------- tela ----------
liberar_acesso()

if not segredo("ASAAS_API_KEY"):
    st.error("Falta o segredo ASAAS_API_KEY (no Streamlit Cloud: Settings > Secrets).")
    st.stop()

st.title("Faturamento em tempo real")
auto = st.sidebar.toggle("Atualizar sozinho a cada 5 minutos", value=False)


@st.fragment(run_every=300 if auto else None)
def painel():
    if st.button("Atualizar agora", type="primary"):
        st.cache_data.clear()

    h = agora().date()
    mes_ini = h.replace(day=1)
    try:
        d = buscar_mes(mes_ini.isoformat(), h.isoformat())
    except Exception as e:
        st.error(f"Erro ao consultar o Asaas: {str(e)[:400]}")
        return

    hoje_df = d[d["data_ref"] == pd.Timestamp(h)]
    dias = (h - mes_ini).days + 1
    tot_h, tot_m = hoje_df["valor"].sum(), d["valor"].sum()

    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric(f"Hoje ({h:%d/%m/%Y})", brl(tot_h))
        st.caption(f"{hoje_df['venda_id'].nunique()} vendas ({len(hoje_df)} parcelas/cobranças)")
    with c2:
        st.metric(f"Mês atual (dia 1 a {h:%d/%m})", brl(tot_m))
        st.caption(f"{d['venda_id'].nunique()} vendas ({len(d)} parcelas/cobranças)")
    with c3:
        st.metric("Média por dia no mês", brl(tot_m / dias))
        st.caption(f"{brl(tot_m)} ÷ {dias} dias · Previsão 30 dias: {brl((tot_m / dias) * 30)}")

    idx = pd.date_range(mes_ini, h, freq="D")
    agg = d.groupby("data_ref").agg(soma=("valor", "sum"), vendas=("venda_id", "nunique")).reindex(idx, fill_value=0)
    tab = pd.DataFrame({
        "Data": idx.strftime("%d/%m/%Y"),
        "Dia": [DIAS_SEM[x.weekday()] for x in idx],
        "Vendas": agg["vendas"].astype(int).values,
        "Valor": [brl(v) for v in agg["soma"].values],
    }).iloc[::-1]
    total = pd.DataFrame([{"Data": "TOTAL", "Dia": "", "Vendas": int(d["venda_id"].nunique()), "Valor": brl(tot_m)}])
    tab = pd.concat([tab, total], ignore_index=True)

    st.subheader("Vendas por dia no mês (pela data da compra)")
    st.dataframe(tab, hide_index=True)
    st.caption(f"Atualizado em {agora():%d/%m/%Y %H:%M:%S} · valor bruto · "
               "os dados ficam em cache por 1 minuto")


painel()
