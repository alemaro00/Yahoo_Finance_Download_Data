from __future__ import annotations

import argparse
import time
from datetime import UTC, date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

TICKERS = ["SPY", "QQQ", "GLD", "AAPL", "MSFT", "NVDA", "EURUSD=X", "GBPUSD=X", "GC=F", "CL=F"]
TARGET_TIMES_UTC = [dtime(hour, 30) for hour in (13, 14, 15, 16, 17, 18, 19)]

INTERVAL = "60m"
INTRADAY_RETENTION_DAYS = 730
DEFAULT_WINDOW_DAYS = 730
MAX_RETRIES = 4
DATA_DIR = Path("data")
TARGET_TIMES_HHMM = {item.strftime("%H:%M") for item in TARGET_TIMES_UTC}


def parse_date(value: str) -> date:
	try:
		return date.fromisoformat(value)
	except ValueError as exc:
		raise ValueError(f"Data non valida: {value}. Usa YYYY-MM-DD") from exc


def parse_date_arg(value: str) -> date:
	try:
		return parse_date(value)
	except ValueError as exc:
		raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Download Yahoo multi-finestra con output Pre_clean/Post_clean in UTC")
	parser.add_argument("--start-date", type=parse_date_arg, default=None, help="Data inizio (YYYY-MM-DD)")
	parser.add_argument("--end-date", type=parse_date_arg, default=None, help="Data fine (YYYY-MM-DD)")
	parser.add_argument(
		"--window-days",
		type=int,
		default=DEFAULT_WINDOW_DAYS,
		help="Dimensione finestra per download annidato (es. 730)",
	)

	args = parser.parse_args()
	if args.window_days < 1:
		parser.error("window-days deve essere >= 1")

	return args


def select_date_range(start_arg: date | None, end_arg: date | None) -> tuple[date, date]:
	today_utc = datetime.now(UTC).date()
	default_start = today_utc - timedelta(days=INTRADAY_RETENTION_DAYS)
	default_end = today_utc

	if start_arg is None and end_arg is None:
		print(f"Se non inserisci nulla, uso default: {default_start} -> {default_end} (UTC)")
		start_raw = input(f"Data start [YYYY-MM-DD] (invio={default_start}): ").strip()
		end_raw = input(f"Data end   [YYYY-MM-DD] (invio={default_end}): ").strip()

		if not start_raw and not end_raw:
			start_date, end_date = default_start, default_end
		elif start_raw and end_raw:
			start_date, end_date = parse_date(start_raw), parse_date(end_raw)
		else:
			raise ValueError("Inserisci entrambe le date oppure lascia entrambe vuote.")
	elif start_arg is not None and end_arg is not None:
		start_date, end_date = start_arg, end_arg
	else:
		raise ValueError("Passa sia --start-date che --end-date, oppure nessuno dei due.")

	if start_date > end_date:
		raise ValueError("La data start deve essere <= data end.")

	if start_date < default_start or end_date > default_end:
		raise ValueError(
			f"Intervallo non valido: scegli date tra {default_start} e {default_end} (UTC)."
		)

	return start_date, end_date


def iter_windows(start_date: date, end_date: date, window_days: int):
	window_start = start_date
	while window_start <= end_date:
		window_end = min(window_start + timedelta(days=window_days - 1), end_date)
		yield window_start, window_end
		window_start = window_end + timedelta(days=1)


def extract_close_from_download(raw: pd.DataFrame) -> pd.DataFrame:
	if raw.empty:
		return pd.DataFrame(columns=TICKERS)

	if isinstance(raw.columns, pd.MultiIndex):
		if "Close" not in raw.columns.get_level_values(0):
			return pd.DataFrame(columns=TICKERS)
		close = raw["Close"]
	else:
		if "Close" not in raw.columns:
			return pd.DataFrame(columns=TICKERS)
		close = raw[["Close"]]

	if isinstance(close, pd.Series):
		close = close.to_frame(name=TICKERS[0])
	if "Close" in close.columns:
		close = close.rename(columns={"Close": TICKERS[0]})

	return close


def fetch_close_window_with_retry(window_start: date, window_end: date) -> pd.DataFrame:
	end_exclusive = window_end + timedelta(days=1)
	tickers_string = " ".join(TICKERS)

	for attempt in range(1, MAX_RETRIES + 1):
		try:
			raw = yf.download(
				tickers=tickers_string,
				start=window_start,
				end=end_exclusive,
				interval=INTERVAL,
				auto_adjust=False,
				actions=False,
				group_by="column",
				threads=True,
				progress=False,
			)

			close = extract_close_from_download(raw)
			if close.empty:
				print(f"Nessun dato per la finestra {window_start} -> {window_end}, salto.")
				return pd.DataFrame(columns=TICKERS)

			return close
		except Exception as exc:
			if attempt == MAX_RETRIES:
				print(f"Finestra {window_start} -> {window_end} fallita: {exc}")
				return pd.DataFrame(columns=TICKERS)

			wait_seconds = 2**attempt
			print(
				f"Errore temporaneo Yahoo su {window_start} -> {window_end}: {exc}. "
				f"Ritento tra {wait_seconds}s (tentativo {attempt}/{MAX_RETRIES})."
			)
			time.sleep(wait_seconds)

	return pd.DataFrame(columns=TICKERS)


def fetch_all_close_chunked(start_date: date, end_date: date, window_days: int) -> pd.DataFrame:
	windows = list(iter_windows(start_date, end_date, window_days))
	chunks: list[pd.DataFrame] = []

	for idx, (window_start, window_end) in enumerate(windows, start=1):
		print(f"Scarico finestra {idx}/{len(windows)}: {window_start} -> {window_end}")
		chunk = fetch_close_window_with_retry(window_start, window_end)
		if not chunk.empty:
			chunks.append(chunk)

	if not chunks:
		return pd.DataFrame(columns=TICKERS)

	merged = pd.concat(chunks)
	merged = merged[~merged.index.duplicated(keep="last")].sort_index()

	for symbol in TICKERS:
		if symbol not in merged.columns:
			merged[symbol] = pd.NA

	return merged[TICKERS]


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


def build_target_index(start_date: date, end_date: date) -> pd.DatetimeIndex:
	return pd.DatetimeIndex(
		[
			pd.Timestamp(datetime.combine(day, target_time), tz="UTC")
			for day in pd.date_range(start_date, end_date, freq="B").date
			for target_time in TARGET_TIMES_UTC
		],
		name="timestamp_utc",
	)


def build_output_paths(start_date: date, end_date: date) -> tuple[Path, Path]:
	range_suffix = f"{start_date.isoformat()}_{end_date.isoformat()}"
	pre_file = DATA_DIR / f"yahoo_prices_{range_suffix}.Pre_clean.txt"
	post_file = DATA_DIR / f"yahoo_prices_{range_suffix}.Post_clean.txt"
	return pre_file, post_file


def render_table_text(df: pd.DataFrame, title: str) -> str:
	body = df.to_string(index=False, na_rep="NaN", float_format=lambda x: f"{x:.6f}")
	return f"# {title}\nrows: {len(df)}\n\n{body}\n"


def main() -> None:
	args = parse_args()
	try:
		start_date, end_date = select_date_range(args.start_date, args.end_date)
	except ValueError as exc:
		print(f"Errore: {exc}")
		raise SystemExit(1)

	target_index = build_target_index(start_date, end_date)
	output = pd.DataFrame(index=target_index)
	effective_start = start_date
	if INTERVAL.endswith("m") or INTERVAL.endswith("h"):
		# Yahoo often rejects the exact 730-day boundary for intraday; use a safe floor.
		safe_retention_floor = datetime.now(UTC).date() - timedelta(days=INTRADAY_RETENTION_DAYS - 1)
		if effective_start < safe_retention_floor:
			print(
				f"Nota: per {INTERVAL} Yahoo usa una finestra effettiva leggermente piu stretta. "
				f"Inizio download effettivo: {safe_retention_floor}"
			)
			effective_start = safe_retention_floor

	if effective_start > end_date:
		close = pd.DataFrame(columns=TICKERS)
	else:
		close = fetch_all_close_chunked(effective_start, end_date, args.window_days)

	for symbol in TICKERS:
		series = close[symbol] if symbol in close.columns else pd.Series(dtype="float64")
		output[symbol] = normalize_symbol_close(series, target_index)

	DATA_DIR.mkdir(parents=True, exist_ok=True)
	pre_clean_file, post_clean_file = build_output_paths(start_date, end_date)

	output_to_save = output.reset_index()
	output_to_save = output_to_save.rename(columns={"timestamp_utc": "datetime_utc"})
	output_to_save["datetime_utc"] = output_to_save["datetime_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
	output_to_save[TICKERS] = output_to_save[TICKERS].round(6)
	output_to_save = output_to_save.dropna(subset=TICKERS, how="all")

	pre_clean_df = output_to_save.copy()
	post_clean_df = pre_clean_df.dropna(subset=TICKERS, how="any")

	pre_clean_file.write_text(render_table_text(pre_clean_df, "Pre_clean"), encoding="utf-8")
	post_clean_file.write_text(render_table_text(post_clean_df, "Post_clean"), encoding="utf-8")

	print(f"Pre_clean salvato in: {pre_clean_file}")
	print(f"Post_clean salvato in: {post_clean_file}")


if __name__ == "__main__":
	main()
