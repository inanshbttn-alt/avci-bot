"""Yükselen Avcısı — KÂĞIT ÜSTÜ takip (gerçek emir YOK, MEXC anahtarı kullanmaz).
15 dakikada bir MEXC'deki tüm USDT çiftlerini tarar.

GİRİŞ (hepsi birden):
- 24 saatlik yükseliş %10 – %30
- 24 saatlik hacim >= 100K USDT
- alış-satış farkı <= %0,5
- fiyatın %1 yakınında hem alış hem satış tarafında >= 500 USDT emir
- son 1 saatte yükselişte (fiyat 1 saat önceki fiyatın üstünde)
- stablecoin / kaldıraçlı token (3L, 3S...) değil
- aynı coinden son 24 saatte çıkılmamış
En çok 2 pozisyon x 50 USDT; birden fazla aday varsa hacmi en yüksek olan.

ÇIKIŞ (hangisi önce): +%10 kâr al / -%3 zarar kes / 24 saat doldu.
15 dk mumun en yüksek/en düşük fiyatına bakılır; aynı mumda ikisi de görülürse ZARAR KES sayılır.
MALİYET: her yönde komisyon %0,1 + kayma %0,2.

DEĞERLENDİRME (4 hafta sonra, önceden sabit): >= 20 işlem, kasa artıda, aynı dönem BTC al-tut'tan iyi.
"""
import os
import re
import json
import time
from datetime import datetime, timezone
import requests

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = "avci_state.json"
API = "https://api.mexc.com/api/v3"

# ---- Kurallar (değiştirme: 4 hafta boyunca sabit) ----
SERMAYE, POZ_USDT, MAX_POZ = 100.0, 50.0, 2
DEG_MIN, DEG_MAX = 0.10, 0.30
HACIM_MIN = 100_000
SPREAD_MAX, DERIN_BANT, DERIN_MIN = 0.005, 0.01, 500
TP, SL, SURE_SA = 0.10, 0.03, 24
FEE, KAYMA = 0.001, 0.002
OZET_SAAT_UTC = 18                       # günlük özet ~21:00 TR
SAAT_MS = 3600 * 1000

STABLES = {"USDC", "FDUSD", "TUSD", "DAI", "USDE", "USDD", "USDP", "PYUSD", "BUSD",
           "EUR", "USD1", "RLUSD", "XAUT", "PAXG", "USDS", "USD0", "EURC", "USDQ"}
KALDIRAC = re.compile(r"\d+[LS]USDT$")


# ---------- yardımcılar ----------
def api(yol, params=None):
    for i in range(3):
        try:
            r = requests.get(API + yol, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


def tg(mesaj):
    print(mesaj)
    if not TG_TOKEN:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT, "text": mesaj[:4000]}, timeout=15)
    except Exception:
        pass


def simdi_ms():
    return int(time.time() * 1000)


def ad(sym):
    return sym[:-4]


def fmt(x):
    return f"{x:.6g}"


def mumlar15(sym, limit):
    k = api("/klines", {"symbol": sym, "interval": "15m", "limit": limit})
    if not k:
        return []
    return [{"t": int(m[0]), "o": float(m[1]), "h": float(m[2]),
             "l": float(m[3]), "c": float(m[4])} for m in k]


def yukle():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return None


def kaydet(st):
    json.dump(st, open(STATE_FILE, "w"), indent=1, ensure_ascii=False)


# ---------- çıkış ----------
def cikislar(st, fiyat):
    for sym, p in list(st["poz"].items()):
        sebep, px = None, None
        for m in mumlar15(sym, 100):
            if m["t"] <= p["giris_ts"]:          # giriş mumu ve öncesi sayılmaz
                continue
            if m["l"] <= p["sl"]:                # kötümser: önce zarar kes
                sebep, px = "🔴 ZARAR KES", min(p["sl"], m["o"])
                break
            if m["h"] >= p["tp"]:
                sebep, px = "🟢 KÂR AL", p["tp"]
                break
        if sebep is None and simdi_ms() - p["giris_ts"] >= SURE_SA * SAAT_MS:
            son = fiyat.get(sym)
            if son:
                sebep, px = "⏰ SÜRE DOLDU", son
        if sebep is None:
            continue

        gelir = p["adet"] * px * (1 - KAYMA) * (1 - FEE)
        kz = gelir - p["usdt"]
        st["nakit"] += gelir
        st["gecmis"].append({"coin": ad(sym), "giris": p["giris_px"], "cikis": px,
                             "kz": round(kz, 2), "yuzde": round(kz / p["usdt"] * 100, 2),
                             "sebep": sebep, "giris_ts": p["giris_ts"], "cikis_ts": simdi_ms()})
        st["son_cikis"][sym] = simdi_ms()
        del st["poz"][sym]
        sure = (simdi_ms() - p["giris_ts"]) / SAAT_MS
        tg(f"{sebep} — {ad(sym)}\n"
           f"Giriş {fmt(p['giris_px'])} → Çıkış {fmt(px)} ({sure:.1f} saat)\n"
           f"Sonuç: {kz:+.2f} USDT ({kz / p['usdt'] * 100:+.1f}%)\n"
           f"Nakit: {st['nakit']:.2f} USDT")


# ---------- giriş ----------
def tahta_uygun(sym):
    d = api("/depth", {"symbol": sym, "limit": 100})
    if not d or not d.get("bids") or not d.get("asks"):
        return None
    bids = [(float(a), float(b)) for a, b in d["bids"]]
    asks = [(float(a), float(b)) for a, b in d["asks"]]
    bid, ask = bids[0][0], asks[0][0]
    orta = (bid + ask) / 2
    spread = (ask - bid) / orta
    derin_al = sum(p * q for p, q in bids if p >= orta * (1 - DERIN_BANT))
    derin_sat = sum(p * q for p, q in asks if p <= orta * (1 + DERIN_BANT))
    if spread > SPREAD_MAX or derin_al < DERIN_MIN or derin_sat < DERIN_MIN:
        return None
    return {"ask": ask, "spread": spread, "derin": min(derin_al, derin_sat)}


def girisler(st, ticker):
    bos = MAX_POZ - len(st["poz"])
    if bos <= 0:
        return
    adaylar = []
    for t in ticker:
        s = t.get("symbol", "")
        if not s.endswith("USDT") or s in st["poz"]:
            continue
        if ad(s) in STABLES or KALDIRAC.search(s):
            continue
        if simdi_ms() - st["son_cikis"].get(s, 0) < SURE_SA * SAAT_MS:
            continue
        try:
            son = float(t["lastPrice"])
            acilis = float(t["openPrice"])
            hacim = float(t.get("quoteVolume") or 0)
        except Exception:
            continue
        if son <= 0 or acilis <= 0:
            continue
        deg = son / acilis - 1
        if DEG_MIN <= deg <= DEG_MAX and hacim >= HACIM_MIN:
            adaylar.append((hacim, s, deg))

    adaylar.sort(reverse=True)                   # hacmi yüksek olan önce
    for hacim, s, deg in adaylar:
        if bos <= 0 or st["nakit"] < 5:
            break
        k = mumlar15(s, 6)
        if len(k) < 5 or k[-1]["c"] <= k[-5]["c"]:   # son 1 saatte yükselişte değil
            continue
        tahta = tahta_uygun(s)
        if not tahta:
            continue
        usdt = min(POZ_USDT, st["nakit"])
        px = tahta["ask"] * (1 + KAYMA)
        adet = usdt * (1 - FEE) / px
        st["nakit"] -= usdt
        st["poz"][s] = {"giris_ts": simdi_ms(), "giris_px": px, "adet": adet, "usdt": usdt,
                        "tp": px * (1 + TP), "sl": px * (1 - SL)}
        bos -= 1
        tg(f"🛒 AL (kâğıt) — {ad(s)}\n"
           f"Fiyat {fmt(px)} | 24s {deg * 100:+.1f}% | hacim {hacim / 1000:.0f}K USDT\n"
           f"Fark %{tahta['spread'] * 100:.2f} | tahta {tahta['derin']:.0f} USDT\n"
           f"Kâr al {fmt(px * (1 + TP))} | Zarar kes {fmt(px * (1 - SL))} | en geç 24 saat\n"
           f"Tutar {usdt:.2f} USDT | kalan nakit {st['nakit']:.2f}")


# ---------- özet ----------
def kasa(st, fiyat):
    return st["nakit"] + sum(p["adet"] * fiyat.get(s, p["giris_px"]) for s, p in st["poz"].items())


def ozet(st, fiyat):
    k = kasa(st, fiyat)
    g = st["gecmis"]
    kazanan = sum(1 for x in g if x["kz"] > 0)
    btc = fiyat.get("BTCUSDT")
    btc_deg = (btc / st["btc_bas"] - 1) * 100 if btc and st.get("btc_bas") else 0.0
    gun = (simdi_ms() - st["basla_ts"]) / (24 * SAAT_MS)
    m = [f"📊 Avcı günlük özet ({gun:.0f}. gün / 28)",
         f"💼 Kasa {k:.2f} USDT → başlangıçtan {k - SERMAYE:+.2f} USDT ({(k / SERMAYE - 1) * 100:+.1f}%)",
         f"₿ Aynı dönem BTC al-tut: {btc_deg:+.1f}%",
         f"🔁 İşlem: {len(g)} (kazanan {kazanan}, kaybeden {len(g) - kazanan}) | hedef ≥20"]
    for s, p in st["poz"].items():
        son = fiyat.get(s, p["giris_px"])
        m.append(f"📦 {ad(s)}: giriş {fmt(p['giris_px'])}, şimdi {fmt(son)} "
                 f"({(son / p['giris_px'] - 1) * 100:+.1f}%)")
    if not st["poz"]:
        m.append("📦 Elde coin yok")
    tg("\n".join(m))


# ---------- ana ----------
def main():
    ticker = api("/ticker/24hr")
    if not ticker:
        print("MEXC verisi alınamadı")
        return
    fiyat = {}
    for t in ticker:
        try:
            fiyat[t["symbol"]] = float(t["lastPrice"])
        except Exception:
            pass

    st = yukle()
    if st is None:
        st = {"basla": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "basla_ts": simdi_ms(),
              "btc_bas": fiyat.get("BTCUSDT"), "nakit": SERMAYE, "poz": {}, "gecmis": [],
              "son_cikis": {}, "son_ozet": ""}
        tg("🚀 Yükselen Avcısı başladı (kâğıt üstü, 100 USDT)\n"
           "Giriş: 24s %10–30, hacim ≥100K, fark ≤%0,5, tahta ≥500 USDT, son 1 saat yükselişte\n"
           "Çıkış: +%10 / -%3 / 24 saat | 2 × 50 USDT | 15 dk'da bir kontrol")

    cikislar(st, fiyat)
    girisler(st, ticker)

    bugun = datetime.now(timezone.utc)
    if bugun.hour >= OZET_SAAT_UTC and st["son_ozet"] != bugun.strftime("%Y-%m-%d"):
        ozet(st, fiyat)
        st["son_ozet"] = bugun.strftime("%Y-%m-%d")

    kaydet(st)


if __name__ == "__main__":
    main()
