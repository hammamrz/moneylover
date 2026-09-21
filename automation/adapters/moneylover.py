"""Adapter ke API tidak resmi Money Lover Web (web.moneylover.me).

Catatan penting sebelum dipakai:

* Money Lover tidak menyediakan API publik. Endpoint di bawah adalah endpoint
  internal yang dipakai aplikasi web-nya dan bisa berubah kapan saja tanpa
  pemberitahuan.
* Ada dua cara autentikasi, keduanya lewat environment variable dan tidak
  pernah ditulis ke repo:

  1. ``MONEYLOVER_JWT`` berisi token sesi web. Umurnya sekitar satu minggu,
     jadi harus diperbarui berkala, dan mengambilnya butuh akses ke
     penyimpanan browser.
  2. ``MONEYLOVER_EMAIL`` dan ``MONEYLOVER_PASSWORD``. Adapter menukarnya
     sendiri menjadi token lewat endpoint OAuth Money Lover, sehingga tidak
     perlu mengambil apa pun dari browser. Konsekuensinya password akun
     tersimpan di environment, dan setiap login menambah satu perangkat
     terdaftar.
* Satu akun hanya boleh memegang lima token perangkat sekaligus; login
  berulang akan ditolak dengan "Maximum device limit reached".
* Mode bawaan adalah ``dry_run``: transaksi hanya dicetak, tidak dikirim.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..models import Transaction

DEFAULT_BASE_URL = 'https://web.moneylover.me/api'
OAUTH_URL = 'https://oauth.moneylover.me/token'
TOKEN_ENV = 'MONEYLOVER_JWT'
# Cloudflare di depan web.moneylover.me menolak permintaan dengan signature
# klien bawaan urllib (error 1010), jadi header ini bukan penyamaran melainkan
# syarat agar permintaan sampai ke aplikasinya sama seperti dari halaman web.
BROWSER_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9,id;q=0.8',
    'Origin': 'https://web.moneylover.me',
    'Referer': 'https://web.moneylover.me/',
}
EMAIL_ENV = 'MONEYLOVER_EMAIL'
PASSWORD_ENV = 'MONEYLOVER_PASSWORD'
CACHE_ENV = 'MONEYLOVER_TOKEN_CACHE'


def _cache_path() -> Path:
    """Lokasi singgahan token, di luar repo dan di luar direktori kerja."""
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / 'moneylover-token.json'


def read_cached_token() -> Optional[str]:
    """Ambil token dari singgahan bila masih berlaku minimal satu jam lagi."""
    path = _cache_path()
    try:
        cached = json.loads(path.read_text(encoding='utf-8')).get('token', '')
    except (OSError, ValueError):
        return None
    payload = jwt_payload(cached) or {}
    expires_at = payload.get('exp')
    if not isinstance(expires_at, (int, float)):
        return None
    if datetime.fromtimestamp(expires_at, tz=timezone.utc) - datetime.now(tz=timezone.utc) < timedelta(hours=1):
        return None
    return cached


def write_cached_token(token: str) -> None:
    """Simpan token agar satu run tidak login berkali-kali.

    Money Lover membatasi lima perangkat per akun dan setiap login mendaftarkan
    satu perangkat baru. Tanpa singgahan ini, satu run harian yang memanggil
    check lalu push akan memakai dua slot sekaligus. Berkasnya hanya bisa
    dibaca pemiliknya, berada di direktori sementara container, dan ikut hilang
    saat container dilepas.
    """
    path = _cache_path()
    try:
        path.write_text(json.dumps({'token': token}), encoding='utf-8')
        path.chmod(0o600)
    except OSError:
        pass


class MoneyLoverError(RuntimeError):
    pass


def jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    """Baca bagian payload JWT tanpa memverifikasi tanda tangannya."""
    parts = (token or '').split('.')
    if len(parts) != 3:
        return None
    segment = parts[1] + '=' * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(segment))
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return None


def describe_token(token: Optional[str] = None) -> Dict[str, Any]:
    """Baca masa berlaku token tanpa memanggil server.

    Payload JWT hanya di-encode base64, bukan dienkripsi, jadi tanggal
    kedaluwarsanya bisa dibaca lokal. Berguna untuk tahu kapan token harus
    diperbarui sebelum run harian gagal dengan 401.
    """
    raw = token or os.environ.get(TOKEN_ENV, '')
    if not raw:
        return {'ada': False, 'pesan': f'{TOKEN_ENV} belum diset'}

    raw = raw.strip()
    if raw.lower().startswith('authjwt '):
        raw = raw[8:].strip()

    payload = jwt_payload(raw)
    if payload is None:
        return {'ada': True, 'valid_bentuk': False,
                'pesan': 'token bukan JWT yang bisa dibaca'}

    report: Dict[str, Any] = {'ada': True, 'valid_bentuk': True}
    expires_at = payload.get('exp')
    if isinstance(expires_at, (int, float)):
        expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc)
        remaining = expiry - datetime.now(tz=timezone.utc)
        report['kedaluwarsa'] = expiry.isoformat()
        report['sisa_hari'] = round(remaining.total_seconds() / 86400, 1)
        report['sudah_kedaluwarsa'] = remaining.total_seconds() <= 0
    return report


def _request(url: str, headers: Dict[str, str], payload: Optional[Dict[str, Any]] = None,
             timeout: int = 30) -> Dict[str, Any]:
    body = json.dumps(payload or {}).encode('utf-8')
    merged = dict(BROWSER_HEADERS)
    merged.update(headers)
    request = urllib.request.Request(url, data=body, method='POST', headers=merged)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', 'replace')[:300]
        raise MoneyLoverError(f'HTTP {exc.code} pada {url}: {detail}') from exc
    except urllib.error.URLError as exc:
        raise MoneyLoverError(f'gagal menghubungi {url}: {exc.reason}') from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MoneyLoverError(f'respons bukan JSON dari {url}: {raw[:200]}') from exc


def login(email: Optional[str] = None, password: Optional[str] = None,
          base_url: str = DEFAULT_BASE_URL, timeout: int = 30) -> str:
    """Tukar email dan password menjadi JWT lewat endpoint OAuth Money Lover.

    Jalur ini dipakai ketika token sesi web tidak bisa diambil dari browser,
    misalnya karena DevTools dimatikan kebijakan perangkat. Kredensial hanya
    dibaca dari environment dan tidak pernah dicatat ke mana pun.
    """
    user = email or os.environ.get(EMAIL_ENV, '')
    secret = password or os.environ.get(PASSWORD_ENV, '')
    if not user or not secret:
        raise MoneyLoverError(
            f'{EMAIL_ENV} dan {PASSWORD_ENV} belum diset, dan {TOKEN_ENV} juga kosong.'
        )

    handshake = _request(
        f'{base_url.rstrip("/")}/user/login-url',
        {'Content-Type': 'application/json', 'Accept': 'application/json'},
        timeout=timeout,
    )
    data = handshake.get('data') or handshake
    request_token = data.get('request_token')
    login_url = data.get('login_url', '')
    if not request_token:
        raise MoneyLoverError(f'request_token tidak ada di respons login-url: {handshake}')

    client_match = re.search(r'client=([^&]+)', login_url)
    client_id = client_match.group(1) if client_match else ''
    if not client_id:
        # login_url pernah berubah bentuk; client yang sama juga tertulis di
        # payload request_token, jadi dipakai sebagai cadangan.
        payload = jwt_payload(request_token) or {}
        client_id = payload.get('client') or ''
    if not client_id:
        raise MoneyLoverError(f'client id tidak ditemukan di login_url: {login_url!r}')

    result = _request(
        OAUTH_URL,
        {
            'Authorization': f'Bearer {request_token}',
            'Client': client_id,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        {'email': user, 'password': secret},
        timeout=timeout,
    )
    token = result.get('access_token')
    if not token:
        # Pesan kesalahan dikutip apa adanya, tetapi kredensial tidak pernah
        # ikut tercetak.
        raise MoneyLoverError(f"login ditolak: {result.get('msg') or result.get('error') or result}")
    return token


class MoneyLoverClient:
    def __init__(self, token: Optional[str] = None, base_url: str = DEFAULT_BASE_URL, timeout: int = 30):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        raw = (token or os.environ.get(TOKEN_ENV, '')).strip()
        # Menerima token dengan atau tanpa awalan "AuthJWT ", karena itulah
        # bentuk yang tersalin saat token diambil langsung dari browser.
        if raw.lower().startswith('authjwt '):
            raw = raw[8:].strip()
        self.auth_method = 'token'
        if not raw:
            cached = read_cached_token()
            if cached:
                raw = cached
                self.auth_method = 'cache'
            else:
                # Tanpa token, login sendiri. Ini membuat Routine harian bisa
                # jalan tanpa ada yang menempelkan token setiap minggu.
                raw = login(base_url=self.base_url, timeout=self.timeout)
                write_cached_token(raw)
                self.auth_method = 'login'
        self.token = raw

    def _post(self, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body = json.dumps(payload or {}).encode('utf-8')
        request = urllib.request.Request(
            f'{self.base_url}{path}',
            data=body,
            method='POST',
            headers={
                **BROWSER_HEADERS,
                'Authorization': f'AuthJWT {self.token}',
                'Content-Type': 'application/json',
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode('utf-8')
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')[:300]
            raise MoneyLoverError(f'HTTP {exc.code} pada {path}: {detail}') from exc
        except urllib.error.URLError as exc:
            raise MoneyLoverError(f'gagal menghubungi {path}: {exc.reason}') from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MoneyLoverError(f'respons bukan JSON pada {path}: {raw[:200]}') from exc

        if isinstance(data, dict) and data.get('error') not in (None, 0):
            raise MoneyLoverError(f"API menolak {path}: {data.get('msg') or data}")
        return data

    def wallets(self) -> List[Dict[str, Any]]:
        return self._post('/wallet/list').get('data', [])

    def categories(self, wallet_id: str) -> List[Dict[str, Any]]:
        return self._post('/category/list', {'walletId': wallet_id}).get('data', [])

    def add_transaction(self, wallet_id: str, category_id: str, amount: float,
                        display_date: str, note: str = '') -> Dict[str, Any]:
        return self._post('/transaction/add', {
            'with': [],
            'account': wallet_id,
            'category': category_id,
            'amount': amount,
            'note': note,
            'displayDate': display_date,
        })


def audit_wallets(wallets: List[Dict[str, Any]], mapped: List[str]) -> Dict[str, Any]:
    """Bandingkan dompet yang dipetakan dengan yang benar-benar aktif.

    Pencatatan hanya menyasar dompet yang ikut dihitung di total dan belum
    diarsipkan. Mengirim ke dompet yang diarsipkan atau sengaja dikeluarkan
    dari total, misalnya kartu kredit atau pos investasi, akan mengacaukan
    angka yang justru ingin dijaga.
    """
    aktif, diabaikan = [], []
    for wallet in wallets:
        name = wallet.get('name', '')
        if wallet.get('exclude_total') or wallet.get('archived'):
            diabaikan.append(name)
        else:
            aktif.append(name)

    mapped_set = {name for name in mapped if name}
    return {
        'ikut_total': sorted(aktif),
        'belum_dipetakan': sorted(set(aktif) - mapped_set),
        'dipetakan_tapi_diabaikan': sorted(mapped_set & set(diabaikan)),
        'dipetakan_tapi_tidak_ada': sorted(
            mapped_set - {wallet.get('name', '') for wallet in wallets}),
    }


def resolve_ids(client: MoneyLoverClient, wallet_name: str) -> Dict[str, Any]:
    """Petakan nama dompet dan kategori Money Lover ke id internalnya."""
    wallets = client.wallets()
    wallet = next(
        (item for item in wallets if item.get('name', '').lower() == wallet_name.lower()),
        None,
    )
    if wallet is None:
        available = ', '.join(item.get('name', '?') for item in wallets)
        raise MoneyLoverError(f"dompet '{wallet_name}' tidak ditemukan. Tersedia: {available}")
    wallet_id = wallet.get('_id') or wallet.get('id')
    categories = {
        item.get('name', '').lower(): (item.get('_id') or item.get('id'))
        for item in client.categories(wallet_id)
    }
    return {'wallet_id': wallet_id, 'categories': categories}


def push(transactions: List[Transaction], rules: Dict[str, Any], config: Dict[str, Any],
         dry_run: bool = True) -> Dict[str, Any]:
    """Kirim transaksi ke Money Lover. Default tidak mengirim apa pun."""
    from ..categorize import wallet_for

    settings = config.get('moneylover', {})
    override = settings.get('category_map', {})
    planned = []
    for item in transactions:
        meta = rules['categories'].get(item.category, {})
        planned.append({
            'fingerprint': item.fingerprint,
            'wallet': wallet_for(item, config),
            'category': override.get(item.category) or meta.get('moneylover') or '',
            'category_parent': meta.get('moneylover_parent') or '',
            'amount': item.amount if item.kind == 'income' else -item.amount,
            'displayDate': item.date.strftime('%Y-%m-%d'),
            'note': ' - '.join(part for part in [item.merchant, item.reference] if part)[:200],
        })

    if dry_run:
        return {'dry_run': True, 'planned': planned, 'sent': [], 'errors': []}

    client = MoneyLoverClient(base_url=settings.get('base_url', DEFAULT_BASE_URL))
    # Satu resolve per dompet, bukan per transaksi: tiap resolve memanggil API
    # dua kali dan daftar kategori tiap dompet tidak berubah selama satu run.
    resolved_cache: Dict[str, Dict[str, Any]] = {}
    sent, errors = [], []

    for entry in planned:
        wallet_name = entry['wallet']
        if not wallet_name:
            errors.append({'fingerprint': entry['fingerprint'],
                           'error': 'dompet tujuan tidak diketahui; lengkapi own_accounts '
                                    'di automation/config.json'})
            continue
        if not entry['category']:
            errors.append({'fingerprint': entry['fingerprint'],
                           'error': 'kategori belum punya padanan di Money Lover'})
            continue

        if wallet_name not in resolved_cache:
            try:
                resolved_cache[wallet_name] = resolve_ids(client, wallet_name)
            except MoneyLoverError as exc:
                resolved_cache[wallet_name] = {'error': str(exc)}
        resolved = resolved_cache[wallet_name]
        if 'error' in resolved:
            errors.append({'fingerprint': entry['fingerprint'], 'error': resolved['error']})
            continue

        # Daftar kategori berbeda per dompet. Bila sub-kategori tidak tersedia
        # di dompet ini, induknya dipakai: arti transaksi tetap terjaga, jauh
        # lebih baik daripada gagal atau jatuh ke Other Expense.
        category_id = resolved['categories'].get(entry['category'].lower())
        if not category_id and entry['category_parent']:
            category_id = resolved['categories'].get(entry['category_parent'].lower())
            if category_id:
                entry['category_used'] = entry['category_parent']
                entry['fallback'] = f"'{entry['category']}' tidak ada di {wallet_name}, dipakai induknya"
        if not category_id:
            errors.append({'fingerprint': entry['fingerprint'],
                           'error': f"kategori '{entry['category']}' dan induknya tidak ada "
                                    f"di dompet {wallet_name}"})
            continue
        entry.setdefault('category_used', entry['category'])
        try:
            client.add_transaction(
                wallet_id=resolved['wallet_id'],
                category_id=category_id,
                amount=abs(entry['amount']),
                display_date=entry['displayDate'],
                note=entry['note'],
            )
            sent.append(entry['fingerprint'])
        except MoneyLoverError as exc:
            errors.append({'fingerprint': entry['fingerprint'], 'error': str(exc)})
    return {'dry_run': False, 'planned': planned, 'sent': sent, 'errors': errors}
