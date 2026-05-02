from __future__ import annotations

import time
from datetime import UTC, date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

TICKERS = ["SPY", "QQQ", "GLD", "AAPL", "MSFT", "NVDA", "EURUSD=X", "GBPUSD=X", "GC=F", "CL=F"]
TARGET_TIMES_UTC = [dtime(hour, 30) for hour in (13, 14, 15, 16, 17, 18, 19)]

START_DATE = date(2024, 4, 29)
END_DATE = date(2026, 4, 29)
INTERVAL = "60m"
INTRADAY_RETENTION_DAYS = 730
MAX_RETRIES = 4
PRE_CLEAN_FILE = Path("data/yahoo_prices_2024-04-29_2026-04-29.Pre_clean.txt")
POST_CLEAN_FILE = Path("data/yahoo_prices_2024-04-29_2026-04-29.Post_clean.txt")
LEGACY_OUTPUT_FILES = [
	Path("data/yahoo_prices_2024-04-29_2026-04-29.csv"),
	Path("data/yahoo_prices_2024-04-29_2026-04-29.table.txt"),
]
TARGET_TIMES_HHMM = {item.strftime("%H:%M") for item in TARGET_TIMES_UTC}


def fetch_all_close_with_retry(start_date: date, end_date_inclusive: date) -> pd.DataFrame:
	end_exclusive = end_date_inclusive + timedelta(days=1)
	tickers_string = " ".join(TICKERS)

	for attempt in range(1, MAX_RETRIES + 1):
		try:
			raw = yf.download(
				tickers=tickers_string,
				start=start_date,
				end=end_exclusive,
				interval=INTERVAL,
				auto_adjust=False,
				actions=False,
				group_by="column",
				threads=True,
				progress=False,
			)

			if raw.empty:
				raise ValueError("download vuoto")

			close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
			if isinstance(close, pd.Series):
				close = close.to_frame(name=TICKERS[0])
			if "Close" in close.columns:
				close = close.rename(columns={"Close": TICKERS[0]})

			return close
		except Exception as exc:
			if attempt == MAX_RETRIES:
				print(f"Download Yahoo fallito: {exc}")
				return pd.DataFrame(columns=TICKERS)

			wait_seconds = 2**attempt
			print(
				f"Errore temporaneo Yahoo: {exc}. "
				f"Ritento tra {wait_seconds}s (tentativo {attempt}/{MAX_RETRIES})."
			)
			time.sleep(wait_seconds)

	return pd.DataFrame(columns=TICKERS)


def normalize_symbol_close(series: pd.Series, target_index: pd.DatetimeIndex) -> pd.Series:
	clean = series.dropna().copy()
	if clean.empty:
		return pd.Series(index=target_index, dtype="float64")

	if clean.index.tz is None:
		clean.index = clean.index.tz_localize("UTC")
	else:
		clean.index = clean.index.tz_convert("UTC")

	minute_mode = int(clean.index.minute.to_series().mode().iloc[0])
	if minute_mode == 0:
		clean.index = clean.index + pd.Timedelta(minutes=30)

	clean = clean[~clean.index.duplicated(keep="last")]
	clean = clean[clean.index.strftime("%H:%M").isin(TARGET_TIMES_HHMM)]
	return clean.reindex(target_index)


def main() -> None:
	target_index = pd.DatetimeIndex(
		[
			pd.Timestamp(datetime.combine(day, target_time), tz="UTC")
			for day in pd.date_range(START_DATE, END_DATE, freq="B").date
			for target_time in TARGET_TIMES_UTC
		],
		name="timestamp_utc",
	)
	output = pd.DataFrame(index=target_index)
	effective_start = max(
		START_DATE,
		datetime.now(UTC).date() - timedelta(days=INTRADAY_RETENTION_DAYS - 1),
	)

	if effective_start > START_DATE:
		print(
			f"Yahoo limita {INTERVAL} agli ultimi {INTRADAY_RETENTION_DAYS} giorni. "
			f"Inizio effettivo: {effective_start:%Y-%m-%d}."
		)

	close = fetch_all_close_with_retry(effective_start, END_DATE)

	for symbol in TICKERS:
		series = close[symbol] if symbol in close.columns else pd.Series(dtype="float64")
		output[symbol] = normalize_symbol_close(series, target_index)

	PRE_CLEAN_FILE.parent.mkdir(parents=True, exist_ok=True)
	for legacy_file in LEGACY_OUTPUT_FILES:
		if legacy_file.exists():
			legacy_file.unlink()

	output_to_save = output.reset_index()
	output_to_save.insert(0, "data", output_to_save["timestamp_utc"].dt.strftime("%Y-%m-%d"))
	output_to_save.insert(1, "ora_utc", output_to_save["timestamp_utc"].dt.strftime("%H:%M"))
	output_to_save = output_to_save.drop(columns=["timestamp_utc"])
	output_to_save[TICKERS] = output_to_save[TICKERS].round(6)
	output_to_save = output_to_save.dropna(subset=TICKERS, how="all")

	pre_clean_df = output_to_save.copy()
	pre_clean_df.insert(
		0,
		"datetime",
		pd.to_datetime(pre_clean_df["data"] + " " + pre_clean_df["ora_utc"], format="%Y-%m-%d %H:%M").dt.strftime("%Y-%m-%d %H:%M:%S"),
	)
	pre_clean_df = pre_clean_df.drop(columns=["data", "ora_utc"])
	post_clean_df = pre_clean_df.dropna(subset=TICKERS, how="any")

	pre_clean_text = pre_clean_df.to_string(index=False, na_rep="NaN", float_format=lambda x: f"{x:.6f}")
	post_clean_text = post_clean_df.to_string(index=False, na_rep="NaN", float_format=lambda x: f"{x:.6f}")

	PRE_CLEAN_FILE.write_text(f"# Pre_clean\nrows: {len(pre_clean_df)}\n\n{pre_clean_text}\n", encoding="utf-8")
	POST_CLEAN_FILE.write_text(f"# Post_clean\nrows: {len(post_clean_df)}\n\n{post_clean_text}\n", encoding="utf-8")

	print(f"Pre_clean salvato in: {PRE_CLEAN_FILE}")
	print(f"Post_clean salvato in: {POST_CLEAN_FILE}")


if __name__ == "__main__":
	main()
