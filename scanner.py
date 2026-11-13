"""Bounded, non-executing heuristic inspection. No claim of antivirus coverage."""
import base64
import binascii
import gzip
import io
import re
from dataclasses import dataclass

MAX_BYTES = 5 * 1024 * 1024

@dataclass(frozen=True)
class Result:
    status: str  # suspicious, unknown, clear
    reasons: tuple[str, ...]


def inspect(data: bytes, filename: str) -> Result:
    if not data or len(data) > MAX_BYTES:
        return Result('unknown', ('пустой или слишком большой файл',))
    binary = data.startswith(b'\x1bLua')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('latin-1')
        binary = True
    views = [text.lower()]
    # Decode simple Lua decimal and hex escapes without evaluating code.
    def unescape(m):
        try:
            return chr(int(m.group(1), 16) if m.group(1) else int(m.group(2)))
        except (ValueError, OverflowError):
            return m.group(0)
    views.append(re.sub(r'\\x([0-9a-fA-F]{2})|\\(\d{1,3})', unescape, text).lower())
    # Inspect bounded base64 literals and gzip payloads; these are encodings, not general decryption.
    packed = data
    if data.startswith(b'\x1f\x8b'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
                packed = f.read(MAX_BYTES + 1)
            if len(packed) > MAX_BYTES:
                return Result('unknown', ('распакованные данные превышают лимит',))
            views.append(packed.decode('utf-8', 'replace').lower())
        except (OSError, EOFError):
            return Result('unknown', ('ошибка распаковки gzip',))
    for match in list(re.finditer(r'(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])', text))[:32]:
        try:
            decoded = base64.b64decode(match.group(), validate=True)
            views.append(decoded.decode('utf-8', 'replace').lower())
        except (ValueError, binascii.Error):
            pass
    joined = '\n'.join(views)
    rules = {
        r'\bos\s*\.\s*execute\s*\(': 'запуск команд через os.execute',
        r'\bio\s*\.\s*popen\s*\(': 'запуск команд через io.popen',
        r'\b(?:rm\s+-[a-z]*r[a-z]*f|curl\b[^\n|]{0,300}\|\s*(?:sh|bash)|wget\b[^\n|]{0,300}\|\s*(?:sh|bash))': 'опасная системная команда',
        r'\b(?:token|password|пароль)\b.{0,100}\b(?:send|upload|post|webhook)\b': 'возможная передача секретов',
    }
    found = tuple(reason for pattern, reason in rules.items() if re.search(pattern, joined))
    if found:
        return Result('suspicious', found)
    if binary or filename.lower().endswith('.luac') or '\x00' in text:
        return Result('unknown', ('байткод или бинарные данные нельзя надёжно проверить этим анализатором',))
    if re.search(r'\b(?:loadstring|load|dofile|loadfile)\s*\(|\bstring\s*\.\s*char\s*\(|\b(?:decrypt|decode|unpack)\s*\(', joined) or re.search(r'[A-Za-z0-9+/]{100,}={0,2}', text):
        return Result('unknown', ('динамическая загрузка или возможная обфускация',))
    if data.startswith(b'\x1f\x8b'):
        return Result('unknown', ('распакованное содержимое требует ручной проверки',))
    return Result('clear', ())
