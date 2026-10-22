# SPDX-License-Identifier: Apache-2.0
# Copyright 2012-2022 The Meson development team

"""
Contains the strict minimum to run scripts.

When the backend needs to call back into Meson during compilation for running
scripts or wrapping commands, it is important to load as little python modules
as possible for performance reasons.
"""

from __future__ import annotations
from dataclasses import dataclass
import os
import typing as T

if T.TYPE_CHECKING:
    from hashlib import _Hash
    from typing_extensions import Literal, Self
    from ..mparser import BaseNode
    from .. import programs
    from .universal import SubProject

    EnvironOrDict = T.Union[T.Dict[str, str], os._Environ[str]]

    EnvInitValueType = T.Dict[str, T.Union[str, T.List[str]]]


class MesonException(Exception):
    '''Exceptions thrown by Meson'''

    def __init__(self, *args: object, file: T.Optional[str] = None,
                 lineno: T.Optional[int] = None, colno: T.Optional[int] = None):
        super().__init__(*args)
        self.file = file
        self.lineno = lineno
        self.colno = colno

    @classmethod
    def from_node(cls, *args: object, node: BaseNode) -> Self:
        """Create a MesonException with location data from a BaseNode

        :param node: A BaseNode to set location data from
        :return: A Meson Exception instance
        """
        return cls(*args, file=node.filename, lineno=node.lineno, colno=node.colno)

class MesonBugException(MesonException):
    '''Exceptions thrown when there is a clear Meson bug that should be reported'''

    def __init__(self, msg: str, file: T.Optional[str] = None,
                 lineno: T.Optional[int] = None, colno: T.Optional[int] = None):
        super().__init__(msg + '\n\n    This is a Meson bug and should be reported!',
                         file=file, lineno=lineno, colno=colno)

class HoldableObject:
    ''' Dummy base class for all objects that can be
        held by an interpreter.baseobjects.ObjectHolder '''
    def __new__(cls, *args: T.Any, **kwargs: T.Any) -> HoldableObject:
        if cls is HoldableObject:
            raise TypeError(f"Can't instantiate abstract class {cls.__name__}")
        return super().__new__(cls)

class EnvironmentVariables(HoldableObject):
    def __init__(self, values: T.Optional[EnvInitValueType] = None,
                 init_method: Literal['set', 'prepend', 'append'] = 'set', separator: str = os.pathsep) -> None:
        self.envvars: T.List[T.Tuple[T.Callable[[T.Dict[str, str], str, T.List[str], str, T.Optional[str]], str], str, T.List[str], str]] = []
        # The set of all env vars we have operations for. Only used for self.has_name()
        self.varnames: T.Set[str] = set()
        self.unset_vars: T.Set[str] = set()
        self.can_use_env = True

        if values:
            init_func = getattr(self, init_method)
            for name, value in values.items():
                v = value if isinstance(value, list) else [value]
                init_func(name, v, separator)

    def __repr__(self) -> str:
        repr_str = "<{0}: {1}>"
        return repr_str.format(self.__class__.__name__, self.envvars)

    def hash(self, hasher: _Hash) -> None:
        myenv = self.get_env({})
        for key in sorted(myenv.keys()):
            hasher.update(bytes(key, encoding='utf-8'))
            hasher.update(b',')
            hasher.update(bytes(myenv[key], encoding='utf-8'))
            hasher.update(b';')

    def has_name(self, name: str) -> bool:
        return name in self.varnames

    def get_names(self) -> T.Set[str]:
        return self.varnames

    def merge(self, other: EnvironmentVariables) -> None:
        for method, name, values, separator in other.envvars:
            self.varnames.add(name)
            self.envvars.append((method, name, values, separator))
            if name in self.unset_vars:
                self.unset_vars.remove(name)
        if other.unset_vars:
            self.can_use_env = False
            self.unset_vars.update(other.unset_vars)

    def set(self, name: str, values: T.List[str], separator: str = os.pathsep) -> None:
        if name in self.unset_vars:
            raise MesonException(f'You cannot set the already unset variable {name!r}')
        self.varnames.add(name)
        self.envvars.append((self._set, name, values, separator))

    def unset(self, name: str) -> None:
        self.can_use_env = False
        if name in self.varnames:
            raise MesonException(f'You cannot unset the {name!r} variable because it is already set')
        self.unset_vars.add(name)

    def append(self, name: str, values: T.List[str], separator: str = os.pathsep) -> None:
        self.can_use_env = False
        if name in self.unset_vars:
            raise MesonException(f'You cannot append to unset variable {name!r}')
        self.varnames.add(name)
        self.envvars.append((self._append, name, values, separator))

    def prepend(self, name: str, values: T.List[str], separator: str = os.pathsep) -> None:
        self.can_use_env = False
        if name in self.unset_vars:
            raise MesonException(f'You cannot prepend to unset variable {name!r}')
        self.varnames.add(name)
        self.envvars.append((self._prepend, name, values, separator))

    @staticmethod
    def _set(env: T.Dict[str, str], name: str, values: T.List[str], separator: str, default_value: T.Optional[str]) -> str:
        return separator.join(values)

    @staticmethod
    def _append(env: T.Dict[str, str], name: str, values: T.List[str], separator: str, default_value: T.Optional[str]) -> str:
        curr = env.get(name, default_value)
        return separator.join(values if curr is None else [curr] + values)

    @staticmethod
    def _prepend(env: T.Dict[str, str], name: str, values: T.List[str], separator: str, default_value: T.Optional[str]) -> str:
        curr = env.get(name, default_value)
        return separator.join(values if curr is None else values + [curr])

    def get_env(self, full_env: EnvironOrDict, default_fmt: T.Optional[str] = None) -> T.Dict[str, str]:
        env = full_env.copy()
        for method, name, values, separator in self.envvars:
            default_value = default_fmt.format(name) if default_fmt else None
            env[name] = method(env, name, values, separator, default_value)
        for name in self.unset_vars:
            env.pop(name, None)
        return env


@dataclass(eq=False)
class ExecutableSerialisation:

    cmd_args: T.List[str]
    env: T.Optional[EnvironmentVariables] = None
    exe_wrapper: T.Optional['programs.ExternalProgram'] = None
    workdir: T.Optional[str] = None
    extra_paths: T.Optional[T.List] = None
    capture: T.Optional[str] = None
    feed: T.Optional[str] = None
    tag: T.Optional[str] = None
    verbose: bool = False
    installdir_map: T.Optional[T.Dict[str, str]] = None

    def __post_init__(self) -> None:
        self.pickled = False
        self.skip_if_destdir = False
        self.subproject = T.cast('SubProject', '')  # avoid circular import
        self.dry_run = False


# The suffixes that mark a source a C++ module interface by extension alone
# (what a future suffix-inference removal would delete in one place).
CPP_MODULE_INTERFACE_SUFFIXES = frozenset({'cppm', 'ixx'})


def module_to_filename(name: str, bmidir: str, suffix: str) -> str:
    """The BMI path a compiler names a C++ module's interface at.

    The mapping is <bmidir>/<name><suffix> with a partition separator ':'
    becoming '-' (gcm.cache/pkg-part.gcm for GCC, ifc.cache/pkg-part.ifc for
    MSVC). Two sides need it and must never disagree: the compiler layer
    (Compiler.module_name_to_filename, which supplies the dir and suffix from
    the compiler itself) and the collator (scripts/depaccumulate.py), which
    names BMIs from logical-names alone because the P1689 output carries no
    compiled-module-path. It lives here rather than on the compiler side
    because the collator has no Compiler instance to ask: it receives the
    cache dir and BMI suffix as command-line arguments, so a method deriving
    them from ``self`` is not one it could ever call. That it is also a
    build-time subprocess run once per edge, where import weight is worth
    something, is a lesser reason pointing the same way.
    """
    return f'{bmidir}/{name.replace(":", "-")}{suffix}'


def header_unit_key(path: str) -> str:
    """How a GCC module mapper program identifies a header unit: the real
    path of whatever name the compiler reached the header by, so every
    spelling of one file -- through any include directory, '..' or a link --
    is one unit. On Windows realpath also returns the on-disk case, so a
    header reached in another case is the same unit there. It is not
    case-folded by platform: that would merge two files a case-sensitive
    directory keeps apart. Where a case-insensitive filesystem is reached in
    two cases on POSIX, the two keys differ and the second import is reported
    as undeclared rather than silently merged.
    Setup, the collator and the server must all key units this way."""
    return os.path.realpath(path)
