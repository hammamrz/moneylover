"""Kategorisasi transaksi dan deteksi transfer antar rekening sendiri."""

from __future__ import annotations

import json
import re
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import Transaction, STATUS_READY, STATUS_REVIEW
from .normalize import last4

RULES_PATH = Path(__file__).parent / 'rules' / 'categories.json'


def load_rules(path: Path = RULES_PATH) -> Dict[str, Any]:
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def undecided_categories(rules: Dict[str, Any]) -> set:
    """Kategori yang berarti 'belum diputuskan', bukan sebuah keputusan.

    Diambil dari ``fallback`` supaya penamaannya tetap satu sumber di
    rules/categories.json. 'transfer' sengaja tidak ikut: ia memang kategori
    yang sah, dipilih karena lawan transaksinya terbukti rekening sendiri.
    """
    fallback = rules.get('fallback', {})
    return {fallback[kind] for kind in ('expense', 'income') if fallback.get(kind)}


@lru_cache(maxsize=4096)
def _token_pattern(token: str) -> 're.Pattern[str]':
    """Pola kata utuh untuk satu kata kunci.

    Pencocokan substring polos pernah membuat kata kunci 'erha' cocok di dalam
    'berhasil', sehingga setiap email berjudul "Transaksi berhasil" dikira
    perawatan diri. Batas di sini bukan ``\b`` karena banyak kata kunci
    mengandung tanda baca seperti 'e-toll', 'h&m', atau 'apple.com/bill'.
    """
    return re.compile(r'(?<![0-9a-z])' + re.escape(token.lower()) + r'(?![0-9a-z])')


def matches(token: str, text: str) -> bool:
    return bool(_token_pattern(token).search(text))


def _haystack(transaction: Transaction) -> str:
    return ' '.join([
        transaction.merchant or '',
        transaction.email.subject or '',
        transaction.note or '',
        transaction.bank or '',
    ]).lower()


def is_own_account(text: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Cek apakah teks menunjuk ke salah satu rekening/dompet milik sendiri."""
    lowered = (text or '').lower()
    digits = last4(text)
    for account in config.get('own_accounts', []):
        aliases = [alias.lower() for alias in account.get('aliases', []) if alias]
        if aliases and any(matches(alias, lowered) for alias in aliases):
            return account
        account_last4 = account.get('last4') or ''
        if account_last4 and digits and account_last4[-4:] == digits:
            return account
    return None


def account_for(transaction: Transaction, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Cari dompet sendiri yang menjadi sumber transaksi, dari penerbit emailnya."""
    bank = (transaction.bank or '').lower()
    if not bank:
        return None
    for account in config.get('own_accounts', []):
        if (account.get('bank') or '').lower() == bank:
            return account
    return None


def wallet_for(transaction: Transaction, config: Dict[str, Any]) -> str:
    """Nama dompet Money Lover tujuan.

    Dikembalikan string kosong bila penerbit emailnya tidak dikenali dan tidak
    ada default; transaksi seperti itu ditahan, bukan dimasukkan ke dompet
    sembarangan, karena salah dompet berarti saldo dua rekening ikut salah.
    """
    account = account_for(transaction, config)
    if account and account.get('moneylover_wallet'):
        return account['moneylover_wallet']
    return config.get('moneylover', {}).get('default_wallet') or ''


def apply_context_overrides(transaction: Transaction, rules: Dict[str, Any],
                            haystack: Optional[str] = None) -> Transaction:
    """Pindahkan kategori ke pohon lain bila teks transaksi memuat penanda.

    Email pemesanan hotel atau tiket tidak memuat apa pun yang membedakan
    perjalanan dinas dari liburan. Penandanya ditulis manual, misalnya
    "perjalanan dinas" di deskripsi, dan di sinilah penanda itu dibaca.
    """
    text = haystack if haystack is not None else _haystack(transaction)
    for override in rules.get('context_overrides', []):
        marker = next((token for token in override.get('any', []) if matches(token, text)), None)
        if not marker:
            continue
        target = override.get('remap', {}).get(transaction.category)
        if not target or target == transaction.category:
            continue
        previous = transaction.category
        transaction.category = target
        transaction.category_label = rules['categories'].get(target, {}).get('label', target)
        transaction.reasons.append(
            f"penanda '{marker}' memindahkan kategori dari '{previous}' ke '{target}'"
        )
    return transaction


def categorize(transaction: Transaction, rules: Dict[str, Any], config: Dict[str, Any]) -> Transaction:
    """Isi ``category`` dan ``category_label``, dan tandai transfer internal."""
    categories = rules['categories']
    haystack = _haystack(transaction)

    # Hanya pihak lawan transaksi yang dicek. Field ``account`` berisi rekening
    # sumber, yang memang selalu milik sendiri; ikut mencocokkannya akan membuat
    # semua belanja terbaca sebagai transfer internal.
    own = is_own_account(transaction.merchant, config)
    if own:
        transaction.kind = 'transfer'
        transaction.category = 'transfer'
        transaction.reasons.append(f"transfer internal: cocok akun '{own.get('label', own.get('id'))}'")
    else:
        matched = None
        for rule in sorted(rules.get('rules', []), key=lambda item: item.get('priority', 0), reverse=True):
            wanted = rule.get('direction')
            if wanted and wanted != transaction.direction:
                continue
            for token in rule.get('any', []):
                if matches(token, haystack):
                    matched = (rule, token)
                    break
            if matched:
                break
        if matched:
            rule, token = matched
            transaction.category = rule['category']
            transaction.reasons.append(f"kategori '{rule['id']}' dari kata kunci '{token}'")
            transaction.confidence = min(1.0, transaction.confidence + 0.05)
        else:
            fallback = rules['fallback']
            transaction.category = fallback.get(transaction.kind, 'lainnya')
            transaction.reasons.append('kategori fallback: tidak ada aturan yang cocok')
            transaction.confidence = max(0.0, transaction.confidence - 0.2)

    apply_context_overrides(transaction, rules, haystack)

    meta = categories.get(transaction.category, {})
    transaction.category_label = meta.get('label', transaction.category)
    if meta.get('type') == 'transfer':
        transaction.kind = 'transfer'

    threshold = config.get('thresholds', {}).get('auto_import_min_confidence', 0.8)
    large = config.get('thresholds', {}).get('large_amount_review', 0)
    if large and transaction.amount >= large:
        transaction.status = STATUS_REVIEW
        transaction.reasons.append(f'nominal >= {large:,.0f}, wajib review manual')
    elif transaction.category in undecided_categories(rules):
        # Confidence mengukur keyakinan pada nominal dan tanggal hasil ekstraksi,
        # bukan pada kategori. Email yang terurai sempurna tapi merchantnya tidak
        # dikenali tetap bernilai tinggi, dan potongan fallback sebesar 0.2 mendarat
        # persis di ambang 0.8 sehingga lolos sebagai 'ready'. Tanpa cabang ini,
        # transaksi yang justru paling butuh keputusan manusia terkirim otomatis
        # sebagai 'Uncategorized Expense'.
        transaction.status = STATUS_REVIEW
        transaction.reasons.append('kategori belum ditentukan, menunggu keputusan manual')
    elif transaction.confidence >= threshold:
        transaction.status = STATUS_READY
    else:
        transaction.status = STATUS_REVIEW
    return transaction


def pair_internal_transfers(transactions: List[Transaction], config: Dict[str, Any]) -> List[Transaction]:
    """Gabungkan pasangan debit+kredit bernominal sama menjadi satu transfer.

    Contoh: transfer dari BCA ke GoPay memicu dua email. Tanpa pemasangan ini,
    saldo tercatat berkurang dua kali dan laporan pengeluaran ikut salah.
    """
    window = timedelta(minutes=config.get('thresholds', {}).get('transfer_pair_window_minutes', 15))
    used: set[int] = set()
    result: List[Transaction] = []

    ordered = sorted(range(len(transactions)), key=lambda i: transactions[i].date)
    for position, index in enumerate(ordered):
        if index in used:
            continue
        current = transactions[index]
        if current.direction != 'debit':
            continue
        for other_index in ordered[position + 1:]:
            if other_index in used:
                continue
            other = transactions[other_index]
            if other.direction != 'credit':
                continue
            if abs(other.amount - current.amount) > 0.01:
                continue
            if abs((other.date - current.date).total_seconds()) > window.total_seconds():
                continue
            if other.bank and current.bank and other.bank == current.bank:
                continue
            current.kind = 'transfer'
            current.category = 'transfer'
            current.category_label = 'Transfer Antar Rekening'
            current.note = ' '.join(filter(None, [
                current.note,
                f'Pasangan dengan email {other.email.message_id or other.email.subject}',
            ])).strip()
            current.reasons.append(
                f'dipasangkan sebagai transfer internal dengan kredit di {other.bank or other.source_id}'
            )
            used.add(other_index)
            break

    for index, transaction in enumerate(transactions):
        if index in used:
            continue
        result.append(transaction)
    return result


def categorize_many(transactions: List[Transaction], rules: Dict[str, Any], config: Dict[str, Any]) -> List[Transaction]:
    categorized = [categorize(item, rules, config) for item in transactions]
    return pair_internal_transfers(categorized, config)
