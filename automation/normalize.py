"""Normalisasi nilai mentah dari email transaksi Indonesia.

Modul ini sengaja deterministik dan tanpa dependensi eksternal: hasil parsing
nominal dan tanggal tidak boleh bergantung pada tebakan model.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

WIB = timezone(timedelta(hours=7), 'WIB')

_CURRENCY_PREFIX = re.compile(r'(?i)\b(rp\.?|idr)\s*')
_NON_NUMERIC = re.compile(r'[^0-9.,\-]')

_ID_MONTHS = {
    'jan': 1, 'januari': 1,
    'feb': 2, 'februari': 2, 'pebruari': 2,
    'mar': 3, 'maret': 3,
    'apr': 4, 'april': 4,
    'mei': 5, 'may': 5,
    'jun': 6, 'juni': 6,
    'jul': 7, 'juli': 7,
    'agu': 8, 'agt': 8, 'ags': 8, 'agustus': 8, 'aug': 8, 'august': 8,
    'sep': 9, 'sept': 9, 'september': 9,
    'okt': 10, 'oktober': 10, 'oct': 10, 'october': 10,
    'nov': 11, 'november': 11,
    'des': 12, 'desember': 12, 'dec': 12, 'december': 12,
}


class ParseError(ValueError):
    """Nilai mentah tidak bisa dinormalisasi dengan aman."""


def parse_amount(raw) -> float:
    """Ubah nominal gaya Indonesia menjadi float.

    Menerima ``Rp125.000,00``, ``IDR 125,000.00``, ``125.000``, ``1.5jt`` tidak
    didukung karena ambigu. Pemisah terakhir dianggap desimal hanya bila diikuti
    satu atau dua digit.
    """
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value < 0:
            return abs(value)
        return value

    if raw is None:
        raise ParseError('nominal kosong')

    text = _CURRENCY_PREFIX.sub(' ', str(raw)).strip()
    text = _NON_NUMERIC.sub('', text)
    text = text.replace('-', '')
    if not text:
        raise ParseError(f'nominal tidak terbaca: {raw!r}')

    last_dot = text.rfind('.')
    last_comma = text.rfind(',')
    sep_index = max(last_dot, last_comma)

    if sep_index == -1:
        digits = text
        decimals = ''
    else:
        tail = text[sep_index + 1:]
        if len(tail) in (1, 2) and tail.isdigit() and text.count(text[sep_index]) == 1:
            digits = text[:sep_index]
            decimals = tail
        else:
            digits = text
            decimals = ''

    digits = digits.replace('.', '').replace(',', '')
    if not digits.isdigit():
        raise ParseError(f'nominal tidak terbaca: {raw!r}')

    value = float(digits)
    if decimals:
        value += float(decimals) / (10 ** len(decimals))
    return round(value, 2)


def parse_date(raw, default_tz: timezone = WIB) -> datetime:
    """Ubah tanggal email menjadi ``datetime`` beraware timezone.

    Mendukung ISO-8601, ``dd/mm/yyyy``, ``dd-mm-yyyy``, dan ``18 Sep 2026``
    dengan nama bulan bahasa Indonesia, dengan atau tanpa jam.
    """
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=default_tz)

    if raw is None:
        raise ParseError('tanggal kosong')

    text = str(raw).strip()
    if not text:
        raise ParseError('tanggal kosong')

    iso_candidate = text.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(iso_candidate)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=default_tz)
    except ValueError:
        pass

    time_match = re.search(r'(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?', text)
    hour, minute, second = 0, 0, 0
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        second = int(time_match.group(3) or 0)
        if hour > 23 or minute > 59 or second > 59:
            hour, minute, second = 0, 0, 0
            time_match = None

    numeric = re.search(r'\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b', text)
    if numeric:
        day, month, year = (int(part) for part in numeric.groups())
        year = _expand_year(year)
        return _build(year, month, day, hour, minute, second, default_tz)

    ymd = re.search(r'\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b', text)
    if ymd:
        year, month, day = (int(part) for part in ymd.groups())
        return _build(year, month, day, hour, minute, second, default_tz)

    named = re.search(r'\b(\d{1,2})[\s\-/](([A-Za-z]{3,9}))\.?[\s\-/](\d{2,4})\b', text)
    if named:
        day = int(named.group(1))
        month = _ID_MONTHS.get(named.group(2).lower())
        if month:
            year = _expand_year(int(named.group(4)))
            return _build(year, month, day, hour, minute, second, default_tz)

    raise ParseError(f'tanggal tidak terbaca: {raw!r}')


def _expand_year(year: int) -> int:
    if year < 100:
        return 2000 + year
    return year


def _build(year, month, day, hour, minute, second, tz) -> datetime:
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=tz)
    except ValueError as exc:
        raise ParseError(f'tanggal tidak valid: {year}-{month}-{day}') from exc


def clean_merchant(raw: Optional[str]) -> str:
    """Rapikan nama merchant dari deskripsi mentah kartu/QRIS."""
    if not raw:
        return ''
    text = str(raw)
    text = re.sub(r'(?i)\b(qris|q\.?r\.?i\.?s\.?)\b', ' ', text)
    text = re.sub(r'[*_]+', ' ', text)
    text = re.sub(r'\b\d{6,}\b', ' ', text)
    text = re.sub(r'(?i)\b(jakarta|bandung|surabaya|idn|ind|id)\b\s*$', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip(' .,-')


def mask_account(raw: Optional[str]) -> str:
    """Simpan hanya 4 digit terakhir nomor rekening/kartu."""
    if not raw:
        return ''
    digits = re.sub(r'\D', '', str(raw))
    if len(digits) < 4:
        return str(raw).strip()
    return f'****{digits[-4:]}'


def last4(raw: Optional[str]) -> str:
    digits = re.sub(r'\D', '', str(raw or ''))
    return digits[-4:] if len(digits) >= 4 else ''
