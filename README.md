
Script Python per scaricare prezzi storici da Yahoo Finance e salvarli in due file `.txt` tabellari.
Quando esegui `src/main.py`, lo script genera 2 file nella cartella `data/`:

1. `...Pre_clean.txt`
- Contiene i prezzi dei ticker selezionati nella variabile `TICKERS` (nel blocco iniziale di `src/main.py`, in pratica la riga dove compare `TICKERS = [...]`, tipicamente intorno alla riga 11):
  ```python
  TICKERS = ["SPY", "QQQ", "GLD", "AAPL", "MSFT", "NVDA", "EURUSD=X", "GBPUSD=X", "GC=F", "CL=F"]
  ```

2. `...Post_clean.txt`
- Stessi dati del `Pre_clean`, ma elimina ogni riga che contiene almeno un `NaN`.
- Risultato: dataset gia pulito, senza righe incomplete.

## Orari E Formato Date
- Tutti gli orari sono in **UTC**.
- I prezzi sono ordinati per fascia oraria dalle **13:30** alle **19:30**.
- Formato datetime usato nei file:
  `YYYY-MM-DDTHH:MM:SSZ`
  (esempio: `2024-05-03T13:30:00Z`).

## Esecuzione Rapida (con `uv`)
1. Crea ambiente virtuale:
   ```powershell
   uv venv .venv
   ```
2. Installa dipendenze:
   ```powershell
   uv pip install --python .venv\Scripts\python.exe -r requirements.txt
   ```
3. Esegui script:
   ```powershell
   .venv\Scripts\python.exe src\main.py
   ```

## Parametri Opzionali
Puoi scegliere intervallo date e dimensione finestra:

```powershell
.venv\Scripts\python.exe src\main.py --start-date 2024-04-29 --end-date 2026-04-29 --window-days 730
```

- `--start-date`: data inizio (`YYYY-MM-DD`)
- `--end-date`: data fine (`YYYY-MM-DD`)
- `--window-days`: ampiezza finestra di download
