#!/usr/bin/env bash
# VPS guncelleme betigi — kodu tazeler, veriye DOKUNMAZ.
# Kullanim (VPS'te, proje dizininde):  bash vps_guncelle.sh
set -euo pipefail

echo "[1/4] Kod guncelleniyor (git pull)..."
git pull --ff-only

echo "[2/4] Bagimliliklar denetleniyor..."
./venv/bin/pip install -r requirements.txt --quiet

echo "[3/4] Servisler yeniden baslatiliyor..."
sudo systemctl restart altin-panel
sudo systemctl restart altin-toplayici 2>/dev/null || true  # toplayici servisi yoksa sorun degil

# Model kodu degistiyse gecelik zinciri beklemeden ciktilari tazele.
# (data/ silinmez; parquet ve json'lar atomik olarak ustune yazilir,
#  canli.db ile tahmin_gunlugu.jsonl'a dokunulmaz.)
echo "[4/4] Analiz zinciri calistiriliyor (birkac dakika surebilir)..."
./venv/bin/python -m veri.indir
./venv/bin/python -m model.egit
./venv/bin/python -m model.senaryo
./venv/bin/python -m model.kisa_vade
./venv/bin/python -m model.volatilite
./venv/bin/python -m model.lig
./venv/bin/python -m model.saglik

echo "Tamam. Panel: http://$(hostname -I | awk '{print $1}'):8050"
