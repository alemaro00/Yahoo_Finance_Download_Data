from __future__ import annotations

import argparse
import time
from datetime import UTC, date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

TICKERS = ["JPY=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X", "EURCHF=X", "EURCAD=X", "EURUSD=X", "EURSEK=X", "EURHUF=X", "HKD=X"]
        #["BTC-USD", "ETH-USD", "USDT-USD", "XRP-USD", "BNB-USD", "BCH-USD", "DOGE-USD", "TRX-USD"]
TARGET_TIMES_UTC = [dtime(hour, 30) for hour in (13, 14, 15, 16, 17, 18, 19)]

INTRADAY_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m"}
SUPPORTED_INTERVALS = INTRADAY_INTERVALS | {"1d", "5d", "1wk", "1mo", "3mo"}
DEFAULT_INTERVAL = "60m"
INTRADAY_RETENTION_DAYS = 730
DEFAULT_NO_LIMIT_START_DAYS = 730
DEFAULT_WINDOW_DAYS = 730
MAX_RETRIES = 4
DATA_DIR = Path("data")
TARGET_TIMES_HHMM = {item.strftime("%H:%M") for item in TARGET_TIMES_UTC}
CLOSE_COLUMNS = [f"{symbol}_close" for symbol in TICKERS]
HIGH_COLUMNS = [f"{symbol}_high" for symbol in TICKERS]
LOW_COLUMNS = [f"{symbol}_low" for symbol in TICKERS]
VOLUME_COLUMNS = [f"{symbol}_volume" for symbol in TICKERS]
SUPPORTED_OUTPUT_FIELDS = ("close", "high", "low", "volume")
RETENTION_SAFETY_BUFFER_DAYS = 1
INTERVAL_RETENTION_DAYS: dict[str, int | None] = {
	"1m": 7,
	"2m": 60,
	"5m": 60,
	"15m": 60,
	"30m": 60,
	"60m": 730,
	"90m": 60,
	"1d": None,
	"5d": None,
	"1wk": None,
	"1mo": None,
	"3mo": None,
}


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


def parse_output_fields(value: str) -> tuple[str, ...]:
	fields = [item.strip().lower() for item in value.split(",") if item.strip()]
	if not fields:
		raise ValueError("fields non puo essere vuoto. Usa almeno un campo.")

	invalid = sorted(set(fields) - set(SUPPORTED_OUTPUT_FIELDS))
	if invalid:
		raise ValueError(
			f"Campi non validi: {', '.join(invalid)}. Valori ammessi: {', '.join(SUPPORTED_OUTPUT_FIELDS)}"
		)

	seen: set[str] = set()
	ordered_unique: list[str] = []
	for field in fields:
		if field not in seen:
			seen.add(field)
			ordered_unique.append(field)

	return tuple(ordered_unique)


def select_output_fields(fields_arg: tuple[str, ...] | None) -> tuple[str, ...]:
	if fields_arg is not None:
		return fields_arg

	default_fields = ",".join(SUPPORTED_OUTPUT_FIELDS)
	try:
		raw = input(
			f"Campi output [{'/'.join(SUPPORTED_OUTPUT_FIELDS)}] separati da virgola "
			f"(invio={default_fields}): "
		).strip()
	except EOFError:
		raw = ""

	if not raw:
		return SUPPORTED_OUTPUT_FIELDS

	return parse_output_fields(raw)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Download Yahoo multi-finestra con output Pre_clean/Post_clean in UTC")
	parser.add_argument(
		"--interval",
		type=str,
		default=None,
		help=f"Intervallo Yahoo. Valori: {', '.join(sorted(SUPPORTED_INTERVALS))}",
	)
	parser.add_argument("--start-date", type=parse_date_arg, default=None, help="Data inizio (YYYY-MM-DD)")
	parser.add_argument("--end-date", type=parse_date_arg, default=None, help="Data fine (YYYY-MM-DD)")
	parser.add_argument(
		"--window-days",
		type=int,
		default=DEFAULT_WINDOW_DAYS,
		help="Dimensione finestra per download annidato (es. 730)",
	)
	parser.add_argument(
		"--fields",
		type=str,
		default=None,
		help=(
			"Campi output separati da virgola. "
			f"Valori: {', '.join(SUPPORTED_OUTPUT_FIELDS)}. "
			"Se omesso, li chiede in modo interattivo."
		),
	)

	args = parser.parse_args()
	if args.window_days < 1:
		parser.error("window-days deve essere >= 1")

	if args.fields is not None:
		try:
			args.fields = parse_output_fields(args.fields)
		except ValueError as exc:
			parser.error(str(exc))

	return args


def normalize_interval(interval: str) -> str:
	value = interval.strip().lower()
	if value not in SUPPORTED_INTERVALS:
		raise ValueError(f"Intervallo non valido: {interval}. Valori ammessi: {', '.join(sorted(SUPPORTED_INTERVALS))}")
	return value


def is_intraday_interval(interval: str) -> bool:
	return interval in INTRADAY_INTERVALS


def get_interval_retention_days(interval: str) -> int | None:
	return INTERVAL_RETENTION_DAYS.get(interval)


def clamp_start_for_retention(start_date: date, interval: str) -> tuple[date, date | None]:
	retention_days = get_interval_retention_days(interval)
	if retention_days is None:
		return start_date, None

	safe_window_days = max(retention_days - RETENTION_SAFETY_BUFFER_DAYS, 1)
	safe_floor = datetime.now(UTC).date() - timedelta(days=safe_window_days - 1)
	if start_date < safe_floor:
		return safe_floor, safe_floor

	return start_date, None


def select_interval(interval_arg: str | None) -> str:
	if interval_arg is not None:
		return normalize_interval(interval_arg)

	try:
		raw = input(
			f"Intervallo [{'/'.join(sorted(SUPPORTED_INTERVALS))}] (invio={DEFAULT_INTERVAL}): "
		).strip()
	except EOFError:
		raw = ""

	if not raw:
		return DEFAULT_INTERVAL

	return normalize_interval(raw)


def select_date_range(start_arg: date | None, end_arg: date | None, interval: str) -> tuple[date, date]:
	today_utc = datetime.now(UTC).date()
	retention_days = get_interval_retention_days(interval)
	allowed_floor = today_utc - timedelta(days=retention_days) if retention_days is not None else None
	default_start = allowed_floor if allowed_floor is not None else today_utc - timedelta(days=DEFAULT_NO_LIMIT_START_DAYS)
	default_end = today_utc

	if start_arg is None and end_arg is None:
		print(f"Se non inserisci nulla, uso default: {default_start} -> {default_end} (UTC)")
		try:
			start_raw = input(f"Data start [YYYY-MM-DD] (invio={default_start}): ").strip()
			end_raw = input(f"Data end   [YYYY-MM-DD] (invio={default_end}): ").strip()
		except EOFError:
			start_raw, end_raw = "", ""

		start_date = parse_date(start_raw) if start_raw else default_start
		end_date = parse_date(end_raw) if end_raw else default_end
	elif start_arg is not None and end_arg is not None:
		start_date, end_date = start_arg, end_arg
	else:
		raise ValueError("Passa sia --start-date che --end-date, oppure nessuno dei due.")

	if start_date > end_date:
		raise ValueError("La data start deve essere <= data end.")

	if end_date > today_utc:
		raise ValueError(f"La data end non puo essere nel futuro (max: {today_utc}).")

	if allowed_floor is not None and start_date < allowed_floor:
		raise ValueError(
			f"Intervallo non valido per {interval}: scegli date tra {allowed_floor} e {today_utc} (UTC)."
		)

	return start_date, end_date


def iter_windows(start_date: date, end_date: date, window_days: int):
	window_start = start_date
	while window_start <= end_date:
		window_end = min(window_start + timedelta(days=window_days - 1), end_date)
		yield window_start, window_end
		window_start = window_end + timedelta(days=1)


def extract_field_from_download(raw: pd.DataFrame, field_name: str) -> pd.DataFrame:
	if raw.empty:
		return pd.DataFrame(columns=TICKERS)

	if isinstance(raw.columns, pd.MultiIndex):
		if field_name not in raw.columns.get_level_values(0):
			return pd.DataFrame(columns=TICKERS)
		field_df = raw[field_name]
	else:
		if field_name not in raw.columns:
			return pd.DataFrame(columns=TICKERS)
		field_df = raw[[field_name]]

	if isinstance(field_df, pd.Series):
		field_df = field_df.to_frame(name=TICKERS[0])
	if field_name in field_df.columns:
		field_df = field_df.rename(columns={field_name: TICKERS[0]})

	return field_df


def merge_symbol_chunks(chunks: list[pd.DataFrame]) -> pd.DataFrame:
	if not chunks:
		return pd.DataFrame(columns=TICKERS)

	merged = pd.concat(chunks)
	merged = merged[~merged.index.duplicated(keep="last")].sort_index()

	for symbol in TICKERS:
		if symbol not in merged.columns:
			merged[symbol] = pd.NA

	return merged[TICKERS]


def fetch_ohlcv_window_with_retry(
	window_start: date,
	window_end: date,
	interval: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
	end_exclusive = window_end + timedelta(days=1)
	tickers_string = " ".join(TICKERS)

	for attempt in range(1, MAX_RETRIES + 1):
		try:
			raw = yf.download(
				tickers=tickers_string,
				start=window_start,
				end=end_exclusive,
				interval=interval,
				auto_adjust=False,
				actions=False,
				group_by="column",
				threads=True,
				progress=False,
			)

			close = extract_field_from_download(raw, "Close")
			high = extract_field_from_download(raw, "High")
			low = extract_field_from_download(raw, "Low")
			volume = extract_field_from_download(raw, "Volume")
			if close.empty and high.empty and low.empty and volume.empty:
				print(f"Nessun dato per la finestra {window_start} -> {window_end}, salto.")
				return (
					pd.DataFrame(columns=TICKERS),
					pd.DataFrame(columns=TICKERS),
					pd.DataFrame(columns=TICKERS),
					pd.DataFrame(columns=TICKERS),
				)

			return close, high, low, volume
		except Exception as exc:
			if attempt == MAX_RETRIES:
				print(f"Finestra {window_start} -> {window_end} fallita: {exc}")
				return (
					pd.DataFrame(columns=TICKERS),
					pd.DataFrame(columns=TICKERS),
					pd.DataFrame(columns=TICKERS),
					pd.DataFrame(columns=TICKERS),
				)

			wait_seconds = 2**attempt
			print(
				f"Errore temporaneo Yahoo su {window_start} -> {window_end}: {exc}. "
				f"Ritento tra {wait_seconds}s (tentativo {attempt}/{MAX_RETRIES})."
			)
			time.sleep(wait_seconds)

	return (
		pd.DataFrame(columns=TICKERS),
		pd.DataFrame(columns=TICKERS),
		pd.DataFrame(columns=TICKERS),
		pd.DataFrame(columns=TICKERS),
	)


def fetch_all_ohlcv_chunked(
	start_date: date,
	end_date: date,
	window_days: int,
	interval: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
	windows = list(iter_windows(start_date, end_date, window_days))
	close_chunks: list[pd.DataFrame] = []
	high_chunks: list[pd.DataFrame] = []
	low_chunks: list[pd.DataFrame] = []
	volume_chunks: list[pd.DataFrame] = []

	for idx, (window_start, window_end) in enumerate(windows, start=1):
		print(f"Scarico finestra {idx}/{len(windows)}: {window_start} -> {window_end}")
		close_chunk, high_chunk, low_chunk, volume_chunk = fetch_ohlcv_window_with_retry(window_start, window_end, interval)
		if not close_chunk.empty:
			close_chunks.append(close_chunk)
		if not high_chunk.empty:
			high_chunks.append(high_chunk)
		if not low_chunk.empty:
			low_chunks.append(low_chunk)
		if not volume_chunk.empty:
			volume_chunks.append(volume_chunk)

	return (
		merge_symbol_chunks(close_chunks),
		merge_symbol_chunks(high_chunks),
		merge_symbol_chunks(low_chunks),
		merge_symbol_chunks(volume_chunks),
	)


def normalize_symbol_series(series: pd.Series, interval: str, target_index: pd.DatetimeIndex | None = None) -> pd.Series:
	clean = series.dropna().copy()
	if clean.empty:
		if target_index is None:
			return pd.Series(dtype="float64")
		return pd.Series(index=target_index, dtype="float64")

	if not is_intraday_interval(interval):
		idx = pd.DatetimeIndex(clean.index)
		idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
		clean.index = idx
	else:
		if clean.index.tz is None:
			clean.index = clean.index.tz_localize("UTC")
		else:
			clean.index = clean.index.tz_convert("UTC")

		minute_mode = int(clean.index.minute.to_series().mode().iloc[0])
		if interval == "60m" and minute_mode == 0:
			clean.index = clean.index + pd.Timedelta(minutes=30)

		if is_intraday_interval(interval):
			clean = clean[clean.index.strftime("%H:%M").isin(TARGET_TIMES_HHMM)]

	clean = clean[~clean.index.duplicated(keep="last")]
	if target_index is None:
		return clean.sort_index()

	return clean.reindex(target_index)


def build_intraday_target_index(start_date: date, end_date: date) -> pd.DatetimeIndex:
	return pd.DatetimeIndex(
		[
			pd.Timestamp(datetime.combine(day, target_time), tz="UTC")
			for day in pd.date_range(start_date, end_date, freq="B").date
			for target_time in TARGET_TIMES_UTC
		],
		name="timestamp_utc",
	)


def build_union_index(series_by_symbol: dict[str, pd.Series]) -> pd.DatetimeIndex:
	all_indexes = [series.index for series in series_by_symbol.values() if not series.empty]
	if not all_indexes:
		return pd.DatetimeIndex([], name="timestamp_utc", tz="UTC")

	union = all_indexes[0]
	for idx in all_indexes[1:]:
		union = union.union(idx)

	return pd.DatetimeIndex(union.sort_values(), name="timestamp_utc")


def build_output_paths(start_date: date, end_date: date) -> tuple[Path, Path]:
	range_suffix = f"{start_date.isoformat()}_{end_date.isoformat()}"
	pre_file = DATA_DIR / f"yahoo_prices_{range_suffix}.Pre_clean.txt"
	post_file = DATA_DIR / f"yahoo_prices_{range_suffix}.Post_clean.txt"
	report_file = DATA_DIR / f"yahoo_prices_{range_suffix}.Timezone_sync_report.txt"
	return pre_file, post_file, report_file


def detect_source_timezone(series: pd.Series) -> str:
	clean = series.dropna()
	if clean.empty:
		return "no_data"

	idx = pd.DatetimeIndex(clean.index)
	return str(idx.tz) if idx.tz is not None else "naive_assumed_UTC"


def format_ts(ts: pd.Timestamp | None) -> str:
	if ts is None or pd.isna(ts):
		return "-"
	return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def render_timezone_sync_report(
	interval: str,
	requested_start: date,
	requested_end: date,
	effective_start: date,
	symbol_rows: list[dict[str, str | int]],
	pre_rows: int,
	post_rows: int,
) -> str:
	report_df = pd.DataFrame(symbol_rows)
	report_table = report_df.to_string(index=False)
	status = "SYNC_OK" if post_rows > 0 else "NO_COMMON_TIMESTAMPS"
	return (
		"# Timezone_and_Synchronization_Report\n"
		f"interval: {interval}\n"
		f"requested_range: {requested_start} -> {requested_end}\n"
		f"effective_download_start: {effective_start}\n"
		f"pre_clean_rows: {pre_rows}\n"
		f"post_clean_rows: {post_rows}\n"
		f"strict_sync_status: {status}\n"
		"\n"
		"# Per_ticker_timezone_and_coverage\n"
		f"{report_table}\n"
	)


def render_table_text(df: pd.DataFrame, title: str) -> str:
	body = df.to_string(index=False, na_rep="NaN", float_format=lambda x: f"{x:.6f}")
	return f"# {title}\nrows: {len(df)}\n\n{body}\n"


def main() -> None:
	args = parse_args()
	try:
		interval = select_interval(args.interval)
	except ValueError as exc:
		print(f"Errore: {exc}")
		raise SystemExit(1)

	try:
		selected_fields = select_output_fields(args.fields)
	except ValueError as exc:
		print(f"Errore: {exc}")
		raise SystemExit(1)

	try:
		start_date, end_date = select_date_range(args.start_date, args.end_date, interval)
	except ValueError as exc:
		print(f"Errore: {exc}")
		raise SystemExit(1)

	target_index = build_intraday_target_index(start_date, end_date) if is_intraday_interval(interval) else None

	effective_start, clipped_floor = clamp_start_for_retention(start_date, interval)
	if clipped_floor is not None:
		print(
			f"Nota: per {interval} Yahoo usa una finestra effettiva leggermente piu stretta. "
			f"Inizio download effettivo: {clipped_floor}"
		)

	if effective_start > end_date:
		close = pd.DataFrame(columns=TICKERS)
		high = pd.DataFrame(columns=TICKERS)
		low = pd.DataFrame(columns=TICKERS)
		volume = pd.DataFrame(columns=TICKERS)
	else:
		close, high, low, volume = fetch_all_ohlcv_chunked(effective_start, end_date, args.window_days, interval)

	normalized_close_by_symbol: dict[str, pd.Series] = {}
	normalized_high_by_symbol: dict[str, pd.Series] = {}
	normalized_low_by_symbol: dict[str, pd.Series] = {}
	normalized_volume_by_symbol: dict[str, pd.Series] = {}
	symbol_rows: list[dict[str, str | int]] = []
	for symbol in TICKERS:
		raw_close = close[symbol] if symbol in close.columns else pd.Series(dtype="float64")
		raw_high = high[symbol] if symbol in high.columns else pd.Series(dtype="float64")
		raw_low = low[symbol] if symbol in low.columns else pd.Series(dtype="float64")
		raw_volume = volume[symbol] if symbol in volume.columns else pd.Series(dtype="float64")

		normalized_close = normalize_symbol_series(raw_close, interval, target_index)
		normalized_high = normalize_symbol_series(raw_high, interval, target_index)
		normalized_low = normalize_symbol_series(raw_low, interval, target_index)
		normalized_volume = normalize_symbol_series(raw_volume, interval, target_index)
		normalized_close_by_symbol[symbol] = normalized_close
		normalized_high_by_symbol[symbol] = normalized_high
		normalized_low_by_symbol[symbol] = normalized_low
		normalized_volume_by_symbol[symbol] = normalized_volume

		available_close = normalized_close.dropna()
		available_high = normalized_high.dropna()
		available_low = normalized_low.dropna()
		available_volume = normalized_volume.dropna()
		symbol_rows.append(
			{
				"ticker": symbol,
				"source_tz_close": detect_source_timezone(raw_close),
				"source_tz_high": detect_source_timezone(raw_high),
				"source_tz_low": detect_source_timezone(raw_low),
				"source_tz_volume": detect_source_timezone(raw_volume),
				"rows_close": int(len(available_close)),
				"first_utc_close": format_ts(available_close.index.min() if not available_close.empty else None),
				"last_utc_close": format_ts(available_close.index.max() if not available_close.empty else None),
				"rows_high": int(len(available_high)),
				"first_utc_high": format_ts(available_high.index.min() if not available_high.empty else None),
				"last_utc_high": format_ts(available_high.index.max() if not available_high.empty else None),
				"rows_low": int(len(available_low)),
				"first_utc_low": format_ts(available_low.index.min() if not available_low.empty else None),
				"last_utc_low": format_ts(available_low.index.max() if not available_low.empty else None),
				"rows_volume": int(len(available_volume)),
				"first_utc_volume": format_ts(available_volume.index.min() if not available_volume.empty else None),
				"last_utc_volume": format_ts(available_volume.index.max() if not available_volume.empty else None),
			}
		)

	if target_index is None:
		combined_series: dict[str, pd.Series] = {}
		for symbol in TICKERS:
			combined_series[f"{symbol}_close"] = normalized_close_by_symbol[symbol]
			combined_series[f"{symbol}_high"] = normalized_high_by_symbol[symbol]
			combined_series[f"{symbol}_low"] = normalized_low_by_symbol[symbol]
			combined_series[f"{symbol}_volume"] = normalized_volume_by_symbol[symbol]
		target_index = build_union_index(combined_series)

	output = pd.DataFrame(index=target_index)

	for symbol in TICKERS:
		output[f"{symbol}_close"] = normalized_close_by_symbol[symbol].reindex(target_index)
		output[f"{symbol}_high"] = normalized_high_by_symbol[symbol].reindex(target_index)
		output[f"{symbol}_low"] = normalized_low_by_symbol[symbol].reindex(target_index)
		output[f"{symbol}_volume"] = normalized_volume_by_symbol[symbol].reindex(target_index)

	DATA_DIR.mkdir(parents=True, exist_ok=True)
	pre_clean_file, post_clean_file, report_file = build_output_paths(start_date, end_date)

	output_to_save = output.reset_index()
	output_to_save = output_to_save.rename(columns={"timestamp_utc": "datetime_utc"})
	output_to_save["datetime_utc"] = output_to_save["datetime_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
	price_columns = CLOSE_COLUMNS + HIGH_COLUMNS + LOW_COLUMNS
	output_to_save[price_columns] = output_to_save[price_columns].round(6)
	output_to_save[VOLUME_COLUMNS] = output_to_save[VOLUME_COLUMNS].round(0)
	field_columns_map = {
		"close": CLOSE_COLUMNS,
		"high": HIGH_COLUMNS,
		"low": LOW_COLUMNS,
		"volume": VOLUME_COLUMNS,
	}
	selected_columns: list[str] = []
	for field in selected_fields:
		selected_columns.extend(field_columns_map[field])
	output_to_save = output_to_save[["datetime_utc", *selected_columns]]
	required_columns = selected_columns
	output_to_save = output_to_save.dropna(subset=required_columns, how="all")

	pre_clean_df = output_to_save.copy()
	post_clean_df = pre_clean_df.dropna(subset=required_columns, how="any")
	removed_rows = len(pre_clean_df) - len(post_clean_df)

	pre_clean_file.write_text(render_table_text(pre_clean_df, "Pre_clean"), encoding="utf-8")
	post_clean_file.write_text(render_table_text(post_clean_df, "Post_clean"), encoding="utf-8")
	report_file.write_text(
		render_timezone_sync_report(
			interval=interval,
			requested_start=start_date,
			requested_end=end_date,
			effective_start=effective_start,
			symbol_rows=symbol_rows,
			pre_rows=len(pre_clean_df),
			post_rows=len(post_clean_df),
		),
		encoding="utf-8",
	)

	print(f"Righe Pre_clean: {len(pre_clean_df)}")
	print(f"Righe Post_clean: {len(post_clean_df)}")
	print(f"Righe eliminate: {removed_rows}")
	print(f"Campi dataset: {', '.join(selected_fields)}")

	print(f"Pre_clean salvato in: {pre_clean_file}")
	print(f"Post_clean salvato in: {post_clean_file}")
	print(f"Timezone report salvato in: {report_file}")


if __name__ == "__main__":
	main()
