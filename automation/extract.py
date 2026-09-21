"""Ekstraksi field transaksi dari email mentah.

Strategi dua lapis:

1. Regex deterministik dari ``rules/sources.json``. Ini jalur utama; nominal
   dan tanggal hanya boleh berasal dari sini atau dari hint yang sudah
   dinormalisasi ulang.
2. Hint dari agen (LLM) untuk layout email yang belum dikenali. Hint tetap
   divalidasi lewat :mod:`automation.normalize`, dan transaksi yang bergantung
   pada hint mendapat confidence lebih rendah sehingga masuk antrean review.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import EmailRef, RawEmail, Transaction, STATUS_READY, STATUS_REVIEW
from .normalize import ParseError, clean_merchant, mask_account, parse_amount, parse_date

RULES_PATH = Path(__file__).parent / 'rules' / 'sources.json'

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'[ \t\xa0]+')


def load_rules(path: Path = RULES_PATH) -> Dict[str, Any]:
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def strip_html(text: str) -> str:
    """Buang tag HTML dan rapikan spasi, sisakan struktur baris."""
    if not text:
        return ''
    cleaned = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', text)
    cleaned = re.sub(r'(?i)<br\s*/?>', '\n', cleaned)
    cleaned = re.sub(r'(?i)</(p|div|tr|li|h[1-6])>', '\n', cleaned)
    cleaned = re.sub(r'(?i)</t[dh]>', ' | ', cleaned)
    cleaned = _TAG_RE.sub(' ', cleaned)
    for entity, char in (('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'),
                         ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'")):
        cleaned = cleaned.replace(entity, char)
    cleaned = _WS_RE.sub(' ', cleaned)
    cleaned = re.sub(r'\n\s*\n+', '\n', cleaned)
    return cleaned.strip()


def detect_source(email: RawEmail, rules: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Cocokkan email ke satu penerbit yang dikenal."""
    sender = (email.sender or '').lower()
    subject = (email.subject or '').lower()
    for source in rules.get('sources', []):
        match = source.get('match', {})
        if any(token.lower() in sender for token in match.get('from', [])):
            return source
    for source in rules.get('sources', []):
        match = source.get('match', {})
        if any(token.lower() in subject for token in match.get('subject', [])):
            return source
    return None


def _first_group(patterns: List[str], text: str) -> Optional[str]:
    for pattern in patterns:
        found = re.search(pattern, text)
        if found and found.group(1).strip():
            return found.group(1).strip()
    return None


def _field_patterns(source: Optional[Dict[str, Any]], generic: Dict[str, Any], name: str) -> List[str]:
    patterns: List[str] = []
    if source:
        patterns.extend((source.get('fields') or {}).get(name, []))
    patterns.extend((generic.get('fields') or {}).get(name, []))
    return patterns


def detect_direction(text: str, source: Optional[Dict[str, Any]], generic: Dict[str, Any]) -> Tuple[str, str]:
    """Tentukan uang masuk atau keluar. Mengembalikan (direction, alasan)."""
    lowered = text.lower()
    candidates = list((source or {}).get('direction_rules', []))
    candidates.extend(generic.get('direction_rules', []))
    candidates.sort(key=lambda rule: rule.get('priority', 0), reverse=True)
    for rule in candidates:
        for token in rule.get('any', []):
            if token.lower() in lowered:
                return rule['direction'], f"kata kunci '{token}'"
    return 'debit', 'default debit (tidak ada penanda arah)'


def _timezone(offset: str) -> timezone:
    found = re.match(r'([+-])(\d{2}):?(\d{2})', offset or '+07:00')
    if not found:
        return timezone(timedelta(hours=7))
    sign = 1 if found.group(1) == '+' else -1
    return timezone(sign * timedelta(hours=int(found.group(2)), minutes=int(found.group(3))))


def extract(email: RawEmail, rules: Dict[str, Any], config: Dict[str, Any]) -> Optional[Transaction]:
    """Ubah satu email menjadi kandidat transaksi, atau ``None`` bila bukan transaksi."""
    generic = rules.get('generic', {})
    tz = _timezone(config.get('timezone', '+07:00'))
    body = strip_html(email.body or '')
    haystack = '\n'.join([email.subject or '', body])
    source = detect_source(email, rules)

    # Sebagian penerbit memakai alamat yang sama untuk email non-transaksi,
    # misalnya tagihan bulanan dan survei. Itu disaring di sini supaya tidak
    # menjadi transaksi palsu.
    subject = (email.subject or '').lower()
    for token in (source or {}).get('ignore_subject', []):
        if token.lower() in subject:
            return None

    hints = email.hints or {}
    reasons: List[str] = []
    confidence = 0.5

    if source:
        confidence += 0.2
        reasons.append(f"sumber dikenali: {source['label']}")
    else:
        reasons.append('sumber belum ada di rules/sources.json')

    amount: Optional[float] = None
    if hints.get('amount') is not None:
        try:
            amount = parse_amount(hints['amount'])
            reasons.append('nominal dari hint agen')
            confidence -= 0.1
        except ParseError:
            amount = None
    if amount is None:
        raw_amount = _first_group(_field_patterns(source, generic, 'amount'), haystack)
        if raw_amount:
            try:
                amount = parse_amount(raw_amount)
                confidence += 0.15
            except ParseError:
                amount = None
    if amount is None or amount <= 0:
        return None

    transaction_date: Optional[datetime] = None
    if hints.get('date'):
        try:
            transaction_date = parse_date(hints['date'], tz)
            reasons.append('tanggal dari hint agen')
        except ParseError:
            transaction_date = None
    if transaction_date is None:
        raw_date = _first_group(_field_patterns(source, generic, 'date'), haystack)
        if raw_date:
            try:
                transaction_date = parse_date(raw_date, tz)
                confidence += 0.1
            except ParseError:
                transaction_date = None
    # Beberapa penerbit menaruh tanggal dan jam di baris terpisah, jadi jamnya
    # ditempelkan setelah tanggalnya terbaca.
    if transaction_date is not None and transaction_date.hour == 0 and transaction_date.minute == 0:
        raw_time = _first_group(_field_patterns(source, generic, 'time'), haystack)
        if raw_time:
            found = re.match(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', raw_time.strip())
            if found:
                hour, minute = int(found.group(1)), int(found.group(2))
                second = int(found.group(3) or 0)
                if hour < 24 and minute < 60 and second < 60:
                    transaction_date = transaction_date.replace(
                        hour=hour, minute=minute, second=second)

    if transaction_date is None and email.received_at:
        try:
            transaction_date = parse_date(email.received_at, tz)
            reasons.append('tanggal diambil dari waktu terima email')
            confidence -= 0.05
        except ParseError:
            transaction_date = None
    if transaction_date is None:
        return None

    direction = (hints.get('direction') or '').lower()
    if direction in ('debit', 'credit'):
        reasons.append('arah dana dari hint agen')
    else:
        direction, why = detect_direction(haystack, source, generic)
        reasons.append(f'arah dana: {why}')
        if why.startswith('default'):
            confidence -= 0.15

    # Pihak lawan transaksi berpindah field tergantung arah dana: pada uang
    # masuk yang relevan adalah pengirim, bukan tujuan yang justru rekening
    # sendiri.
    merchant_fields = ['merchant_credit', 'merchant'] if direction == 'credit' else ['merchant']
    merchant_raw = hints.get('merchant') or ''
    for name in merchant_fields:
        if merchant_raw:
            break
        merchant_raw = _first_group(_field_patterns(source, generic, name), haystack) or ''
    merchant = clean_merchant(merchant_raw)
    if not merchant and source and source.get('kind') == 'merchant':
        merchant = source['label']
    if merchant:
        confidence += 0.05
    else:
        reasons.append('nama merchant tidak ditemukan')

    account_raw = hints.get('account') or _first_group(_field_patterns(source, generic, 'account'), haystack) or ''
    reference = hints.get('reference') or _first_group(_field_patterns(source, generic, 'reference'), haystack) or ''

    transaction = Transaction(
        date=transaction_date,
        amount=amount,
        direction=direction,
        kind='expense' if direction == 'debit' else 'income',
        merchant=merchant,
        account=mask_account(account_raw),
        bank=(source or {}).get('label', ''),
        channel=(hints.get('channel') or (source or {}).get('channel_default', '')),
        reference=str(reference).strip(),
        confidence=max(0.0, min(confidence, 1.0)),
        status=STATUS_REVIEW,
        reasons=reasons,
        source_id=(source or {}).get('id', 'unknown'),
        email=EmailRef(
            message_id=email.message_id,
            thread_id=email.thread_id,
            sender=email.sender,
            subject=email.subject,
            received_at=email.received_at,
        ),
    )
    transaction.status = STATUS_READY if transaction.confidence >= 0.8 else STATUS_REVIEW
    return transaction


def extract_many(emails: List[RawEmail], rules: Dict[str, Any], config: Dict[str, Any]) -> List[Transaction]:
    results: List[Transaction] = []
    for email in emails:
        found = extract(email, rules, config)
        if found:
            results.append(found)
    return results
