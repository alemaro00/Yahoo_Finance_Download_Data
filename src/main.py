from __future__ import annotations

import argparse
import time
from datetime import UTC, date, datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

TICKERS = ["JPY=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X", "EURCHF=X", "EURCAD=X", "EURUSD=X", "EURSEK=X", "EURHUF=X", "HKD=X"]
TARGET_TIMES_UTC = [dtime(hour, 30) for hour in (13, 14, 15, 16, 17, 18, 19)]
DAILY_REFERENCE_TIME_UTC = dtime(15, 30)

INTRADAY_INTERVALS = {"1m", "2m", "5m", "15m", "30m", "60m", "90m"}
SUPPORTED_INTERVALS = INTRADAY_INTERVALS | {"1d", "5d", "1wk", "1mo", "3mo"}
DEFAULT_INTERVAL = "60m"
INTRADAY_RETENTION_DAYS = 730
DEFAULT_NO_LIMIT_START_DAYS = 730
DEFAULT_WINDOW_DAYS = 730
MAX_RETRIES = 4
DATA_DIR = Path("data")
TARGET_TIMES_HHMM = {item.strftime("%H:%M") for item in TARGET_TIMES_UTC}
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

	args = parser.parse_args()
	if args.window_days < 1:
		parser.error("window-days deve essere >= 1")

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

	safe_floor = datetime.now(UTC).date() - timedelta(days=retention_days - 1)
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


def fetch_close_window_with_retry(window_start: date, window_end: date, interval: str) -> pd.DataFrame:
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


def fetch_all_close_chunked(start_date: date, end_date: date, window_days: int, interval: str) -> pd.DataFrame:
	windows = list(iter_windows(start_date, end_date, window_days))
	chunks: list[pd.DataFrame] = []

	for idx, (window_start, window_end) in enumerate(windows, start=1):
		print(f"Scarico finestra {idx}/{len(windows)}: {window_start} -> {window_end}")
		chunk = fetch_close_window_with_retry(window_start, window_end, interval)
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


def normalize_symbol_close(series: pd.Series, interval: str, target_index: pd.DatetimeIndex | None = None) -> pd.Series:
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


def build_daily_reference_index(start_date: date, end_date: date) -> pd.DatetimeIndex:
	return pd.DatetimeIndex(
		[
			pd.Timestamp(datetime.combine(day, DAILY_REFERENCE_TIME_UTC), tz="UTC")
			for day in pd.date_range(start_date, end_date, freq="B").date
		],
		name="timestamp_utc",
	)


def normalize_daily_bar_to_reference(series: pd.Series, target_index: pd.DatetimeIndex) -> pd.Series:
	if target_index.empty:
		return pd.Series(index=target_index, dtype="float64")

	clean = series.dropna().copy()
	if clean.empty:
		return pd.Series(index=target_index, dtype="float64")

	idx = pd.DatetimeIndex(clean.index)
	idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
	clean.index = pd.DatetimeIndex(
		[pd.Timestamp(datetime.combine(ts.date(), DAILY_REFERENCE_TIME_UTC), tz="UTC") for ts in idx],
		name="timestamp_utc",
	)
	clean = clean[~clean.index.duplicated(keep="last")]
	return clean.reindex(target_index)


def normalize_intraday_to_daily_reference(series: pd.Series, interval: str, target_index: pd.DatetimeIndex) -> pd.Series:
	if target_index.empty:
		return pd.Series(index=target_index, dtype="float64")

	clean = series.dropna().copy()
	if clean.empty:
		return pd.Series(index=target_index, dtype="float64")

	idx = pd.DatetimeIndex(clean.index)
	idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
	clean.index = idx

	minute_mode = int(clean.index.minute.to_series().mode().iloc[0])
	if interval == "60m" and minute_mode == 0:
		clean.index = clean.index + pd.Timedelta(minutes=30)

	selected_values: list[float] = []
	selected_index: list[pd.Timestamp] = []
	for target_ts in target_index:
		day_rows = clean[(clean.index.date == target_ts.date()) & (clean.index >= target_ts)]
		if day_rows.empty:
			continue
		selected_index.append(target_ts)
		selected_values.append(float(day_rows.iloc[0]))

	if not selected_values:
		return pd.Series(index=target_index, dtype="float64")

	result = pd.Series(
		selected_values,
		index=pd.DatetimeIndex(selected_index, name="timestamp_utc", tz="UTC"),
		dtype="float64",
	)
	result = result[~result.index.duplicated(keep="last")]
	return result.reindex(target_index)


def build_daily_1530_hybrid_dataset(
	start_date: date,
	end_date: date,
	window_days: int,
) -> tuple[pd.DatetimeIndex, dict[str, pd.Series], list[dict[str, str | int]], date]:
	target_index = build_daily_reference_index(start_date, end_date)
	intraday_floor = datetime.now(UTC).date() - timedelta(days=INTRADAY_RETENTION_DAYS - 1)
	older_end = min(end_date, intraday_floor - timedelta(days=1))
	recent_start = max(start_date, intraday_floor)

	has_older = start_date <= older_end
	has_recent = recent_start <= end_date

	older_close = (
		fetch_all_close_chunked(start_date, older_end, window_days, "1d") if has_older else pd.DataFrame(columns=TICKERS)
	)
	recent_close = (
		fetch_all_close_chunked(recent_start, end_date, window_days, "60m") if has_recent else pd.DataFrame(columns=TICKERS)
	)

	empty_target_index = pd.DatetimeIndex([], name="timestamp_utc", tz="UTC")
	older_target_index = target_index[target_index.date <= older_end] if has_older else empty_target_index
	recent_target_index = target_index[target_index.date >= recent_start] if has_recent else empty_target_index

	normalized_by_symbol: dict[str, pd.Series] = {}
	symbol_rows: list[dict[str, str | int]] = []
	for symbol in TICKERS:
		older_raw = older_close[symbol] if symbol in older_close.columns else pd.Series(dtype="float64")
		recent_raw = recent_close[symbol] if symbol in recent_close.columns else pd.Series(dtype="float64")

		older_norm = normalize_daily_bar_to_reference(older_raw, older_target_index)
		recent_norm = normalize_intraday_to_daily_reference(recent_raw, "60m", recent_target_index)

		combined = pd.concat([older_norm.dropna(), recent_norm.dropna()]).sort_index()
		combined = combined[~combined.index.duplicated(keep="last")]
		normalized = combined.reindex(target_index)
		normalized_by_symbol[symbol] = normalized

		available = normalized.dropna()
		source_tz = f"hybrid_1d:{detect_source_timezone(older_raw)}|60m:{detect_source_timezone(recent_raw)}"
		symbol_rows.append(
			{
				"ticker": symbol,
				"source_tz": source_tz,
				"rows": int(len(available)),
				"exact_1530_rows": int(recent_norm.notna().sum()),
				"proxy_1d_rows": int(older_norm.notna().sum()),
				"first_utc": format_ts(available.index.min() if not available.empty else None),
				"last_utc": format_ts(available.index.max() if not available.empty else None),
			}
		)

	return target_index, normalized_by_symbol, symbol_rows, intraday_floor


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
	report_note: str | None = None,
) -> str:
	report_df = pd.DataFrame(symbol_rows)
	report_table = report_df.to_string(index=False)
	status = "SYNC_OK" if post_rows > 0 else "NO_COMMON_TIMESTAMPS"
	note_line = f"note: {report_note}\n" if report_note else ""
	return (
		"# Timezone_and_Synchronization_Report\n"
		f"interval: {interval}\n"
		f"requested_range: {requested_start} -> {requested_end}\n"
		f"effective_download_start: {effective_start}\n"
		f"{note_line}"
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
		start_date, end_date = select_date_range(args.start_date, args.end_date, interval)
	except ValueError as exc:
		print(f"Errore: {exc}")
		raise SystemExit(1)

	normalized_by_symbol: dict[str, pd.Series] = {}
	symbol_rows: list[dict[str, str | int]] = []
	report_note: str | None = None

	if interval == "1d":
		target_index, normalized_by_symbol, symbol_rows, intraday_floor = build_daily_1530_hybrid_dataset(
			start_date=start_date,
			end_date=end_date,
			window_days=args.window_days,
		)
		effective_start = start_date
		report_note = (
			f"1d uses hybrid policy: <= recent retention exact@15:30 from 60m, "
			f"older dates proxy from native 1d; intraday_floor={intraday_floor}"
		)
		if start_date < intraday_floor:
			print(
				"Nota 1d@15:30: storico precedente al limite intraday Yahoo "
				f"({intraday_floor}) riempito con proxy da barre giornaliere 1d."
			)
	else:
		target_index = build_intraday_target_index(start_date, end_date) if is_intraday_interval(interval) else None

		effective_start, clipped_floor = clamp_start_for_retention(start_date, interval)
		if clipped_floor is not None:
			print(
				f"Nota: per {interval} Yahoo usa una finestra effettiva leggermente piu stretta. "
				f"Inizio download effettivo: {clipped_floor}"
			)

		if effective_start > end_date:
			close = pd.DataFrame(columns=TICKERS)
		else:
			close = fetch_all_close_chunked(effective_start, end_date, args.window_days, interval)

		for symbol in TICKERS:
			raw_series = close[symbol] if symbol in close.columns else pd.Series(dtype="float64")
			normalized = normalize_symbol_close(raw_series, interval, target_index)
			normalized_by_symbol[symbol] = normalized

			available = normalized.dropna()
			symbol_rows.append(
				{
					"ticker": symbol,
					"source_tz": detect_source_timezone(raw_series),
					"rows": int(len(available)),
					"first_utc": format_ts(available.index.min() if not available.empty else None),
					"last_utc": format_ts(available.index.max() if not available.empty else None),
				}
			)

		if target_index is None:
			target_index = build_union_index(normalized_by_symbol)

	output = pd.DataFrame(index=target_index)

	for symbol in TICKERS:
		output[symbol] = normalized_by_symbol[symbol].reindex(target_index)

	DATA_DIR.mkdir(parents=True, exist_ok=True)
	pre_clean_file, post_clean_file, report_file = build_output_paths(start_date, end_date)

	output_to_save = output.reset_index()
	output_to_save = output_to_save.rename(columns={"timestamp_utc": "datetime_utc"})
	output_to_save["datetime_utc"] = output_to_save["datetime_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
	output_to_save[TICKERS] = output_to_save[TICKERS].round(6)
	output_to_save = output_to_save.dropna(subset=TICKERS, how="all")

	pre_clean_df = output_to_save.copy()
	post_clean_df = pre_clean_df.dropna(subset=TICKERS, how="any")

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
			report_note=report_note,
		),
		encoding="utf-8",
	)

	print(f"Pre_clean salvato in: {pre_clean_file}")
	print(f"Post_clean salvato in: {post_clean_file}")
	print(f"Timezone report salvato in: {report_file}")


if __name__ == "__main__":
	main()
