"""Struktur data transaksi hasil ekstraksi email."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

STATUS_READY = 'ready'
STATUS_REVIEW = 'needs_review'
STATUS_IMPORTED = 'imported'
STATUS_IGNORED = 'ignored'
STATUS_DUPLICATE = 'duplicate'


@dataclass
class EmailRef:
    """Jejak email asal, supaya setiap baris transaksi bisa diaudit."""

    message_id: str = ''
    thread_id: str = ''
    sender: str = ''
    subject: str = ''
    received_at: str = ''

    def link(self) -> str:
        if not self.message_id:
            return ''
        return f'https://mail.google.com/mail/u/0/#all/{self.message_id}'


@dataclass
class Transaction:
    date: datetime
    amount: float
    direction: str            # debit | credit
    kind: str                 # expense | income | transfer
    merchant: str = ''
    account: str = ''
    bank: str = ''
    channel: str = ''         # transfer | card | ewallet | qris | va | cash
    reference: str = ''
    category: str = ''
    category_label: str = ''
    confidence: float = 0.0
    status: str = STATUS_REVIEW
    reasons: List[str] = field(default_factory=list)
    source_id: str = ''
    email: EmailRef = field(default_factory=EmailRef)
    note: str = ''

    @property
    def fingerprint(self) -> str:
        """Identitas stabil untuk deduplikasi lintas run.

        Sengaja tidak memakai message id: satu transaksi sering dikirim ulang
        lewat email berbeda (notifikasi bank + struk merchant).
        """
        parts = [
            self.date.strftime('%Y-%m-%d'),
            f'{self.amount:.2f}',
            self.direction,
            (self.reference or self.merchant or '').lower().strip(),
            self.bank.lower(),
        ]
        return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data['date'] = self.date.isoformat()
        data['fingerprint'] = self.fingerprint
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Transaction':
        payload = dict(data)
        payload.pop('fingerprint', None)
        payload['date'] = datetime.fromisoformat(payload['date'])
        email = payload.get('email') or {}
        payload['email'] = EmailRef(**email) if isinstance(email, dict) else EmailRef()
        payload['reasons'] = list(payload.get('reasons') or [])
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})

    def summary(self, currency: str = 'Rp') -> str:
        sign = '-' if self.kind == 'expense' else ('+' if self.kind == 'income' else '~')
        label = self.merchant or self.email.subject or '(tanpa keterangan)'
        return (
            f"{self.date.strftime('%d/%m/%Y %H:%M')} | {sign}{currency}{self.amount:,.0f} | "
            f"{label} | {self.category_label or self.category} | {self.bank or self.source_id}"
        )


@dataclass
class RawEmail:
    """Email mentah yang diserahkan agen Gmail ke pipeline."""

    message_id: str = ''
    thread_id: str = ''
    sender: str = ''
    subject: str = ''
    received_at: str = ''
    body: str = ''
    # Field opsional yang boleh diisi agen bila layout email tidak dikenali
    # regex. Nilai di sini selalu dinormalisasi ulang oleh pipeline.
    hints: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RawEmail':
        alias = {
            'id': 'message_id',
            'messageId': 'message_id',
            'threadId': 'thread_id',
            'from': 'sender',
            'date': 'received_at',
            'receivedAt': 'received_at',
            'snippet': 'body',
            'text': 'body',
        }
        payload: Dict[str, Any] = {}
        for key, value in data.items():
            payload[alias.get(key, key)] = value
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in payload.items() if k in known}
        payload['hints'] = payload.get('hints') or {}
        return cls(**payload)

    def haystack(self) -> str:
        return '\n'.join([self.subject or '', self.body or ''])


def find_optional(items: List[Any], predicate) -> Optional[Any]:
    for item in items:
        if predicate(item):
            return item
    return None
