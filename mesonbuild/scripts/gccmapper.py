# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 The Meson development team

"""A GCC module mapper server, spawned by the compiler itself through
-fmodule-mapper='|<command>'.

GCC talks to it over stdin/stdout in the libcody line protocol: one request
per line, a batch of requests joined by a trailing ' ;' on every line but the
last, and one reply per request in the same shape. A word is bare when it
holds only [-+_/%.A-Za-z0-9]; anything else is single-quoted with \\\\, \\',
\\n, \\t and two-digit lower-case hex escapes for every other control byte.

The server only answers lookups. Every BMI it names is produced by an edge the
build already orders before the request is made, so it never builds, creates
or writes anything; it reads two kinds of JSON data file the build writes:

--units      The BMI class's declared header units of one target (or the
             declaring target of a header-unit edge): the unit's real path
             -> its BMI. Written at setup.
--table      One translation unit's lookups, written by the collator after
             the scan: named modules by name, header units by real path.
             Present on compile edges only.

Header units are matched by real path, not by spelling, so every spelling
that reaches one file (through any -I entry, or '..' from a subdirectory)
names the one BMI. With no table the request comes from a scan or a
header-unit edge: a named module is then only ever asked for its name
(NameOnly), which the scan records but never opens, so it is answered with
GCC's default name without needing the module to exist yet.

Only the standard library and what the 'meson' entry point has already
loaded may be imported here: the server runs once per compile.
"""

from __future__ import annotations

import json
import sys
import typing as T

from ..utils.core import header_unit_key, module_to_filename

# Cody::Flags::NameOnly: only the CMI name is wanted, never its contents.
NAME_ONLY = 1

_BARE = frozenset(b'-+_/%.'
                  b'abcdefghijklmnopqrstuvwxyz'
                  b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                  b'0123456789')
_HEX = b'0123456789abcdef'
_ESCAPES = {ord('\\'): ord('\\'), ord("'"): ord("'"), ord('n'): ord('\n'),
            ord('t'): ord('\t'), ord('_'): ord(' ')}


class MalformedLine(Exception):
    pass


def lex(line: bytes) -> T.List[str]:
    """Split one request line (without its newline or batch marker) into words.

    A word is a run of bare bytes and quoted stretches, so `a'b c'` is one
    word. Raw bytes above 0x7e are accepted inside quotes, as GCC sends a
    non-ASCII path without escaping it. Words are returned as str with
    undecodable bytes surrogate-escaped, so a path round-trips exactly.
    """
    words: T.List[bytearray] = []
    word: T.Optional[bytearray] = None
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        i += 1
        if c in b' \t':
            word = None
            continue
        if c < 0x20 or c == 0x7f:
            raise MalformedLine(line)
        if word is None:
            word = bytearray()
            words.append(word)
        if c != ord("'"):
            word.append(c)
            continue
        while True:
            if i >= n:
                raise MalformedLine(line)
            c = line[i]
            i += 1
            if c == ord("'"):
                break
            if c < 0x20:
                raise MalformedLine(line)
            if c == ord('\\'):
                if i >= n:
                    raise MalformedLine(line)
                esc = _ESCAPES.get(line[i])
                if esc is not None:
                    c = esc
                    i += 1
                else:
                    # One or two hex digits, as the protocol's own reader takes.
                    v = digits = 0
                    while digits < 2 and i < n and line[i] in _HEX:
                        v = v * 16 + _HEX.index(line[i])
                        i += 1
                        digits += 1
                    if not digits:
                        raise MalformedLine(line)
                    c = v
            word.append(c)
    return [bytes(w).decode('utf-8', 'surrogateescape') for w in words]


def quote(word: str) -> bytes:
    """Encode one word for a reply. Bytes above 0x7e are hex-escaped: older
    GCC readers reject them raw even inside quotes."""
    raw = word.encode('utf-8', 'surrogateescape')
    if raw and all(c in _BARE for c in raw):
        return raw
    out = bytearray(b"'")
    for c in raw:
        if c in (ord('\\'), ord("'")):
            out += bytes((ord('\\'), c))
        elif c == ord('\n'):
            out += b'\\n'
        elif c == ord('\t'):
            out += b'\\t'
        elif c < 0x20 or c >= 0x7f:
            out += b'\\' + _HEX[c >> 4:(c >> 4) + 1] + _HEX[c & 15:(c & 15) + 1]
        else:
            out.append(c)
    out += b"'"
    return bytes(out)


def split_batches(stream: T.Iterable[bytes]) -> T.Iterator[T.List[bytes]]:
    """Group request lines into batches. Every line of a batch but the last
    ends in ' ;'; a quoted word cannot end that way, since ';' is never bare
    and always sits inside its quotes."""
    batch: T.List[bytes] = []
    for raw in stream:
        line = raw.rstrip(b'\n').rstrip(b'\r')
        more = line.endswith(b' ;')
        batch.append(line[:-2] if more else line)
        if not more:
            yield batch
            batch = []
    if batch:
        yield batch


def is_header_unit(name: str) -> bool:
    # A module name is dotted identifiers with an optional ':partition'; a
    # header unit is named by its path, which always holds a separator (GCC
    # writes a relative one as './...').
    return '/' in name or '\\' in name


class Mapper:
    def __init__(self, bmi_class: str, units_path: str, table_path: T.Optional[str]) -> None:
        self.bmi_class = bmi_class
        self.units_path = units_path
        self.table_path = table_path
        self._units: T.Optional[T.Dict[str, T.Any]] = None
        self._table: T.Optional[T.Dict[str, T.Any]] = None
        self._real: T.Dict[str, str] = {}

    @staticmethod
    def _load(path: str) -> T.Dict[str, T.Any]:
        with open(path, encoding='utf-8') as f:
            return T.cast('T.Dict[str, T.Any]', json.load(f))

    @property
    def units(self) -> T.Dict[str, T.Any]:
        if self._units is None:
            self._units = self._load(self.units_path)
        return self._units

    @property
    def table(self) -> T.Optional[T.Dict[str, T.Any]]:
        if self._table is None and self.table_path is not None:
            self._table = self._load(self.table_path)
        return self._table

    def realpath(self, name: str) -> str:
        # GCC names a header by the path it reached it through, relative to
        # the working directory (the build directory) unless absolute.
        real = self._real.get(name)
        if real is None:
            real = self._real[name] = header_unit_key(name)
        return real

    def _undeclared(self, name: str) -> str:
        target = self.units.get('target')
        where = f' of target {target}' if target else ''
        return (f'header unit {name} is not declared in the cpp_header_units{where} '
                f'(BMI class {self.bmi_class}); add it there to import it')

    def header_unit(self, name: str) -> T.Tuple[bool, str]:
        real = self.realpath(name)
        table = self.table
        if table is not None:
            bmi = table.get('header_units', {}).get(real)
            if bmi is not None:
                return True, bmi
            if real in self.units.get('header_units', {}):
                return False, (f'header unit {name} is declared but missing from this '
                               f'translation unit\'s module table {self.table_path}; '
                               'its scan did not report importing it')
            return False, self._undeclared(name)
        bmi = self.units.get('header_units', {}).get(real)
        if bmi is not None:
            return True, bmi
        return False, self._undeclared(name)

    def module(self, name: str, flags: int) -> T.Tuple[bool, str]:
        table = self.table
        if table is not None:
            bmi = table.get('modules', {}).get(name)
            if bmi is not None:
                return True, bmi
            return False, f'module {name} is not in the module table {self.table_path}'
        if flags & NAME_ONLY:
            # Only the name is recorded; GCC never opens this path.
            return True, module_to_filename(name, 'gcm.cache', '.gcm')
        return False, f'module {name} cannot be resolved without a module table'

    def translate(self, name: str) -> bool:
        # An #include becomes an import exactly when the header has a BMI
        # here, as with a mapper file naming it.
        if self.table is not None:
            return self.realpath(name) in self.table.get('header_units', {})
        return self.realpath(name) in self.units.get('header_units', {})

    def answer(self, words: T.List[str]) -> bytes:
        verb = words[0] if words else ''
        if verb == 'HELLO':
            return b'HELLO 1 meson'
        if verb == 'MODULE-REPO':
            # Every path answered is complete, relative to the build directory.
            return b"PATHNAME ''"
        if verb in {'MODULE-EXPORT', 'MODULE-IMPORT', 'INCLUDE-TRANSLATE'} and len(words) >= 2:
            name = words[1]
            try:
                flags = int(words[2]) if len(words) > 2 else 0
            except ValueError:
                return b'ERROR ' + quote(f'malformed flags in {verb} {name}')
            if verb == 'INCLUDE-TRANSLATE':
                if not self.translate(name):
                    return b'BOOL FALSE'
                ok, text = self.header_unit(name)
            elif is_header_unit(name):
                ok, text = self.header_unit(name)
            else:
                ok, text = self.module(name, flags)
            return (b'PATHNAME ' if ok else b'ERROR ') + quote(text)
        if verb == 'MODULE-COMPILED' and len(words) >= 2:
            return b'OK'
        return b'ERROR ' + quote(f'unsupported request {" ".join(words)}')


def serve(mapper: Mapper, stdin: T.BinaryIO, stdout: T.BinaryIO) -> None:
    for batch in split_batches(stdin):
        replies = []
        for line in batch:
            try:
                replies.append(mapper.answer(lex(line)))
            except MalformedLine:
                replies.append(b'ERROR ' + quote('malformed request ' +
                                                 line.decode('utf-8', 'surrogateescape')))
        stdout.write(b' ;\n'.join(replies) + b'\n')
        stdout.flush()


def run(args: T.List[str]) -> int:
    # Hand-parsed: three options, all required but --table, on a hot path.
    opts: T.Dict[str, str] = {}
    it = iter(args)
    for arg in it:
        if arg not in {'--bmi-class', '--units', '--table'}:
            print(f'gccmapper: unknown argument {arg!r}', file=sys.stderr)
            return 2
        val = next(it, None)
        if val is None:
            print(f'gccmapper: {arg} needs a value', file=sys.stderr)
            return 2
        opts[arg] = val
    if '--bmi-class' not in opts or '--units' not in opts:
        print('gccmapper: --bmi-class and --units are required', file=sys.stderr)
        return 2
    mapper = Mapper(opts['--bmi-class'], opts['--units'], opts.get('--table'))
    serve(mapper, sys.stdin.buffer, sys.stdout.buffer)
    return 0
