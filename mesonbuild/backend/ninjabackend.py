# SPDX-License-Identifier: Apache-2.0
# Copyright 2012-2017 The Meson development team
# Copyright © 2023-2025 Intel Corporation

from __future__ import annotations

from collections import Counter, defaultdict, OrderedDict
from dataclasses import dataclass
from enum import Enum, unique
from functools import lru_cache
from pathlib import PurePath, Path
from textwrap import dedent
import dataclasses
import hashlib
import itertools
import json
import os
import pickle
import re
import subprocess
import typing as T

from . import backends
from .. import modules
from .. import mesonlib
from .. import build
from .. import mlog
from .. import compilers
from .. import tooldetect
from ..arglist import CompilerArgs
from ..compilers import Compiler, is_library
from ..linkers import ArLikeLinker, RSPFileSyntax, StaticLinker
from ..mesonlib import (
    File, LibType, MachineChoice, MesonBugException, MesonException, OrderedSet, PerMachine,
    ProgressBar, quote_arg, unique_list
)
from ..mesonlib import get_compiler_for_source, has_path_sep, is_parent_path, lookbehind, path_has_root
from ..options import OptionKey
from ..utils.core import default_cmi_path
from .backends import CleanTrees
from ..build import GeneratedList, InvalidArguments

if T.TYPE_CHECKING:
    from typing_extensions import Literal, TypedDict

    from .._typing import ImmutableListProtocol
    from ..compilers.compilers import Language
    from ..compilers.cs import CsCompiler
    from ..compilers.fortran import FortranCompiler
    from ..compilers.rust import RustCompiler
    from ..compilers.swift import SwiftCompiler
    from ..mesonlib import FileOrString
    from .backends import TargetIntrospectionData, CompilerIntrospectionData, LinkerIntrospectionData

    CommandArgTypes = T.TypeVar('CommandArgTypes', 'NinjaCommandArg', str, 'NinjaCommandArg | str')
    CommandArgs = T.List[CommandArgTypes]
    FileList = T.List[File] | T.List[str] | T.List[File | str]
    ListifiedStr = str | T.List[str]
    RUST_EDITIONS = Literal['2015', '2018', '2021', '2024']

    class NinjaRuleArgs(TypedDict, total=False):
        rspable: bool
        rspfile_quote_style: RSPFileSyntax


FORTRAN_INCLUDE_PAT = r"^\s*#?include\s*['\"](\w+\.\w+)['\"]"
FORTRAN_MODULE_PAT = r"^\s*\bmodule\b\s+(\w+)\s*(?:!+.*)*$"
FORTRAN_SUBMOD_PAT = r"^\s*\bsubmodule\b\s*\((\w+:?\w+)\)\s*(\w+)"
FORTRAN_USE_PAT = r"^\s*use,?\s*(?:non_intrinsic)?\s*(?:::)?\s*(\w+)"

def cmd_quote(arg: str) -> str:
    # see: https://docs.microsoft.com/en-us/windows/desktop/api/shellapi/nf-shellapi-commandlinetoargvw#remarks

    # backslash escape any existing double quotes
    # any existing backslashes preceding a quote are doubled
    arg = re.sub(r'(\\*)"', lambda m: '\\' * (len(m.group(1)) * 2 + 1) + '"', arg)
    # any terminal backslashes likewise need doubling
    arg = re.sub(r'(\\*)$', lambda m: '\\' * (len(m.group(1)) * 2), arg)
    # and double quote
    arg = f'"{arg}"'

    return arg

# How ninja executes command lines differs between Unix and Windows
# (see https://ninja-build.org/manual.html#ref_rule_command)
if mesonlib.is_windows():
    quote_func = cmd_quote
    execute_wrapper = ['cmd', '/c']  # unused
    rmfile_prefix = ['del', '/f', '/s', '/q', '{}', '&&']
else:
    quote_func = quote_arg
    execute_wrapper = []
    rmfile_prefix = ['rm', '-f', '{}', '&&']

def gcc_rsp_quote(s: str) -> str:
    # see: the function buildargv() in libiberty
    #
    # this differs from sh-quoting in that a backslash *always* escapes the
    # following character, even inside single quotes.

    s = s.replace('\\', '\\\\')

    return quote_func(s)

# a conservative estimate of the command-line length limit
rsp_threshold = mesonlib.get_rsp_threshold()

# ninja variables whose value should remain unquoted. The value of these ninja
# variables (or variables we use them in) is interpreted directly by ninja
# (e.g. the value of the depfile variable is a pathname that ninja will read
# from, etc.), so it must not be shell quoted.
raw_names = {'DEPFILE_UNQUOTED', 'DESC', 'pool', 'description', 'targetdep', 'dyndep'}

NINJA_QUOTE_BUILD_PAT = re.compile(r"[$ :\n]")
NINJA_QUOTE_VAR_PAT = re.compile(r"[$ \n]")

def ninja_quote(text: str, is_build_line: bool = False) -> str:
    if '\n' in text:
        errmsg = f'''Ninja does not support newlines in rules. The content was:

{text}

Please report this error with a test case to the Meson bug tracker.'''
        raise MesonException(errmsg)

    quote_re = NINJA_QUOTE_BUILD_PAT if is_build_line else NINJA_QUOTE_VAR_PAT
    if ' ' in text or '$' in text or (is_build_line and ':' in text):
        return quote_re.sub(r'$\g<0>', text)

    return text


@dataclass
class TargetDependencyScannerInfo:

    """Information passed to the depscanner about a target.

    :param private_dir: The private scratch directory for the target.
    :param source2object: A mapping of source file names to the objects that
        will be created from them.
    :param sources: a list of sources mapping them to the language rules to use
        to scan them.
    """

    private_dir: str
    source2object: T.Dict[str, str]
    sources: T.List[T.Tuple[str, Literal['cpp', 'fortran']]]


@unique
class Quoting(Enum):
    both = 0
    notShell = 1
    notNinja = 2
    none = 3

class NinjaCommandArg:
    def __init__(self, s: str, quoting: Quoting = Quoting.both) -> None:
        self.s = s
        self.quoting = quoting

    def __str__(self) -> str:
        return self.s

    @staticmethod
    def list(l: T.List[str], q: Quoting) -> T.List[NinjaCommandArg]:
        return [NinjaCommandArg(i, q) for i in l]

@dataclass
class NinjaComment:
    comment: str

    def write(self, outfile: T.TextIO) -> None:
        for l in self.comment.split('\n'):
            outfile.write('# ')
            outfile.write(l)
            outfile.write('\n')
        outfile.write('\n')

class NinjaRule:
    def __init__(self, rule: str, command: CommandArgs, args: CommandArgs,
                 description: str, rspable: bool = False, deps: T.Optional[str] = None,
                 depfile: T.Optional[str] = None, extra: T.Optional[str] = None,
                 rspfile_quote_style: RSPFileSyntax = RSPFileSyntax.GCC,
                 restat: bool = False):

        def strToCommandArg(c: T.Union[NinjaCommandArg, str]) -> NinjaCommandArg:
            if isinstance(c, NinjaCommandArg):
                return c

            # deal with common cases here, so we don't have to explicitly
            # annotate the required quoting everywhere
            if c == '&&':
                # shell constructs shouldn't be shell quoted
                return NinjaCommandArg(c, Quoting.notShell)
            if c.startswith('$'):
                varp = mesonlib.unwrap(re.match(r'\$\{?(\w*)\}?', c))
                var: str = varp.group(1)
                if var not in raw_names:
                    # ninja variables shouldn't be ninja quoted, and their value
                    # is already shell quoted
                    return NinjaCommandArg(c, Quoting.none)
                else:
                    # shell quote the use of ninja variables whose value must
                    # not be shell quoted (as it also used by ninja)
                    return NinjaCommandArg(c, Quoting.notNinja)

            return NinjaCommandArg(c)

        self.name = rule
        self.command: T.List[NinjaCommandArg] = [strToCommandArg(c) for c in command]  # includes args which never go into a rspfile
        self.args: T.List[NinjaCommandArg] = [strToCommandArg(a) for a in args]  # args which will go into a rspfile, if used
        self.description = description
        self.deps = deps  # depstyle 'gcc' or 'msvc'
        self.depfile = depfile
        self.extra = extra
        self.restat = restat
        self.rspable = rspable  # if a rspfile can be used
        self.refcount = 0
        self.rsprefcount = 0
        self.rspfile_quote_style = rspfile_quote_style
        self.command_str = ' '.join([self._quoter(x) for x in self.command + self.args])
        self.var_refs = [m for m in re.finditer(r'(\${\w+}|\$\w+)?[^$]*', self.command_str)
                         if m.start(1) != -1]

        if self.depfile == '$DEPFILE':
            self.depfile += '_UNQUOTED'

    @staticmethod
    def _quoter(x: NinjaCommandArg, qf: T.Callable[[str], str] = quote_func) -> str:
        if x.quoting == Quoting.none:
            return x.s
        elif x.quoting == Quoting.notNinja:
            return qf(x.s)
        elif x.quoting == Quoting.notShell:
            return ninja_quote(x.s)
        return ninja_quote(qf(str(x)))

    def write(self, outfile: T.TextIO) -> None:
        rspfile_args = self.args
        rspfile_quote_func: T.Callable[[str], str]
        if self.rspfile_quote_style in {RSPFileSyntax.MSVC, RSPFileSyntax.TASKING}:
            rspfile_quote_func = cmd_quote
            rspfile_args = [NinjaCommandArg('$in_newline', arg.quoting) if arg.s == '$in' else arg for arg in rspfile_args]
        else:
            rspfile_quote_func = gcc_rsp_quote

        def rule_iter() -> T.Iterable[str]:
            if self.refcount:
                yield ''
            if self.rsprefcount:
                yield '_RSP'

        for rsp in rule_iter():
            outfile.write(f'rule {self.name}{rsp}\n')
            if rsp == '_RSP':
                if self.rspfile_quote_style is RSPFileSyntax.TASKING:
                    outfile.write(' command = {} --option-file=$out.rsp\n'.format(' '.join([self._quoter(x) for x in self.command])))
                else:
                    outfile.write(' command = {} @$out.rsp\n'.format(' '.join([self._quoter(x) for x in self.command])))
                outfile.write(' rspfile = $out.rsp\n')
                outfile.write(' rspfile_content = {}\n'.format(' '.join([self._quoter(x, rspfile_quote_func) for x in rspfile_args])))
            else:
                outfile.write(' command = {}\n'.format(self.command_str))
            if self.deps:
                outfile.write(f' deps = {self.deps}\n')
            if self.depfile:
                outfile.write(f' depfile = {self.depfile}\n')
            outfile.write(f' description = {self.description}\n')
            if self.restat:
                outfile.write(' restat = 1\n')
            if self.extra:
                for l in self.extra.split('\n'):
                    outfile.write(' ')
                    outfile.write(l)
                    outfile.write('\n')
            outfile.write('\n')

    def _length_estimate(self, infiles: str, outfiles: str,
                         elems: T.List[T.Tuple[str, T.List[str]]]) -> int:
        # determine variables
        # this order of actions only approximates ninja's scoping rules, as
        # documented at: https://ninja-build.org/manual.html#ref_scope
        ninja_vars = dict(elems)
        if self.deps is not None:
            ninja_vars['deps'] = [self.deps]
        if self.depfile is not None:
            ninja_vars['depfile'] = [self.depfile]
        ninja_vars['in'] = [infiles]
        ninja_vars['out'] = [outfiles]

        # expand variables in command
        estimate = len(self.command_str)
        for m in self.var_refs:
            estimate -= m.end(1) - m.start(1)
            chunk = m.group(1)
            if chunk[1] == '{':
                chunk = chunk[2:-1]
            else:
                chunk = chunk[1:]
            chunk = ninja_vars.get(chunk, []) # undefined ninja variables are empty
            estimate += len(' '.join(chunk))

        # determine command length
        return estimate

    def should_use_rspfile(self, element: NinjaBuildElement) -> bool:
        if not self.rspable:
            return False

        infilenames = ' '.join([ninja_quote(i, True) for i in element.infilenames])
        outfilenames = ' '.join([ninja_quote(i, True) for i in element.outfilenames])

        return self._length_estimate(infilenames,
                                     outfilenames,
                                     element.elems) >= rsp_threshold

class NinjaBuildElement:

    rule: NinjaRule

    def __init__(self, all_outputs: T.Set[str], outfilenames: ListifiedStr, rulename: str, infilenames: ListifiedStr, implicit_outs: T.Optional[T.List[str]] = None):
        self.implicit_outfilenames = implicit_outs or []
        if isinstance(outfilenames, str):
            self.outfilenames = [outfilenames]
        else:
            self.outfilenames = outfilenames
        assert isinstance(rulename, str)
        self.rulename = rulename
        if isinstance(infilenames, str):
            self.infilenames = [infilenames]
        else:
            self.infilenames = infilenames
        self.deps: T.Set[str] = set()
        self.orderdeps: T.Set[str] = set()
        self.elems: T.List[T.Tuple[str, T.List[str]]] = []
        self.all_outputs = all_outputs
        self.output_errors = ''

    def add_dep(self, dep: ListifiedStr) -> None:
        if isinstance(dep, list):
            self.deps.update(dep)
        else:
            self.deps.add(dep)

    def add_orderdep(self, dep: ListifiedStr) -> None:
        if isinstance(dep, list):
            self.orderdeps.update(dep)
        else:
            self.orderdeps.add(dep)

    def add_item(self, name: str, elems: T.Union[ListifiedStr, CompilerArgs]) -> None:
        # Always convert from GCC-style argument naming to the naming used by the
        # current compiler. Also filter system include paths, deduplicate, etc.
        if isinstance(elems, CompilerArgs):
            elems = elems.to_native()
        if isinstance(elems, str):
            elems = [elems]
        self.elems.append((name, elems))

        if name == 'DEPFILE':
            self.elems.append((name + '_UNQUOTED', elems))

    @mesonlib.lazy_property
    def _should_use_rspfile(self) -> bool:
        # 'phony' is a rule built-in to ninja
        if self.rulename == 'phony':
            return False

        if not self.rule:
            raise MesonBugException(f"build statement for {self.outfilenames} references unmapped rule {self.rulename}")

        return self.rule.should_use_rspfile(self)

    def count_rule_references(self) -> None:
        if self.rulename != 'phony':
            if self._should_use_rspfile:
                self.rule.rsprefcount += 1
            else:
                self.rule.refcount += 1

    def write(self, outfile: T.TextIO) -> None:
        if self.output_errors:
            raise MesonException(self.output_errors)
        ins = ' '.join([ninja_quote(i, True) for i in self.infilenames])
        outs = ' '.join([ninja_quote(i, True) for i in self.outfilenames])
        implicit_outs = ' '.join([ninja_quote(i, True) for i in self.implicit_outfilenames])
        if implicit_outs:
            implicit_outs = ' | ' + implicit_outs
        use_rspfile = self._should_use_rspfile
        if use_rspfile:
            rulename = self.rulename + '_RSP'
            mlog.debug(f'Command line for building {self.outfilenames} is long, using a response file')
        else:
            rulename = self.rulename
        line = f'build {outs}{implicit_outs}: {rulename} {ins}'
        if len(self.deps) > 0:
            line += ' | ' + ' '.join([ninja_quote(x, True) for x in sorted(self.deps)])
        if len(self.orderdeps) > 0:
            orderdeps = [str(x) for x in self.orderdeps]
            line += ' || ' + ' '.join([ninja_quote(x, True) for x in sorted(orderdeps)])
        line += '\n'
        # This is the only way I could find to make this work on all
        # platforms including Windows command shell. Slash is a dir separator
        # on Windows, too, so all characters are unambiguous and, more importantly,
        # do not require quoting, unless explicitly specified, which is necessary for
        # the csc compiler.
        line = line.replace('\\', '/')
        if mesonlib.is_windows():
            # Support network paths as backslash, otherwise they are interpreted as
            # arguments for compile/link commands when using MSVC
            line = ' '.join(
                (l.replace('//', '\\\\', 1) if l.startswith('//') else l)
                for l in line.split(' ')
            )
        outfile.write(line)

        if use_rspfile:
            if self.rule.rspfile_quote_style in {RSPFileSyntax.MSVC, RSPFileSyntax.TASKING}:
                qf = cmd_quote
            else:
                qf = gcc_rsp_quote
        else:
            qf = quote_func

        for e in self.elems:
            (name, elems) = e
            should_quote = name not in raw_names
            line = f' {name} = '
            newelems = []
            for i in elems:
                if mesonlib.is_windows():
                    # Support network paths with double-backslash (UNC)
                    # Officially //foo/bar is not an UNC and mostly doesn't work
                    # in Windows
                    if i.startswith('//'):
                        i = i.replace('//', '\\\\', 1)
                if not should_quote or i == '&&': # Hackety hack hack
                    newelems.append(ninja_quote(i))
                else:
                    newelems.append(ninja_quote(qf(i)))
            line += ' '.join(newelems)
            line += '\n'
            outfile.write(line)
        outfile.write('\n')

    def check_outputs(self) -> None:
        for n in self.outfilenames:
            if n in self.all_outputs:
                self.output_errors = f'Multiple producers for Ninja target "{n}". Please rename your targets.'
            self.all_outputs.add(n)

@dataclass
class NinjaBuild:
    rules: list[NinjaRule | NinjaComment] = dataclasses.field(default_factory=list, init=False)
    ruledict: dict[str, NinjaRule] = dataclasses.field(default_factory=dict, init=False)
    build_elements: list[NinjaBuildElement | NinjaComment] = dataclasses.field(default_factory=list, init=False)

    def add_rule_comment(self, comment: NinjaComment) -> None:
        self.rules.append(comment)

    def add_build_comment(self, comment: NinjaComment) -> None:
        self.build_elements.append(comment)

    def add_rule(self, rule: NinjaRule) -> None:
        if rule.name in self.ruledict:
            raise MesonException(f'Tried to add rule {rule.name} twice.')
        self.rules.append(rule)
        self.ruledict[rule.name] = rule

    def has_rule(self, name: str) -> bool:
        return name in self.ruledict

    def should_use_rspfile(self, build: NinjaBuildElement) -> bool:
        if build.rulename == 'phony':
            return False

        if build.rulename in self.ruledict:
            return self.ruledict[build.rulename].should_use_rspfile(build)

        mlog.warning(f"build statement for {build.outfilenames} references nonexistent rule {build.rulename}")
        return False

    def add_build(self, build: NinjaBuildElement) -> None:
        build.check_outputs()
        self.build_elements.append(build)

        if build.rulename != 'phony':
            # reference rule
            if build.rulename in self.ruledict:
                build.rule = self.ruledict[build.rulename]
            else:
                mlog.warning(f"build statement for {build.outfilenames} references nonexistent rule {build.rulename}")

    def write(self, outfile: T.TextIO) -> None:
        for b in self.build_elements:
            if isinstance(b, NinjaBuildElement):
                b.count_rule_references()

        for r in self.rules:
            r.write(outfile)

        for b in ProgressBar(self.build_elements, desc='Writing build.ninja'):
            b.write(outfile)

@dataclass
class RustDep:

    name: str

    # equal to the order value of the `RustCrate`
    crate: int

    def to_json(self) -> T.Dict[str, object]:
        return {
            "crate": self.crate,
            "name": self.name,
        }

@dataclass
class RustCrate:

    # When the json file is written, the list of Crates will be sorted by this
    # value
    order: int

    display_name: str
    root_module: str
    crate_type: str
    target_name: str
    edition: RUST_EDITIONS
    deps: T.List[RustDep]
    cfg: T.List[str]

    # This is set to True for members of this project, and False for all
    # subprojects
    is_workspace_member: bool
    proc_macro_dylib_path: T.Optional[str] = None

    def to_json(self) -> T.Dict[str, object]:
        ret: T.Dict[str, object] = {
            "display_name": self.display_name,
            "root_module": self.root_module,
            "edition": self.edition,
            "cfg": self.cfg,
            "is_proc_macro": self.proc_macro_dylib_path is not None,
            "deps": [d.to_json() for d in self.deps],
        }

        if (proc_macro := self.proc_macro_dylib_path) is not None:
            ret["proc_macro_dylib_path"] = proc_macro

        return ret


@dataclass
class ImportStdInfo:
    gen_target: NinjaBuildElement
    gen_module_file: str
    gen_objects: T.List[str]


@dataclass
class BmiClassInfo:
    """A BMI equivalence class present in this build (Compiler.get_bmi_class_key).

    subdir is the class's subdirectory of the module cache, or None when the
    machine has a single class (flat cache, command lines identical to a
    build without BMI classes). relevant_args keeps the class's BMI-relevant
    flags in original command-line order for variant compiles.
    """
    subdir: T.Optional[str]
    relevant_args: T.List[str]


@dataclass
class ModuleInterfaceSource:
    """A source a module provider compiled as an interface unit, recorded so
    a BMI-only variant can recompile it under another class."""
    rel_src: str
    obj_basename: str
    header_deps: T.Tuple[FileOrString, ...]
    order_deps: T.Tuple[FileOrString, ...]
    # An internal (implementation) partition: cl flags it /internalPartition
    # rather than /interface, in the variant recompile as on the compile edge.
    is_internal_partition: bool = False
    # A private interface (cpp_private_module_interfaces, or every interface
    # of a module-providing executable): excluded from BMI-only variant
    # recompilation, since no consumer outside the target could ever import
    # it and compiling it there would be wasted work at best.
    is_private: bool = False


@dataclass
class BmiVariant:
    """A synthesized BMI-only recompilation of a provider's module interfaces
    for one BMI class. Emits BMIs into the class's cache subdir and no
    objects; consumers link the original provider."""
    private_dir: str
    provmap: str
    dyndep: str

class NinjaBackend(backends.Backend):

    def __init__(self, build: T.Optional[build.Build]):
        super().__init__(build)
        self.name = 'ninja'
        self.ninja = NinjaBuild()
        self.ninja_filename = 'build.ninja'
        self.fortran_deps: T.Dict[str, T.Dict[str, File]] = {}
        self.all_outputs: T.Set[str] = set()
        self.all_pch: T.Dict[str, T.Set[str]] = defaultdict(set)
        self.all_structured_sources: T.Set[str] = set()
        # 1st level: target; 2nd: compiler or linker; 3rd: individual keys
        self.introspection_data: dict[str, dict[tuple[str, tuple[str, ...]] | tuple[str, ...],
                                                TargetIntrospectionData]] = {}
        self.created_llvm_ir_rule = PerMachine(False, False)
        self.rust_crates: T.Dict[str, RustCrate] = {}
        self.implicit_meson_outs: T.List[str] = []
        self._uses_dyndeps = False
        self._generated_header_cache: T.Dict[str, T.List[FileOrString]] = {}
        # nvcc chokes on thin archives:
        #   nvlink fatal   : Could not open input file 'libfoo.a.p'
        #   nvlink fatal   : elfLink internal error
        # hence we disable them if 'cuda' is enabled globally. See also
        # - https://github.com/mesonbuild/meson/pull/9453
        # - https://github.com/mesonbuild/meson/issues/9479#issuecomment-953485040
        self.allow_thin_archives = PerMachine[bool](True, True)
        self.import_std: T.Optional[ImportStdInfo] = None
        # (unit key, consumer id) pairs already warned about for a header unit
        # shared across dialects, so each divergence is reported once; the class
        # that first declared each unit is recorded at edge creation.
        self._warned_header_unit_divergence: T.Set[T.Tuple[str, str]] = set()
        # (mode, spelling) -> the BMI class, target and machine that first
        # declared it, for the divergence warning on a unit that still shares
        # one flat BMI: the degraded path where per-class naming is unavailable
        # (see warn_on_header_unit_divergence). The machine is folded in because
        # that flat BMI is shared build-wide, so two machines declaring one unit
        # there also collide -- across compilers whose BMIs do not interchange.
        self._header_unit_class: T.Dict[str, T.Tuple[T.Tuple[str, ...], str, MachineChoice]] = {}
        # Spellings already warned about for a header unit GCC cannot name.
        # Keyed by the unit, not the importer: the unit is what is unnameable,
        # and every target importing it fails the same way.
        self._warned_header_unit_names: T.Set[str] = set()
        # (target id, spelling) already warned about for a header unit the
        # target's own forced includes pull in ahead of it. Keyed by the
        # importer too, unlike the unnameable warning: it is the target's args
        # that are wrong, and another target declaring the same unit is fine.
        self._warned_preincluded_header_units: T.Set[T.Tuple[str, str]] = set()
        # (real directory, class tag) -> build-relative alias root (None if the
        # platform could not make one), for GCC header-unit naming. The class
        # tag is None for a space-free mapper key and a BMI-class digest for a
        # per-class unit name; the two keyings never share a root.
        self._dir_aliases: T.Dict[T.Tuple[str, T.Optional[str]], T.Optional[str]] = {}
        # Inverse of the above for the roots that exist: alias root ->
        # real directory, so an already-aliased path can be re-aliased to
        # another class (a BMI-only variant re-targets the provider's spelling).
        self._alias_root_real: T.Dict[str, str] = {}
        # Whether this build tree can make directory links at all, decided once
        # (a canary link) so every target agrees: two same-class targets that
        # disagreed would compute two names for one BMI. None until probed.
        self._aliasing_available: T.Optional[bool] = None
        # Where the compiler resolves a header unit to, memoised: the probe
        # spawns the compiler, and a unit is declared by many targets. The
        # value is (resolved path or None, whether the compiler exited clean).
        self._probed_header_units: T.Dict[T.Tuple[T.Any, ...], T.Tuple[T.Optional[str], bool]] = {}
        # The compiler's built-in system include chain, in search order,
        # probed once per (machine, compiler): a GCC system-mode header unit
        # is named through this chain, so aliasing it renames the unit per
        # class. Keyed by (for_machine, id, exelist); the built-in chain does
        # not vary with a target's own include flags.
        self._gcc_include_chains: T.Dict[T.Tuple[T.Any, ...], T.List[str]] = {}
        # Machines whose system-unit chain aliasing has already reported a
        # degradation, so the warning that a chain-alias root could not be
        # placed fires once per (machine, compiler) rather than per declarer.
        self._warned_system_chain_alias: T.Set[T.Tuple[T.Any, ...]] = set()
        # Header units: global dedup of unit build edges, keyed by
        # (mode, resolved header identity) -- plus the declarer's BMI class where
        # the compiler builds units per class -> the edge output (the real BMI on
        # every compiler). The identity is the header's real path, so two targets
        # spelling one unit alike but resolving it to different files earn
        # distinct BMIs; a unit with no identity to resolve -- a system header, or
        # a user one off the -I path -- falls back to its spelling, where that
        # spelling is the only axis a same-name collision could split on anyway.
        # Plus per-target caches of those outputs
        # (ordered before the target's scans/compiles), of the flags MSVC and
        # clang compiles need to name each unit's BMI, and of the (resolved name,
        # BMI) pairs a GCC target's mapper lines are written from.
        self._header_units: T.Dict[str, str] = {}
        self._target_header_unit_outputs: T.Dict[str, T.List[str]] = {}
        self._target_header_unit_consumer_args: T.Dict[str, T.List[str]] = {}
        self._target_header_unit_bmis: T.Dict[str, T.List[T.Tuple[str, str]]] = {}
        # Per-target header units inherited from the module providers it links,
        # grouped by provider (cl only -- see _imported_header_units).
        self._target_imported_header_units: T.Dict[str, T.List[T.Tuple[build.BuildTarget, T.List[T.Union[File, str]]]]] = {}
        # Per-target build-relative paths every generator (custom_target,
        # generator) of the target writes, so a unit exporting a generated
        # header can order behind the edge that produces it.
        self._target_generated_outputs: T.Dict[str, T.List[str]] = {}
        # (mode, resolved file) -> the spelling of it that builds the BMI, so
        # declared spellings of one file share it; and the alias spellings'
        # default-named paths already linked to a group's BMI.
        self._header_unit_group: T.Dict[T.Tuple[str, str], str] = {}
        self._header_unit_alias_links: T.Dict[str, str] = {}
        # BMI class registry, frozen by _compute_bmi_class_registry before any
        # target is generated; only populated for compilers that
        # supports_bmi_classes(). Variants are memoized per (provider id,
        # class key); provider interface-unit sources are recorded during the
        # provider's own compile generation.
        self._bmi_classes: T.Dict[T.Tuple[MachineChoice, T.Tuple[str, ...]], BmiClassInfo] = {}
        self._bmi_variants: T.Dict[T.Tuple[str, T.Tuple[str, ...]], BmiVariant] = {}
        self._target_module_interfaces: T.DefaultDict[str, T.List[ModuleInterfaceSource]] = defaultdict(list)

    def create_phony_target(self, dummy_outfile: str, rulename: str, phony_infilenames: ListifiedStr) -> NinjaBuildElement:
        '''
        We need to use aliases for targets that might be used as directory
        names to workaround a Ninja bug that breaks `ninja -t clean`.
        This is used for 'reserved' targets such as 'test', 'install',
        'benchmark', etc, and also for RunTargets.
        https://github.com/mesonbuild/meson/issues/1644
        '''
        if dummy_outfile.startswith('meson-internal__'):
            raise AssertionError(f'Invalid usage of create_phony_target with {dummy_outfile!r}')

        to_name = f'meson-internal__{dummy_outfile}'
        elem = NinjaBuildElement(self.all_outputs, dummy_outfile, 'phony', to_name)
        self.add_build(elem)

        return NinjaBuildElement(self.all_outputs, to_name, rulename, phony_infilenames)

    def detect_vs_dep_prefix(self, tempfilename: str) -> T.TextIO:
        '''VS writes its dependency in a locale dependent format.
        Detect the search prefix to use.'''
        # TODO don't hard-code host
        for compiler in self.environment.coredata.compilers.host.values():
            # Have to detect the dependency format

            # IFort / masm on windows is MSVC like, but doesn't have /showincludes
            if compiler.language in {'fortran', 'masm'}:
                continue
            if compiler.id == 'pgi' and mesonlib.is_windows():
                # for the purpose of this function, PGI doesn't act enough like MSVC
                return open(tempfilename, 'a', encoding='utf-8')
            if compiler.get_argument_syntax() == 'msvc':
                break
        else:
            # None of our compilers are MSVC, we're done.
            return open(tempfilename, 'a', encoding='utf-8')
        filebase = 'incdetect.' + compilers.lang_suffixes[compiler.language][0]
        filename = os.path.join(self.environment.get_scratch_dir(),
                                filebase)
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(dedent('''\
                #include"incdetect2"
                int dummy;
            '''))
        filename_inc = os.path.join(self.environment.get_scratch_dir(),
                                    'incdetect2')
        with open(filename_inc, 'w', encoding='utf-8') as f:
            pass

        # The output of cl dependency information is language
        # and locale dependent. Any attempt at converting it to
        # Python strings leads to failure. We _must_ do this detection
        # in raw byte mode and write the result in raw bytes.
        #
        # Pass the compiler's external args (e.g. -I flags from c_args /
        # cpp_args in a native file) so that cl.exe can locate system headers
        # even when the INCLUDE environment variable is not set — for example,
        # when using a bundled MSVC toolchain outside a VS Developer Shell.
        extra_args = self.environment.coredata.get_external_args(
            MachineChoice.HOST, compiler.language)
        pc = subprocess.Popen(compiler.get_exelist() +
                              ['/showIncludes', '/c', filebase] + extra_args,
                              cwd=self.environment.get_scratch_dir(),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (stdout, stderr) = pc.communicate()

        # 'Note: including file: d:\build\meson-private\incdetect2', however
        # different locales have different messages with a different
        # number of colons. Match up to the drive name 'd:\' or
        # a relative path '.\'.
        # When used in cross compilation, the path separator is a
        # forward slash rather than a backslash so handle both; i.e.
        # the path is /build/meson-private/incdetect or ./incdetect2.
        # With certain cross compilation wrappings of MSVC, the paths
        # use backslashes, but without the leading drive name, so
        # allow the path to start with any path separator, i.e.
        # \build\meson-private\incdetect2
        matchre = re.compile(rb"^(.*\s)([a-zA-Z]:[\\/]|\.?[\\/]).*incdetect2$")

        def detect_prefix(out: bytes) -> T.TextIO:
            for line in re.split(rb'\r?\n', out):
                match = matchre.match(line)
                if match:
                    with open(tempfilename, 'ab') as binfile:
                        binfile.write(b'msvc_deps_prefix = ' + match.group(1) + b'\n')
                    return open(tempfilename, 'a', encoding='utf-8')
            return None

        # Some cl wrappers (e.g. Squish Coco) output dependency info
        # to stderr rather than stdout
        result = detect_prefix(stdout) or detect_prefix(stderr)
        if result:
            return result

        raise MesonException(f'Could not determine vs dep dependency prefix string. output: {stderr!r} {stdout!r}')

    def generate(self, capture: bool = False, vslite_ctx: T.Optional[T.Dict] = None) -> T.Optional[T.Dict[str, T.Dict[Language, T.List[str]]]]:
        captured_compile_args_per_target: T.Dict[str, T.Dict[Language, T.List[str]]] = {}
        if vslite_ctx:
            # We don't yet have a use case where we'd expect to make use of this,
            # so no harm in catching and reporting something unexpected.
            raise MesonBugException('We do not expect the ninja backend to be given a valid \'vslite_ctx\'')
        if self.environment:
            for for_machine in MachineChoice:
                if 'cuda' in self.environment.coredata.compilers[for_machine]:
                    mlog.debug('cuda enabled globally, disabling thin archives for {}, since nvcc/nvlink cannot handle thin archives natively'.format(for_machine))
                    self.allow_thin_archives[for_machine] = False

        ninja = tooldetect.detect_ninja_command_and_version(log=True)
        if self.environment.coredata.optstore.get_value_for(OptionKey('vsenv')):
            builddir = Path(self.environment.get_build_dir())
            try:
                # For prettier printing, reduce to a relative path. If
                # impossible (e.g., because builddir and cwd are on
                # different Windows drives), skip and use the full path.
                builddir = builddir.relative_to(Path.cwd())
            except ValueError:
                pass
            meson_command = mesonlib.join_args(mesonlib.get_meson_command())
            mlog.log()
            mlog.log('Visual Studio environment is needed to run Ninja. It is recommended to use Meson wrapper:')
            mlog.log(f'{meson_command} compile -C {builddir}')
        if ninja is None:
            raise MesonException('Could not detect Ninja v1.8.2 or newer')
        (self.ninja_command, self.ninja_version) = ninja
        self.ninja_has_dyndeps = mesonlib.version_compare(self.ninja_version, '>=1.10.0')
        outfilename = os.path.join(self.environment.get_build_dir(), self.ninja_filename)
        tempfilename = outfilename + '~'
        with open(tempfilename, 'w', encoding='utf-8') as outfile:
            outfile.write(f'# This is the build file for project "{self.build.get_project()}"\n')
            outfile.write('# It is autogenerated by the Meson build system.\n')
            outfile.write('# Do not edit by hand.\n\n')
            outfile.write('ninja_required_version = 1.8.2\n\n')

            num_pools = self.environment.coredata.optstore.get_value_for('backend_max_links')
            assert isinstance(num_pools, int)
            if num_pools > 0:
                outfile.write(f'''pool link_pool
  depth = {num_pools}

''')

        with self.detect_vs_dep_prefix(tempfilename) as outfile:
            self.generate_rules()
            self.generate_phony()
            self.add_build_comment(NinjaComment('Build rules for targets'))

            # Optionally capture compile args per target, for later use (i.e. VisStudio project's NMake intellisense include dirs, defines, and compile options).
            if capture:
                for target in self.build.get_targets().values():
                    if isinstance(target, build.BuildTarget):
                        captured_compile_args_per_target[target.get_id()] = self.generate_common_compile_args_per_src_type(target)

            self._compute_bmi_class_registry()
            # Freezing the registry itself calls _generate_single_compile, which
            # caches each target's compile args -- but a spaced multi-class GCC
            # target respells those through its BMI-class root, a class the
            # registry only knows once frozen. Drop that pre-freeze cache so
            # generation recomputes the args against the finished registry.
            self._generate_single_compile_target_args.cache_clear()
            for t in ProgressBar(self.build.get_targets().values(), desc='Generating targets'):
                self.generate_target(t)
            mlog.log_timestamp("Targets generated")
            # Every alias root this generation needs has now been placed or
            # adopted (targets no longer generating means _alias_root_real is
            # complete), so any stale one a prior configure left behind can be
            # told apart from a live one and swept.
            self._prune_stale_dir_aliases()
            self.add_build_comment(NinjaComment('Test rules'))
            self.generate_tests()
            mlog.log_timestamp("Tests generated")
            self.add_build_comment(NinjaComment('Install rules'))
            self.generate_install()
            mlog.log_timestamp("Install generated")
            self.generate_dist()
            mlog.log_timestamp("Dist generated")
            key = OptionKey('b_coverage')
            if key in self.environment.coredata.optstore and\
                    self.environment.coredata.optstore.get_value_for('b_coverage'):
                gcovr_exe, gcovr_version, lcov_exe, lcov_version, genhtml_exe, llvm_cov_exe = tooldetect.find_coverage_tools(self.environment.coredata)
                mlog.debug(f'Using {gcovr_exe} ({gcovr_version}), {lcov_exe} and {llvm_cov_exe} for code coverage')
                if gcovr_exe or (lcov_exe and genhtml_exe):
                    self.add_build_comment(NinjaComment('Coverage rules'))
                    self.generate_coverage_rules(gcovr_exe, gcovr_version, llvm_cov_exe)
                    mlog.log_timestamp("Coverage rules generated")
                else:
                    # FIXME: since we explicitly opted in, should this be an error?
                    # The docs just say these targets will be created "if possible".
                    mlog.warning('Need gcovr or lcov/genhtml to generate any coverage reports')
            self.add_build_comment(NinjaComment('Suffix'))
            self.generate_utils()
            mlog.log_timestamp("Utils generated")
            self.generate_ending()

            self.ninja.write(outfile)
            mlog.log_timestamp("build.ninja generated")

            default = 'default all\n\n'
            outfile.write(default)
        # Only overwrite the old build file after the new one has been
        # fully created.
        os.replace(tempfilename, outfilename)
        mlog.cmd_ci_include(outfilename)  # For CI debugging
        # Refresh Ninja's caches. https://github.com/ninja-build/ninja/pull/1685
        # Cannot use when running with dyndeps: https://github.com/ninja-build/ninja/issues/1952
        if ((mesonlib.version_compare(self.ninja_version, '>= 1.12.0') or
                (mesonlib.version_compare(self.ninja_version, '>=1.10.0') and not self._uses_dyndeps))
                and os.path.exists(os.path.join(self.environment.build_dir, '.ninja_log'))):
            subprocess.call(self.ninja_command + ['-t', 'restat'], cwd=self.environment.build_dir)
            subprocess.call(self.ninja_command + ['-t', 'cleandead'], cwd=self.environment.build_dir)
        self.generate_compdb()
        self.generate_rust_project_json()

        return captured_compile_args_per_target

    def generate_rust_project_json(self) -> None:
        """Generate a rust-analyzer compatible rust-project.json file."""
        if not self.rust_crates:
            return
        with open(os.path.join(self.environment.get_build_dir(), 'rust-project.json'),
                  'w', encoding='utf-8') as f:
            compiler = T.cast('RustCompiler', self.environment.coredata.compilers.host['rust'])
            sysroot = compiler.get_sysroot()
            json.dump(
                {
                    "sysroot": sysroot,
                    "sysroot_src": os.path.join(sysroot, 'lib/rustlib/src/rust/library/'),
                    "crates": [c.to_json() for c in self.rust_crates.values()],
                },
                f, indent=4)

    # http://clang.llvm.org/docs/JSONCompilationDatabase.html
    def generate_compdb(self) -> None:
        rules = []
        # TODO: Rather than an explicit list here, rules could be marked in the
        # rule store as being wanted in compdb
        for for_machine in MachineChoice:
            for compiler in self.environment.coredata.compilers[for_machine].values():
                rules += [f"{rule}{ext}" for rule in [self.compiler_to_rule_name(compiler)]
                          for ext in ['', '_RSP']]
                rules += [f"{rule}{ext}" for rule in [self.compiler_to_pch_rule_name(compiler)]
                          for ext in ['', '_RSP']]
                # Add custom MIL link rules to get the files compiled by the TASKING compiler family to MIL files included in the database
                if compiler.get_id() == 'tasking':
                    rule = self.get_compiler_rule_name('tasking_mil_compile', compiler.for_machine)
                    rules.append(rule)
                    rules.append(f'{rule}_RSP')
        compdb_options = ['-x'] if mesonlib.version_compare(self.ninja_version, '>=1.9') else []
        ninja_compdb = self.ninja_command + ['-t', 'compdb'] + compdb_options + rules
        builddir = self.environment.get_build_dir()
        try:
            jsondb = subprocess.check_output(ninja_compdb, cwd=builddir)
            with open(os.path.join(builddir, 'compile_commands.json'), 'wb') as f:
                f.write(jsondb)
        except Exception:
            mlog.warning('Could not create compilation database.', fatal=False)

    # Get all generated headers. Any source file might need them so
    # we need to add an order dependency to them.
    def get_generated_headers(self, target: build.BuildTarget) -> T.List[FileOrString]:
        tid = target.get_id()
        if tid in self._generated_header_cache:
            return self._generated_header_cache[tid]
        header_deps: T.List[FileOrString] = []
        # XXX: Why don't we add deps to CustomTarget headers here?
        for genlist in target.get_generated_sources():
            if isinstance(genlist, (build.CustomTarget, build.CustomTargetIndex)):
                continue
            for src in genlist.get_outputs():
                if compilers.is_header(src):
                    header_deps.append(self.get_target_generated_dir(target, genlist, src))
        if target.vala_header:
            vala_header = File.from_built_file(self.get_target_dir(target), target.vala_header)
            header_deps.append(vala_header)
        # Recurse and find generated headers
        for dep in itertools.chain(target.link_targets, target.link_whole_targets):
            if isinstance(dep, (build.StaticLibrary, build.SharedLibrary)):
                header_deps += self.get_generated_headers(dep)
        if isinstance(target, build.CompileTarget):
            header_deps.extend(target.get_generated_headers())
        self._generated_header_cache[tid] = header_deps
        return header_deps

    def get_target_generated_sources(self, target: build.BuildTarget) -> T.MutableMapping[str, build.TargetSources]:
        """
        Returns a dictionary with the keys being the path to the file
        (relative to the build directory) and the value being the File object
        representing the same path.
        """
        srcs: T.MutableMapping[str, build.TargetSources] = {}
        for gensrc in target.get_generated_sources():
            for s in gensrc.get_outputs():
                rel_src = self.get_target_generated_dir(target, gensrc, s)
                srcs[rel_src] = File.from_built_relative(rel_src)
        return srcs

    def get_target_sources(self, target: build.BuildTarget) -> T.MutableMapping[str, File]:
        srcs: T.MutableMapping[str, File] = OrderedDict()
        for s in target.get_sources():
            # BuildTarget sources are always mesonlib.File files which are
            # either in the source root, or generated with configure_file and
            # in the build root
            if not isinstance(s, File):
                raise InvalidArguments(f'All sources in target {s!r} must be of type mesonlib.File')
            f = s.rel_to_builddir(self.build_to_src)
            srcs[f] = s
        return srcs

    def get_target_source_can_unity(self, target: build.BuildTarget, source: FileOrString) -> bool:
        if isinstance(source, File):
            source = source.fname
        if compilers.is_llvm_ir(source) or \
           compilers.is_assembly(source):
            return False
        suffix = os.path.splitext(source)[1][1:].lower()
        for lang in backends.LANGS_CANT_UNITY:
            if lang not in target.compilers:
                continue
            if suffix in target.compilers[lang].file_suffixes:
                return False
        return True

    def create_target_source_introspection(self, target: build.Target, comp: compilers.Compiler,
                                           parameters: CompilerArgs | T.List[str],
                                           sources: FileList,
                                           generated_sources: FileList,
                                           unity_sources: list[File] | None = None) -> None:
        '''
        Adds the source file introspection information for a language of a target

        Internal introspection storage format:
        self.introspection_data = {
            '<target ID>': {
                <id tuple>: {
                    'language: 'lang',
                    'compiler': ['comp', 'exe', 'list'],
                    'parameters': ['UNIQUE', 'parameter', 'list'],
                    'sources': [],
                    'generated_sources': [],
                }
            }
        }
        '''
        tid = target.get_id()
        lang = comp.get_language()
        tgt = self.introspection_data[tid]
        # Find an existing entry or create a new one
        id_hash: tuple[str, tuple] = (lang, tuple(parameters))

        src_block: T.Optional[CompilerIntrospectionData]
        src_block = T.cast('T.Optional[CompilerIntrospectionData]', tgt.get(id_hash, None))
        if src_block is None:
            # Convert parameters
            if isinstance(parameters, CompilerArgs):
                parameters = parameters.to_native(copy=True)
            parameters = comp.compute_parameters_with_absolute_paths(parameters, self.build_dir)
            # The new entry
            src_block = {
                'language': lang,
                'machine': comp.for_machine.get_lower_case_name(),
                'compiler': comp.get_exelist(),
                'parameters': parameters,
                'sources': [],
                'generated_sources': [],
                'unity_sources': [],
            }
            tgt[id_hash] = src_block

        def compute_path(file: FileOrString) -> str:
            """ Make source files absolute """
            if isinstance(file, File):
                return file.absolute_path(self.source_dir, self.build_dir)
            return os.path.normpath(os.path.join(self.build_dir, file))

        src_block['sources'].extend(compute_path(x) for x in sources)
        src_block['generated_sources'].extend(compute_path(x) for x in generated_sources)
        if unity_sources:
            src_block['unity_sources'].extend(compute_path(x) for x in unity_sources)

    def create_target_linker_introspection(self, target: build.Target, linker: T.Union[Compiler, StaticLinker], parameters: CompilerArgs) -> None:
        tid = target.get_id()
        tgt = self.introspection_data[tid]
        lnk_hash: tuple[str, ...] = tuple(parameters)
        lnk_block: T.Optional[LinkerIntrospectionData]
        lnk_block = T.cast('T.Optional[LinkerIntrospectionData]', tgt.get(lnk_hash, None))
        if lnk_block is None:
            paramlist = parameters.to_native(copy=True)

            if isinstance(linker, Compiler):
                linkers = linker.get_linker_exelist()
            else:
                linkers = linker.get_exelist()

            lnk_block = {
                'linker': linkers,
                'parameters': paramlist,
            }
            tgt[lnk_hash] = lnk_block

    def generate_target(self, target: T.Union[build.Target]) -> None:
        if isinstance(target, build.CustomTarget):
            self.generate_custom_target(target)
            return
        if isinstance(target, build.RunTarget):
            self.generate_run_target(target)
            return
        assert isinstance(target, build.BuildTarget)
        self.check_cpp_modules_fortran_mix(target)
        self.check_cpp_modules_std(target)
        self.check_clang_cpp_modules_scanner(target)
        os.makedirs(self.get_target_private_dir_abs(target), exist_ok=True)
        compiled_sources: T.List[str] = []
        source2object: T.Dict[str, str] = {}
        name = target.get_id()
        if name in self.processed_targets:
            return
        self.processed_targets.add(name)
        # Initialize an empty introspection source list
        self.introspection_data[name] = {}
        # Generate rules for all dependency targets
        self.process_target_dependencies(target)

        self.generate_shlib_aliases(target, self.get_target_dir(target))

        # Generate rules for GeneratedLists
        self.generate_generator_list_rules(target)

        # If target uses a language that cannot link to C objects,
        # just generate for that language and return.
        if isinstance(target, build.Jar):
            self.generate_jar_target(target)
            return
        if 'cs' in target.compilers:
            self.generate_cs_target(target)
            return
        if 'swift' in target.compilers:
            self.generate_swift_target(target)
            return

        # CompileTarget compiles all its sources and does not do a final link.
        # This is, for example, a preprocessor.
        is_compile_target = isinstance(target, build.CompileTarget)

        # Preexisting target C/C++ sources to be built; dict of full path to
        # source relative to build root and the original File object.
        target_sources: T.MutableMapping[str, File]

        # GeneratedList and CustomTarget sources to be built; dict of the full
        # path to source relative to build root and the generating target/list
        generated_sources: T.MutableMapping[str, build.TargetSources]

        # List of sources that have been transpiled from a DSL (like Vala) into
        # a language that is handled below, such as C or C++
        transpiled_sources: T.List[str]

        if target.uses_vala():
            # Sources consumed by valac are filtered out. These only contain
            # C/C++ sources, objects, generated libs, and unknown sources now.
            target_sources, generated_sources, \
                transpiled_sources = self.generate_vala_compile(target)
        elif 'cython' in target.compilers:
            target_sources, generated_sources, \
                transpiled_sources = self.generate_cython_transpile(target)
        else:
            target_sources = self.get_target_sources(target)
            generated_sources = self.get_target_generated_sources(target)
            transpiled_sources = []
        self.scan_fortran_module_outputs(target)

        # Generate rules for building the remaining source files in this target
        outname = self.get_target_filename(target)
        obj_list = []
        is_unity = self.is_unity(target)
        header_deps = []
        unity_src: list[File] = []
        unity_deps = [] # Generated sources that must be built before compiling a Unity target.
        header_deps += self.get_generated_headers(target)

        if is_unity:
            # Warn about incompatible sources if a unity build is enabled
            langs = set(target.compilers.keys())
            langs_cant = langs.intersection(backends.LANGS_CANT_UNITY)
            if langs_cant:
                langs_are = langs = ', '.join(langs_cant).upper()
                langs_are += ' are' if len(langs_cant) > 1 else ' is'
                msg = f'{langs_are} not supported in Unity builds yet, so {langs} ' \
                      f'sources in the {target.name!r} target will be compiled normally'
                mlog.log(mlog.red('FIXME'), msg)

        # Get a list of all generated headers that will be needed while building
        # this target's sources (generated sources and preexisting sources).
        # This will be set as dependencies of all the target's sources. At the
        # same time, also deal with generated sources that need to be compiled.
        generated_source_files: T.List[File] = []
        for rel_src in generated_sources:
            raw_src = File.from_built_relative(rel_src)
            if compilers.is_source(rel_src):
                if is_unity and self.get_target_source_can_unity(target, rel_src):
                    unity_deps.append(raw_src)
                    unity_src.append(raw_src)
                else:
                    generated_source_files.append(raw_src)
            elif compilers.is_object(rel_src):
                obj_list.append(rel_src)
            elif compilers.is_library(rel_src) or modules.is_module_library(rel_src):
                pass
            elif is_compile_target:
                generated_source_files.append(raw_src)
            else:
                # Assume anything not specifically a source file is a header. This is because
                # people generate files with weird suffixes (.inc, .fh) that they then include
                # in their source files.
                header_deps.append(raw_src)

        # For D language, the object of generated source files are added
        # as order only deps because other files may depend on them
        d_generated_deps = []

        # These are the generated source files that need to be built for use by
        # this target. We create the Ninja build file elements for this here
        # because we need `header_deps` to be fully generated in the above loop.
        for src in generated_source_files:
            if not compilers.is_separate_compile(src):
                continue
            if compilers.is_llvm_ir(src):
                o, s = self.generate_llvm_ir_compile(target, src)
            else:
                o, s = self.generate_single_compile(target, src, True, order_deps=header_deps)
            compiled_sources.append(s)
            source2object[s] = o
            obj_list.append(o)
            if s.split('.')[-1] in compilers.lang_suffixes['d']:
                d_generated_deps.append(o)

        use_pch = self.target_uses_pch(target)
        if use_pch and target.has_pch():
            pch_objects = self.generate_pch(target, header_deps=header_deps)
        else:
            pch_objects = []

        o, od = self.flatten_object_list(target)
        obj_list.extend(o)
        fortran_order_deps = self.get_fortran_order_deps(od)

        fortran_inc_args: T.List[str] = []
        if target.uses_fortran():
            fortran_inc_args = mesonlib.listify([target.compilers['fortran'].get_include_args(
                self.get_target_private_dir(t), is_system=False) for t in od if t.uses_fortran()])

            # add the private directories of all transitive dependencies, which
            # are needed for their mod files
            fc = target.compilers['fortran']
            for t in target.get_all_linked_targets():
                fortran_inc_args.extend(fc.get_include_args(
                    self.get_target_private_dir(t), False))

        # Generate compilation targets for sources generated by transpilers.
        #
        # Do not try to unity-build the generated source files, as these
        # often contain duplicate symbols and will fail to compile properly.
        #
        # Gather all generated source files and header before generating the
        # compilation rules, to be able to add correct dependencies on the
        # generated headers.
        transpiled_source_files = []
        for src in transpiled_sources:
            raw_src = File.from_built_relative(src)
            # Generated targets are ordered deps because the must exist
            # before the sources compiling them are used. After the first
            # compile we get precise dependency info from dep files.
            # This should work in all cases. If it does not, then just
            # move them from orderdeps to proper deps.
            if compilers.is_header(src):
                header_deps.append(raw_src)
            else:
                transpiled_source_files.append(raw_src)
        for src in transpiled_source_files:
            o, s = self.generate_single_compile(target, src, True, [], header_deps)
            obj_list.append(o)

        # Generate compile targets for all the preexisting sources for this target
        for src in target_sources.values():
            if not compilers.is_separate_compile(src):
                continue
            if compilers.is_header(src) and not is_compile_target:
                continue
            if compilers.is_llvm_ir(src):
                o, s = self.generate_llvm_ir_compile(target, src)
                obj_list.append(o)
            elif is_unity and self.get_target_source_can_unity(target, src):
                unity_src.append(src)
            else:
                o, s = self.generate_single_compile(target, src, False, [],
                                                    [*header_deps, *d_generated_deps, *fortran_order_deps],
                                                    fortran_inc_args)
                obj_list.append(o)
                compiled_sources.append(s)
                source2object[s] = o

        if is_unity:
            for src in self.generate_unity_files(target, unity_src):
                o, s = self.generate_single_compile(target, src, True, [*unity_deps, *header_deps, *d_generated_deps],
                                                    fortran_order_deps, fortran_inc_args, unity_src)
                obj_list.append(o)
                compiled_sources.append(s)
                source2object[s] = o
        if is_compile_target:
            # Skip the link stage for this special type of target
            return

        if not isinstance(target, build.StaticLibrary):
            final_obj_list = obj_list
        elif target.prelink:
            final_obj_list = self.generate_prelink(target, obj_list)
        else:
            final_obj_list = obj_list

        self.generate_dependency_scan_target(target, compiled_sources, source2object, fortran_order_deps)

        if isinstance(target, build.SharedLibrary):
            self.generate_shsym(target)
        if target.uses_rust():
            self.generate_rust_target(target, outname, final_obj_list, fortran_order_deps)
            return

        linker, stdlib_args = self.determine_linker_and_stdlib_args(target)
        elem = self.generate_link(target, outname, final_obj_list, linker, pch_objects, stdlib_args=stdlib_args)
        self.add_build(elem)
        #In AIX, we archive shared libraries. If the instance is a shared library, we add a command to archive the shared library
        #object and create the build element.
        if isinstance(target, build.SharedLibrary) and self.environment.machines[target.for_machine].is_aix():
            if target.aix_so_archive:
                elem = NinjaBuildElement(self.all_outputs, linker.get_archive_name(outname), 'AIX_LINKER', [outname])
                self.add_build(elem)

    @lru_cache(maxsize=None)
    def cpp_module_pipeline_applies(self, target: build.BuildTargetTypes | build.Target) -> bool:
        """Whether the C++ module pipeline is even a candidate for this target.

        The structural question, asked before any of "does the target want
        modules" and "is a scanner available": no answer here depends on the
        compiler's capabilities or on what the target declares. Kept separate
        because those later answers are not interchangeable with this one -- a
        target that reaches 'none' through this predicate has no module problem
        to report, while a target that reaches it past this predicate is one
        the pipeline wanted and could not have.

        Excluded, and why:
        - ninja without dyndep support: nothing scans, on any compiler.
        - a preprocess-only target (-E): it neither writes a BMI nor imports
          one, and generate_target returns before a collate is ever emitted for
          it, so mappers, scans and header units would name outputs nothing
          produces.
        - a target with Fortran sources: a target gets one scanner and this one
          uses Fortran's (should_use_dyndeps_for_target enables it without this
          pipeline). check_cpp_modules_fortran_mix reports the C++ modules such
          a target cannot have.
        - a target with no C++ compiler at all.
        """
        if not self.ninja_has_dyndeps:
            return False
        if not isinstance(target, build.BuildTarget):
            return False
        if isinstance(target, build.CompileTarget):
            return False
        if target.uses_fortran():
            return False
        return 'cpp' in target.compilers

    @lru_cache(maxsize=None)
    def cpp_module_scanner_for_target(self, target: build.BuildTargetTypes | build.Target) -> Literal['none', 'regex', 'p1689']:
        """Which C++ module scanning pipeline this target uses, if any.

        'p1689': the compiler's own P1689 scanner feeding the per-target
        collator (GCC >= 14, cl >= 19.32, Clang with a feature-probed
        clang-scan-deps -- supports_cpp_modules_p1689); handles partitions
        and import std.
        'regex': the homemade regex scanner, flat named modules only: older
        but modules-capable compilers (GCC < 14, cl 19.28.28617 - 19.31), and
        the legacy escape hatch of a bare -fmodules/-fmodules-ts in the
        target's cpp_args.
        'none': the target wants no C++ modules, or wants them and no scanner
        can serve it (Clang without clang-scan-deps -- the one case a caller
        may want to report; cpp_module_pipeline_applies has already screened
        out the targets the pipeline never applies to).

        A target opts in by declaration (a module-interface source, the
        cpp_modules/cpp_header_units kwargs, or linking a module provider --
        uses_cpp_modules()); source contents are never read. cl before build
        19.28.28617 (VS 16.8/16.9) has broken modules, and
        current_vs_supports_modules() rejects a too-old developer prompt.

        Memoized: re-asked once per source on the ninja-gen hot path, and the
        inputs (ninja/compiler versions, dev-prompt, target flags) are frozen
        by generation time.
        """
        if not self.cpp_module_pipeline_applies(target):
            return 'none'
        assert isinstance(target, build.BuildTarget)
        cpp = target.compilers['cpp']
        if target.uses_cpp_modules():
            if cpp.get_id() == 'gcc':
                return 'p1689' if cpp.supports_cpp_modules_p1689() else 'regex'
            if cpp.get_id() == 'msvc' and mesonlib.current_vs_supports_modules() \
                    and mesonlib.version_compare(cpp.version, '>=19.28.28617'):
                return 'p1689' if cpp.supports_cpp_modules_p1689() else 'regex'
            # Clang has no regex fallback: its P1689 support lives in the
            # separate clang-scan-deps tool, feature-probed by
            # supports_cpp_modules_p1689. Without it, fall through -- the
            # legacy -fmodules/-fmodules-ts escape hatch below still applies,
            # and check_clang_cpp_modules_scanner reports the gap at
            # generation time.
            if cpp.get_id() == 'clang' and cpp.supports_cpp_modules_p1689():
                return 'p1689'
        for arg in cpp.get_cpp_modules_args():
            if arg in target.extra_args['cpp']:
                return 'regex'
        return 'none'

    def should_use_dyndeps_for_target(self, target: build.BuildTargetTypes | build.Target) -> bool:
        # The other side of the exclusion cpp_module_pipeline_applies makes: a
        # Fortran target dyndeps on Fortran's own scanner, never on the C++ one.
        if self.ninja_has_dyndeps and isinstance(target, build.BuildTarget) \
                and target.uses_fortran():
            return True
        return self.cpp_module_scanner_for_target(target) != 'none'

    def generate_dependency_scan_target(self, target: build.BuildTarget,
                                        compiled_sources: T.List[str],
                                        source2object: T.Dict[str, str],
                                        object_deps: T.List[File]) -> None:
        if not self.should_use_dyndeps_for_target(target):
            return
        self._uses_dyndeps = True
        if self.target_uses_p1689_cpp_modules(target):
            self.generate_p1689_module_collate_target(target, compiled_sources, source2object)
            return
        json_file, depscan_file = self.get_dep_scan_file_for(target)
        pickle_base = target.name + '.dat'
        pickle_file = os.path.join(self.get_target_private_dir(target), pickle_base).replace('\\', '/')
        pickle_abs = os.path.join(self.get_target_private_dir_abs(target), pickle_base).replace('\\', '/')
        rule_name = 'depscan'
        scan_sources = list(self.select_sources_to_scan(compiled_sources))

        scaninfo = TargetDependencyScannerInfo(
            self.get_target_private_dir(target), source2object, scan_sources)

        write = True
        if os.path.exists(pickle_abs):
            with open(pickle_abs, 'rb') as p:
                old = pickle.load(p)
            write = old != scaninfo

        if write:
            with open(pickle_abs, 'wb') as p:
                pickle.dump(scaninfo, p)

        elem = NinjaBuildElement(self.all_outputs, json_file, rule_name, pickle_file)
        # A full dependency is required on all scanned sources, if any of them
        # are updated we need to rescan, as they may have changed the modules
        # they use or export.
        for s in scan_sources:
            elem.deps.add(s[0])
        elem.add_orderdep(self.order_deps_to_strings(target, object_deps))
        elem.add_item('name', target.name)
        self.add_build(elem)

        infiles: T.Set[str] = set()
        for t in target.get_all_linked_targets():
            if self.should_use_dyndeps_for_target(t) and not self.target_uses_p1689_cpp_modules(t):
                assert isinstance(t, build.BuildTarget)
                infiles.add(self.get_dep_scan_file_for(t)[0])
        _, od = self.flatten_object_list(target)
        infiles.update({self.get_dep_scan_file_for(t)[0] for t in od if t.uses_fortran()})

        elem = NinjaBuildElement(self.all_outputs, depscan_file, 'depaccumulate', [json_file] + sorted(infiles))
        elem.add_item('name', target.name)
        self.add_build(elem)

    @classmethod
    def _has_cpp_source(cls, target: build.BuildTarget) -> bool:
        # Whether the target compiles a C++ source of its own. Not the same
        # question as 'cpp' in target.compilers: process_compilers also adds a
        # language a target merely *links* (to pick the linker), so a Fortran
        # program linking a C++ library carries a cpp compiler and no C++ TU.
        for src in target.get_sources():
            if cls.source_scan_language(src) == 'cpp' and not compilers.is_header(src):
                return True
        for gen in target.get_generated_sources():
            for out in gen.get_outputs():
                if cls.source_scan_language(out) == 'cpp' and not compilers.is_header(out):
                    return True
        return False

    def check_cpp_modules_fortran_mix(self, target: build.BuildTarget) -> None:
        # A target with Fortran sources is scanned by the Fortran module
        # scanner, and a target gets one scanner: cpp_module_scanner_for_target
        # returns 'none' for it, so its C++ sources are compiled with no module
        # flags, no scan and no dyndep. A C++ module in such a target cannot
        # work, and left alone it fails at build time with a raw compiler error
        # about `export module` needing -fmodules. Say so at setup instead, and
        # name the shape that does work: the C++ modules go in a C++ library
        # the Fortran target links.
        if not target.uses_fortran():
            return
        if target.provides_cpp_modules():
            raise MesonException(
                f'Target {target.name!r} has both Fortran sources and C++ module '
                'sources, which Meson does not support in one target: a target '
                'gets a single module scanner, and a Fortran target uses the '
                'Fortran one, so its C++ modules would never be compiled as '
                'modules. Move the C++ module sources into a C++ library and '
                'link it from this target.')
        if target.uses_cpp_modules() and self._has_cpp_source(target):
            # It links a module provider and has C++ TUs of its own. Those TUs
            # are not module-enabled, so an `import` in one of them fails at
            # compile time; if none imports anything, the build is fine, so
            # this is a warning rather than an error.
            mlog.warning(
                f'Target "{target.name}" has both Fortran and C++ sources and links a '
                'C++ module provider. C++ modules are not supported in a mixed '
                'Fortran/C++ target, so its C++ sources cannot import those modules. '
                'Move the C++ sources into a C++ library and link it from this target.')

    def check_cpp_modules_std(self, target: build.BuildTarget) -> None:
        # C++ modules need C++20 or later; with an older cpp_std the compiler
        # rejects `export module` / `import` at build time, so fail during setup
        # with a clear message instead. uses_cpp_modules() walks the link graph,
        # so this must run at generation time (not target creation) when it is
        # frozen -- calling it earlier poisons its memoized result. A
        # preprocess-only target is exempt: -E never parses `export module` or
        # `import`, so an older cpp_std preprocesses a module interface fine.
        #
        # Not keyed on cpp_module_pipeline_applies: this is a property of the
        # source language, not of the pipeline. A module source needs c++20 to
        # compile whether or not Meson would have scanned it -- with a ninja too
        # old for dyndep, say -- so the exemption here is only the one case
        # where the source is never compiled as C++ at all.
        if isinstance(target, build.CompileTarget):
            return
        if 'cpp' not in target.compilers or not target.uses_cpp_modules():
            return
        from ..compilers.cpp import cpp_std_supports_modules
        std = str(self.get_target_option(target, OptionKey(
            'cpp_std', machine=target.for_machine, subproject=target.subproject)))
        if not cpp_std_supports_modules(std):
            raise MesonException(
                f'Target {target.name!r} uses C++ modules, which require cpp_std '
                f'c++20 or later; got cpp_std={std}.')

    def check_clang_cpp_modules_scanner(self, target: build.BuildTarget) -> None:
        # Clang's module pipeline needs a working clang-scan-deps (feature-
        # probed, no version assumption). Without it a module-using target
        # would silently get no scanning and fail at build time with confusing
        # compiler errors, so report the missing tool at setup instead. The
        # legacy -fmodules/-fmodules-ts regex escape hatch is exempt: it does
        # not need the scanner, and answers 'regex' rather than 'none'.
        #
        # The three conditions below are the only three there are: the pipeline
        # applies, the target wants modules, and no scanner answered. Blaming
        # clang-scan-deps for a target the pipeline never applied to would send
        # the reader hunting a tool that is installed and fine, which is why
        # that question is asked first and asked by name.
        if not self.cpp_module_pipeline_applies(target):
            return
        cpp = target.compilers['cpp']
        if cpp.get_id() != 'clang' or not target.uses_cpp_modules():
            return
        if self.cpp_module_scanner_for_target(target) == 'none':
            raise MesonException(
                f'Target {target.name!r} uses C++ modules with Clang, which '
                'requires a clang-scan-deps with P1689 support (-format=p1689) '
                'matching the compiler; none was found.')

    def target_uses_p1689_cpp_modules(self, target: build.BuildTargetTypes | build.Target) -> bool:
        """Whether this target should use the P1689 module pipeline (GCC/MSVC)."""
        return self.cpp_module_scanner_for_target(target) == 'p1689'

    def target_uses_p1689_cpp_modules_edge(self, target: build.BuildTargetTypes, compiler: Compiler) -> bool:
        """Whether this compiler's edges in this target are P1689 C++ module edges.

        Target-wide, for the whole-target questions (the PCH is dropped for
        every source or for none). A question about one compile -- what flags
        it takes, what it depends on -- is source_uses_p1689_cpp_modules.
        """
        return compiler.get_language() == 'cpp' and self.target_uses_p1689_cpp_modules(target)

    def source_uses_p1689_cpp_modules(self, target: build.BuildTargetTypes, compiler: Compiler,
                                      src: FileOrString) -> bool:
        """Whether this specific compile/scan edge of src is a P1689 module edge.

        The C++ compiler also compiles sources the collate never scans (it
        assembles .s/.S). Such an edge gets no module flags, no per-TU module
        mapper and no header-unit deps -- the collate declares those outputs
        only for the sources select_sources_to_scan yields, so a compile that
        depended on them would depend on a file nothing produces.
        """
        return (self.target_uses_p1689_cpp_modules_edge(target, compiler)
                and self.source_scan_language(src) == 'cpp')

    def get_provided_modules_file_for(self, target: build.BuildTarget) -> str:
        return os.path.join(self.get_target_private_dir(target), 'provided-modules.json')

    def get_private_modules_file_for(self, target: build.BuildTarget) -> str:
        # A bare JSON list of the private module names target provides --
        # names only, never paths, so a consumer can recognize a private
        # module (for the diagnostic in depaccumulate.run_p1689) but never
        # resolve one.
        return os.path.join(self.get_target_private_dir(target), 'private-modules.json')

    @staticmethod
    def private_map_display_for(target: build.BuildTarget) -> str:
        """How depaccumulate names target in a private-module diagnostic.

        Never an identity: two targets can share a name (a name is only unique
        within one subdir), so the collator compares get_id() and prints this.
        The subdir a name repeats across is the one thing that tells the two
        apart for a reader, and it cannot be recovered from an id, whose subdir
        part is a hash -- so it is spelled out here.
        """
        subdir = target.get_subdir()
        if not subdir:
            return f'{target.name!r}'
        # Forward slashes: the same string is asserted on by the POSIX and the
        # Windows test drivers.
        defined_in = '/'.join(subdir.split(os.sep)) + '/meson.build'
        return f'{target.name!r} (defined in {defined_in})'

    def private_map_ref_for(self, target: build.BuildTarget) -> T.Tuple[str, str, str]:
        """(private-modules.json, target id, display) for --dep-private-map."""
        return (self.get_private_modules_file_for(target), target.get_id(),
                self.private_map_display_for(target))

    @staticmethod
    def get_ddi_file_for(objfile: str) -> str:
        # One P1689 scan result per object, kept next to it in the private dir.
        return objfile + '.ddi'

    def generate_cpp_module_scan(self, target: build.BuildTarget, compiler: Compiler,
                                 rel_src: str, rel_obj: str,
                                 commands: T.Union['CompilerArgs', T.List[str]],
                                 header_deps: T.Optional[T.List[FileOrString]] = None,
                                 order_deps: T.Optional[T.List[File] | T.List[FileOrString]] = None,
                                 header_unit_override: T.Optional[T.List[str]] = None,
                                 pch_dep: T.Optional[T.List[str]] = None,
                                 class_tag: T.Optional[str] = None) -> None:
        if not self.source_uses_p1689_cpp_modules(target, compiler, rel_src):
            return
        if compiler.get_id() == 'clang':
            # Registered lazily (idempotent): creating clang's scan rule needs
            # the clang-scan-deps feature probe, which projects without module
            # targets must not pay for.
            self.generate_cpp_module_scan_rule(compiler)
        if compiler.get_id() == 'gcc' and target.cpp_header_units:
            # A GCC scan carries no mapper, so it reaches a header unit only at
            # its default-named path -- the one its own include spelling
            # mangles to. Respell the scan's include path and source through the
            # class root (the variant's when a BMI-only variant scan passes one,
            # else the target's own) so each class scans, and reads, its own
            # class's unit BMI. The compile keeps its spelling: only the scan
            # ever sees the alias, so a non-spaced unit's CMI stays alias-free.
            tag = class_tag if header_unit_override is not None else \
                self._header_unit_class_subdir_for(target.for_machine, compiler,
                                                   self._bmi_class_key_of(target))
            if tag is not None and self._header_unit_aliasing_available():
                commands = self._reclass_include_args(list(commands), tag)
                rel_src = self._reclass_path(rel_src, tag)
                if self._declares_system_header_unit(target.cpp_header_units):
                    # A system unit (import <h>;) is named through the built-in
                    # chain, which no -I of ours reaches, so alias that whole
                    # chain on the scan too: the mapper-less scan then resolves
                    # every system header under this class's chain roots and
                    # reads its own class's BMIs. Per target, not per unit --
                    # one scan resolves all of a TU's system imports through
                    # the aliased chain or none of them, so all the target's
                    # system units are provisioned at class-aliased names
                    # (_provision_header_unit_edges appends the same args).
                    # The compile keeps the real chain.
                    commands = commands + self._gcc_system_chain_isystem_args(
                        compiler, commands, tag)
        ddi = self.get_ddi_file_for(rel_obj)
        depfile = ddi + '.d'
        rulename = self.get_compiler_rule_name('cpp', compiler.for_machine, 'MODULE_SCAN')
        elem = NinjaBuildElement(self.all_outputs, ddi, rulename, rel_src)
        if header_unit_override is not None:
            # A BMI-only variant scan: depend on the variant class's unit
            # BMIs, whose consumer flags already ride `commands` -- the
            # target's own units belong to another class and must not appear.
            hu_outputs = header_unit_override
        else:
            hu_outputs = self.provision_header_units(target, compiler)
            if compiler.get_id() == 'clang' and hu_outputs:
                # A clang scan hard-errors on a header-unit import with no
                # matching -fmodule-file, even with the BMI built (no ambient
                # lookup), so the per-unit flags must ride the scan too.
                # Appending is non-mutating; the compile's `commands` is
                # untouched.
                commands = commands + self._target_header_unit_consumer_args.get(target.get_id(), [])
        elem.add_item('ARGS', commands)
        elem.add_item('OBJ', rel_obj)
        elem.add_item('DEPFILE', depfile)
        # The scan preprocesses the source, so anything the compile needs to
        # exist first must exist for the scan too: generated headers (e.g. a
        # configure_file / custom_target output the source #includes) and other
        # order-only inputs, plus the declared header units -- otherwise the
        # scanner errors on a missing generated header, or fails cold on a
        # header-unit import and yields an empty .ddi. The PCH is a real
        # input for the same reason: the scan args force-include it, and its
        # macros can gate imports.
        self.add_header_deps(target, elem, header_deps or [])
        elem.add_dep(pch_dep or [])
        elem.add_orderdep(self.order_deps_to_strings(target, order_deps or []))
        if compiler.get_id() == 'clang':
            # Real inputs, not order-only: the scan command names the pcms, so
            # a missing unit is a graph error rather than a build-time race.
            elem.add_dep(hu_outputs)
        else:
            elem.add_orderdep(hu_outputs)
        self.add_build(elem)

    @staticmethod
    def get_bmi_file_for(compiler: Compiler, objfile: str) -> str:
        # The source-keyed BMI standing at the object path (extension swapped
        # for the compiler's BMI suffix), so knowable at generation time: what
        # clang's bare -fmodule-output writes next to the object, and what a
        # BMI-only variant edge declares as its output.
        return objfile.rsplit('.', 1)[0] + compiler.get_module_bmi_suffix()

    @staticmethod
    def get_bmi_stamp_for(compiler: Compiler, objfile: str) -> str:
        # The harvest edge's declared output. The collator derives the same
        # path from the .ddi's primary-output (run_harvest/--stamp-suffix must
        # stay in lockstep with this).
        return objfile + compiler.get_module_bmi_suffix() + '.stamp'

    def generate_cpp_module_harvest(self, target: build.BuildTarget, compiler: Compiler,
                                    rel_obj: str, bmi_dir: str) -> None:
        """Publish a source-keyed BMI into ``bmi_dir`` (the shared class cache,
        or a target's private directory) under the module's own name, read
        from the interface's already-scanned .ddi. Used by Clang's own
        pipeline (which self-names BMIs after the source) and by BMI-only
        variants on any compiler. The edge's command line is static -- the
        module name never appears on it -- and its stamp is what consumers'
        dyndep entries order against.
        """
        self.generate_cpp_module_harvest_rule()
        pcm = self.get_bmi_file_for(compiler, rel_obj)
        ddi = self.get_ddi_file_for(rel_obj)
        stamp = self.get_bmi_stamp_for(compiler, rel_obj)
        elem = NinjaBuildElement(self.all_outputs, stamp, 'cpp_module_harvest', [pcm, ddi])
        elem.add_item('PCM', pcm)
        elem.add_item('DDI', ddi)
        elem.add_item('BMIDIR', bmi_dir)
        elem.add_item('BMISUFFIX', compiler.get_module_bmi_suffix())
        self.add_build(elem)

    def generate_p1689_module_collate_target(self, target: build.BuildTarget,
                                           compiled_sources: T.List[str],
                                           source2object: T.Dict[str, str]) -> None:
        """Emit the per-target P1689 collate edge.

        It consumes this target's per-source .ddi scans plus the provided-module
        maps of its module-providing dependencies, and produces the dyndep that
        orders the module compiles together with this target's own provided-module
        map. A single module cache at the build root is shared by every compile,
        with per-machine class subdirs keeping a cross build's build-machine and
        host-machine BMIs apart, and the cache dir is created up front for
        cl/clang. Per-compiler collator flags come from _module_collate_depargs.
        """
        cpp = target.compilers['cpp']
        private = self._module_private_bmi_dir_for(target)
        if private is not None:
            # Unconditional across compilers, mirroring _get_or_create_bmi_variant's
            # own private_dir creation: unlike the shared class cache, there is no
            # existing evidence GCC auto-creates this new top-level path shape.
            os.makedirs(os.path.join(self.environment.get_build_dir(), private), exist_ok=True)
        if cpp.get_id() in {'msvc', 'clang'}:
            # cl does not create /ifcOutput itself, and clang consumers name
            # pcm.cache before the first harvest populates it. Created even when
            # this target has its own private dir: that dir's search path still
            # needs the shared class cache for its dependencies' public modules.
            os.makedirs(os.path.join(self.environment.get_build_dir(),
                                     self._module_shared_bmi_dir_for(target)),
                        exist_ok=True)
        if cpp.get_id() == 'clang':
            self.warn_on_clang_modules_ccache(cpp)

        ddis: T.List[str] = []
        mappers: T.List[str] = []
        for src, lang in self.select_sources_to_scan(compiled_sources):
            if lang == 'cpp':
                ddis.append(self.get_ddi_file_for(source2object[src]))
                mappers.append(source2object[src] + '.mapper')

        dep_provmaps: T.List[str] = []
        dep_private_maps: T.List[T.Tuple[str, str, str]] = []
        for t in target.get_all_linked_targets():
            if isinstance(t, build.BuildTarget) and self.target_uses_p1689_cpp_modules(t) \
                    and t.provides_cpp_modules():
                dep_provmaps.append(self._module_provmap_for(target, t))
                # Unlike the provmap, never routed through a BMI-class
                # variant: a private module is never recompiled into a
                # variant (_get_or_create_bmi_variant excludes it), and a
                # name-only list has no per-class content, so a consumer
                # always reads the provider's own private-modules.json.
                dep_private_maps.append(self.private_map_ref_for(t))
        dep_provmaps.sort()
        dep_private_maps.sort()

        dyndep_file = self.get_dep_scan_file_for(target)[1]
        provmap_file = self.get_provided_modules_file_for(target)
        private_map_file = self.get_private_modules_file_for(target)
        elem = NinjaBuildElement(self.all_outputs, [dyndep_file, provmap_file, private_map_file],
                                 'cpp_module_collate', sorted(ddis))
        if cpp.get_id() == 'gcc':
            # The per-TU module mappers the compile edges rebuild on. They
            # are written copy-if-different, so restat keeps an unchanged
            # mapper from recompiling its TU when a sibling module changes.
            elem.implicit_outfilenames.extend(sorted(mappers))
            elem.add_item('restat', '1')
        if dep_provmaps:
            elem.add_dep(dep_provmaps)
        if dep_private_maps:
            elem.add_dep([p for p, _, _ in dep_private_maps])
        elem.add_item('DYNDEP', dyndep_file)
        elem.add_item('PROVMAP', provmap_file)
        # A target's own public provides are always named in the shared class
        # cache: a private provide is redirected to --private-bmi-dir in
        # DEPARGS below instead (_module_collate_depargs), never here.
        elem.add_item('BMIDIR', self._module_shared_bmi_dir_for(target))
        elem.add_item('BMISUFFIX', cpp.get_module_bmi_suffix())
        elem.add_item('DEPARGS', self._module_collate_depargs(target, cpp, dep_provmaps, dep_private_maps))
        elem.add_item('name', target.name)
        self.add_build(elem)

    def _module_collate_depargs(self, target: build.BuildTarget, cpp: Compiler,
                                dep_provmaps: T.List[str],
                                dep_private_maps: T.List[T.Tuple[str, str, str]]) -> T.List[str]:
        """Per-compiler flags for the P1689 collator (depaccumulate --p1689).

        Clang BMIs reach the shared cache via harvest edges, so the dyndep orders
        consumers against the harvest stamps (--stamp-suffix, in lockstep with
        get_bmi_stamp_for), and the collator rejects a provide from a source
        Clang did not compile as an interface unit -- extension-less interfaces
        declared via cpp_module_interfaces (or cpp_private_module_interfaces)
        are passed as source paths so the collator accepts them. For cl the
        declared header units are passed so the collator can flag an import of
        one this target never declared (cl reports header-unit requires from a
        cold scan; GCC cannot, so it is omitted there). A GCC target's collate
        also writes the per-TU module mappers (--mapper-suffix, in lockstep
        with the compile edges' get_module_mapper_args paths) its BMI lookups
        go through. A mapper disables GCC's default module->CMI naming
        outright, so it must also name every header unit the TU imports: the one
        the compile computes a name for that differs from the scan's, paired
        outright (--header-unit-bmi), and the scan-reported names by reproducing
        the default naming under the shared cache (--default-cmi-root).

        --private-map (this target's own private module names) is always
        passed; --private-bmi-dir/--private-interface-object/--all-provides-private
        only when this target actually has a private module of its own; and
        --dep-private-map once per linked, module-providing dependency, always
        naming that dependency's own private-modules.json directly (never a
        BMI-class variant's -- a private module never appears in any variant).
        A dependency is identified to the collator by its target *id*, not its
        name: the collator refuses two private providers of one name in a
        single link, and target names repeat across subdirs, so a name would
        make two distinct providers look like one (private_map_ref_for pairs
        the id with a display string the collator only ever prints).
        --private-interface-object names a provide by its object path (a
        P1689 rule's primary-output): only Clang's P1689 output carries a
        source-path for a provide at all, so the collator cannot use one as
        a compiler-agnostic privacy key the way --interface-source (a
        genuinely Clang-only concern) does.
        """
        depargs: T.List[str] = ['--private-map', self.get_private_modules_file_for(target),
                                self.private_map_display_for(target)]
        private_dir = self._module_private_bmi_dir_for(target)
        if private_dir is not None:
            depargs += ['--private-bmi-dir', private_dir]
            if target.all_cpp_modules_private():
                # Nothing can link this target, so every module it provides is
                # private by construction: no per-source list is needed, or
                # even meaningful, since cpp_private_module_interfaces may be
                # empty here. A target only *some* of whose modules are
                # private (a library, or an export_dynamic executable, with
                # cpp_private_module_interfaces) takes the branch below
                # instead -- privatizing its public provides too would name
                # their BMIs in the private dir no compile writes them to.
                depargs += ['--all-provides-private']
            else:
                for p in sorted(self._private_module_interface_objs(target)):
                    depargs += ['--private-interface-object', p]
        for path, tid, display in dep_private_maps:
            depargs += ['--dep-private-map', path, tid, display]
        for pm in dep_provmaps:
            depargs += ['--dep-provmap', pm]
        if cpp.get_id() == 'clang':
            depargs += ['--stamp-suffix', cpp.get_module_bmi_suffix() + '.stamp']
            # Declared interfaces, internal partitions and private interfaces
            # are all compiled as interface units (Clang emits a BMI only for
            # those), so pass all three as source paths the collator's
            # interface check accepts.
            for p in sorted(self._module_interface_paths(target)
                            | self._internal_partition_paths(target)
                            | self._private_module_interface_paths(target)):
                depargs += ['--interface-source', p]
        if cpp.get_id() == 'gcc':
            depargs += ['--mapper-suffix', '.mapper']
            depargs += ['--default-cmi-root', cpp.get_module_cache_dir()]
            self.provision_header_units(target, cpp)
            depargs += self._header_unit_bmi_args(
                cpp, self._target_header_unit_bmis.get(target.get_id(), []))
        if cpp.get_id() == 'msvc':
            for hu in target.cpp_header_units:
                mode, spelling = self._parse_header_unit(hu, self.build_to_src)
                depargs += ['--header-unit', f'{mode}:{spelling}']
        return depargs

    def _header_unit_bmi_args(self, cpp: Compiler,
                              bmis: T.List[T.Tuple[str, str]]) -> T.List[str]:
        # The compile-computed name of each unit paired with its BMI. A pair
        # whose name is the BMI's own stem is dropped: --default-cmi-root already
        # reconstructs it, so the pair would be redundant (which covers a whole
        # spaced or single-class build, where the scan and compile names
        # coincide with the stem). What survives is a BMI sitting at a name
        # other than its own -- a real-spelling compile whose BMI stands at the
        # scan's alias-named path -- so the collate learns that name too.
        cache_dir = cpp.get_module_cache_dir()
        suffix = cpp.get_module_bmi_suffix()
        depargs: T.List[str] = []
        for name, bmi in bmis:
            if bmi != default_cmi_path(name, cache_dir, suffix):
                depargs += ['--header-unit-bmi', name, bmi]
        return depargs

    def _module_provmap_for(self, consumer: build.BuildTarget,
                            provider: build.BuildTarget) -> str:
        """The provided-modules map `consumer` resolves `provider`'s modules
        from: the provider's own when their BMI classes agree, else a
        BMI-only variant in the consumer's class. Every compiler on the
        P1689 path supports classes.
        """
        return self._provmap_for_class(provider, self._bmi_class_key_of(consumer))

    def _provmap_for_class(self, provider: build.BuildTarget,
                           class_key: T.Tuple[str, ...]) -> str:
        if self._bmi_class_key_of(provider) == class_key:
            return self.get_provided_modules_file_for(provider)
        return self._get_or_create_bmi_variant(provider, class_key).provmap

    def _get_or_create_bmi_variant(self, provider: build.BuildTarget,
                                   class_key: T.Tuple[str, ...]) -> BmiVariant:
        """Synthesize, once per (provider, BMI class), a BMI-only
        recompilation of the provider's module interface units under that
        class: the same scan/collate/harvest pipeline, but the compiles use
        the compiler's BMI-only mode (clang --precompile, cl /ifcOnly), emit
        BMIs into the class's cache subdir and produce no objects. Consumers
        keep linking the provider's own objects, so the
        one-provider-per-module-name rule is untouched. The variant is not a
        target: its edges hang off consuming collates and dyndeps only, so a
        variant nobody links is never built.

        Only public interfaces are recompiled here (ModuleInterfaceSource.is_private
        is filtered out below): a private interface -- cpp_private_module_interfaces,
        or every interface of a module-providing executable -- has no consumer
        outside its own target, so a variant of it would be pure waste, and it is
        never published for a cross-target consumer to reach in the first place.
        A recompiled public interface may still legally *import* a private module
        (the provider's own, or a linked dependency's), so the variant's collate is
        told about both via --own-private-map/--dep-private-map: not to resolve
        the import (a private interface is never recompiled into a variant, so it
        still can't), but so an illegal cross-class import of one gets a precise
        diagnostic instead of a misleading "provided by no target" one.
        """
        memo_key = (provider.get_id(), class_key)
        existing = self._bmi_variants.get(memo_key)
        if existing is not None:
            return existing
        cpp = provider.compilers['cpp']
        info = self._bmi_classes[(provider.for_machine, class_key)]
        assert info.subdir is not None, 'a class divergence implies more than one class'
        vid = f'{provider.get_id()}@bmi@{info.subdir}'
        private_dir = os.path.join('meson-private', vid)
        variant = BmiVariant(private_dir,
                             os.path.join(private_dir, 'provided-modules.json'),
                             os.path.join(private_dir, 'depscan.dd'))
        self._bmi_variants[memo_key] = variant
        build_dir = self.environment.get_build_dir()
        os.makedirs(os.path.join(build_dir, private_dir), exist_ok=True)
        os.makedirs(os.path.join(build_dir, cpp.get_module_cache_dir(info.subdir)), exist_ok=True)

        # The provider's BMI-irrelevant flags (its includes, warnings, ...)
        # plus the class's BMI-relevant flags; later flags win, so the class
        # overrides the provider on any conflict.
        _, irrelevant = cpp.split_bmi_args(self._generate_single_compile(provider, cpp))
        args = irrelevant + info.relevant_args
        # The include path keeps the provider's spellings: a variant compile,
        # like any compile, never sees a class alias, so the CMIs it writes
        # record the provider's paths. Only the variant's scan is re-aliased
        # (inside generate_cpp_module_scan, via class_tag); the unit pairs
        # below carry the compile-computed names, and the collate join binds
        # them to the scan-named BMI in each variant TU's mapper -- the same
        # composition as a target's own edges.
        # An interface's `import <hdr>;` must resolve this class's unit BMI,
        # reusing a class-mate target's edges when they exist. Built from the
        # same pre-module args as a target's own units. `args` keeps growing
        # below, so the unit edges get their own copy.
        hu_outputs, hu_args, hu_bmis = self._provision_class_header_units(
            cpp, provider, args.copy(), class_key,
            self._header_unit_class_subdir_for(provider.for_machine, cpp, class_key),
            vid, None)
        modargs = cpp.get_module_compile_args(info.subdir)
        if cpp.get_id() == 'clang' and '-fmodules' in args:
            # Same shape as the compile-edge dance in generate_single_compile.
            modargs = [a for a in modargs if a not in {'-fmodules', '-fno-modules'}]
        if cpp.get_id() == 'msvc':
            # The variant rule names its own explicit-file /ifcOutput; drop
            # the directory pair, keep /ifcSearchDir for the class cache
            # lookup (the variant's own imports resolve there).
            i = modargs.index('/ifcOutput')
            del modargs[i:i + 2]
        args += modargs
        args += hu_args

        self.generate_bmi_variant_compile_rule(cpp)
        rulename = self.get_compiler_rule_name('cpp', cpp.for_machine, 'BMI_VARIANT')
        recs = [r for r in self._target_module_interfaces[provider.get_id()] if not r.is_private]
        ddis: T.List[str] = []
        for rec in recs:
            # cl and clang need each unit flagged explicitly, mirroring the
            # compile-edge split in generate_single_compile (an internal
            # partition takes /internalPartition, not /interface). GCC infers
            # the kind from the source. Computed per rec because a target may
            # mix interfaces and internal partitions.
            iface_args = self._module_unit_iface_args(cpp, rec.is_internal_partition)
            # The BMI is the edge's primary output, standing where the object
            # stands in the provider's own pipeline: the scan's .ddi names it
            # as primary-output, so the variant's dyndep and harvest stamp
            # derive from it exactly as they do from an object path.
            vpcm = self.get_bmi_file_for(cpp, os.path.join(private_dir, rec.obj_basename))
            header_deps = list(rec.header_deps)
            order_deps: T.List[FileOrString] = list(rec.order_deps)
            self.generate_cpp_module_scan(provider, cpp, rec.rel_src, vpcm,
                                          args + iface_args,
                                          header_deps=header_deps, order_deps=order_deps,
                                          header_unit_override=hu_outputs,
                                          class_tag=info.subdir)
            ddis.append(self.get_ddi_file_for(vpcm))
            elem = NinjaBuildElement(self.all_outputs, vpcm, rulename, rec.rel_src)
            # cl's BMI_VARIANT rule no longer bakes /interface: the per-unit
            # interface/internalPartition flag rides ARGS, as on the compile
            # edge. clang/gcc carry their unit flag in the rule itself.
            compile_iface = iface_args if cpp.get_id() == 'msvc' else []
            elem.add_item('ARGS', args + compile_iface)
            if cpp.get_id() in {'clang', 'gcc'}:
                elem.add_item('DEPFILE', vpcm + '.d')
            if cpp.get_id() == 'gcc':
                # The variant's collate writes this mapper; it sends the
                # export to $out and the imports to the class cache.
                elem.add_item('MAPPER', vpcm + '.mapper')
                elem.add_dep(vpcm + '.mapper')
            self.add_header_deps(provider, elem, header_deps)
            elem.add_orderdep(self.order_deps_to_strings(provider, order_deps))
            elem.add_dep(hu_outputs)
            elem.add_item('dyndep', variant.dyndep)
            elem.add_orderdep(variant.dyndep)
            self.add_build(elem)
            self.generate_cpp_module_harvest(provider, cpp, vpcm, cpp.get_module_cache_dir(info.subdir))

        # The variant's own imports must resolve in its class too: a linked
        # provider in the same class keeps its original provmap, a divergent
        # one gets (or reuses) a variant, recursively. A linked provider's
        # private names are never routed through a variant of their own
        # (private-modules.json is names-only and class-independent, always
        # a dependency's own file -- the same asymmetry the normal collate's
        # dep_private_maps relies on), so this mirrors that loop directly
        # rather than going through _provmap_for_class for the private side.
        dep_provmaps: T.List[str] = []
        dep_private_maps: T.List[T.Tuple[str, str, str]] = []
        for t in provider.get_all_linked_targets():
            if isinstance(t, build.BuildTarget) and self.target_uses_p1689_cpp_modules(t) \
                    and t.provides_cpp_modules():
                dep_provmaps.append(self._provmap_for_class(t, class_key))
                dep_private_maps.append(self.private_map_ref_for(t))
        dep_provmaps = sorted(set(dep_provmaps))
        dep_private_maps = sorted(set(dep_private_maps))
        # The provider's own private-modules.json: a recompiled public
        # interface may legally import the provider's own private module
        # (same target, same unkeyed-by-class private dir). Always exists --
        # every target satisfying target_uses_p1689_cpp_modules()
        # unconditionally gets its own cpp_module_collate edge elsewhere in
        # generate(), which always declares this file as an output, empty
        # JSON list or not.
        own_private_map = self.get_private_modules_file_for(provider)
        elem = NinjaBuildElement(self.all_outputs, [variant.dyndep, variant.provmap],
                                 'cpp_module_collate', sorted(ddis))
        if cpp.get_id() == 'gcc':
            # As on a target collate: the variant compiles rebuild on these.
            elem.implicit_outfilenames.extend(
                d[:-len('.ddi')] + '.mapper' for d in sorted(ddis))
            elem.add_item('restat', '1')
        if dep_provmaps:
            elem.add_dep(dep_provmaps)
        if dep_private_maps:
            elem.add_dep([p for p, _, _ in dep_private_maps])
        elem.add_dep(own_private_map)
        elem.add_item('DYNDEP', variant.dyndep)
        elem.add_item('PROVMAP', variant.provmap)
        elem.add_item('BMIDIR', cpp.get_module_cache_dir(info.subdir))
        elem.add_item('BMISUFFIX', cpp.get_module_bmi_suffix())
        depargs: T.List[str] = ['--own-private-map', own_private_map,
                                self.private_map_display_for(provider)]
        for path, tid, display in dep_private_maps:
            depargs += ['--dep-private-map', path, tid, display]
        for pm in dep_provmaps:
            depargs += ['--dep-provmap', pm]
        depargs += ['--stamp-suffix', cpp.get_module_bmi_suffix() + '.stamp']
        if cpp.get_id() == 'gcc':
            depargs += ['--mapper-suffix', '.mapper',
                        '--default-cmi-root', cpp.get_module_cache_dir()]
            depargs += self._header_unit_bmi_args(cpp, hu_bmis)
        for rec in recs:
            depargs += ['--interface-source', os.path.normpath(rec.rel_src)]
        if cpp.get_id() == 'msvc':
            # The provider's declared units, as in _module_collate_depargs:
            # cl reports header-unit requires from the variant's scans too,
            # and the collator checks them against the declared set.
            for hu in provider.cpp_header_units:
                mode, spelling = self._parse_header_unit(hu, self.build_to_src)
                depargs += ['--header-unit', f'{mode}:{spelling}']
        elem.add_item('DEPARGS', depargs)
        elem.add_item('name', vid)
        self.add_build(elem)
        self._uses_dyndeps = True
        return variant

    def warn_on_clang_modules_ccache(self, cpp: Compiler) -> None:
        # Module compiles carry -fmodules -fno-modules so ccache falls back to
        # the real compiler instead of serving stale objects (see
        # ClangCPPCompiler.get_module_compile_args). Tell users why module TUs
        # stop getting cache hits. Checks both a launcher in the command line
        # and PATH masquerade (e.g. Fedora's /usr/lib64/ccache).
        import shutil
        exelist = cpp.get_exelist()
        wrapped = exelist != cpp.get_exelist(ccache=False)
        if not wrapped:
            exe = shutil.which(exelist[0])
            wrapped = exe is not None and \
                os.path.basename(os.path.realpath(exe)).lower() in {'ccache', 'ccache.exe'}
        if wrapped:
            mlog.warning(
                'ccache is wrapping the Clang compiler while C++ modules are in use. '
                'ccache cannot cache module compiles correctly (it does not track the '
                'contents of imported BMIs), so Meson makes it fall back to the real '
                'compiler for them; module-enabled sources will not benefit from ccache.',
                once=True, fatal=False)

    @staticmethod
    def _render_bmi_divergence(name_a: str, key_a: T.Tuple[str, ...],
                               name_b: str, key_b: T.Tuple[str, ...]) -> str:
        """The "<flags> only in <target>" clauses of the header-unit divergence
        warning."""
        adiff = sorted((Counter(key_a) - Counter(key_b)).keys())
        bdiff = sorted((Counter(key_b) - Counter(key_a)).keys())
        parts = [', '.join(repr(f) for f in diff) + f' only in {n!r}'
                 for diff, n in ((adiff, name_a), (bdiff, name_b)) if diff]
        return '; '.join(parts) + '.'

    # What GCC records as a CMI's dialect and refuses to read it back under, cut
    # down to the flags a Meson option sets (cpp_std, cpp_eh, cpp_rtti). Its full
    # set is wider and moves between releases; a flag reaching it through raw
    # cpp_args is left to GCC's own scan error.
    _GCC_DIALECT_FLAGS = frozenset({'-fno-exceptions', '-fno-rtti'})

    @classmethod
    def _dialect_of(cls, class_key: T.Tuple[str, ...]) -> T.Tuple[str, ...]:
        return tuple(sorted(f for f in class_key
                            if f.startswith('-std=') or f in cls._GCC_DIALECT_FLAGS))

    def warn_on_header_unit_divergence(self, target: build.BuildTarget,
                                       class_key: T.Tuple[str, ...],
                                       unit_key: str, spelling: str) -> None:
        """Warn when targets declaring one header unit disagree on the dialect,
        or on the machine, once per (unit, consumer).

        Reached only on the degraded path: the build tree cannot make the
        directory links a per-class unit is named through (machine-wide), or a
        system unit resolves through neither its aliased chain nor a reclassed
        -I and lands on its real path (per unit; see the fall-through in
        _provision_header_unit_edges). There
        a scan can only reach the unit at its one default-named path, whichever
        class built it, and GCC rejects a CMI whose dialect differs from the
        reader's. Two machines sharing that one flat path is the same hazard on
        another axis: the build-machine and host-machine compilers write
        incompatible BMIs to it. A user or system unit earns a BMI per class and
        per machine otherwise -- named through its class's include-path or
        built-in-chain aliases -- and never arrives here. Warned about because
        GCC's own error names a CMI path and a language level, not the two
        targets that disagree.
        """
        owner_key, owner_name, owner_machine = self._header_unit_class[unit_key]
        machine_split = target.for_machine != owner_machine
        if not machine_split and self._dialect_of(class_key) == self._dialect_of(owner_key):
            return
        pair = (unit_key, target.get_id())
        if pair in self._warned_header_unit_divergence:
            return
        self._warned_header_unit_divergence.add(pair)
        if machine_split:
            mlog.warning(
                f'Targets {target.name!r} and {owner_name!r} import the same C++ '
                f'header unit {spelling!r} but build for different machines '
                f'({target.for_machine.get_lower_case_name()} and '
                f'{owner_machine.get_lower_case_name()}); on this degraded path '
                'they share one default-named header unit BMI, which the '
                'build-machine and host-machine compilers write incompatibly, so '
                'this build will fail when it scans. Declare the unit in only one '
                "machine's targets, or give the tree the directory links a "
                'per-machine unit is named through.')
            return
        mlog.warning(
            f'Targets {target.name!r} and {owner_name!r} import the same C++ '
            f'header unit {spelling!r} but compile with divergent dialects: '
            + self._render_bmi_divergence(target.name, self._dialect_of(class_key),
                                          owner_name, self._dialect_of(owner_key))
            + ' GCC records the dialect in the header unit and rejects it under '
            'any other, so this build will fail when it scans. Give the targets '
            'the same cpp_std, cpp_eh and cpp_rtti, or stop sharing the unit '
            'between them.')

    @staticmethod
    def _has_space(s: str) -> bool:
        return any(c.isspace() for c in s)

    @staticmethod
    def _read_dir_link(abs_alias: str) -> T.Optional[str]:
        """The target a directory link points at, or None if it is not a link.

        os.readlink reads a POSIX symlink and, on Python >= 3.8, a Windows
        directory symlink or junction; it raises OSError on a real directory or
        a missing path. A junction's target comes back with a '\\\\?\\'
        extended-length prefix, which is stripped so the value compares equal to
        the plain target it was made from.
        """
        try:
            target = os.readlink(abs_alias)
        except OSError:
            return None
        prefix = '\\\\?\\'
        if mesonlib.is_windows() and target.startswith(prefix):
            target = target[len(prefix):]
        return target

    @staticmethod
    def _same_dir(a: str, b: str) -> bool:
        return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))

    @staticmethod
    def _remove_dir_link(abs_alias: str) -> None:
        # A Windows directory symlink and a junction are both reparse-point
        # directories, removed with rmdir; a POSIX symlink is removed with
        # unlink.
        if mesonlib.is_windows():
            os.rmdir(abs_alias)
        else:
            os.unlink(abs_alias)

    @staticmethod
    def _make_dir_link(target: str, link: str) -> bool:
        """Create a directory link at *link* pointing at *target*. Never raises;
        returns whether a link now exists.

        POSIX makes a symlink. Windows tries a directory symlink first -- it
        needs privilege or Developer Mode but is the only form that reaches a
        target on a network share -- then falls back to an NTFS junction, which
        any user can make on a local volume. A platform that can do neither
        (FAT/exFAT, or a junction target off the local volume) leaves the header
        unit unnameable, and check_header_unit_names reports it.
        """
        if not mesonlib.is_windows():
            try:
                os.symlink(target, link)
                return True
            except (OSError, NotImplementedError):
                return False
        try:
            os.symlink(target, link, target_is_directory=True)
            return True
        except OSError:
            pass
        try:
            from _winapi import CreateJunction
        except ImportError:
            return False
        try:
            CreateJunction(target, link)
            return True
        except OSError:
            return False

    def _dir_alias_root(self, real_dir: str,
                        class_tag: T.Optional[str] = None) -> T.Optional[str]:
        """A build-relative alias root for a directory, reached through a link
        that does not move the files under it, or None when the platform cannot
        express one (the caller then falls back to check_header_unit_names).

        GCC names a header unit by the *text* of the path it was resolved
        through, and does not resolve the link in that text, so routing an
        include path through an alias root renames every unit under it without
        moving a header. This serves two unrelated needs:

        - class_tag is None: a space-free spelling of a spaced directory. A
          module mapper cannot quote whitespace, so a unit under a spaced path
          is otherwise unnameable.
        - class_tag is a BMI-class digest: a per-class name for a unit, so that
          each BMI class reaches a header through its own root and so earns its
          own default-named CMI.

        The root is a pure function of (real_dir, class_tag): two classes over
        one directory get two roots, and one class across reconfigures gets the
        same root. The link is a symlink on POSIX; on Windows a directory
        symlink where the build has the privilege for it, else an NTFS junction
        (see _make_dir_link).
        """
        try:
            return self._dir_aliases[(real_dir, class_tag)]
        except KeyError:
            pass
        # The None keying hashes the directory alone -- its path is the whole
        # identity. A class tag folds in so one directory yields a distinct root
        # per class; the separator keeps two (tag, dir) splits from colliding.
        if class_tag is None:
            digest = hashlib.sha256(real_dir.encode()).hexdigest()[:12]
        else:
            digest = hashlib.sha256(
                (class_tag + '\0' + real_dir).encode()).hexdigest()[:12]
        return self._place_dir_alias(real_dir, class_tag,
                                     f'meson-private/imap/{digest}')

    def _place_dir_alias(self, real_dir: str, class_tag: T.Optional[str],
                         rel: str) -> T.Optional[str]:
        # The shared half of the alias-root makers: create (or adopt) the link
        # at `rel` and register it both ways, so idempotency and re-classing
        # behave the same whichever spelling the root uses.
        alias: T.Optional[str] = None
        abs_alias = os.path.join(self.environment.get_build_dir(), rel)
        try:
            os.makedirs(os.path.dirname(abs_alias), exist_ok=True)
            # Idempotent: regeneration must land on the same link, and a stale
            # one (source dir moved) must be replaced, not kept.
            existing = self._read_dir_link(abs_alias)
            if existing is not None and self._same_dir(existing, real_dir):
                alias = rel
            else:
                if existing is not None:
                    self._remove_dir_link(abs_alias)
                if self._make_dir_link(real_dir, abs_alias):
                    alias = rel
        except OSError:
            mlog.debug(f'Could not create an alias root for {real_dir!r}.')
        self._dir_aliases[(real_dir, class_tag)] = alias
        if alias is not None:
            self._alias_root_real[alias.replace('\\', '/')] = real_dir
        return alias

    def _prune_stale_dir_aliases(self) -> None:
        """Remove alias-root entries this generation did not (re)place.

        A root is a pure function of (real dir, class tag): a reconfigure that
        drops a divergence, or renames a class by changing a BMI-affecting
        option, computes a different set of roots than the one on disk from
        the run before. The orphaned entries are harmless sitting there -- a
        dangling or still-valid link a few bytes each -- but nothing else ever
        reclaims them, so left alone they accumulate across every reconfigure.
        `_alias_root_real` names exactly the roots this run placed or adopted
        (registered through `_place_dir_alias`), so anything else under the
        root directory that is itself a directory link is stale and goes; a
        plain directory or file is never touched.

        Run once, after every target has been generated, so this reads the
        finished registry rather than a partial one target order could
        change. A depfile from an earlier build may still name a path this
        prunes, but pruning only ever follows a changed class set, and a
        changed class set respells the affected scans' `-I` entries (and,
        for a user unit, its source) the same way -- their command line is
        therefore different too, so ninja reruns those scans regardless of
        whether the old depfile path still resolves; a missing path there
        only makes that happen sooner, never later.
        """
        build_dir = self.environment.get_build_dir()
        live = set(self._alias_root_real.keys())
        root = 'meson-private/imap'
        abs_root = os.path.join(build_dir, root)
        try:
            names = os.listdir(abs_root)
        except OSError:
            return
        for name in names:
            if name == '.canary':
                # Only ever a transient probe of _header_unit_aliasing_available,
                # never a registered root, but also never stale-scannable.
                continue
            rel = f'{root}/{name}'
            if rel in live:
                continue
            abs_entry = os.path.join(abs_root, name)
            if self._read_dir_link(abs_entry) is None:
                continue
            self._remove_dir_link(abs_entry)

    def _respell_dir(self, d: str, class_tag: T.Optional[str] = None,
                     force_all: bool = False) -> T.Optional[str]:
        """A respelled build-relative directory, reached through an alias root.

        A directory under the source tree is respelled by substituting the
        source root's alias for its prefix, keeping the tail intact. That
        textual substitution is the whole point: a unit declared as
        'foo/../hdr.h' and an importer in foo/ must produce the same key, which
        they only do while they share one alias prefix. Aliasing each directory
        separately would give the importer 'foo_alias/../hdr.h' and the unit
        'root_alias/foo/../hdr.h' -- two names for one BMI, and neither finds
        the other's.

        Two callers with different needs: a space-free spelling of a spaced
        directory (the default), and a per-class spelling that embeds the BMI
        class in every unit name reached through the directory (force_all,
        which respells even a space-free directory since the class part must
        ride regardless of whitespace).
        """
        # Already an alias root: respelling it again would alias a symlink and
        # (outside the source tree) collapse the tail. The first pass is
        # authoritative, so a compile respelled through the class root and its
        # scan respelled again land on one spelling.
        if d.replace('\\', '/').startswith('meson-private/imap/'):
            return d
        if not force_all and not self._has_space(d):
            return d
        root = self.build_to_src
        # Separator-insensitive: build_to_src and d are OS-joined (backslash on
        # Windows), so a literal '/' prefix check would never match a
        # subdirectory there and each one would fall through to being aliased
        # on its own -- exactly the two-names-for-one-BMI outcome above warns
        # against.
        if d == root or d.replace('\\', '/').startswith(root.replace('\\', '/') + '/'):
            alias = self._dir_alias_root(self.environment.get_source_dir(), class_tag)
            return None if alias is None else alias + d[len(root):]
        # Outside the source tree (an absolute include dir, or a generated
        # source under a spaced build subdirectory): nothing traverses out of
        # it, so it needs no prefix preserved and can be aliased whole.
        return self._dir_alias_root(
            os.path.normpath(os.path.join(self.environment.get_build_dir(), d)),
            class_tag)

    def _respell_path(self, p: str, class_tag: T.Optional[str] = None,
                      force_all: bool = False) -> T.Optional[str]:
        # Only the directory matters: a quote-form import searches the
        # includer's directory first, spelled as the source was spelled on the
        # command line, so that is what lands in the key. A spaced basename is
        # harmless -- it is never a mapper key.
        head, tail = os.path.split(p)
        if not head:
            return p
        alias = self._respell_dir(head, class_tag, force_all)
        return None if alias is None else os.path.join(alias, tail)

    def _respell_include_args(self, args: T.List[str],
                              class_tag: T.Optional[str] = None,
                              force_all: bool = False) -> T.List[str]:
        out: T.List[str] = []
        it = iter(args)
        for arg in it:
            for flag in ('-I', '-isystem'):
                if arg == flag:
                    out.append(arg)
                    nxt = next(it, None)
                    if nxt is not None:
                        out.append(self._respell_dir(nxt, class_tag, force_all) or nxt)
                    break
                if arg.startswith(flag):
                    d = arg[len(flag):]
                    out.append(flag + (self._respell_dir(d, class_tag, force_all) or d))
                    break
            else:
                out.append(arg)
        return out

    def _reclass_dir(self, d: str, new_tag: T.Optional[str]) -> str:
        """A build-relative directory respelled through the alias root of
        `new_tag`'s BMI class, whatever class it currently carries.

        A directory not yet aliased is respelled outright (force_all, so a
        space-free directory still picks up the class part). One already routed
        through an alias root -- a BMI-only variant inherits the provider's
        class-respelled include path -- is un-aliased back to its real
        directory first (the inverse map), then re-aliased through the new
        class's root, so the variant reaches its own class's unit BMI rather
        than the provider's.
        """
        norm = d.replace('\\', '/')
        prefix = 'meson-private/imap/'
        if norm.startswith(prefix):
            # The alias root is the single component after imap/; the rest is
            # the tail a source-tree alias preserves. Recover the real
            # directory the root stands for and re-alias it through new_tag.
            first = norm[len(prefix):].split('/', 1)[0]
            rel = prefix + first
            tail = d[len(rel):]
            real_dir = self._alias_root_real.get(rel)
            if real_dir is None:
                return d
            new_alias = self._dir_alias_root(real_dir, new_tag)
            return d if new_alias is None else new_alias + tail
        return self._respell_dir(d, new_tag, force_all=True) or d

    def _reclass_path(self, p: str, new_tag: T.Optional[str]) -> str:
        head, tail = os.path.split(p)
        if not head:
            return p
        return os.path.join(self._reclass_dir(head, new_tag), tail)

    def _reclass_include_args(self, args: T.List[str], new_tag: T.Optional[str]) -> T.List[str]:
        out: T.List[str] = []
        it = iter(args)
        for arg in it:
            for flag in ('-I', '-isystem'):
                if arg == flag:
                    out.append(arg)
                    nxt = next(it, None)
                    if nxt is not None:
                        out.append(self._reclass_dir(nxt, new_tag))
                    break
                if arg.startswith(flag):
                    out.append(flag + self._reclass_dir(arg[len(flag):], new_tag))
                    break
            else:
                out.append(arg)
        return out

    def _header_unit_aliasing_available(self) -> bool:
        """Whether this build tree can make the directory links a per-class GCC
        header unit is named through, decided once for the whole build.

        Degrade the whole machine, not one target: two same-class targets that
        disagreed on whether they alias would compute two names for one BMI. A
        FAT/exFAT tree, or a Windows junction target off the volume, cannot
        express a link, and then the class fork falls back to the shared-flat
        naming plus the divergence warning.
        """
        if self._aliasing_available is None:
            rel = 'meson-private/imap/.canary'
            abs_canary = os.path.join(self.environment.get_build_dir(), rel)
            try:
                os.makedirs(os.path.dirname(abs_canary), exist_ok=True)
                if self._read_dir_link(abs_canary) is not None:
                    self._remove_dir_link(abs_canary)
                ok = self._make_dir_link(self.environment.get_build_dir(), abs_canary)
                if ok:
                    self._remove_dir_link(abs_canary)
                self._aliasing_available = ok
            except OSError:
                self._aliasing_available = False
        return self._aliasing_available

    def _header_unit_spelling(self, hu: T.Union[File, str],
                              compiler: Compiler,
                              class_tag: T.Optional[str] = None,
                              force_all: bool = False) -> T.Tuple[str, str]:
        """(mode, spelling) of a declared header unit as the edges spell it.

        A File-declared unit carries a path, which on GCC is respelled through
        the same alias its importers resolve through -- both sides must name
        the unit identically or they name two different units.
        """
        mode, spelling = self._parse_header_unit(hu, self.build_to_src)
        if compiler.get_id() == 'gcc' and (force_all or self._has_space(spelling)):
            spelling = self._respell_path(spelling, class_tag, force_all) or spelling
        return mode, spelling

    def _compile_needs_space_free_respell(self, target: build.BuildTarget, compiler: Compiler) -> bool:
        """Whether this target's compile include path and sources are respelled
        through an alias root: only GCC module targets that declare header
        units. A named module's mapper key is its module name, which cannot
        contain whitespace, so nothing else needs it.

        Only spaced entries actually move -- a mapper key cannot hold a space --
        so a target with no spaced path is respelled to itself and keeps the
        real spelling on its compile. The per-class scan respell is separate
        (generate_cpp_module_scan): a non-spaced unit's class rides its scan
        name, never its compile, so no alias reaches the CMI.
        """
        return (compiler.get_language() == 'cpp' and compiler.get_id() == 'gcc'
                and bool(target.cpp_header_units)
                and self.target_uses_p1689_cpp_modules(target))

    @staticmethod
    def _include_dirs_of(args: T.Iterable[str]) -> T.List[str]:
        # The include search path of a compile command, in order, in both the
        # joined ('-Idir') and separated ('-I', 'dir') spellings.
        dirs: T.List[str] = []
        it = iter(args)
        for arg in it:
            for flag in ('-I', '-isystem'):
                if arg == flag:
                    nxt = next(it, None)
                    if nxt is not None:
                        dirs.append(nxt)
                    break
                if arg.startswith(flag):
                    dirs.append(arg[len(flag):])
                    break
        return dirs

    @staticmethod
    def _without_forced_includes(args: T.Iterable[str]) -> T.List[str]:
        """A compile command with its forced includes (-include, -imacros) and
        their arguments dropped, in both the detached ('-include', 'x.h') and
        joined ('-includex.h') spellings the compiler accepts.

        Where a header resolves is a question about the include search path
        alone, so a forced include cannot move the answer the -H probe is
        after -- but it can hide it. Its text is preprocessed ahead of the
        probe's own #include, so a forced include that reaches the probed
        header (directly or through another header) leaves that #include
        skipped by the header's own include guard, and -H then reports it
        nowhere. Dropping them leaves the trace with exactly one entry at
        depth one -- the header asked for -- which is what makes taking the
        first such line safe.
        """
        out: T.List[str] = []
        it = iter(args)
        for arg in it:
            for flag in ('-include', '-imacros'):
                if arg == flag:
                    next(it, None)
                    break
                if arg.startswith(flag):
                    break
            else:
                out.append(arg)
        return out

    def _header_unit_mapper_key(self, args: T.Union['CompilerArgs', T.List[str]],
                                spelling: str) -> T.Optional[str]:
        """The path a GCC module mapper would have to name a header unit by:
        the header as resolved on the include path, which is what an importer
        looks the unit up as. None when the spelling resolves nowhere on this
        target's -I path -- a system header out of the compiler's own search
        path, whose location we would have to probe the compiler to learn.
        """
        if any(c.isspace() for c in spelling):
            # A File-spelled unit already carries its path.
            return spelling
        build_dir = self.environment.get_build_dir()
        for d in self._include_dirs_of(args):
            if os.path.isfile(os.path.join(build_dir, d, spelling)):
                return os.path.join(d, spelling)
        return None

    def _header_unit_gcc_name(self, compiler: Compiler,
                              args: T.Union['CompilerArgs', T.List[str]],
                              mode: str, spelling: str) -> T.Optional[str]:
        """The name GCC gives a header unit: the key an importer looks it up by,
        and the stem of the CMI path it writes by default.

        It is the header as the command line spells it -- './' plus the -I entry
        plus the spelling, or the path outright when that entry (or, for a system
        header, the compiler's own search path) is absolute. The text is kept as
        found: '..' is not collapsed, so a header reached two ways has two names.
        None when the header resolves nowhere.
        """
        key = self._header_unit_mapper_key(args, spelling) if mode == 'user' else None
        if key is None:
            # Off the -I path: a system header, or a quote-form spelling falling
            # through to the compiler's own search path.
            key = self._probe_header_unit_path(compiler, args, mode, spelling)
        if key is None:
            return None
        is_abs = os.path.isabs(key)
        key = key.replace('\\', '/')
        if is_abs:
            return key
        # One './', even when the -I entry is '.': GCC names a hit in the build
        # directory './hdr.h', not '././hdr.h'.
        while key.startswith('./'):
            key = key[2:]
        return './' + key

    def _run_header_probe(self, compiler: Compiler, arglist: T.List[str],
                          mode: str, spelling: str) -> T.Tuple[T.Optional[str], bool]:
        """Preprocess an #include of `spelling` under `arglist` and report
        (where -H says it resolved, whether the compiler exited clean).

        -H names each header opened, prefixed by a dot per level of nesting, so
        the first '. ' line is the header asked for, spelled as the lookup found
        it -- there is nothing else at depth one to confuse it with. None where
        no such line came back, which is not on its own an error: a header the
        args already pulled in is skipped by its own include guard and opens no
        file. The exit status tells those apart from a run that never got that
        far, and both callers need to know which they got. Memoised on the args
        it is handed, an -isystem entry being enough to move the answer.
        """
        cache_key = (compiler.get_id(), tuple(compiler.get_exelist()), tuple(arglist), mode, spelling)
        if cache_key in self._probed_header_units:
            return self._probed_header_units[cache_key]
        include = f'<{spelling}>' if mode == 'system' else f'"{spelling}"'
        cmd = compiler.get_exelist() + arglist + ['-E', '-H', '-x', 'c++', '-']
        found: T.Optional[str] = None
        try:
            # From the build directory: the -I entries are relative to it, and so
            # is the answer. stderr is read whatever the exit status, since -H
            # prints what it opened before reporting any error inside it.
            p, _, stderr = mesonlib.Popen_safe(cmd, write=f'#include {include}\n',
                                               cwd=self.environment.get_build_dir())
            ok = p.returncode == 0
        except OSError:
            stderr = ''
            ok = False
        for line in stderr.splitlines():
            if line.startswith('. '):
                found = line[2:].strip()
                break
        self._probed_header_units[cache_key] = (found, ok)
        return found, ok

    def _probe_header_unit_path(self, compiler: Compiler,
                                args: T.Union['CompilerArgs', T.List[str]],
                                mode: str, spelling: str) -> T.Optional[str]:
        """Where the compiler resolves a header unit that no -I entry of ours
        accounts for: a system header, or a quote-form spelling falling through
        to the compiler's own search path.

        Probed without the target's forced includes (_without_forced_includes),
        which cannot change where a header resolves and can only hide it -- so
        two targets whose args differ in nothing else also share the one answer.
        """
        found, _ = self._run_header_probe(
            compiler, self._without_forced_includes(args), mode, spelling)
        if found is None:
            mlog.debug(f'Could not probe the path of header unit {spelling!r}.')
        return found

    def _gcc_include_chain(self, compiler: Compiler,
                           args: T.Union['CompilerArgs', T.List[str]]) -> T.List[str]:
        """GCC's built-in ``#include <...>`` search chain, in order, probed and
        memoised once per (machine, compiler).

        A system-mode header unit is named through this chain, not through any
        -I of ours, so aliasing the chain is what respells such a unit. Only the
        compiler's own directories are wanted here: a target's own -I/-isystem
        entries are respelled separately (_reclass_include_args), so any that
        surface in the probe's ``<...>`` block are dropped, leaving a chain that
        does not vary with a target's include flags. Non-existent entries GCC
        lists but never opens are dropped too, as GCC itself skips them.
        """
        key = (compiler.for_machine, compiler.get_id(), tuple(compiler.get_exelist()))
        cached = self._gcc_include_chains.get(key)
        if cached is not None:
            return cached
        arglist = self._without_forced_includes(args)
        cmd = compiler.get_exelist() + arglist + ['-E', '-v', '-x', 'c++', '-']
        block: T.List[str] = []
        try:
            _, _, stderr = mesonlib.Popen_safe(cmd, write='\n',
                                               cwd=self.environment.get_build_dir())
        except OSError:
            stderr = ''
        collecting = False
        for line in stderr.splitlines():
            if line.startswith('#include <...> search starts here:'):
                collecting = True
            elif line.startswith('End of search list.'):
                break
            elif collecting and line.strip():
                block.append(line.strip())
        build_dir = self.environment.get_build_dir()
        def real(d: str) -> str:
            return os.path.normcase(os.path.realpath(os.path.join(build_dir, d)))
        user = {real(d) for d in self._include_dirs_of(arglist)}
        chain: T.List[str] = []
        seen: T.Set[str] = set()
        for d in block:
            r = real(d)
            if r in user or r in seen or not os.path.isdir(r):
                continue
            seen.add(r)
            chain.append(d)
        self._gcc_include_chains[key] = chain
        return chain

    def _gcc_system_chain_isystem_args(self, compiler: Compiler,
                                       args: T.Union['CompilerArgs', T.List[str]],
                                       class_tag: str) -> T.List[str]:
        """`-isystem` aliases of GCC's whole built-in chain, in order, each
        routed through `class_tag`'s alias root, led by
        `-fno-canonical-system-headers`.

        GCC names a system header unit by the search-path text it resolves
        through, so aliasing the chain renames the unit per class. But the
        compiler otherwise keeps the shorter of an include spelling and a
        header's realpath, discarding an alias longer than the realpath (a
        system realpath can be as short as /usr/include, undercutting any
        meson-private spelling) and with it the per-class name;
        -fno-canonical-system-headers turns that off. The flag and the aliases
        it protects are therefore one unit -- every edge and probe that models
        this resolution takes both or neither, or its mapper key would not
        match what the edge asks for.

        GCC dedups include directories by identity, so an alias of a built-in
        directory substitutes it at the built-in's own position -- search order
        and include_next are preserved, only the text a resolved system header
        carries changes. Aliasing a late entry alone would instead hoist it
        ahead of the earlier chain, so it is the whole chain or, when any one
        root cannot be placed, none of it: a partially aliased chain is a
        reordered one, while an empty result just leaves the scan resolving
        real names and every system unit degrading to the shared flat path.
        """
        chain = self._gcc_include_chain(compiler, args)
        if not chain:
            return []
        build_dir = self.environment.get_build_dir()
        real_dirs = [os.path.normpath(os.path.join(build_dir, d)) for d in chain]
        out: T.List[str] = ['-fno-canonical-system-headers']
        for rd in real_dirs:
            alias = self._dir_alias_root(rd, class_tag)
            if alias is None:
                # A root under our own meson-private/imap/ that could not be
                # placed (a stray entry occupying its digest path) degrades
                # system-unit naming to the shared flat path -- never a partial,
                # reordered chain. The result is deterministic per directory, so
                # every declarer agrees; said once per machine, since the
                # shared-BMI consequence is otherwise silent until the
                # divergence warning (or GCC's own scan error) fires.
                key = (compiler.for_machine, compiler.get_id(),
                       tuple(compiler.get_exelist()))
                if key not in self._warned_system_chain_alias:
                    self._warned_system_chain_alias.add(key)
                    digest = hashlib.sha256(
                        (class_tag + '\0' + rd).encode()).hexdigest()[:12]
                    mlog.warning(
                        f'Cannot create the directory link '
                        f'{os.path.join(build_dir, "meson-private", "imap", digest)!r} '
                        'that per-class naming of C++ system header units routes '
                        'through: the path is occupied, or its parent is not a '
                        'directory. System header units on this machine degrade '
                        'to one shared BMI per unit, so targets importing one '
                        'under divergent dialects will conflict.')
                return []
            out += ['-isystem', alias]
        return out

    def _declares_system_header_unit(self, header_units: T.Sequence[T.Union[File, str]]) -> bool:
        # Whether any declared unit is system-mode (import <h>;) -- the
        # per-target trigger for aliasing the built-in chain on scan edges.
        return any(self._parse_header_unit(hu, self.build_to_src)[0] == 'system'
                   for hu in header_units)

    def check_header_unit_names(self, target: build.BuildTarget,
                                args: T.Union['CompilerArgs', T.List[str]]) -> None:
        """Report the declared header units GCC cannot name.

        GCC names a unit by its resolved header path: that path is both the key
        an importer's module mapper looks the unit up by and the stem of the CMI
        path GCC writes it to by default. Two things can leave a unit without a
        usable name, and each gets its own diagnosis.

        The header resolves nowhere (no name at all). Nothing can be built for
        it -- a name is what both halves of the naming scheme are a function of --
        so _provision_header_unit_edges emits no edge, and the unit is only ever
        reported here. A *user* unit is answered first by walking the target's own
        -I/-isystem entries for the file, which is a filesystem fact and asks no
        compiler: failing that walk means the header is on none of them, and the
        compiler probe that follows is a courtesy for a quote-form spelling
        falling through to the compiler's own search path. Nothing about the
        resulting build can work, so it is an error. A *system* spelling has no
        such offline answer -- only the probe, a compiler run that also comes back
        empty when the target's own args fail to preprocess, when a sysroot cannot
        be entered, or when the compiler cannot be spawned at all. A probe that
        misses must not take the build down with it: the unit is dropped with a
        warning, which costs nothing where it was declared but never imported, and
        where it *is* imported the compiler reports the missing header itself, at
        the importer's own scan.

        The resolved path holds whitespace. A mapper key ends at the first space
        or tab and has no quoting or escape form, so the unit cannot be named in
        one. Such a header is normally reached through a space-free alias
        (_respell_dir), which leaves no whitespace in the key at all; this fires
        only where that alias was needed and the platform could not make one, or
        where the path is the compiler's own and no -I of ours can respell it.
        The unit still builds, at the one default-named path a mapper-less edge
        can give it, so this stays a warning.

        Reported once per unit: every target importing it fails the same way.
        """
        compiler = target.compilers['cpp']
        for hu in target.cpp_header_units:
            mode, spelling = self._header_unit_spelling(hu, compiler)
            key = self._header_unit_gcc_name(compiler, args, mode, spelling)
            if key is not None and not self._has_space(key):
                continue
            if key is None and mode == 'user':
                raise MesonException(
                    f'Cannot resolve the C++ header unit {spelling!r} declared by '
                    f'target {target.name!r}: no such header on its include path. '
                    'A header unit is named by the path it resolves to, which GCC '
                    'needs before anything can be built for it, so a header that '
                    'does not exist yet cannot be one: a header generated during '
                    'the build (a custom_target or generator output) is not usable '
                    'as a header unit here. Generate it at configure time '
                    '(configure_file), add the include directory that holds it, or '
                    'drop it from cpp_header_units and #include it instead.')
            if spelling in self._warned_header_unit_names:
                continue
            self._warned_header_unit_names.add(spelling)
            if key is None:
                mlog.warning(
                    f'Cannot resolve the C++ header unit {spelling!r} declared by '
                    f'target {target.name!r}: the compiler finds no such header. It '
                    'is on no include path, or it is generated during the build -- a '
                    'header unit is named by the path it resolves to, which GCC needs '
                    'at configure time, so a header that does not exist yet cannot be '
                    'one. Meson builds no BMI for it: a source importing it fails to '
                    'build, the compiler reporting the missing header itself. Fix the '
                    'spelling or the dependency that provides the header, or drop it '
                    'from cpp_header_units.', fatal=False)
                continue
            mlog.warning(
                f'Target {target.name!r} imports the C++ header unit {spelling!r}, '
                f'which resolves to {key!r} -- a path containing whitespace. GCC '
                'resolves modules through a module mapper, and a mapper cannot name '
                'a header unit whose path contains a space: the compile will fail '
                'with "no such module". Meson normally routes such a header through '
                'a space-free alias, but could not create one here (creating it needs '
                'symlink support, which Windows does not give unprivileged builds). '
                'Move the source tree to a path without spaces.', fatal=False)

    def warn_on_preincluded_header_units(self, target: build.BuildTarget,
                                         args: T.Union['CompilerArgs', T.List[str]]) -> None:
        """Warn when a target's forced includes (-include, -imacros) already
        pull in a header the same target declares as a header unit.

        A header unit is compiled as its own main file, so a forced include that
        reaches the same header first leaves nothing for the unit to be built
        from: the compiler either rejects it (a '#pragma once' header, read once
        as text and once as the unit, collides with itself) or accepts an empty
        one, whose importers then find none of the declarations they came for. A
        header cannot be both text and a unit in one compile, and the unit's
        edge cannot drop the forced include to make room -- a unit built under
        different preprocessor state than its consumers is what the BMI classes
        exist to prevent.

        Asked of the compiler rather than guessed at: an #include that opens no
        file under the target's own args is one the args had already opened. A
        run that failed outright says nothing either way -- a forced include may
        name a header generated later in the build, which does not exist at setup
        time -- so only a clean run is read.
        """
        arglist = list(args)
        stripped = self._without_forced_includes(arglist)
        if stripped == arglist:
            return
        compiler = target.compilers['cpp']
        for hu in target.cpp_header_units:
            mode, spelling = self._header_unit_spelling(hu, compiler)
            if self._header_unit_gcc_name(compiler, args, mode, spelling) is None:
                # Resolves nowhere even without the forced includes: a different
                # complaint, and not this one's to make.
                continue
            found, ok = self._run_header_probe(compiler, arglist, mode, spelling)
            if not ok or found is not None:
                continue
            pair = (target.get_id(), spelling)
            if pair in self._warned_preincluded_header_units:
                continue
            self._warned_preincluded_header_units.add(pair)
            mlog.warning(
                f'Target {target.name!r} declares the C++ header unit {spelling!r}, '
                'but its own arguments force-include a header that already includes '
                'it. The unit is compiled from that header as a main file, and its '
                'include guard has been tripped by then: the compiler will either '
                'reject the unit or build an empty one, whose importers see none of '
                'its declarations. Drop the forced include, or stop declaring the '
                'header as a unit.', fatal=False)

    def _bmi_class_key_of(self, target: build.BuildTarget) -> T.Tuple[str, ...]:
        cpp = target.compilers['cpp']
        return cpp.get_bmi_class_key(self._generate_single_compile(target, cpp))

    def _compute_bmi_class_registry(self) -> None:
        """Freeze the per-machine set of BMI equivalence classes before any
        target is generated (compile args must not depend on generation order).
        The whole build keeps the flat cache dir only when it has exactly one
        (machine, class) entry; otherwise every class gets its own subdirectory
        named by a digest of the machine identity and the class key. Folding the
        machine in keeps a cross build's two compilers apart: identical
        BMI-relevant flags on the build and host machines (the common case) no
        longer collide in one subdir, and a two-machine build never flattens
        into a shared cache even at one class per machine, where the BMIs are
        still not interchangeable. Only compilers with supports_bmi_classes()
        participate -- the same registry keys their header units per class; the
        rest keep the divergence warning. The key is computed from
        _generate_single_compile, which excludes get_module_compile_args, so the
        chosen dir cannot feed back into the key.
        """
        per_machine: T.DefaultDict[MachineChoice, T.Dict[T.Tuple[str, ...], T.List[str]]] = defaultdict(dict)
        for t in self.build.get_targets().values():
            if not isinstance(t, build.BuildTarget) or not self.target_uses_p1689_cpp_modules(t):
                continue
            cpp = t.compilers['cpp']
            if not cpp.supports_bmi_classes():
                continue
            relevant, _ = cpp.split_bmi_args(self._generate_single_compile(t, cpp))
            per_machine[t.for_machine].setdefault(tuple(sorted(relevant)), relevant)
        multi = sum(len(classes) for classes in per_machine.values()) > 1
        for machine, classes in per_machine.items():
            for key, relevant in classes.items():
                subdir = hashlib.sha256(
                    (machine.get_lower_case_name() + '\x00' + '\x00'.join(key)).encode()
                ).hexdigest()[:12] if multi else None
                self._bmi_classes[(machine, key)] = BmiClassInfo(subdir, relevant)

    def _bmi_class_subdir_for(self, target: build.BuildTarget) -> T.Optional[str]:
        if 'cpp' not in target.compilers or not target.compilers['cpp'].supports_bmi_classes():
            return None
        info = self._bmi_classes.get((target.for_machine, self._bmi_class_key_of(target)))
        return info.subdir if info else None

    def _module_shared_bmi_dir_for(self, target: build.BuildTarget) -> str:
        """The class-cache directory a target's dependencies' public modules are
        found in -- also this target's own BMI directory, unless it is a
        module-providing executable (see _module_private_bmi_dir_for).
        """
        cpp = target.compilers['cpp']
        return cpp.get_module_cache_dir(self._bmi_class_subdir_for(target))

    def _module_private_bmi_dir_for(self, target: build.BuildTarget) -> T.Optional[str]:
        """A directory outside the shared cache for the modules of
        target.provides_private_cpp_modules(): a link sink, whose modules
        nothing could ever import anyway, or any target with an explicit
        cpp_private_module_interfaces declaration. Only the target's private
        modules go here -- a target may well publish others to the shared
        cache alongside (_is_private_module_source decides per source).
        Unkeyed by BMI class: a private module's only possible consumers are
        this target's own translation units, which all share one class.
        """
        if target.provides_private_cpp_modules():
            return os.path.join('meson-private', f'{target.get_id()}@bmi-private')
        return None

    def _private_module_interface_paths(self, target: build.BuildTarget) -> T.Set[str]:
        # Normalized build-root-relative paths of the sources this target
        # declares as private module interfaces (cpp_private_module_interfaces),
        # the same comparable key as _module_interface_paths.
        return self._module_kwarg_paths(target, target.cpp_private_module_interfaces)

    def _private_module_interface_objs(self, target: build.BuildTarget) -> T.Set[str]:
        """Build-root-relative object paths of this target's own private
        interface compiles, matching a P1689 rule's primary-output.

        The collator cannot key privacy by source-path: only Clang's P1689
        output carries one for a provide, GCC's carries none at all (only
        logical-name and is-interface), so a source-path is not a
        compiler-agnostic key. primary-output, unlike source-path, is part of
        the P1689 schema every compiler's output includes.
        """
        target_dir = self.get_target_private_dir(target)
        return {os.path.join(target_dir, rec.obj_basename)
                for rec in self._target_module_interfaces.get(target.get_id(), [])
                if rec.is_private}

    def _is_declared_private_interface(self, target: build.BuildTarget, src: 'FileOrString') -> bool:
        # Whether src is one of target's declared cpp_private_module_interfaces sources.
        if not target.cpp_private_module_interfaces:
            return False
        key = src.rel_to_builddir(self.build_to_src) if isinstance(src, File) else src
        return os.path.normpath(key) in self._private_module_interface_paths(target)

    def _is_private_module_source(self, target: build.BuildTarget, src: 'FileOrString') -> bool:
        """Whether src's module (if any) is private: every source of a target
        whose modules are wholly private (a link sink -- nothing can import
        them), or a source this target explicitly names in
        cpp_private_module_interfaces.

        The same predicate decides --all-provides-private on the collate
        (_module_collate_depargs), so the dyndep and the compile that writes
        the BMI always agree on which directory it lands in.
        """
        if target.all_cpp_modules_private():
            return True
        return self._is_declared_private_interface(target, src)

    def _header_unit_class_subdir_for(self, for_machine: MachineChoice, compiler: Compiler,
                                      class_key: T.Tuple[str, ...]) -> T.Optional[str]:
        # The header-unit analogue of _bmi_class_subdir_for, keyed directly (the
        # BMI-only variant path provisions units without a target to look the
        # key up from).
        if not compiler.supports_bmi_classes():
            return None
        info = self._bmi_classes.get((for_machine, class_key))
        return info.subdir if info else None

    @staticmethod
    def source_scan_language(src: FileOrString) -> T.Optional[Literal['cpp', 'fortran']]:
        """The scanning language of a source, or None if it is not scanned.

        The suffix alone decides this, and it is the only thing that does:
        the C++ compiler also compiles sources with no modules in them (it
        assembles .s/.S), so a source's compile edge belongs to the module
        pipeline only if this says 'cpp' -- otherwise nothing scans it and
        nothing declares its per-TU outputs.
        """
        fname = src.fname if isinstance(src, File) else src
        ext = os.path.splitext(fname)[1][1:]
        if ext.lower() in compilers.lang_suffixes['cpp'] or ext == 'C':
            return 'cpp'
        if ext.lower() in compilers.lang_suffixes['fortran']:
            return 'fortran'
        return None

    def select_sources_to_scan(self, compiled_sources: T.List[str],
                               ) -> T.Iterable[T.Tuple[str, Literal['cpp', 'fortran']]]:
        # in practice pick up C++ and Fortran files. If some other language
        # requires scanning (possibly Java to deal with inner class files)
        # then add them here.
        for source in compiled_sources:
            if isinstance(source, mesonlib.File):
                source = source.rel_to_builddir(self.build_to_src)
            lang = self.source_scan_language(source)
            if lang is not None:
                yield source, lang

    def process_target_dependencies(self, target: build.BuildTarget) -> None:
        for t in target.get_dependencies():
            if t.get_id() not in self.processed_targets:
                self.generate_target(t.get_target())

    def custom_target_generator_inputs(self, target: build.CustomTarget) -> None:
        for s in target.sources:
            if isinstance(s, build.GeneratedList):
                self.generate_genlist_for_target(s, target)

    def generate_custom_target(self, target: build.CustomTarget) -> None:
        self.custom_target_generator_inputs(target)
        (srcs, ofilenames, cmd) = self.eval_custom_target_command(target)
        deps = self.get_paths_for_dep_outputs(target, target.get_dependencies())
        deps += self.get_target_depend_files(target)
        deps += self.get_paths_for_dep_outputs(target, target.extra_depends)
        if target.build_always_stale:
            deps.append('PHONY')
        if target.depfile_type == 'gcc':
            rulename = 'CUSTOM_COMMAND_DEP'
        elif target.depfile_type == 'msvc':
            rulename = 'CUSTOM_COMMAND_MSVC_DEP'
        else:
            rulename = 'CUSTOM_COMMAND'
        elem = NinjaBuildElement(self.all_outputs, ofilenames, rulename, srcs)
        elem.add_dep(deps)

        cmd, reason = self.as_meson_exe_cmdline(target.command[0], cmd[1:],
                                                extra_bdeps=target.get_transitive_build_target_deps(),
                                                capture=ofilenames[0] if target.capture else None,
                                                feed=srcs[0] if target.feed else None,
                                                env=target.env,
                                                can_use_rsp_file=target.rspable,
                                                verbose=target.console)
        if reason:
            cmd_type = f' (wrapped by meson {reason})'
        else:
            cmd_type = ''
        if target.depfile is not None:
            depfile = target.get_dep_outname(elem.infilenames)
            rel_dfile = os.path.join(self.get_target_dir(target), depfile)
            abs_pdir = os.path.join(self.environment.get_build_dir(), self.get_target_dir(target))
            os.makedirs(abs_pdir, exist_ok=True)
            elem.add_item('DEPFILE', rel_dfile)
        if target.console:
            elem.add_item('pool', 'console')
        full_name = Path(target.subdir, target.name).as_posix()
        elem.add_item('COMMAND', cmd)
        elem.add_item('description', target.description.format(full_name) + cmd_type)
        self.add_build(elem)
        self.processed_targets.add(target.get_id())

    def build_run_target_name(self, target: build.RunTarget) -> str:
        if target.subproject != '':
            subproject_prefix = f'{target.subproject}@@'
        else:
            subproject_prefix = ''
        return f'{subproject_prefix}{target.name}'

    def generate_run_target(self, target: build.RunTarget) -> None:
        target_name = self.build_run_target_name(target)
        if not target.command:
            # This is an alias target, it has no command, it just depends on
            # other targets.
            elem = NinjaBuildElement(self.all_outputs, target_name, 'phony', [])
        else:
            target_env = self.get_run_target_env(target)
            _, _, cmd = self.eval_custom_target_command(target)
            meson_exe_cmd, reason = self.as_meson_exe_cmdline(target.command[0], cmd[1:],
                                                              env=target_env,
                                                              verbose=True)
            cmd_type = f' (wrapped by meson {reason})' if reason else ''
            elem = self.create_phony_target(target_name, 'CUSTOM_COMMAND', [])
            elem.add_item('COMMAND', meson_exe_cmd)
            elem.add_item('description', f'Running external command {target.name}{cmd_type}')
            elem.add_item('pool', 'console')
        deps = self.get_paths_for_dep_outputs(target, target.get_dependencies())
        deps += self.get_target_depend_files(target)
        elem.add_dep(deps)
        self.add_build(elem)
        self.processed_targets.add(target.get_id())

    def generate_coverage_command(self, elem: NinjaBuildElement, outputs: T.List[str],
                                  gcovr_exe: T.Optional[str], llvm_cov_exe: T.Optional[str]) -> None:
        targets = self.build.get_targets().values()
        use_llvm_cov = False
        exe_args = []
        if gcovr_exe is not None:
            exe_args += ['--gcov', gcovr_exe]
        if llvm_cov_exe is not None:
            exe_args += ['--llvm-cov', llvm_cov_exe]

        for target in targets:
            if not isinstance(target, build.BuildTarget):
                continue
            for compiler in target.compilers.values():
                if compiler.get_id() == 'clang' and not compiler.info.is_darwin():
                    use_llvm_cov = True
                    break
        elem.add_item('COMMAND', self.environment.get_build_command() +
                      ['--internal', 'coverage'] +
                      outputs +
                      [self.environment.get_source_dir(),
                       os.path.join(self.environment.get_source_dir(),
                                    self.build.get_subproject_dir()),
                       self.environment.get_build_dir(),
                       self.environment.get_log_dir()] +
                      exe_args +
                      (['--use-llvm-cov'] if use_llvm_cov else []))

    def generate_coverage_rules(self, gcovr_exe: T.Optional[str], gcovr_version: T.Optional[str], llvm_cov_exe: T.Optional[str]) -> None:
        e = self.create_phony_target('coverage', 'CUSTOM_COMMAND', 'PHONY')
        self.generate_coverage_command(e, [], gcovr_exe, llvm_cov_exe)
        e.add_item('description', 'Generating coverage reports')
        self.add_build(e)
        self.generate_coverage_legacy_rules(gcovr_exe, gcovr_version, llvm_cov_exe)

    def generate_coverage_legacy_rules(self, gcovr_exe: T.Optional[str], gcovr_version: T.Optional[str], llvm_cov_exe: T.Optional[str]) -> None:
        e = self.create_phony_target('coverage-html', 'CUSTOM_COMMAND', 'PHONY')
        self.generate_coverage_command(e, ['--html'], gcovr_exe, llvm_cov_exe)
        e.add_item('description', 'Generating HTML coverage report')
        self.add_build(e)

        if gcovr_exe:
            e = self.create_phony_target('coverage-xml', 'CUSTOM_COMMAND', 'PHONY')
            self.generate_coverage_command(e, ['--xml'], gcovr_exe, llvm_cov_exe)
            e.add_item('description', 'Generating XML coverage report')
            self.add_build(e)

            e = self.create_phony_target('coverage-text', 'CUSTOM_COMMAND', 'PHONY')
            self.generate_coverage_command(e, ['--text'], gcovr_exe, llvm_cov_exe)
            e.add_item('description', 'Generating text coverage report')
            self.add_build(e)

            if mesonlib.version_compare(gcovr_version, '>=4.2'):
                e = self.create_phony_target('coverage-sonarqube', 'CUSTOM_COMMAND', 'PHONY')
                self.generate_coverage_command(e, ['--sonarqube'], gcovr_exe, llvm_cov_exe)
                e.add_item('description', 'Generating Sonarqube XML coverage report')
                self.add_build(e)

    def generate_install(self) -> None:
        self.create_install_data_files()
        elem = self.create_phony_target('install', 'CUSTOM_COMMAND', 'PHONY')
        elem.add_dep('all')
        elem.add_item('DESC', 'Installing files')
        elem.add_item('COMMAND', self.environment.get_build_command() + ['install', '--no-rebuild'])
        elem.add_item('pool', 'console')
        self.add_build(elem)

    def generate_tests(self) -> None:
        self.serialize_tests()
        cmd = self.environment.get_build_command(True) + ['test', '--no-rebuild']
        if not self.environment.coredata.optstore.get_value_for(OptionKey('stdsplit')):
            cmd += ['--no-stdsplit']
        if self.environment.coredata.optstore.get_value_for(OptionKey('errorlogs')):
            cmd += ['--print-errorlogs']
        elem = self.create_phony_target('test', 'CUSTOM_COMMAND', ['all', 'meson-test-prereq', 'PHONY'])
        elem.add_item('COMMAND', cmd)
        elem.add_item('DESC', 'Running all tests')
        elem.add_item('pool', 'console')
        self.add_build(elem)

        # And then benchmarks.
        cmd = self.environment.get_build_command(True) + [
            'test', '--benchmark', '--logbase',
            'benchmarklog', '--num-processes=1', '--no-rebuild']
        elem = self.create_phony_target('benchmark', 'CUSTOM_COMMAND', ['all', 'meson-benchmark-prereq', 'PHONY'])
        elem.add_item('COMMAND', cmd)
        elem.add_item('DESC', 'Running benchmark suite')
        elem.add_item('pool', 'console')
        self.add_build(elem)

    def generate_rules(self) -> None:
        self.add_rule_comment(NinjaComment('Rules for module scanning.'))
        self.generate_scanner_rules()
        self.add_rule_comment(NinjaComment('Rules for compiling.'))
        self.generate_compile_rules()
        self.add_rule_comment(NinjaComment('Rules for linking.'))
        self.generate_static_link_rules()
        self.generate_dynamic_link_rules()
        self.add_rule_comment(NinjaComment('Other rules'))

        # Ninja errors out if you have deps = gcc but no depfile, so we must
        # have two rules for custom commands.
        self.add_rule(NinjaRule('CUSTOM_COMMAND', ['$COMMAND'], [], '$DESC',
                                restat=True))
        self.add_rule(NinjaRule('CUSTOM_COMMAND_DEP', ['$COMMAND'], [], '$DESC',
                                deps='gcc', depfile='$DEPFILE',
                                restat=True))
        self.add_rule(NinjaRule('CUSTOM_COMMAND_MSVC_DEP', ['$COMMAND'], [], '$DESC',
                                deps='msvc',
                                restat=True))
        self.add_rule(NinjaRule('COPY_FILE', self.environment.get_build_command() + ['--internal', 'copy'],
                                ['$in', '$out'], 'Copying $in to $out'))

        c = self.environment.get_build_command() + \
            ['--internal',
             'regenerate',
             self.environment.get_source_dir(),
             # Ninja always runs from the build_dir. This includes cases where the user moved the
             # build directory and invalidated most references. Make sure it still regenerates.
             '.']
        self.add_rule(NinjaRule('REGENERATE_BUILD',
                                c, [],
                                'Regenerating build files',
                                extra='generator = 1'))

    def add_rule_comment(self, comment: NinjaComment) -> None:
        self.ninja.add_rule_comment(comment)

    def add_build_comment(self, comment: NinjaComment) -> None:
        self.ninja.add_build_comment(comment)

    def add_rule(self, rule: NinjaRule) -> None:
        self.ninja.add_rule(rule)

    def add_build(self, build: NinjaBuildElement) -> None:
        self.ninja.add_build(build)

    def generate_phony(self) -> None:
        self.add_build_comment(NinjaComment('Phony build target, always out of date'))
        elem = NinjaBuildElement(self.all_outputs, 'PHONY', 'phony', '')
        self.add_build(elem)

    def generate_jar_target(self, target: build.Jar) -> None:
        fname = target.get_filename()
        outname_rel = os.path.join(self.get_target_dir(target), fname)
        src_list = target.get_sources()
        resources = target.get_java_resources()
        compiler = target.compilers['java']
        c = 'c'
        m = 'm'
        e = ''
        f = 'f'
        main_class = target.get_main_class()
        if main_class != '':
            e = 'e'

        # Add possible java generated files to src list
        generated_sources = self.get_target_generated_sources(target)
        gen_src_list = []
        for rel_src in generated_sources:
            raw_src = File.from_built_relative(rel_src)
            if rel_src.endswith('.java'):
                gen_src_list.append(raw_src)

        compile_args = self.determine_java_compile_args(target, compiler)
        class_list = self.generate_java_compile([*src_list, *gen_src_list], target, compiler, compile_args)
        class_dep_list = [os.path.join(self.get_target_private_dir(target), i) for i in class_list]
        manifest_path = os.path.join(self.get_target_private_dir(target), 'META-INF', 'MANIFEST.MF')
        manifest_fullpath = os.path.join(self.environment.get_build_dir(), manifest_path)
        os.makedirs(os.path.dirname(manifest_fullpath), exist_ok=True)
        with open(manifest_fullpath, 'w', encoding='utf-8') as manifest:
            if any(target.link_targets):
                manifest.write('Class-Path: ')
                cp_paths = [os.path.join(self.get_target_dir(l), l.get_filename()) for l in target.link_targets]
                manifest.write(' '.join(cp_paths))
            manifest.write('\n')
        jar_rule = 'java_LINKER'
        commands = [c + m + e + f]
        commands.append(manifest_path)
        if e != '':
            commands.append(main_class)
        commands.append(self.get_target_filename(target))
        # Java compilation can produce an arbitrary number of output
        # class files for a single source file. Thus tell jar to just
        # grab everything in the final package.
        commands += ['-C', self.get_target_private_dir(target), '.']
        elem = NinjaBuildElement(self.all_outputs, outname_rel, jar_rule, [])
        elem.add_dep(class_dep_list)
        if resources:
            # Copy all resources into the root of the jar.
            elem.add_orderdep(self.__generate_sources_structure(Path(self.get_target_private_dir(target)), resources)[0])
        elem.add_item('ARGS', commands)
        self.add_build(elem)
        # Create introspection information
        self.create_target_source_introspection(target, compiler, compile_args, src_list, gen_src_list)

    def generate_cs_resource_tasks(self, target: build.BuildTarget) -> T.Tuple[T.List[str], T.List[str]]:
        args = []
        deps = []
        for r in target.resources:
            rel_sourcefile = os.path.join(self.build_to_src, target.subdir, r)
            if r.endswith('.resources'):
                a = '-resource:' + rel_sourcefile
            elif r.endswith('.txt') or r.endswith('.resx'):
                ofilebase = os.path.splitext(os.path.basename(r))[0] + '.resources'
                ofilename = os.path.join(self.get_target_private_dir(target), ofilebase)
                elem = NinjaBuildElement(self.all_outputs, ofilename, "CUSTOM_COMMAND", rel_sourcefile)
                elem.add_item('COMMAND', ['resgen', rel_sourcefile, ofilename])
                elem.add_item('DESC', f'Compiling resource {rel_sourcefile}')
                self.add_build(elem)
                deps.append(ofilename)
                a = '-resource:' + ofilename
            else:
                raise InvalidArguments(f'Unknown resource file {r}.')
            args.append(a)
        return args, deps

    def generate_cs_target(self, target: build.BuildTarget) -> None:
        fname = target.get_filename()
        outname_rel = os.path.join(self.get_target_dir(target), fname)
        src_list = target.get_sources()
        compiler = T.cast('CsCompiler', target.compilers['cs'])
        rel_srcs = [os.path.normpath(s.rel_to_builddir(self.build_to_src)) for s in src_list]
        deps = []
        commands = self.generate_basic_compiler_args(target, compiler)
        commands += target.extra_args['cs']
        if isinstance(target, build.Executable):
            commands.append('-target:exe')
        elif isinstance(target, build.SharedLibrary):
            commands.append('-target:library')
        else:
            raise MesonException('Unknown C# target type.')
        (resource_args, resource_deps) = self.generate_cs_resource_tasks(target)
        commands += resource_args
        deps += resource_deps
        commands += compiler.get_output_args(outname_rel)
        for l in target.link_targets:
            lname = os.path.join(self.get_target_dir(l), l.get_filename())
            commands += compiler.get_link_args(lname)
            deps.append(lname)
        if '-g' in commands:
            outputs = [outname_rel, outname_rel + '.mdb']
        else:
            outputs = [outname_rel]
        generated_sources = self.get_target_generated_sources(target)
        generated_rel_srcs = []
        for rel_src in generated_sources:
            if rel_src.lower().endswith('.cs'):
                generated_rel_srcs.append(os.path.normpath(rel_src))
            deps.append(os.path.normpath(rel_src))

        for dep in target.get_external_deps():
            commands.extend_direct(dep.get_link_args())

        elem = NinjaBuildElement(self.all_outputs, outputs, self.compiler_to_rule_name(compiler), rel_srcs + generated_rel_srcs)
        elem.add_dep(deps)
        elem.add_item('ARGS', commands)
        self.add_build(elem)

        self.create_target_source_introspection(target, compiler, commands, rel_srcs, generated_rel_srcs)

    def determine_java_compile_args(self, target: build.Jar, compiler: Compiler) -> T.List[str]:
        args = self.generate_basic_compiler_args(target, compiler)
        args += target.get_java_args()
        args += compiler.get_output_args(self.get_target_private_dir(target))
        args += target.get_classpath_args()
        curdir = target.get_subdir()
        sourcepaths = [os.path.join(self.build_to_src, curdir)]
        sourcepaths.append(os.path.normpath(curdir))
        for i in target.include_dirs:
            sourcepaths.extend(i.abs_string_list(self.source_dir, self.build_dir))
        args += ['-sourcepath', os.pathsep.join(sourcepaths)]
        return list(args)

    def generate_java_compile(self, srcs: T.List[File], target: build.BuildTarget, compiler: Compiler, args: T.List[str]) -> T.List[str]:
        deps = [os.path.join(self.get_target_dir(l), l.get_filename()) for l in target.link_targets]
        generated_sources = self.get_target_generated_sources(target)
        for rel_src in generated_sources:
            if rel_src.endswith('.java'):
                deps.append(rel_src)

        rel_srcs = []
        plain_class_paths = []
        rel_objs = []
        for src in srcs:
            rel_src = src.rel_to_builddir(self.build_to_src)
            rel_srcs.append(rel_src)

            # Preserve any additional path components on top of the target's subdir
            plain_class_path = os.path.relpath(src.relative_name(), target.get_subdir())
            plain_class_path = plain_class_path[:-4] + 'class'
            plain_class_paths.append(plain_class_path)
            rel_obj = os.path.join(self.get_target_private_dir(target), plain_class_path)
            rel_objs.append(rel_obj)
        element = NinjaBuildElement(self.all_outputs, rel_objs, self.compiler_to_rule_name(compiler), rel_srcs)
        element.add_dep(deps)
        element.add_item('ARGS', args)
        element.add_item('FOR_JAR', self.get_target_filename(target))
        self.add_build(element)
        return plain_class_paths

    def generate_java_link(self) -> None:
        rule = 'java_LINKER'
        command = ['jar', '$ARGS']
        description = 'Creating JAR $out'
        self.add_rule(NinjaRule(rule, command, [], description))

    def determine_dep_vapis(self, target: build.BuildTarget) -> T.List[str]:
        """
        Peek into the sources of BuildTargets we're linking with, and if any of
        them was built with Vala, assume that it also generated a .vapi file of
        the same name as the BuildTarget and return the path to it relative to
        the build directory.
        """
        result: OrderedSet[str] = OrderedSet()
        for dep in itertools.chain(target.link_targets, target.link_whole_targets):
            if not (isinstance(dep, build.BuildTarget) and dep.is_linkable_target()):
                continue
            for i in dep.sources:
                if i.split('.')[-1] in compilers.lang_suffixes['vala']:
                    if dep.vala_vapi is not None:
                        vapiname = dep.vala_vapi
                        fullname = os.path.join(self.get_target_dir(dep), vapiname)
                        result.add(fullname)
                        break
        return list(result)

    def split_vala_sources(self, t: build.BuildTarget) -> \
            T.Tuple[T.MutableMapping[str, build.TargetSources], T.MutableMapping[str, build.TargetSources],
                    T.MutableMapping[str, File], T.MutableMapping[str, build.TargetSources]]:
        """
        Splits the target's sources into .vala, .gs, .vapi, and other sources.
        Handles both preexisting and generated sources.

        Returns a tuple (vala, vapi, others) each of which is a dictionary with
        the keys being the path to the file (relative to the build directory)
        and the value being the object that generated or represents the file.
        """
        vala: T.MutableMapping[str, build.TargetSources] = OrderedDict()
        vapi: T.MutableMapping[str, build.TargetSources] = OrderedDict()
        others: T.MutableMapping[str, File] = OrderedDict()
        othersgen: T.MutableMapping[str, build.TargetSources] = OrderedDict()
        # Split preexisting sources
        for s in t.get_sources():
            # BuildTarget sources are always mesonlib.File files which are
            # either in the source root, or generated with configure_file and
            # in the build root
            if not isinstance(s, File):
                raise InvalidArguments(f'All sources in target {t!r} must be of type mesonlib.File, not {s!r}')
            f = s.rel_to_builddir(self.build_to_src)
            if s.endswith(('.vala', '.gs')):
                vala[f] = s
            elif s.endswith('.vapi'):
                vapi[f] = s
            else:
                others[f] = s
        # Split generated sources
        for gensrc in t.get_generated_sources():
            for s in gensrc.get_outputs():
                f = self.get_target_generated_dir(t, gensrc, s)
                if s.endswith(('.vala', '.gs')):
                    gensrctype = vala
                elif s.endswith('.vapi'):
                    gensrctype = vapi
                # Generated non-Vala (C/C++) sources. Won't be used for
                # generating the Vala compile rule below.
                else:
                    gensrctype = othersgen
                # Duplicate outputs are disastrous
                if f in gensrctype and gensrctype[f] != gensrc:
                    msg = 'Duplicate output {0!r} from {1!r} {2!r}; ' \
                          'conflicts with {0!r} from {4!r} {3!r}' \
                          ''.format(f, type(gensrc).__name__, gensrc.name,
                                    gensrctype[f], type(gensrctype[f]).__name__)
                    raise InvalidArguments(msg)
                # Store 'somefile.vala': GeneratedList (or CustomTarget)
                gensrctype[f] = gensrc
        return vala, vapi, others, othersgen

    def generate_vala_compile(self, target: build.BuildTarget) -> \
            T.Tuple[T.MutableMapping[str, File], T.MutableMapping[str, build.TargetSources], T.List[str]]:
        """Vala is compiled into C. Set up all necessary build steps here."""
        (vala_src, vapi_src, others, othersgen) = self.split_vala_sources(target)
        extra_dep_files = []
        if not vala_src:
            raise InvalidArguments(f'Vala library {target.name!r} has no Vala or Genie source files.')

        valac = target.compilers['vala']
        c_out_dir = self.get_target_private_dir(target)
        # C files generated by valac
        vala_c_src: T.List[str] = []
        # Files generated by valac
        valac_outputs: T.List = []
        # All sources that are passed to valac on the commandline
        all_files = list(vapi_src)
        # Passed as --basedir
        srcbasedir = os.path.join(self.build_to_src, target.get_subdir())
        for (vala_file, gensrc) in vala_src.items():
            all_files.append(vala_file)
            # Figure out where the Vala compiler will write the compiled C file
            #
            # If the Vala file is in a subdir of the build dir (in our case
            # because it was generated/built by something else), and is also
            # a subdir of --basedir (because the builddir is in the source
            # tree, and the target subdir is the source root), the subdir
            # components from the source root till the private builddir will be
            # duplicated inside the private builddir. Otherwise, just the
            # basename will be used.
            #
            # If the Vala file is outside the build directory, the paths from
            # the --basedir till the subdir will be duplicated inside the
            # private builddir.
            if isinstance(gensrc, (build.CustomTarget, build.CustomTargetIndex, build.GeneratedList)) or gensrc.is_built:
                vala_c_file = os.path.splitext(os.path.basename(vala_file))[0] + '.c'
                # Check if the vala file is in a subdir of --basedir
                abs_srcbasedir = os.path.join(self.environment.get_source_dir(), target.get_subdir())
                abs_vala_file = os.path.join(self.environment.get_build_dir(), vala_file)
                if is_parent_path(abs_srcbasedir, abs_vala_file):
                    vala_c_subdir = PurePath(abs_vala_file).parent.relative_to(abs_srcbasedir)
                    vala_c_file = os.path.join(str(vala_c_subdir), vala_c_file)
            else:
                path_to_target = os.path.join(self.build_to_src, target.get_subdir())
                if vala_file.startswith(path_to_target):
                    vala_c_file = os.path.splitext(os.path.relpath(vala_file, path_to_target))[0] + '.c'
                else:
                    vala_c_file = os.path.splitext(os.path.basename(vala_file))[0] + '.c'
            # All this will be placed inside the c_out_dir
            vala_c_file = os.path.join(c_out_dir, vala_c_file)
            vala_c_src.append(vala_c_file)
            valac_outputs.append(vala_c_file)

        args = self.generate_basic_compiler_args(target, valac)
        b_colorout = self.get_target_option(target, 'b_colorout')
        assert isinstance(b_colorout, str)
        args += valac.get_colorout_args(b_colorout)
        # Tell Valac to output everything in our private directory. Sadly this
        # means it will also preserve the directory components of Vala sources
        # found inside the build tree (generated sources).
        args += ['--directory', c_out_dir]
        args += ['--basedir', srcbasedir]
        if target.is_linkable_target():
            assert isinstance(target, build.LinkableTarget)
            # Library name
            args += ['--library', target.name]
            # Outputted header
            if target.vala_header is not None:
                hname = os.path.join(self.get_target_dir(target), target.vala_header)
                args += ['--header', hname]
                if self.is_unity(target):
                    # Without this the declarations will get duplicated in the .c
                    # files and cause a build failure when all of them are
                    # #include-d in one .c file.
                    # https://github.com/mesonbuild/meson/issues/1969
                    args += ['--use-header']
                valac_outputs.append(hname)
            # Outputted vapi file
            if target.vala_vapi is not None:
                vapiname = os.path.join(self.get_target_dir(target), target.vala_vapi)
                # Force valac to write the vapi and gir files in the target build dir.
                # Without this, it will write it inside c_out_dir
                args += ['--vapi', os.path.join('..', target.vala_vapi)]
                valac_outputs.append(vapiname)
            # Generate GIR if requested
            if target.vala_gir is not None:
                girname = os.path.join(self.get_target_dir(target), target.vala_gir)
                args += ['--gir', os.path.join('..', target.vala_gir)]
                valac_outputs.append(girname)
                shared_target = target.get('shared')
                if isinstance(shared_target, build.SharedLibrary):
                    args += ['--shared-library', shared_target.get_filename()]
        # Detect gresources and add --gresources/--gresourcesdir arguments for each
        gres_dirs = []
        for gensrc in othersgen.values():
            if isinstance(gensrc, modules.GResourceTarget):
                gres_xml, = self.get_custom_target_sources(gensrc)
                args += ['--gresources=' + gres_xml]
                for source_dir in gensrc.source_dirs:
                    gres_dirs += [source_dir]
                # Ensure that resources are built before vala sources
                # This is required since vala code using [GtkTemplate] effectively depends on .ui files
                # GResourceHeaderTarget is not suitable due to lacking depfile
                gres_c, = gensrc.get_outputs()
                extra_dep_files += [os.path.join(self.get_target_dir(gensrc), gres_c)]
        for gres_dir in OrderedSet(gres_dirs):
            args += [f'--gresourcesdir={gres_dir}']
        dependency_vapis = self.determine_dep_vapis(target)
        extra_dep_files += dependency_vapis
        extra_dep_files.extend(self.get_target_depend_files(target))
        args += target.get_extra_args('vala')
        element = NinjaBuildElement(self.all_outputs, valac_outputs,
                                    self.compiler_to_rule_name(valac),
                                    all_files + dependency_vapis)
        element.add_item('ARGS', args)
        depfile = valac.depfile_for_object(os.path.join(self.get_target_dir(target), target.name))
        element.add_item('DEPFILE', depfile)
        element.add_dep(extra_dep_files)
        self.add_build(element)
        self.create_target_source_introspection(target, valac, args, all_files, [])
        return others, othersgen, vala_c_src

    def generate_cython_transpile(self, target: build.BuildTarget) -> \
            T.Tuple[T.MutableMapping[str, File], T.MutableMapping[str, build.TargetSources], T.List[str]]:
        """Generate rules for transpiling Cython files to C or C++"""

        static_sources: T.MutableMapping[str, File] = OrderedDict()
        generated_sources: T.MutableMapping[str, build.TargetSources] = OrderedDict()
        cython_sources: T.List[str] = []

        cython = target.compilers['cython']

        args: T.List[str] = []
        args += cython.get_always_args()
        debug = self.get_target_option(target, 'debug')
        assert isinstance(debug, bool)
        args += cython.get_debug_args(debug)
        optimization = self.get_target_option(target, 'optimization')
        assert isinstance(optimization, str)
        args += cython.get_optimization_args(optimization)
        args += cython.get_option_compile_args(target, target.subproject)
        args += cython.get_option_std_args(target, target.subproject)
        args += self.build.get_global_args(cython, target.for_machine)
        args += self.build.get_project_args(cython, target)
        args += target.get_extra_args('cython')

        ext = self.get_target_option(target, OptionKey('cython_language', machine=target.for_machine))

        pyx_sources = []  # Keep track of sources we're adding to build
        pyx_count = 0
        for src in target.get_sources():
            if src.endswith('.pyx'):
                pyx_count += 1
        for gen in target.get_generated_sources():
            for ssrc in gen.get_outputs():
                if ssrc.endswith('.pyx'):
                    pyx_count += 1

        for src in target.get_sources():
            if src.endswith('.pyx'):
                # Use basename to avoid too nested targets which can cause a
                # problem with MAX_PATH on Windows
                outname = os.path.basename(src.fname) if pyx_count == 1 else src.fname
                output = os.path.join(self.get_target_private_dir(target), f'{outname}.{ext}')
                element = NinjaBuildElement(
                    self.all_outputs, [output],
                    self.compiler_to_rule_name(cython),
                    [src.absolute_path(self.environment.get_source_dir(), self.environment.get_build_dir())])
                element.add_item('ARGS', args)
                self.add_build(element)
                # TODO: introspection?
                cython_sources.append(output)
                pyx_sources.append(element)
            else:
                static_sources[src.rel_to_builddir(self.build_to_src)] = src

        header_deps = []  # Keep track of generated headers for those sources
        for gen in target.get_generated_sources():
            if isinstance(gen, GeneratedList):
                builddir = self.get_target_private_dir(target)
            else:
                builddir = self.get_target_dir(gen)
            for ssrc in gen.get_outputs():
                ssrc = os.path.join(builddir, ssrc)
                if ssrc.endswith('.pyx'):
                    # Use basename to avoid too nested targets which can cause
                    # a problem with MAX_PATH on Windows
                    outname = os.path.basename(ssrc) if pyx_count == 1 else ssrc
                    output = os.path.join(self.get_target_private_dir(target), f'{outname}.{ext}')
                    element = NinjaBuildElement(
                        self.all_outputs, [output],
                        self.compiler_to_rule_name(cython),
                        [ssrc])
                    element.add_item('ARGS', args)
                    self.add_build(element)
                    pyx_sources.append(element)
                    # TODO: introspection?
                    cython_sources.append(output)
                else:
                    generated_sources[ssrc] = mesonlib.File.from_built_file(builddir, ssrc)
                    # Following logic in L883-900 where we determine whether to add generated source
                    # as a header(order-only) dep to the .so compilation rule
                    if not compilers.is_source(ssrc) and \
                            not compilers.is_object(ssrc) and \
                            not compilers.is_library(ssrc) and \
                            not modules.is_module_library(ssrc):
                        header_deps.append(ssrc)
        for source in pyx_sources:
            source.add_orderdep(header_deps)

        return static_sources, generated_sources, cython_sources

    def _generate_copy_target(self, src: FileOrString, output: Path) -> None:
        """Create a target to copy a source file from one location to another."""
        if isinstance(src, File):
            instr = src.absolute_path(self.environment.source_dir, self.environment.build_dir)
        else:
            instr = src
        elem = NinjaBuildElement(self.all_outputs, [str(output)], 'COPY_FILE', [instr])
        elem.add_orderdep(instr)
        self.add_build(elem)

    def __generate_sources_structure(self, root: Path, structured_sources: build.StructuredSources,
                                     main_file_ext: T.Union[str, T.Tuple[str, ...]] = tuple(),
                                     ) -> T.Tuple[T.List[str], T.Optional[str]]:
        first_file: T.Optional[str] = None
        orderdeps: T.List[str] = []
        for path, files in structured_sources.sources.items():
            for file in files:
                if isinstance(file, File):
                    out = root / path / Path(file.fname).name
                    self._generate_copy_target(file, out)
                    out_s = str(out)
                    orderdeps.append(out_s)
                    if first_file is None and out_s.endswith(main_file_ext):
                        first_file = out_s
                else:
                    for f in file.get_outputs():
                        out = root / path / f
                        out_s = str(out)
                        orderdeps.append(out_s)
                        self._generate_copy_target(str(Path(file.subdir) / f), out)
                        if first_file is None and out_s.endswith(main_file_ext):
                            first_file = out_s
        return orderdeps, first_file

    def _add_rust_project_entry(self, name: str, main_rust_file: str, args: CompilerArgs,
                                crate_type: str, target_name: str,
                                from_subproject: bool, proc_macro_dylib_path: T.Optional[str],
                                deps: T.List[RustDep]) -> None:
        raw_edition: T.Optional[str] = mesonlib.first(reversed(args), lambda x: x.startswith('--edition'))
        edition = '2015' if not raw_edition else raw_edition.split('=', 1)[-1]

        cfg: T.List[str] = []
        arg_itr: T.Iterator[str] = iter(args)
        for arg in arg_itr:
            if arg == '--cfg':
                cfg.append(next(arg_itr))
            elif arg.startswith('--cfg'):
                cfg.append(arg[len('--cfg'):])

        crate = RustCrate(
            len(self.rust_crates),
            self._get_rust_crate_name(name),
            main_rust_file,
            crate_type,
            target_name,
            T.cast('RUST_EDITIONS', edition),
            deps,
            cfg,
            is_workspace_member=not from_subproject,
            proc_macro_dylib_path=proc_macro_dylib_path,
        )

        self.rust_crates[name] = crate

    @staticmethod
    def _get_rust_crate_name(target_name: str) -> str:
        # Rustc replaces - with _. spaces or dots are not allowed, so we replace them with underscores
        # Also +SUFFIX is dropped, which can be used to distinguish host from build crates
        crate_name = target_name.replace('-', '_').replace(' ', '_').replace('.', '_')
        return crate_name.split('+', 1)[0]

    @staticmethod
    def _get_rust_dependency_name(target: build.BuildTarget, dependency: build.BuildTarget) -> str:
        crate_name_raw = target.rust_dependency_map.get(dependency.name, None)
        if crate_name_raw is None:
            dependency_crate_name = NinjaBackend._get_rust_crate_name(dependency.name)
            crate_name_raw = target.rust_dependency_map.get(dependency_crate_name, dependency.name)
        return NinjaBackend._get_rust_crate_name(crate_name_raw)

    def generate_rust_sources(self, target: build.BuildTarget) -> T.Tuple[T.List[str], str]:
        orderdeps: T.List[str] = []

        # Rust compiler takes only the main file as input and
        # figures out what other files are needed via import
        # statements and magic.
        main_rust_file: T.Optional[str] = None
        if target.structured_sources:
            if target.structured_sources.needs_copy():
                _ods, main_rust_file = self.__generate_sources_structure(Path(
                    self.get_target_private_dir(target)) / 'structured', target.structured_sources, '.rs')
                if main_rust_file is None:
                    raise MesonException('Could not find a rust file to treat as the main file for ', target.name)
            else:
                # The only way to get here is to have only files in the "root"
                # positional argument, which are all generated into the same
                # directory
                for g in target.structured_sources.sources['']:
                    if isinstance(g, File):
                        if g.endswith('.rs'):
                            main_rust_file = g.rel_to_builddir(self.build_to_src)
                    elif isinstance(g, GeneratedList):
                        for h in g.get_outputs():
                            if h.endswith('.rs'):
                                main_rust_file = os.path.join(self.get_target_private_dir(target), h)
                                break
                    else:
                        for h in g.get_outputs():
                            if h.endswith('.rs'):
                                main_rust_file = os.path.join(g.get_builddir(), h)
                                break
                    if main_rust_file is not None:
                        break

                _ods = []
                for f in target.structured_sources.as_list():
                    if isinstance(f, File):
                        _ods.append(f.rel_to_builddir(self.build_to_src))
                    else:
                        _ods.extend([os.path.join(self.build_to_src, f.subdir, s)
                                     for s in f.get_outputs()])
            self.all_structured_sources.update(_ods)
            orderdeps.extend(_ods)
            return orderdeps, main_rust_file

        for i in target.get_sources():
            if main_rust_file is None and i.endswith('.rs'):
                main_rust_file = i.rel_to_builddir(self.build_to_src)
        for g in target.get_generated_sources():
            for i in g.get_outputs():
                if isinstance(g, GeneratedList):
                    fname = os.path.join(self.get_target_private_dir(target), i)
                else:
                    fname = os.path.join(g.get_builddir(), i)
                if main_rust_file is None and fname.endswith('.rs'):
                    main_rust_file = fname
                orderdeps.append(fname)

        return orderdeps, main_rust_file

    def get_rust_compiler_args(self, target: build.BuildTarget, rustc: RustCompiler, src_crate_type: str,
                               depfile: T.Optional[str] = None) -> CompilerArgs:
        # Compiler args for compiling this target
        args = compilers.get_base_compile_args(target, rustc, self.environment)

        target_name = self.get_target_filename(target)
        args.extend(['--crate-type', src_crate_type])

        # If we're dynamically linking, add those arguments
        if target.rust_crate_type in {'bin', 'dylib', 'cdylib'}:
            args.extend(rustc.get_linker_always_args())
            args += compilers.get_base_link_args(target, rustc, self.environment)

        args += self.get_target_type_link_args(target, rustc)

        if target.rust_crate_type in {'bin', 'dylib', 'cdylib'}:
            args += rustc.get_build_link_args(target, self.build)
            args += rustc.get_target_link_args(target)

        cargs = args + self.generate_basic_compiler_args(target, rustc)
        cargs += ['--crate-name', self._get_rust_crate_name(target.name)]
        if depfile:
            cargs += rustc.get_dependency_gen_args(target_name, depfile)
        cargs += rustc.get_output_args(target_name)
        cargs += ['-C', 'metadata=' + target.get_id()]
        cargs += target.get_extra_args('rust')
        return cargs

    def get_rust_compiler_deps_and_args(self, target: build.BuildTarget, rustc: RustCompiler,
                                        obj_list: T.List[str]) -> T.Tuple[T.List[str], T.List[RustDep], T.List[str]]:
        deps: T.List[str] = []
        project_deps: T.List[RustDep] = []
        args: T.List[str] = []

        def _link_library(libname: str, static: bool, bundle: bool = False) -> None:
            orig_libname = libname
            type_ = 'static' if static else 'dylib'
            modifiers = []
            # Except with -Clink-arg, search is limited to the -L search paths
            dir_, libname = os.path.split(libname)
            linkdirs.add(dir_)
            if not bundle and static:
                modifiers.append('-bundle')
            if rustc.has_verbatim():
                modifiers.append('+verbatim')
            else:
                libname = rustc.lib_file_to_l_arg(libname)
                if libname is None:
                    raise MesonException(f"rustc does not implement '-l{type_}:+verbatim'; cannot link to '{orig_libname}' due to nonstandard name")

            if modifiers:
                type_ += ':' + ','.join(modifiers)
            args.append(f'-l{type_}={libname}')

        for o in obj_list:
            args.append(f'-Clink-arg={o}')
            deps.append(o)

        deps.extend([self.get_dependency_filename(t) for t in target.link_depends])

        linkdirs: OrderedSet[str] = OrderedSet()
        external_deps = target.external_deps.copy()
        target_deps = target.get_dependencies()
        for d in target_deps:
            # rlibs only store -l flags, not -L; help out rustc and always
            # add the -L flag, in case it's needed to find non-bundled
            # dependencies of an rlib.  At this point we don't have
            # information on whether this is a direct dependency (which
            # might use -Clink-arg= below) or an indirect one, so always
            # add to linkdirs.
            linkdirs.add(self.get_target_dir(d))
            deps.append(self.get_dependency_filename(d))
            if isinstance(d, build.StaticLibrary):
                external_deps.extend(d.external_deps)
            if d.uses_rust_abi():
                if d not in target.link_targets and d not in target.link_whole_targets:
                    # Indirect Rust ABI dependency, we only need its path in linkdirs.
                    continue
                assert isinstance(d, build.BuildTarget)
                # specify `extern CRATE_NAME=OUTPUT_FILE` for each Rust
                # dependency, so that collisions with libraries in rustc's
                # sysroot don't cause ambiguity
                d_name = self._get_rust_dependency_name(target, d)
                args += ['--extern', '{}={}'.format(d_name, self.get_target_filename(d))]
                project_deps.append(RustDep(d_name, self.rust_crates[d.name].order))
                continue

            # Link a C ABI library

            # Pass native libraries directly to the linker with "-C link-arg"
            # because rustc's "-l:+verbatim=" is not portable and we cannot rely
            # on linker to find the right library without using verbatim filename.
            # For example "-lfoo" won't find "foo.so" in the case name_prefix set
            # to "", or would always pick the shared library when both "libfoo.so"
            # and "libfoo.a" are available.
            # See https://doc.rust-lang.org/rustc/command-line-arguments.html#linking-modifiers-verbatim.
            #
            # However, rustc static linker (rlib and staticlib) requires using
            # "-l" argument and does not rely on platform specific dynamic linker.
            lib = self.get_target_filename_for_linking(d)
            link_whole = d in target.link_whole_targets
            if isinstance(target, build.StaticLibrary) or (isinstance(target, build.Executable) and rustc.get_crt_static()):
                static = isinstance(d, build.StaticLibrary)
                _link_library(lib, static, bundle=link_whole)
            elif link_whole:
                link_whole_args = rustc.linker.get_link_whole_for([lib])
                args += [f'-Clink-arg={a}' for a in link_whole_args]
            else:
                args.append(f'-Clink-arg={lib}')

        for e in external_deps:
            prev: T.Optional[str] = None
            for prev, a in lookbehind(e.get_link_args()):
                if prev == '-framework':
                    args.append(f'-lframework={a}')
                    continue
                elif a.startswith('-L'):
                    args.append(a)
                    continue
                elif a.startswith('-F'):
                    path = a[2:]
                    args.append(f'-Lframework={path}')
                    continue
                elif a == '-framework':
                    # handled once the framework name is available
                    continue
                elif is_library(a):
                    if isinstance(target, build.StaticLibrary):
                        static = a.endswith(('.a', '.lib'))
                        _link_library(a, static)
                        continue

                    dir_, _ = os.path.split(a)
                    linkdirs.add(dir_)

                args.append(f'-Clink-arg={a}')

        for d in linkdirs:
            d = d or '.'
            args.append(f'-L{d}')

        # Because of the way rustc links, this must come after any potential
        # library need to link with their stdlibs (C++ and Fortran, for example)
        args.extend(f'-Clink-arg={a}' for a in target.get_used_stdlib_args('rust'))

        has_shared_deps = any(isinstance(dep, build.SharedLibrary) for dep in target_deps)
        has_rust_shared_deps = any(isinstance(dep, build.SharedLibrary) and dep.uses_rust()
                                   and dep.rust_crate_type == 'dylib'
                                   for dep in target_deps)

        if target.rust_crate_type in {'dylib', 'proc-macro'}:
            # also add prefer-dynamic if any of the Rust libraries we link
            # against are dynamic or this is a dynamic library itself,
            # otherwise we'll end up with multiple implementations of libstd.
            has_rust_shared_deps = True
        elif self.get_target_option(target, 'rust_dynamic_std'):
            if target.rust_crate_type == 'staticlib':
                # staticlib crates always include a copy of the Rust libstd,
                # therefore it is not possible to also link it dynamically.
                # The options to avoid this (-Z staticlib-allow-rdylib-deps and
                # -Z staticlib-prefer-dynamic) are not yet stable; alternatively,
                # one could use "--emit obj" (implemented in the pull request at
                # https://github.com/mesonbuild/meson/pull/11213) or "--emit rlib"
                # (officially not recommended for linking with C programs).
                raise MesonException('rust_dynamic_std does not support staticlib crates yet')
            # want libstd as a shared dep
            has_rust_shared_deps = True

        # Add link args specific to this BuildTarget type that must not be overridden by dependencies
        args += self.get_target_type_link_args_post_dependencies(target, rustc)

        if has_rust_shared_deps:
            args += ['-C', 'prefer-dynamic']
        if has_shared_deps or has_rust_shared_deps:
            args += self.get_build_rpath_args(target, rustc)

        return deps, project_deps, args

    def generate_rust_target(self, target: build.BuildTarget, target_name: str, obj_list: T.List[str],
                             fortran_order_deps: T.List[File]) -> None:
        orderdeps, main_rust_file = self.generate_rust_sources(target)
        if main_rust_file is None:
            raise RuntimeError('A Rust target has no Rust sources. This is weird. Also a bug. Please report')

        rustc = T.cast('RustCompiler', target.compilers['rust'])
        args = rustc.compiler_args()

        depfile = os.path.join(self.get_target_private_dir(target), target.name + '.d')
        args += self.get_rust_compiler_args(target, rustc, target.rust_crate_type, depfile)

        deps, project_deps, deps_args = self.get_rust_compiler_deps_and_args(target, rustc, obj_list)
        args += deps_args

        proc_macro_dylib_path = None
        if target.rust_crate_type == 'proc-macro':
            proc_macro_dylib_path = self.get_target_filename_abs(target)

        self._add_rust_project_entry(target.name,
                                     os.path.abspath(os.path.join(self.environment.build_dir, main_rust_file)),
                                     args, target.rust_crate_type, target_name,
                                     bool(target.subproject),
                                     proc_macro_dylib_path,
                                     project_deps)

        compiler_name = self.compiler_to_rule_name(rustc)
        element = NinjaBuildElement(self.all_outputs, target_name, compiler_name, main_rust_file)
        if orderdeps:
            element.add_orderdep(orderdeps)
        element.add_orderdep(self.order_deps_to_strings(target, fortran_order_deps))
        if deps:
            # dependencies need to cause a relink, they're not just for ordering
            element.add_dep(deps)
        element.add_item('ARGS', args)
        element.add_item('targetdep', depfile)
        self.add_build(element)
        self.create_target_source_introspection(target, rustc, args, [main_rust_file], [])

        if target.doctests:
            assert target.doctests.target is not None
            rustdoc = rustc.get_rustdoc()
            args = rustdoc.get_exe_args() + \
                self.get_rust_compiler_args(target.doctests.target, rustdoc, target.rust_crate_type)
            o, _ = self.flatten_object_list(target.doctests.target)
            obj_list = unique_list(obj_list + o)
            # Rustc does not add files in the obj_list to Rust rlibs,
            # and is added by Meson to all of the dependencies, including here.
            _, _, deps_args = self.get_rust_compiler_deps_and_args(target.doctests.target, rustdoc, obj_list)
            args += deps_args
            target.doctests.cmd_args = args.to_native() + [main_rust_file] + target.doctests.cmd_args

    @staticmethod
    def get_rule_suffix(for_machine: MachineChoice) -> str:
        return PerMachine('_FOR_BUILD', '')[for_machine]

    @classmethod
    def get_compiler_rule_name(cls, lang: str, for_machine: MachineChoice, mode: str = 'COMPILER') -> str:
        return f'{lang}_{mode}{cls.get_rule_suffix(for_machine)}'

    @classmethod
    def compiler_to_rule_name(cls, compiler: Compiler) -> str:
        return cls.get_compiler_rule_name(compiler.get_language(), compiler.for_machine, compiler.mode)

    @classmethod
    def compiler_to_pch_rule_name(cls, compiler: Compiler) -> str:
        return cls.get_compiler_rule_name(compiler.get_language(), compiler.for_machine, 'PCH')

    def swift_module_file_name(self, target: build.BuildTarget) -> str:
        return os.path.join(self.get_target_private_dir(target),
                            target.swift_module_name + '.swiftmodule')

    def determine_swift_dep_modules(self, target: build.BuildTarget) -> T.List[str]:
        result = []
        for l in target.link_targets:
            if self.is_swift_target(l):
                assert isinstance(l, build.BuildTarget) # for mypy
                result.append(self.swift_module_file_name(l))
        return result

    def get_swift_link_deps(self, target: build.BuildTarget) -> T.List[str]:
        result = []
        for l in target.link_targets:
            result.append(self.get_target_filename(l))
        return result

    def split_swift_generated_sources(self, target: build.BuildTarget) -> T.List[str]:
        all_srcs = self.get_target_generated_sources(target)
        srcs: T.List[str] = []
        for i in all_srcs:
            if i.endswith('.swift'):
                srcs.append(i)
        return srcs

    def generate_swift_target(self, target: build.BuildTarget) -> None:
        module_name = target.swift_module_name
        swiftc = T.cast('SwiftCompiler', target.compilers['swift'])
        abssrc = []
        relsrc = []
        abs_headers = []
        header_imports = []

        if not target.uses_swift_cpp_interop():
            cpp_targets = [t for t in target.link_targets if t.uses_swift_cpp_interop()]
            if cpp_targets != []:
                target_word = 'targets' if len(cpp_targets) > 1 else 'target'
                first = ', '.join(repr(t.name) for t in cpp_targets[:-1])
                and_word = ' and ' if len(cpp_targets) > 1 else ''
                last = repr(cpp_targets[-1].name)
                enable_word = 'enable' if len(cpp_targets) > 1 else 'enables'
                raise MesonException('Swift target {0} links against {1} {2}{3}{4} which {5} C++ interoperability. '
                                     'This requires {0} to also have it enabled. '
                                     'Add "swift_interoperability_mode: \'cpp\'" to the definition of {0}.'
                                     .format(repr(target.name), target_word, first, and_word, last, enable_word))

        for i in target.get_sources():
            if swiftc.can_compile(i):
                rels = i.rel_to_builddir(self.build_to_src)
                abss = os.path.normpath(os.path.join(self.environment.get_build_dir(), rels))
                relsrc.append(rels)
                abssrc.append(abss)
            elif compilers.is_header(i):
                relh = i.rel_to_builddir(self.build_to_src)
                absh = os.path.normpath(os.path.join(self.environment.get_build_dir(), relh))
                abs_headers.append(absh)
                header_imports += swiftc.get_header_import_args(absh)
            else:
                raise InvalidArguments(f'Swift target {target.get_basename()} contains a non-swift source file.')
        os.makedirs(self.get_target_private_dir_abs(target), exist_ok=True)
        compile_args = self.generate_basic_compiler_args(target, swiftc)
        compile_args += swiftc.get_module_args(module_name)
        compile_args += swiftc.get_cxx_interoperability_args(target)
        compile_args += self.build.get_project_args(swiftc, target)
        compile_args += self.build.get_global_args(swiftc, target.for_machine)
        if isinstance(target, (build.StaticLibrary, build.SharedLibrary)):
            # swiftc treats modules with a single source file, and the main.swift file in multi-source file modules
            # as top-level code. This is undesirable in library targets since it emits a main function. Add the
            # -parse-as-library option as necessary to prevent emitting the main function while keeping files explicitly
            # named main.swift treated as the entrypoint of the module in case this is desired.
            if len(abssrc) == 1 and os.path.basename(abssrc[0]) != 'main.swift':
                compile_args += swiftc.get_library_args()
        for i in reversed(target.get_include_dirs()):
            for path in i.abs_string_list(self.source_dir, self.build_dir):
                compile_args.extend(swiftc.get_include_args(path, False))
        compile_args += target.get_extra_args('swift')
        link_args = swiftc.get_output_args(os.path.join(self.environment.get_build_dir(), self.get_target_filename(target)))
        link_args += self.build.get_project_link_args(swiftc, target)
        link_args += self.build.get_global_link_args(swiftc, target.for_machine)
        rundir = self.get_target_private_dir(target)
        out_module_name = self.swift_module_file_name(target)
        in_module_files = self.determine_swift_dep_modules(target)
        abs_module_dirs = self.determine_swift_dep_dirs(target)
        module_includes = []
        for x in abs_module_dirs:
            module_includes += swiftc.get_include_args(x, False)
        link_deps = self.get_swift_link_deps(target)
        abs_link_deps = [os.path.join(self.environment.get_build_dir(), x) for x in link_deps]
        for d in target.link_targets:
            reldir = self.get_target_dir(d)
            if reldir == '':
                reldir = '.'
            link_args += ['-L', os.path.normpath(os.path.join(self.environment.get_build_dir(), reldir))]
        rel_generated = self.split_swift_generated_sources(target)
        abs_generated = [os.path.join(self.environment.get_build_dir(), x) for x in rel_generated]
        # We need absolute paths because swiftc needs to be invoked in a subdir
        # and this is the easiest way about it.
        objects = [] # Relative to swift invocation dir
        rel_objects = [] # Relative to build.ninja
        for i in abssrc + abs_generated:
            base = os.path.basename(i)
            oname = os.path.splitext(base)[0] + '.o'
            objects.append(oname)
            rel_objects.append(os.path.join(self.get_target_private_dir(target), oname))

        rulename = self.compiler_to_rule_name(swiftc)

        # Swiftc does not seem to be able to emit objects and module files in one go.
        elem = NinjaBuildElement(self.all_outputs, rel_objects, rulename, abssrc)
        elem.add_dep(in_module_files + rel_generated)
        elem.add_dep(abs_headers)
        elem.add_item('ARGS', swiftc.get_compile_only_args() + compile_args + header_imports + abs_generated + module_includes)
        elem.add_item('RUNDIR', rundir)
        self.add_build(elem)

        # -g makes swiftc create a .o file with potentially the same name as one of the compile target generated ones.
        mod_gen_args = [el for el in compile_args if el != '-g']

        elem = NinjaBuildElement(self.all_outputs, out_module_name, rulename, abssrc)
        elem.add_dep(in_module_files + rel_generated)
        elem.add_item('ARGS', swiftc.get_mod_gen_args() + mod_gen_args + abs_generated + module_includes)
        elem.add_item('RUNDIR', rundir)
        self.add_build(elem)
        if isinstance(target, build.StaticLibrary):
            elem = self.generate_link(target, self.get_target_filename(target),
                                      rel_objects, self.build.static_linker[target.for_machine])
            self.add_build(elem)
        elif isinstance(target, build.Executable):
            elem = NinjaBuildElement(self.all_outputs, self.get_target_filename(target), rulename, [])
            elem.add_dep(rel_objects)
            elem.add_dep(link_deps)
            elem.add_item('ARGS', link_args + swiftc.get_std_exe_link_args() + objects + abs_link_deps)
            elem.add_item('RUNDIR', rundir)
            self.add_build(elem)
        else:
            raise MesonException('Swift supports only executable and static library targets.')
        # Introspection information
        self.create_target_source_introspection(target, swiftc, compile_args + header_imports + module_includes, relsrc, rel_generated)

    def _rsp_options(self, tool: T.Union['Compiler', 'StaticLinker']) -> NinjaRuleArgs:
        """Helper method to get rsp options.

        rsp_file_syntax() is only guaranteed to be implemented if
        can_linker_accept_rsp() returns True.
        """
        options: NinjaRuleArgs = {'rspable': tool.can_linker_accept_rsp()}
        if options['rspable']:
            options['rspfile_quote_style'] = tool.rsp_file_syntax()
        return options

    def generate_static_link_rules(self) -> None:
        num_pools = self.environment.coredata.optstore.get_value_for('backend_max_links')
        assert isinstance(num_pools, int)
        if 'java' in self.environment.coredata.compilers.host:
            self.generate_java_link()
        for for_machine in MachineChoice:
            static_linker = self.build.static_linker[for_machine]
            if static_linker is None:
                continue
            rule = 'STATIC_LINKER{}'.format(self.get_rule_suffix(for_machine))
            cmdlist: CommandArgs = []
            args = ['$in']
            # FIXME: Must normalize file names with pathlib.Path before writing
            #        them out to fix this properly on Windows. See:
            # https://github.com/mesonbuild/meson/issues/1517
            # https://github.com/mesonbuild/meson/issues/1526
            if isinstance(static_linker, ArLikeLinker) and not mesonlib.is_windows():
                # `ar` has no options to overwrite archives. It always appends,
                # which is never what we want. Delete an existing library first if
                # it exists. https://github.com/mesonbuild/meson/issues/1355
                cmdlist = execute_wrapper + [c.format('$out') for c in rmfile_prefix]
            cmdlist += static_linker.get_exelist()
            cmdlist += ['$LINK_ARGS']
            cmdlist += NinjaCommandArg.list(static_linker.get_output_args('$out'), Quoting.none)
            # The default ar on MacOS (at least through version 12), does not
            # add extern'd variables to the symbol table by default, and
            # requires that apple's ranlib be called with a special flag
            # instead after linking
            if static_linker.id == 'applear':
                # This is a bit of a hack, but we assume that that we won't need
                # an rspfile on MacOS, otherwise the arguments are passed to
                # ranlib, not to ar
                cmdlist.extend(args)
                args = []
                # Ensure that we use the user-specified ranlib if any, and
                # fallback to just picking up some ranlib otherwise
                ranlib = self.environment.lookup_binary_entry(for_machine, 'ranlib')
                if ranlib is None:
                    ranlib = ['ranlib']
                cmdlist.extend(['&&'] + ranlib + ['-c', '$out'])
            description = 'Linking static target $out'
            if num_pools > 0:
                pool = 'pool = link_pool'
            else:
                pool = None

            options = self._rsp_options(static_linker)
            self.add_rule(NinjaRule(rule, cmdlist, args, description, **options, extra=pool))

    def generate_dynamic_link_rules(self) -> None:
        num_pools = self.environment.coredata.optstore.get_value_for('backend_max_links')
        assert isinstance(num_pools, int)
        for for_machine in MachineChoice:
            complist = self.environment.coredata.compilers[for_machine]
            for langname, compiler in complist.items():
                if langname in {'java', 'vala', 'rust', 'cs', 'cython'}:
                    continue
                rule = '{}_LINKER{}'.format(langname, self.get_rule_suffix(for_machine))
                command = compiler.get_linker_exelist()
                args = ['$ARGS'] + NinjaCommandArg.list(compiler.get_linker_output_args('$out'), Quoting.none) + ['$in', '$LINK_ARGS']
                description = 'Linking target $out'
                if num_pools > 0:
                    pool = 'pool = link_pool'
                else:
                    pool = None

                options = self._rsp_options(compiler)
                self.add_rule(NinjaRule(rule, command, args, description, **options, extra=pool))
            if self.environment.machines[for_machine].is_aix() and complist:
                rule = 'AIX_LINKER{}'.format(self.get_rule_suffix(for_machine))
                description = 'Archiving AIX shared library'
                cmdlist = compiler.get_command_to_archive_shlib()
                args = []
                options = {}
                self.add_rule(NinjaRule(rule, cmdlist, args, description, **options, extra=None))
            if self.environment.machines[for_machine].is_os2() and complist:
                rule = 'IMPORTLIB{}'.format(self.get_rule_suffix(for_machine))
                description = 'Generating import library $out'
                command = ['emximp']
                args = ['-o', '$out', '$in']
                options = {}
                self.add_rule(NinjaRule(rule, command, args, description, **options, extra=None))

        args = self.environment.get_build_command() + \
            ['--internal',
             'symbolextractor',
             self.environment.get_build_dir(),
             '$in',
             '$IMPLIB',
             '$out']
        symrule = 'SHSYM'
        symcmd = args + ['$CROSS']
        syndesc = 'Generating symbol file $out'
        self.add_rule(NinjaRule(symrule, symcmd, [], syndesc, restat=True))

    def generate_java_compile_rule(self, compiler: Compiler) -> None:
        rule = self.compiler_to_rule_name(compiler)
        command = compiler.get_exelist() + ['$ARGS', '$in']
        description = 'Compiling Java sources for $FOR_JAR'
        self.add_rule(NinjaRule(rule, command, [], description))

    def generate_cs_compile_rule(self, compiler: 'CsCompiler') -> None:
        rule = self.compiler_to_rule_name(compiler)
        command = compiler.get_exelist()
        args = ['$ARGS', '$in']
        description = 'Compiling C Sharp target $out'
        self.add_rule(NinjaRule(rule, command, args, description,
                                rspable=mesonlib.is_windows(),
                                rspfile_quote_style=compiler.rsp_file_syntax()))

    def generate_vala_compile_rules(self, compiler: Compiler) -> None:
        rule = self.compiler_to_rule_name(compiler)
        command = compiler.get_exelist()
        description = 'Compiling Vala source $in'

        depargs = compiler.get_dependency_gen_args('$out', '$DEPFILE')
        depfile = '$DEPFILE' if depargs else None
        depstyle = 'gcc' if depargs else None

        args = depargs + ['$ARGS', '$in']

        self.add_rule(NinjaRule(rule, command + args, [], description,
                                depfile=depfile,
                                deps=depstyle,
                                restat=True))

    def generate_cython_compile_rules(self, compiler: 'Compiler') -> None:
        rule = self.compiler_to_rule_name(compiler)
        description = 'Compiling Cython source $in'
        command = compiler.get_exelist()

        depargs = compiler.get_dependency_gen_args('$out', '$DEPFILE')
        depfile = '$out.dep' if depargs else None

        args: CommandArgs = depargs + ['$ARGS', '$in']
        args += NinjaCommandArg.list(compiler.get_output_args('$out'), Quoting.none)
        self.add_rule(NinjaRule(rule, command + args, [],
                                description,
                                depfile=depfile,
                                restat=True))

    def generate_rust_compile_rules(self, compiler: RustCompiler) -> None:
        rule = self.compiler_to_rule_name(compiler)
        command = compiler.get_exelist() + ['$ARGS', '$in']
        description = 'Compiling Rust source $in'
        depfile = '$targetdep'
        depstyle = 'gcc'
        self.add_rule(NinjaRule(rule, command, [], description, deps=depstyle,
                                depfile=depfile))

    def generate_swift_compile_rules(self, compiler: SwiftCompiler) -> None:
        rule = self.compiler_to_rule_name(compiler)
        wd_args = compiler.get_working_directory_args('$RUNDIR')

        if wd_args is not None:
            invoc = compiler.get_exelist() + wd_args
        else:
            full_exe = self.environment.get_build_command() + [
                '--internal',
                'dirchanger',
                '$RUNDIR',
            ]
            invoc = full_exe + compiler.get_exelist()

        command = invoc + ['$ARGS', '$in']
        description = 'Compiling Swift source $in'
        self.add_rule(NinjaRule(rule, command, [], description, restat=True))

    def use_dyndeps_for_fortran(self) -> bool:
        '''Use the new Ninja feature for scanning dependencies during build,
        rather than up front. Remove this and all old scanning code once Ninja
        minimum version is bumped to 1.10.'''
        return self.ninja_has_dyndeps

    def get_fortran_order_deps(self, deps: T.List[build.BuildTarget]) -> T.List[File]:
        # We don't need this order dep if we're using dyndeps, as the
        # depscanner will handle this for us, which produces a better dependency
        # graph
        if self.use_dyndeps_for_fortran():
            return []

        return [File(True, *os.path.split(self.get_target_filename(t))) for t in deps
                if t.uses_fortran()]

    def generate_fortran_dep_hack(self, crstr: str) -> None:
        if self.use_dyndeps_for_fortran():
            return
        rule = f'FORTRAN_DEP_HACK{crstr}'
        if mesonlib.is_windows():
            cmd = ['cmd', '/C']
        else:
            cmd = ['true']
        self.add_rule_comment(NinjaComment('''Workaround for these issues:
https://groups.google.com/forum/#!topic/ninja-build/j-2RfBIOd_8
https://gcc.gnu.org/bugzilla/show_bug.cgi?id=47485'''))
        self.add_rule(NinjaRule(rule, cmd, [], 'Dep hack', restat=True))

    def generate_llvm_ir_compile_rule(self, compiler: Compiler) -> None:
        if self.created_llvm_ir_rule[compiler.for_machine]:
            return
        rule = self.get_compiler_rule_name('llvm_ir', compiler.for_machine)
        command = compiler.get_exelist()
        args = ['$ARGS'] + NinjaCommandArg.list(compiler.get_output_args('$out'), Quoting.none) + compiler.get_compile_only_args() + ['$in']
        description = 'Compiling LLVM IR object $in'

        options = self._rsp_options(compiler)

        self.add_rule(NinjaRule(rule, command, args, description, **options))
        self.created_llvm_ir_rule[compiler.for_machine] = True

    def generate_tasking_mil_compile_rules(self, compiler: Compiler) -> None:
        rule = self.get_compiler_rule_name('tasking_mil_compile', compiler.for_machine)
        depargs = NinjaCommandArg.list(compiler.get_dependency_gen_args('$out', '$DEPFILE'), Quoting.none)
        command = compiler.get_exelist()
        args = ['$ARGS'] + depargs + NinjaCommandArg.list(compiler.get_output_args('$out'), Quoting.none) + ['-cm', '$in']
        description = 'Compiling to C object $in'
        if compiler.get_depfile_format() == 'msvc':
            deps = 'msvc'
            depfile = None
        else:
            deps = 'gcc'
            depfile = '$DEPFILE'

        options = self._rsp_options(compiler)

        self.add_rule(NinjaRule(rule, command, args, description, **options, deps=deps, depfile=depfile))

    def generate_tasking_mil_link_rules(self, compiler: Compiler) -> None:
        rule = self.get_compiler_rule_name('tasking_mil_link', compiler.for_machine)
        command = compiler.get_exelist()
        args = ['$ARGS', '--mil-link'] + NinjaCommandArg.list(compiler.get_output_args('$out'), Quoting.none) + ['-c', '$in']
        description = 'MIL linking object $out'

        options = self._rsp_options(compiler)

        self.add_rule(NinjaRule(rule, command, args, description, **options))

    def generate_compile_rule_for(self, langname: str, compiler: Compiler) -> None:
        if langname == 'java':
            self.generate_java_compile_rule(compiler)
            return
        if langname == 'cs':
            if self.environment.machines.matches_build_machine(compiler.for_machine):
                self.generate_cs_compile_rule(T.cast('CsCompiler', compiler))
            return
        if langname == 'vala':
            self.generate_vala_compile_rules(compiler)
            return
        if langname == 'rust':
            self.generate_rust_compile_rules(T.cast('RustCompiler', compiler))
            return
        if langname == 'swift':
            if self.environment.machines.matches_build_machine(compiler.for_machine):
                self.generate_swift_compile_rules(T.cast('SwiftCompiler', compiler))
            return
        if langname == 'cython':
            self.generate_cython_compile_rules(compiler)
            return
        crstr = self.get_rule_suffix(compiler.for_machine)
        options = self._rsp_options(compiler)
        restat = False
        if langname == 'fortran':
            self.generate_fortran_dep_hack(crstr)
            # gfortran does not update the modification time of *.mod files, therefore restat is needed.
            # See also: https://github.com/ninja-build/ninja/pull/2275
            restat = True
        rule = self.compiler_to_rule_name(compiler)
        if langname == 'cuda':
            # for cuda, we manually escape target name ($out) as $CUDA_ESCAPED_TARGET because nvcc doesn't support `-MQ` flag
            depargs = NinjaCommandArg.list(compiler.get_dependency_gen_args('$CUDA_ESCAPED_TARGET', '$DEPFILE'), Quoting.none)
        else:
            depargs = NinjaCommandArg.list(compiler.get_dependency_gen_args('$out', '$DEPFILE'), Quoting.none)
        command = compiler.get_exelist()
        args = ['$ARGS'] + depargs + NinjaCommandArg.list(compiler.get_output_args('$out'), Quoting.none) + compiler.get_compile_only_args() + ['$in']
        description = f'Compiling {compiler.get_display_language()} object $out'
        if compiler.get_depfile_format() == 'msvc':
            deps = 'msvc'
            depfile = None
        else:
            deps = 'gcc'
            depfile = '$DEPFILE'
        self.add_rule(NinjaRule(rule, command, args, description, **options,
                                deps=deps, depfile=depfile, restat=restat))

    def generate_pch_rule_for(self, langname: str, compiler: Compiler) -> None:
        if langname not in {'c', 'cpp'}:
            return
        rule = self.compiler_to_pch_rule_name(compiler)
        depargs = compiler.get_dependency_gen_args('$out', '$DEPFILE')

        if compiler.get_argument_syntax() == 'msvc':
            output = []
        else:
            output = NinjaCommandArg.list(compiler.get_output_args('$out'), Quoting.none)

        if 'mwcc' in compiler.id:
            output[0].s = '-precompile'
            command = compiler.get_exelist() + ['$ARGS'] + depargs + output + ['$in'] # '-c' must be removed
        else:
            command = compiler.get_exelist() + ['$ARGS'] + depargs + output + compiler.get_compile_only_args() + ['$in']
        description = 'Precompiling header $in'
        if compiler.get_depfile_format() == 'msvc':
            deps = 'msvc'
            depfile = None
        else:
            deps = 'gcc'
            depfile = '$DEPFILE'
        self.add_rule(NinjaRule(rule, command, [], description, deps=deps,
                                depfile=depfile))

    def generate_scanner_rules(self) -> None:
        rulename = 'depscan'
        if self.ninja.has_rule(rulename):
            # Scanning command is the same for native and cross compilation.
            return

        command = self.environment.get_build_command() + \
            ['--internal', 'depscan']
        args = ['$picklefile', '$out', '$in']
        description = 'Scanning target $name for modules'
        rule = NinjaRule(rulename, command, args, description)
        self.add_rule(rule)

        rulename = 'depaccumulate'
        command = self.environment.get_build_command() + \
            ['--internal', 'depaccumulate']
        args = ['$out', '$in']
        description = 'Generating dynamic dependency information for target $name'
        rule = NinjaRule(rulename, command, args, description)
        self.add_rule(rule)

        # GCC/MSVC named modules use a P1689 scan (per source) plus a
        # dedicated collate rule that turns the per-source scans + dependency
        # provided-module maps into a dyndep and this target's own map.
        rulename = 'cpp_module_collate'
        if not self.ninja.has_rule(rulename):
            command = self.environment.get_build_command() + \
                ['--internal', 'depaccumulate', '--p1689']
            args = ['--dyndep', '$DYNDEP', '--provmap', '$PROVMAP',
                    '--bmi-dir', '$BMIDIR', '--bmi-suffix', '$BMISUFFIX',
                    '$DEPARGS', '$in']
            description = 'Collating C++ module dependencies for target $name'
            rule = NinjaRule(rulename, command, args, description)
            self.add_rule(rule)

    def generate_cpp_module_harvest_rule(self) -> None:
        # Publishes a source-keyed BMI into the shared cache under the
        # module's own name, read from the interface's .ddi at build time.
        # Ordering rides the stamp: consumers' dyndep inputs point at it, so
        # the cache path itself never needs to enter Ninja's graph. Registered
        # lazily (with the first harvest edge) so builds without a harvesting
        # pipeline see no new rule.
        rulename = 'cpp_module_harvest'
        if self.ninja.has_rule(rulename):
            return
        command = self.environment.get_build_command() + \
            ['--internal', 'depaccumulate', '--harvest']
        args = ['--pcm', '$PCM', '--ddi', '$DDI', '--bmi-dir', '$BMIDIR',
                '--bmi-suffix', '$BMISUFFIX', '--stamp', '$out']
        description = 'Publishing C++ module BMI of $in'
        rule = NinjaRule(rulename, command, args, description)
        self.add_rule(rule)

    def generate_bmi_variant_compile_rule(self, compiler: Compiler) -> None:
        # Compiles a module interface unit straight to a BMI, never an object
        # (clang --precompile, cl /ifcOnly, gcc -fmodule-only). The
        # interface-unit flags cover extension-less declared interfaces.
        # Registered lazily with the first variant.
        rulename = self.get_compiler_rule_name('cpp', compiler.for_machine, 'BMI_VARIANT')
        if self.ninja.has_rule(rulename):
            return
        command = compiler.get_exelist()
        description = 'Precompiling C++ module BMI $out'
        if compiler.get_id() == 'msvc':
            # The per-unit /interface or /internalPartition flag rides $ARGS
            # (added by the variant edge), so the shared rule can serve both an
            # interface and an internal-partition recompile.
            args = ['$ARGS'] + NinjaCommandArg.list(
                ['/showIncludes', '/ifcOnly',
                 '/ifcOutput', '$out', '/c', '$in'], Quoting.none)
            self.add_rule(NinjaRule(rulename, command, args, description, deps='msvc'))
            return
        depargs = NinjaCommandArg.list(compiler.get_dependency_gen_args('$out', '$DEPFILE'), Quoting.none)
        if compiler.get_id() == 'gcc':
            # GCC has no BMI output flag: the per-edge $MAPPER (written by
            # the variant's collate) maps the module name to $out, and
            # -fmodule-only skips the object. The pre-15 driver needs the
            # language spelled out for module extensions, as on compile
            # edges.
            lang = ['-x', 'c++'] if mesonlib.version_compare(compiler.version, '<15') else []
            args = ['$ARGS'] + depargs + NinjaCommandArg.list(
                ['-fmodule-mapper=$MAPPER', '-fmodule-only'] + lang + ['-c', '$in'], Quoting.none)
        else:
            args = ['$ARGS'] + depargs + ['-x', 'c++-module', '--precompile', '$in', '-o', '$out']
        self.add_rule(NinjaRule(rulename, command, args,
                                description, deps='gcc', depfile='$DEPFILE'))

    def generate_cpp_module_scan_rule(self, compiler: Compiler) -> None:
        rulename = self.get_compiler_rule_name('cpp', compiler.for_machine, 'MODULE_SCAN')
        if self.ninja.has_rule(rulename):
            return
        description = 'Scanning $in for C++ module dependencies'
        if compiler.get_id() == 'clang':
            # Clang's scanner is the separate clang-scan-deps tool, wrapping
            # the full compile command after '--'. It preprocesses with the
            # wrapped -MD/-MF, so the scan gets a make-style header depfile
            # exactly like GCC's; -c/-o inside the wrapper are inspected, not
            # executed (nothing is compiled). invoke-compiler makes the
            # scanner ask the wrapped compiler for its resource directory --
            # the default recipe guesses it from the compiler path, which
            # fails for a bare 'clang++' and loses the builtin headers
            # (float.h and friends) during the scan's preprocessing.
            command = compiler.get_module_scanner_exelist()
            args = NinjaCommandArg.list(['-format=p1689',
                                         '-resource-dir-recipe=invoke-compiler',
                                         '-o', '$out', '--'], Quoting.none) + \
                compiler.get_exelist() + \
                NinjaCommandArg.list(['$ARGS', '-c', '$in', '-o', '$OBJ',
                                      '-MD', '-MF', '$DEPFILE'], Quoting.none)
            rule = NinjaRule(rulename, command, args, description, deps='gcc', depfile='$DEPFILE')
            self.add_rule(rule)
            return
        command = compiler.get_exelist()
        # $ARGS carries exactly the target's compile flags (same dialect/BMI
        # flags as the compile edge); the scan scaffolding references the
        # per-source outputs. No -c: scanning must not compile.
        scanargs = NinjaCommandArg.list(
            compiler.get_module_scanner_args('$out', '$OBJ', '$DEPFILE'), Quoting.none)
        args = ['$ARGS'] + scanargs + ['$in']
        # GCC emits a make-style header depfile alongside the scan; cl does not,
        # so the scan there tracks only its source (generated-header existence is
        # covered by order-only deps in generate_cpp_module_scan).
        if compiler.get_id() == 'gcc':
            rule = NinjaRule(rulename, command, args, description, deps='gcc', depfile='$DEPFILE')
        else:
            rule = NinjaRule(rulename, command, args, description)
        self.add_rule(rule)

    def generate_cpp_header_unit_rule(self, compiler: Compiler) -> None:
        # Compiles a header unit's BMI, named by the import spelling
        # ($SPELLING) resolved on the include path from $ARGS.
        rulename = self.get_compiler_rule_name('cpp', compiler.for_machine, 'HEADER_UNIT')
        if self.ninja.has_rule(rulename):
            return
        command = compiler.get_exelist()
        description = 'Building C++ header unit $SPELLING'
        if compiler.get_id() == 'msvc':
            # cl writes the BMI to the path we pick ($out) and emits no object;
            # /headerName:$HUMODE is quote or angle. /showIncludes feeds the msvc
            # deps parser so editing the header rebuilds the unit.
            huargs = NinjaCommandArg.list(
                ['/exportHeader', '/headerName:$HUMODE', '/ifcOutput', '$out',
                 '/showIncludes', '$SPELLING'], Quoting.none)
            args = ['$ARGS'] + huargs
            self.add_rule(NinjaRule(rulename, command, args, description, deps='msvc'))
            return
        if compiler.get_id() == 'clang':
            # Clang names the BMI directly (-o $out) and emits no object, so
            # the edge's declared output is the real pcm -- no stamp, no touch.
            # -fmodule-header is the precompile action (no -c); $HULANG is
            # c++-user-header or c++-system-header and the spelling resolves on
            # the include path from $ARGS.
            depargs = NinjaCommandArg.list(compiler.get_dependency_gen_args('$out', '$DEPFILE'), Quoting.none)
            args = ['$ARGS'] + depargs + ['-fmodule-header', '-x', '$HULANG',
                                          '$SPELLING', '-o', '$out']
            self.add_rule(NinjaRule(rulename, command, args, description, deps='gcc', depfile='$DEPFILE'))
            return
        # GCC takes a header unit's CMI path from its mapper, so $HUMAPPER -- one
        # setup-written line naming the resolved header and this edge's $out --
        # is what lets the edge declare the real .gcm. It is empty for a unit no
        # mapper key can name (whitespace in the path), which falls back to
        # default naming; $out is that same path, so the output appears either
        # way. -fmodule-only skips the object; $HULANG is c++-{user,system}-header.
        modargs = NinjaCommandArg.list(compiler.get_module_compile_args(), Quoting.none)
        depargs = NinjaCommandArg.list(compiler.get_dependency_gen_args('$out', '$DEPFILE'), Quoting.none)
        args = ['$ARGS'] + modargs + ['$HUMAPPER'] + depargs + \
            ['-fmodule-only', '-x', '$HULANG', '-c', '$SPELLING']
        self.add_rule(NinjaRule(rulename, command, args, description, deps='gcc', depfile='$DEPFILE'))

    @staticmethod
    def _parse_header_unit(hu: T.Union[File, str], build_to_src: str) -> T.Tuple[str, str]:
        # A declared header unit is a File (user header, spelled build-relative),
        # a '<pkg/hdr.h>' string (system header), or a plain string (user
        # header). Returns (mode, spelling); mode is 'user' or 'system'.
        if isinstance(hu, File):
            return 'user', hu.rel_to_builddir(build_to_src)
        if hu.startswith('<') and hu.endswith('>'):
            return 'system', hu[1:-1]
        return 'user', hu

    def _module_kwarg_paths(self, target: build.BuildTarget,
                            entries: T.Sequence[T.Union[str, File, 'build.CustomTarget', 'build.CustomTargetIndex', 'build.GeneratedList']]) -> T.Set[str]:
        # Normalized build-root-relative paths of the sources a module kwarg
        # declares. This is the form rel_src and the P1689 scan's source-path
        # take, so the per-source interface flags, the Clang harvest edge and
        # the collator all decide interface-ness from one comparable key and
        # cannot drift. A generated entry keys on its build-tree output path
        # (the same key get_target_generated_dir gives the compile loop);
        # object forms declare every C++ output, a string names one; a source
        # a generated output shadows is disambiguated at setup, so both the
        # static and generated keys may safely be added for a string.
        paths: T.Set[str] = set()
        gen_sources = target.get_generated_sources()

        def add_generated(gensrc: 'build.GeneratedTypes', output: str) -> None:
            # get_target_generated_dir resolves a CustomTargetIndex to its parent
            # target's dir, the same build-tree path the compile loop computes.
            paths.add(os.path.normpath(self.get_target_generated_dir(target, gensrc, output)))

        for entry in entries:
            if isinstance(entry, (build.CustomTarget, build.CustomTargetIndex, build.GeneratedList)):
                for output in entry.get_outputs():
                    add_generated(entry, output)
                continue
            if isinstance(entry, File):
                paths.add(os.path.normpath(entry.rel_to_builddir(self.build_to_src)))
                continue
            # A string names either a static source or a generated output, never
            # both (setup rejected the ambiguous both-match). Prefer the
            # generated output when one exists -- a generated name is not on
            # disk in the source tree, so from_source_file would reject it.
            gen_hits = [g for g in gen_sources if entry in g.get_outputs()]
            if gen_hits:
                for gensrc in gen_hits:
                    add_generated(gensrc, entry)
            else:
                f = File.from_source_file(self.source_dir, target.get_subdir(), entry)
                paths.add(os.path.normpath(f.rel_to_builddir(self.build_to_src)))
        return paths

    def _module_interface_paths(self, target: build.BuildTarget) -> T.Set[str]:
        return self._module_kwarg_paths(target, target.cpp_module_interfaces)

    def _is_declared_module_interface(self, target: build.BuildTarget, src: 'FileOrString') -> bool:
        # Whether src is one of target's declared cpp_module_interfaces sources.
        if not target.cpp_module_interfaces:
            return False
        key = src.rel_to_builddir(self.build_to_src) if isinstance(src, File) else src
        return os.path.normpath(key) in self._module_interface_paths(target)

    def _internal_partition_paths(self, target: build.BuildTarget) -> T.Set[str]:
        # Normalized build-root-relative paths of the sources this target
        # declares as internal (implementation) partitions (cpp_internal_partitions),
        # the same comparable key as _module_interface_paths. An internal
        # partition is a BMI-producing module unit like an interface, but MSVC
        # compiles it with /internalPartition instead of /interface (and rejects
        # an interface file extension for it) -- so a generated internal
        # partition, which cannot be recognised by extension, has this kwarg as
        # its only channel on MSVC.
        return self._module_kwarg_paths(target, target.cpp_internal_partitions)

    def _is_declared_internal_partition(self, target: build.BuildTarget, src: 'FileOrString') -> bool:
        # Whether src is one of target's declared cpp_internal_partitions sources.
        if not target.cpp_internal_partitions:
            return False
        key = src.rel_to_builddir(self.build_to_src) if isinstance(src, File) else src
        return os.path.normpath(key) in self._internal_partition_paths(target)

    def _module_unit_iface_args(self, cpp: Compiler, is_internal_partition: bool) -> T.List[str]:
        # Per-unit flag telling cl/clang this TU is a module unit, mirroring the
        # compile-edge split in generate_single_compile. cl needs
        # /internalPartition for an internal partition (not /interface); clang's
        # -x c++-module covers both kinds; GCC infers the kind from the source
        # but its pre-15 driver does not know the module extensions and needs
        # the language spelled out.
        if cpp.get_id() == 'msvc':
            return ['/internalPartition', '/TP'] if is_internal_partition else ['/interface', '/TP']
        if cpp.get_id() == 'clang':
            return ['-x', 'c++-module']
        return ['-x', 'c++'] if mesonlib.version_compare(cpp.version, '<15') else []

    def provision_header_units(self, target: build.BuildTarget, compiler: Compiler) -> T.List[str]:
        """Emit build edges for this target's declared header units and return
        their outputs.

        Each entry is an *import spelling*: 'pkg/hdr.h' is a user header,
        '<pkg/hdr.h>' is a system header. It is resolved on the target's include
        path, so the BMI matches what a consumer's `import "pkg/hdr.h";` /
        `import <pkg/hdr.h>;` looks up. The returned outputs are added as
        order-only deps to the target's scans and implicit deps to its compiles
        so the scanner is never cold (GCC) and BMIs exist at compile.

        Every compiler writes the BMI to the path we pick. cl and clang make
        consumers name it, and those per-target /headerUnit (-fmodule-file) flags
        are recorded for the compile site -- and, on clang, for the scan site too.
        GCC resolves it through the module mapper instead, so no unit BMI path
        ever reaches a GCC command line.

        Edges are deduped by (mode, resolved header identity) plus, for compilers
        with supports_bmi_classes(), the declarer's BMI class: each class builds
        its own BMI (a unit is interface-only, so per-class copies are purely
        additive) and each consumer resolves its own class's. Two targets that
        spell one unit alike but resolve it to different files keep distinct
        BMIs. On a single-class machine the key carries no class part, keeping
        paths and edges byte-identical to a pre-class build.
        """
        cid = compiler.get_id()
        if cid not in ('gcc', 'msvc', 'clang'):
            return []
        tid = target.get_id()
        if tid in self._target_header_unit_outputs:
            return self._target_header_unit_outputs[tid]
        outputs: T.List[str] = []
        consumer_args: T.List[str] = []
        bmis: T.List[T.Tuple[str, str]] = []
        if target.cpp_header_units or self._imported_header_units(target, compiler):
            # Build each unit with the target's full compile args (base +
            # per-target), the same a consumer sees, so the BMI freezes the same
            # preprocessor state -- a unit built under different macros (e.g. cl
            # /MDd's _DEBUG) than the importer is IFNDR.
            args = self._generate_single_compile(target, compiler)
            class_key = compiler.get_bmi_class_key(args)
            class_subdir = self._header_unit_class_subdir_for(target.for_machine, compiler, class_key)
            if cid == 'gcc':
                # The only place a unit GCC cannot name is reported. The edges
                # below just skip such a unit; what it means is decided here.
                self.check_header_unit_names(target, args)
                # Costs a probe only on a target that forces includes at all.
                self.warn_on_preincluded_header_units(target, args)
            outputs, consumer_args, bmis = self._provision_class_header_units(
                compiler, target, args, class_key, class_subdir, target.name, target)
        self._target_header_unit_outputs[tid] = outputs
        self._target_header_unit_consumer_args[tid] = consumer_args
        self._target_header_unit_bmis[tid] = bmis
        return outputs

    def _provision_class_header_units(self, compiler: Compiler, target: build.BuildTarget,
                                      args: T.Union['CompilerArgs', T.List[str]],
                                      class_key: T.Tuple[str, ...],
                                      class_subdir: T.Optional[str],
                                      declarer: str,
                                      warn_target: T.Optional[build.BuildTarget]
                                      ) -> T.Tuple[T.List[str], T.List[str], T.List[T.Tuple[str, str]]]:
        """Every header unit `target`'s TUs must be able to name, in one class:
        the units it declares itself, plus the ones it inherits by importing
        another target's module (see _imported_header_units, empty off cl).

        An inherited unit is built in `target`'s class -- that is the BMI its TUs
        must read -- but from the *provider's* BMI-irrelevant flags, because the
        spelling is the provider's and only the provider's include path resolves
        it. That is the same composition a BMI-only variant uses for the
        interface itself (_get_or_create_bmi_variant), and it means a consumer
        and the provider's variant compute one key and emit one edge, whichever
        of them the backend generates first. The provider is the owner too, so a
        unit exporting a header that target generates still orders behind its
        generator. The (GCC name, BMI) pairs stay the target's own: they feed
        GCC's mapper, and GCC inherits nothing here.
        """
        seen: T.List[T.List[str]] = []
        outputs, consumer_args, bmis = self._provision_header_unit_edges(
            compiler, target.cpp_header_units, args, class_key, class_subdir,
            declarer, warn_target, target, seen)
        relevant, _ = compiler.split_bmi_args(args)
        for provider, units in self._imported_header_units(target, compiler):
            _, irrelevant = compiler.split_bmi_args(
                self._generate_single_compile(provider, compiler))
            prov_outputs, prov_args, _ = self._provision_header_unit_edges(
                compiler, units, irrelevant + relevant, class_key, class_subdir,
                provider.name, None, provider, seen)
            outputs += [o for o in prov_outputs if o not in outputs]
            consumer_args += prov_args
        return outputs, consumer_args, bmis

    def _imported_header_units(self, target: build.BuildTarget, compiler: Compiler
                               ) -> T.List[T.Tuple[build.BuildTarget, T.List[T.Union[File, str]]]]:
        """The header units declared by the module providers this target links,
        grouped by provider and minus anything the target declares itself.

        cl names a header unit only through a /headerUnit mapping on the command
        line, and an imported module's BMI does not carry the mappings its own
        interface was built with. So a TU importing a module whose interface
        imports a header unit has to be handed that unit's BMI as well, or cl
        fails the import outright (C7612, "could not find header unit"). GCC
        reaches the unit at its default-named CMI path and Clang records the
        pcm's own path inside the importing pcm, so neither needs this -- and on
        Clang it would be actively harmful, since a unit named for a target is
        loaded into every one of its TUs, which is what makes declaring a std
        header unit alongside `import std;` unusable there.
        """
        if compiler.get_id() != 'msvc':
            return []
        tid = target.get_id()
        cached = self._target_imported_header_units.get(tid)
        if cached is not None:
            return cached
        seen = {self._header_unit_spelling(hu, compiler) for hu in target.cpp_header_units}
        groups: T.List[T.Tuple[build.BuildTarget, T.List[T.Union[File, str]]]] = []
        for t in sorted(target.get_all_linked_targets(), key=lambda t: t.get_id()):
            if not isinstance(t, build.BuildTarget) or not t.cpp_header_units \
                    or not t.provides_cpp_modules() \
                    or not self.target_uses_p1689_cpp_modules(t):
                continue
            units: T.List[T.Union[File, str]] = []
            for hu in t.cpp_header_units:
                key = self._header_unit_spelling(hu, compiler)
                if key not in seen:
                    seen.add(key)
                    units.append(hu)
            if units:
                groups.append((t, units))
        self._target_imported_header_units[tid] = groups
        return groups

    def _target_generated_output_paths(self, target: build.BuildTarget) -> T.List[str]:
        """Build-relative paths of every file this target's generators
        (custom_target, generator) will write. A header unit exporting one of
        them must order behind the edge that produces it, exactly as the
        target's own compiles and scans already do.
        """
        tid = target.get_id()
        cached = self._target_generated_outputs.get(tid)
        if cached is not None:
            return cached
        outs: T.List[str] = []
        for gensrc in target.get_generated_sources():
            for s in gensrc.get_outputs():
                outs.append(self.get_target_generated_dir(target, gensrc, s))
        self._target_generated_outputs[tid] = outs
        return outs

    def _header_unit_generated_deps(self, owner: T.Optional[build.BuildTarget],
                                    spelling: str) -> T.List[str]:
        """The owner's generated outputs that a header unit's spelling names.

        A HEADER_UNIT edge reads the header off disk at run time but names no
        input, so a build-time-generated header must be wired in as an
        order-only dep or the edge can run before its generator does. Only cl
        and clang reach here for such a unit: GCC cannot name a header that
        resolves nowhere at setup and drops it before any edge is emitted.
        """
        if owner is None:
            return []
        want = os.path.normpath(spelling).replace('\\', '/')
        deps: T.List[str] = []
        for out in self._target_generated_output_paths(owner):
            norm = os.path.normpath(out).replace('\\', '/')
            if norm == want or norm.endswith('/' + want):
                deps.append(out)
        return deps

    def _provision_header_unit_edges(self, compiler: Compiler,
                                     header_units: T.List[T.Union[File, str]],
                                     args: T.Union['CompilerArgs', T.List[str]],
                                     class_key: T.Tuple[str, ...],
                                     class_subdir: T.Optional[str],
                                     declarer: str,
                                     warn_target: T.Optional[build.BuildTarget],
                                     owner: T.Optional[build.BuildTarget] = None,
                                     seen_consumer_args: T.Optional[T.List[T.List[str]]] = None
                                     ) -> T.Tuple[T.List[str], T.List[str], T.List[T.Tuple[str, str]]]:
        """The edge-emitting half of provision_header_units, also used by
        BMI-only module variants to build their class's units (with no
        warn_target: reuse there is same-class by construction). `owner` is the
        target whose generators may supply a declared header, used to order a
        unit edge behind a build-time-generated header. `seen_consumer_args` lets
        a caller that provisions one class's units in several calls -- a target's
        own plus each imported provider's -- share the flag dedup across them.
        Returns (unit outputs, per-unit consumer args, per-unit (GCC name, BMI)
        pairs).
        """
        if not header_units:
            return [], [], []
        cid = compiler.get_id()
        if cid == 'clang':
            # Registered lazily (idempotent), like clang's scan rule: only
            # projects declaring header units grow the rule.
            self.generate_cpp_header_unit_rule(compiler)
        rulename = self.get_compiler_rule_name('cpp', compiler.for_machine, 'HEADER_UNIT')
        is_msvc = cid == 'msvc'
        is_gcc = cid == 'gcc'
        suffix = compiler.get_module_bmi_suffix()
        cache_dir = compiler.get_module_cache_dir()
        outputs: T.List[str] = []
        consumer_args: T.List[str] = []
        if seen_consumer_args is None:
            seen_consumer_args = []
        bmis: T.List[T.Tuple[str, str]] = []
        # The chain aliases the declarer's scan edges will carry, mirrored here
        # so the scan names below are computed under the same include path the
        # scan actually resolves through. Per declarer, not per unit: one scan
        # resolves every system header through the aliased chain or none.
        chain_args: T.List[str] = []
        if is_gcc and class_subdir is not None \
                and self._header_unit_aliasing_available() \
                and self._declares_system_header_unit(header_units):
            chain_args = self._gcc_system_chain_isystem_args(compiler, args, class_subdir)
        for hu in header_units:
            mode, spelling = self._header_unit_spelling(hu, compiler)
            # One BMI per file and, when classes are in play, per class; the
            # canonical spelling of the group builds it and the others are extra
            # names for it (see the provision_header_units docstring). Within a
            # key the first edge's args build the unit for every consumer, so the
            # key must carry the file, not just its spelling: two units spelled
            # alike but resolving to different files are different units. The
            # identity is the resolved real path, or the spelling itself for a
            # unit with none -- a system unit, or a user one off the -I path.
            ident = self._header_unit_identity(args, mode, spelling)
            canon = self._canonical_header_unit(args, mode, spelling, ident)
            base = f'{mode}:{ident if ident is not None else canon}'
            if is_gcc and mode == 'user' and class_subdir is not None \
                    and self._header_unit_aliasing_available():
                # The supported per-class path: every class aliases symmetrically,
                # with no designated owner class. The scan resolves the unit
                # under the class root (its name embeds the class, hence a
                # class-specific default CMI path a mapper-less scan can read);
                # the compile keeps its own spelling and a per-unit mapper writes
                # the CMI to that scan-named path. On a non-spaced tree the two
                # spellings differ and the CMI records the real path; on a spaced
                # tree both are the space-free alias and coincide.
                scan_args = self._reclass_include_args(list(args), class_subdir) + chain_args
                scan_name = self._header_unit_gcc_name(compiler, scan_args, mode, canon)
                compile_name = self._header_unit_gcc_name(compiler, args, mode, canon)
                if scan_name is None or compile_name is None:
                    continue
                key = f'{base}:{class_subdir}'
                output = self._header_units.get(key)
                if output is None:
                    output = default_cmi_path(scan_name, cache_dir, suffix)
                    mapper = self._class_header_unit_path(key, canon, '.mapper')
                    self._write_header_unit_mapper(mapper, compile_name, output)
                    self._add_header_unit_edge(output, rulename, args, canon, mode,
                                               mapper, owner, is_msvc, is_gcc)
                    self._header_units[key] = output
                outputs.append(output)
                # The pair carries the compile-computed name; the collate joins
                # every name of this BMI into each importer's mapper, so a scan
                # reporting the alias name and a compile asking the real one both
                # resolve. A pair whose name is the BMI's own stem is redundant
                # with --default-cmi-root reconstruction and suppressed there.
                bmis.append((compile_name, output))
                if spelling != canon:
                    # An extra declared spelling of the same file: one BMI, an
                    # extra name for it. A scan through the alias reaches the unit
                    # only at that spelling's own default path, so link the BMI
                    # there; the compile resolves it through the mapper.
                    alias_scan = self._header_unit_gcc_name(compiler, scan_args, mode, spelling)
                    alias_compile = self._header_unit_gcc_name(compiler, args, mode, spelling)
                    if alias_scan is not None:
                        link = self._alias_header_unit_link(alias_scan, output, cache_dir, suffix)
                        if link not in outputs:
                            outputs.append(link)
                        # The compile resolves the alias spelling to the one BMI
                        # through the mapper; the link only serves the mapper-less
                        # scan. So the pair names that BMI, not the link.
                        if alias_compile is not None:
                            bmis.append((alias_compile, output))
                continue
            if is_gcc and mode == 'system' and chain_args:
                # The per-class path for a system unit on a multi-class machine.
                # Its name is the built-in chain path the compiler resolves it
                # to, which no -I of ours renames -- so the scan aliases the
                # whole chain and reads the unit under this class's roots, while
                # the compile keeps the real chain (so __FILE__, diagnostics and
                # debug info stay real). The scan name is build-relative through
                # the alias root, so the CMI path it mangles to is colon-free
                # even on Windows; default_cmi_path keeps the drive-letter mangling
                # for the degraded path alone.
                scan_args = self._reclass_include_args(list(args), class_subdir) + chain_args
                scan_name = self._header_unit_gcc_name(compiler, scan_args, mode, canon)
                compile_name = self._header_unit_gcc_name(compiler, args, mode, canon)
                if scan_name is not None and compile_name is not None \
                        and scan_name != compile_name:
                    key = f'{base}:{class_subdir}'
                    output = self._header_units.get(key)
                    if output is None:
                        output = default_cmi_path(scan_name, cache_dir, suffix)
                        mapper = self._class_header_unit_path(key, canon, '.mapper')
                        self._write_header_unit_mapper(mapper, compile_name, output)
                        self._add_header_unit_edge(output, rulename, args, canon, mode,
                                                   mapper, owner, is_msvc, is_gcc)
                        self._header_units[key] = output
                    outputs.append(output)
                    # The pair carries the compile-computed (real chain) name;
                    # the collate joins every name of this BMI into each
                    # importer's mapper, so a scan reporting the alias name and
                    # a compile asking the real one both resolve.
                    bmis.append((compile_name, output))
                    continue
                # Identical names mean the alias did not enter resolution at all
                # (the scan reported the real name for this unit): the flagged
                # chain args defeat the compiler's shorter-of-two canonicalization,
                # so a placed alias survives, but a unit resolving through neither
                # the aliased chain nor a reclassed -I still lands on its real
                # path. Fall through to the shared flat naming that name reaches --
                # the same degradation, per unit, that a link-less tree is
                # machine-wide.
            key = base if class_subdir is None else f'{base}:{class_subdir}'
            name: T.Optional[str] = None
            mappable = flat = False
            if is_gcc:
                name = self._header_unit_gcc_name(compiler, args, mode, canon)
                if name is None:
                    # A header that resolves nowhere has no name, and GCC derives
                    # both halves of a unit's identity from the name: the mapper
                    # key its importers look it up by, and the default CMI path it
                    # writes the BMI to. An edge for it could only declare an
                    # output GCC will not write -- with no name there is no mapper
                    # to send it elsewhere, so it falls back to that default path --
                    # leaving the output permanently missing and the edge, plus
                    # every scan and compile ordered on it, dirty on every ninja
                    # run. So the unit is not built at all, and nothing waits on a
                    # BMI that will not appear. check_header_unit_names has already
                    # said so at setup (fatally, for a user unit).
                    continue
                owner_key, _, _ = self._header_unit_class.setdefault(
                    base, (class_key, declarer, compiler.for_machine))
                if warn_target is not None:
                    self.warn_on_header_unit_divergence(warn_target, class_key, base, canon)
                # A mapper key holds no whitespace, so a unit under a spaced path
                # keeps GCC's default naming and takes no mapper of its own
                # (check_header_unit_names reports it).
                mappable = not self._has_space(name)
                # Scan edges carry no mapper, so a scan of any class reaches a
                # unit only at its default-named path. The class that declared it
                # first therefore builds its BMI there -- not a copy of one -- and
                # only the other classes get a class-keyed path. Those classes
                # scan against the owner's macros, which can misdirect a scan only
                # if the unit computes a macro from flag-dependent state and that
                # macro gates an import; their compiles resolve their own BMI
                # regardless.
                flat = not mappable or class_key == owner_key
                if flat:
                    key = base
            output = self._header_units.get(key)
            if output is None:
                mapper: T.Optional[str] = None
                if is_gcc and flat:
                    assert name is not None, 'a unit GCC cannot name is skipped above'
                    output = default_cmi_path(name, cache_dir, suffix)
                else:
                    output = self._class_header_unit_path(key, canon, suffix)
                if mappable:
                    # Under meson-private even when the BMI is not: written at
                    # setup and never rebuilt, so it must not sit in a cache
                    # directory the compiler owns and a user may clear.
                    mapper = self._class_header_unit_path(key, canon, '.mapper')
                    self._write_header_unit_mapper(mapper, name, output)
                self._add_header_unit_edge(output, rulename, args, canon, mode,
                                           mapper, owner, is_msvc, is_gcc)
                self._header_units[key] = output
            outputs.append(output)
            if is_gcc:
                # This target's scans read the unit at the default-named path
                # whatever class it is in, so order that BMI before them too.
                flat_output = self._header_units[base]
                if flat_output not in outputs:
                    outputs.append(flat_output)
                # Every declared spelling of the group names the class's one BMI.
                alias = spelling != canon
                sname = self._header_unit_gcc_name(compiler, args, mode, spelling) \
                    if alias else name
                if sname is not None:
                    bmis.append((sname, output))
                if alias and mappable and sname is not None and not self._has_space(sname):
                    link = self._alias_header_unit_link(sname, flat_output, cache_dir, suffix)
                    if link not in outputs:
                        outputs.append(link)
            # Returns [] on GCC: its consumers resolve through the mapper. Whole
            # lists, not flags, are deduped: clang names only the BMI, so two
            # spellings of one unit repeat a flag it must see once, while cl
            # names the spelling too and needs a pair for each.
            cargs = compiler.get_header_unit_consumer_args(mode, spelling, output)
            if cargs and cargs not in seen_consumer_args:
                seen_consumer_args.append(cargs)
                consumer_args += cargs
        return outputs, consumer_args, bmis

    def _add_header_unit_edge(self, output: str, rulename: str,
                              args: T.Union['CompilerArgs', T.List[str]],
                              spelling: str, mode: str, mapper: T.Optional[str],
                              owner: T.Optional[build.BuildTarget],
                              is_msvc: bool, is_gcc: bool) -> None:
        """One HEADER_UNIT edge, in the one shape every naming mode shares. The
        caller has already chosen the output path and, where the compiler
        resolves a unit through one, written the mapper this only wires in.
        """
        elem = NinjaBuildElement(self.all_outputs, output, rulename, [])
        elem.add_item('ARGS', args)
        elem.add_item('SPELLING', spelling)
        if is_msvc:
            elem.add_item('HUMODE', 'quote' if mode == 'user' else 'angle')
        else:
            elem.add_item('HULANG', f'c++-{mode}-header')
            elem.add_item('DEPFILE', output + '.d')
        if is_gcc:
            # A setup-written file is a fine ninja input: what ninja rejects is
            # an input that no rule makes and no file provides.
            elem.add_item('HUMAPPER', [f'-fmodule-mapper={mapper}'] if mapper else [])
            if mapper is not None:
                elem.add_dep(mapper)
        # The command reads the header off disk; a generated one must be written
        # first. Order-only, like the consumers' own dep on it: the header's
        # content reaches the compile through the BMI's rebuild, not through
        # this edge re-firing on every touch.
        elem.add_orderdep(self._header_unit_generated_deps(owner, spelling))
        self.add_build(elem)

    def _canonical_header_unit(self, args: T.Union['CompilerArgs', T.List[str]],
                               mode: str, spelling: str,
                               ident: T.Optional[str] = None) -> str:
        """The spelling that builds the BMI for whatever file `spelling` names.

        Declared spellings of one file share one BMI; the rest become extra names
        for it. The group is build-wide, not per-target: a unit's default-named
        CMI path is a pure function of its resolved name, so each name has to have
        exactly one producer across the build, and two targets that disagreed on
        which spelling was canonical would emit two. `ident` is
        _header_unit_identity when the caller already has it, resolved here
        otherwise; the header is realpath'd once either way.
        """
        if ident is None:
            ident = self._header_unit_identity(args, mode, spelling)
        if ident is None:
            return spelling
        return self._header_unit_group.setdefault((mode, ident), spelling)

    def _header_unit_identity(self, args: T.Union['CompilerArgs', T.List[str]],
                              mode: str, spelling: str) -> T.Optional[str]:
        """Which file a declared header unit names, for grouping its spellings.

        The only place a header-unit path is ever resolved: mapper keys, CMI path
        mangling and every emitted spelling stay textual, because the compiler
        names a unit by the text it was reached through. Resolving one of those
        would both lose the alias GCC is keying on and collapse a space-free
        include alias back into the spaced path it stands in for.

        None for a system unit -- it is named from the compiler's own search path,
        and a file reachable as both a user and a system unit stays two units.
        """
        if mode != 'user':
            return None
        key = self._header_unit_mapper_key(args, spelling)
        if key is None:
            return None
        return os.path.realpath(os.path.join(self.environment.get_build_dir(), key))

    def _alias_header_unit_link(self, alias_name: str, canonical_bmi: str,
                                cache_dir: str, suffix: str) -> str:
        """Put the group's BMI at an alias spelling's default-named path too.

        Scan edges carry no mapper, so a TU importing through an alias reaches the
        unit only at the path that spelling's own name mangles to. A compile
        resolves it through the mapper and needs no such file, but the scan that
        comes first does -- so the BMI is linked there rather than built twice.
        """
        output = default_cmi_path(alias_name, cache_dir, suffix)
        if output not in self._header_unit_alias_links:
            rulename = 'cpp_header_unit_alias'
            if not self.ninja.has_rule(rulename):
                # Lazily, so a build with no aliased unit keeps the rule set it
                # had. --link hard-links where it can and copies where it cannot.
                self.add_rule(NinjaRule(
                    rulename,
                    self.environment.get_build_command() + ['--internal', 'copy', '--link'],
                    ['$in', '$out'], 'Aliasing C++ header unit $out'))
            self.add_build(NinjaBuildElement(self.all_outputs, output, rulename,
                                             canonical_bmi))
            self._header_unit_alias_links[output] = canonical_bmi
        return output

    @staticmethod
    def _class_header_unit_path(key: str, spelling: str, suffix: str) -> str:
        # Forward slashes so the path is safe in the ninja file on Windows (cl
        # accepts either). The key carries the class, so one spelling's BMIs in
        # different classes cannot collide.
        digest = hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]
        safe = os.path.basename(spelling) or 'header'
        return f'meson-private/header-units/{safe}.{digest}{suffix}'

    def _write_header_unit_mapper(self, path: str, name: str, output: str) -> None:
        # The unit edge's whole mapper: one line sending the header GCC is about
        # to compile to the BMI path the edge declares. Copy-if-different, like
        # the collate's per-TU mappers: an unchanged mapper must not rebuild its
        # unit. newline='' because GCC's mapper parser reads a key to the newline
        # without stripping a carriage return, so a CRLF would break the lookup.
        full = os.path.join(self.environment.get_build_dir(), path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        line = f'{name} {output}\n'
        try:
            with open(full, encoding='utf-8', newline='') as f:
                if f.read() == line:
                    return
        except OSError:
            pass
        with open(full, 'w', encoding='utf-8', newline='') as f:
            f.write(line)

    def generate_compile_rules(self) -> None:
        for for_machine in MachineChoice:
            clist = self.environment.coredata.compilers[for_machine]
            for langname, compiler in clist.items():
                if compiler.get_id() == 'clang':
                    self.generate_llvm_ir_compile_rule(compiler)
                if compiler.get_id() == 'tasking':
                    self.generate_tasking_mil_compile_rules(compiler)
                self.generate_compile_rule_for(langname, compiler)
                self.generate_pch_rule_for(langname, compiler)
                if langname == 'cpp' and compiler.get_id() in ('gcc', 'msvc'):
                    # Clang's scan rule is registered lazily with the first
                    # module scan edge instead: creating it needs the
                    # clang-scan-deps feature probe, which non-module clang
                    # projects should never pay for.
                    self.generate_cpp_module_scan_rule(compiler)
                if langname == 'cpp' and compiler.get_id() in ('gcc', 'msvc'):
                    # Clang's header-unit rule is registered lazily with the
                    # first unit edge (provision_header_units) instead, so
                    # projects without header units don't grow an unused rule.
                    self.generate_cpp_header_unit_rule(compiler)
                for mode in compiler.get_modes():
                    self.generate_compile_rule_for(langname, mode)

    def generate_generator_list_rules(self, target: build.BuildTarget) -> None:
        # CustomTargets have already written their rules and
        # CustomTargetIndexes don't actually get generated, so write rules for
        # GeneratedLists here
        for genlist in target.get_generated_sources():
            if isinstance(genlist, (build.CustomTarget, build.CustomTargetIndex)):
                continue
            self.generate_genlist_for_target(genlist, target)

    def replace_paths(self, target: build.BuildTarget | build.CustomTarget, args: T.List[str], override_subdir: T.Optional[str] = None) -> T.List[str]:
        if override_subdir:
            source_target_dir = os.path.join(self.build_to_src, override_subdir)
        else:
            source_target_dir = self.get_target_source_dir(target)
        relout = self.get_target_private_dir(target)
        args = [x.replace("@SOURCE_DIR@", self.build_to_src).replace("@BUILD_DIR@", relout)
                for x in args]
        args = [x.replace("@CURRENT_SOURCE_DIR@", source_target_dir) for x in args]
        args = [x.replace("@SOURCE_ROOT@", self.build_to_src).replace("@BUILD_ROOT@", '.')
                for x in args]
        args = [x.replace('\\', '/') for x in args]
        return args

    def generate_genlist_for_target(self, genlist: build.GeneratedList, target: build.BuildTarget | build.CustomTarget) -> None:
        for x in genlist.depends:
            if isinstance(x, build.GeneratedList):
                self.generate_genlist_for_target(x, target)
        generator = genlist.get_generator()
        subdir = genlist.subdir
        exe = generator.get_exe()
        infilelist = genlist.get_inputs()
        dependencies = self.get_target_depend_files(genlist)
        dependencies += self.get_paths_for_dep_outputs(target, generator.depends)
        dependencies += self.get_paths_for_dep_outputs(target, genlist.extra_depends)
        for curfile in infilelist:
            infilename = curfile.rel_to_builddir(self.build_to_src, self.get_target_private_dir(target))
            base_args = generator.get_arglist(infilename)
            outfiles = genlist.get_outputs_for(curfile)
            outfilespriv = [os.path.join(self.get_target_private_dir(target), of) for of in outfiles]

            if len(generator.outputs) == 1:
                sole_output = outfilespriv[0]
            else:
                sole_output = f'{curfile}'

            if generator.depfile is None:
                rulename = 'CUSTOM_COMMAND'
                args = base_args
            else:
                rulename = 'CUSTOM_COMMAND_DEP'
                depfilename = generator.get_dep_outname(infilename)
                if genlist.preserve_path_from:
                    path_segment = genlist.get_preserved_path_segment(curfile)
                    depfilename = os.path.join(path_segment, depfilename)
                depfile = os.path.join(self.get_target_private_dir(target), depfilename)
                args = [x.replace('@DEPFILE@', depfile) for x in base_args]
            args = [x.replace("@INPUT@", infilename).replace('@OUTPUT@', sole_output)
                    for x in args]
            args = self.replace_outputs(args, self.get_target_private_dir(target), outfiles)
            args = self.replace_paths(target, args, override_subdir=subdir)
            cmdlist, reason = self.as_meson_exe_cmdline(exe,
                                                        self.replace_extra_args(args, genlist),
                                                        capture=outfilespriv[0] if generator.capture else None,
                                                        env=genlist.env)
            abs_pdir = os.path.join(self.environment.get_build_dir(), self.get_target_dir(target))
            os.makedirs(abs_pdir, exist_ok=True)

            elem = NinjaBuildElement(self.all_outputs, outfilespriv, rulename, infilename)
            elem.add_dep(dependencies)
            if generator.depfile is not None:
                elem.add_item('DEPFILE', depfile)

            if len(generator.outputs) == 1:
                what = f'{sole_output!r}'
            else:
                # since there are multiple outputs, we log the source that caused the rebuild
                what = f'from {sole_output!r}'
            if reason:
                reason = f' (wrapped by meson {reason})'
            elem.add_item('DESC', f'Generating {what}{reason}')

            elem.add_item('COMMAND', cmdlist)
            self.add_build(elem)

    def scan_fortran_module_outputs(self, target: build.BuildTarget) -> None:
        """
        Find all module and submodule made available in a Fortran code file.
        """
        if self.use_dyndeps_for_fortran():
            return
        compiler = None
        # TODO other compilers
        for lang, c in self.environment.coredata.compilers.host.items():
            if lang == 'fortran':
                compiler = c
                break
        if compiler is None:
            self.fortran_deps[target.get_basename()] = {}
            return

        modre = re.compile(FORTRAN_MODULE_PAT, re.IGNORECASE)
        submodre = re.compile(FORTRAN_SUBMOD_PAT, re.IGNORECASE)
        module_files: T.Dict[str, File] = {}
        submodule_files: T.Dict[str, File] = {}
        for s in target.get_sources():
            # FIXME, does not work for Fortran sources generated by
            # custom_target() and generator() as those are run after
            # the configuration (configure_file() is OK)
            if not compiler.can_compile(s):
                continue
            filename = s.absolute_path(self.environment.get_source_dir(),
                                       self.environment.get_build_dir())
            # Fortran keywords must be ASCII.
            with open(filename, encoding='ascii', errors='ignore') as f:
                for line in f:
                    modmatch = modre.match(line)
                    if modmatch is not None:
                        modname = modmatch.group(1).lower()
                        if modname in module_files:
                            raise InvalidArguments(
                                f'Namespace collision: module {modname} defined in '
                                f'two files {module_files[modname]} and {s}.')
                        module_files[modname] = s
                    else:
                        submodmatch = submodre.match(line)
                        if submodmatch is not None:
                            # '_' is arbitrarily used to distinguish submod from mod.
                            parents = submodmatch.group(1).lower().split(':')
                            submodname = parents[0] + '_' + submodmatch.group(2).lower()

                            if submodname in submodule_files:
                                raise InvalidArguments(
                                    f'Namespace collision: submodule {submodname} defined in '
                                    f'two files {submodule_files[submodname]} and {s}.')
                            submodule_files[submodname] = s

        self.fortran_deps[target.get_basename()] = {**module_files, **submodule_files}

    def get_fortran_deps(self, compiler: FortranCompiler, src: Path, target: build.BuildTarget) -> T.List[str]:
        """
        Find all module and submodule needed by a Fortran target
        """
        if self.use_dyndeps_for_fortran():
            return []

        dirname = Path(self.get_target_private_dir(target))
        tdeps = self.fortran_deps[target.get_basename()]
        srcdir = Path(self.source_dir)

        mod_files = _scan_fortran_file_deps(src, srcdir, dirname, tdeps, compiler)
        return mod_files

    def get_no_stdlib_link_args(self, target: build.BuildTarget, linker: Compiler | StaticLinker) -> T.List[str]:
        if hasattr(linker, 'language') and linker.language in self.build.stdlibs[target.for_machine]:
            return linker.get_no_stdlib_link_args()
        return []

    def get_compile_debugfile_args(self, compiler: Compiler, target: build.BuildTarget, objfile: str) -> T.List[str]:
        # The way MSVC uses PDB files is documented exactly nowhere so
        # the following is what we have been able to decipher via
        # reverse engineering.
        #
        # Each object file gets the path of its PDB file written
        # inside it.  This can be either the final PDB (for, say,
        # foo.exe) or an object pdb (for foo.obj). If the former, then
        # each compilation step locks the pdb file for writing, which
        # is a bottleneck and object files from one target cannot be
        # used in a different target. The latter seems to be the
        # sensible one (and what Unix does) but there is a catch.  If
        # you try to use precompiled headers MSVC will error out
        # because both source and pch pdbs go in the same file and
        # they must be the same.
        #
        # This means:
        #
        # - pch files must be compiled anew for every object file (negating
        #   the entire point of having them in the first place)
        # - when using pch, output must go to the target pdb
        #
        # Since both of these are broken in some way, use the one that
        # works for each target. This unfortunately means that you
        # can't combine pch and object extraction in a single target.
        #
        # PDB files also lead to filename collisions. A target foo.exe
        # has a corresponding foo.pdb. A shared library foo.dll _also_
        # has pdb file called foo.pdb. So will a static library
        # foo.lib, which clobbers both foo.pdb _and_ the dll file's
        # export library called foo.lib (by default, currently we name
        # them libfoo.a to avoid this issue). You can give the files
        # unique names such as foo_exe.pdb but VC also generates a
        # bunch of other files which take their names from the target
        # basename (i.e. "foo") and stomp on each other.
        #
        # CMake solves this problem by doing two things. First of all
        # static libraries do not generate pdb files at
        # all. Presumably you don't need them and VC is smart enough
        # to look up the original data when linking (speculation, not
        # tested). The second solution is that you can only have
        # target named "foo" as an exe, shared lib _or_ static
        # lib. This makes filename collisions not happen. The downside
        # is that you can't have an executable foo that uses a shared
        # library libfoo.so, which is a common idiom on Unix.
        #
        # If you feel that the above is completely wrong and all of
        # this is actually doable, please send patches.

        if target.has_pch():
            tfilename = self.get_target_debug_filename_abs(target)
            if not tfilename:
                tfilename = self.get_target_filename_abs(target)
            return compiler.get_compile_debugfile_args(tfilename, pch=True)
        else:
            return compiler.get_compile_debugfile_args(objfile, pch=False)

    def get_link_debugfile_name(self, linker: T.Union[Compiler, StaticLinker], target: build.BuildTarget) -> T.Optional[str]:
        filename = self.get_target_debug_filename(target)
        if filename:
            return linker.get_link_debugfile_name(filename)
        return None

    def get_link_debugfile_args(self, linker: T.Union[Compiler, StaticLinker], target: build.BuildTarget) -> T.List[str]:
        filename = self.get_target_debug_filename(target)
        if filename:
            return linker.get_link_debugfile_args(filename)
        return []

    def generate_llvm_ir_compile(self, target: build.BuildTarget, src: FileOrString) -> T.Tuple[str, str]:
        compiler = get_compiler_for_source(target.compilers.values(), src)
        commands = compiler.compiler_args()
        # Compiler args for compiling this target
        commands += compilers.get_base_compile_args(target, compiler, self.environment)
        if isinstance(src, File):
            if src.is_built:
                src_filename = os.path.join(src.subdir, src.fname)
            else:
                src_filename = src.fname
        elif os.path.isabs(src):
            src_filename = os.path.basename(src)
        else:
            src_filename = src
        obj_basename = self.canonicalize_filename(src_filename)
        rel_obj = os.path.join(self.get_target_private_dir(target), obj_basename)
        rel_obj += '.' + self.environment.machines[target.for_machine].get_object_suffix()
        commands += self.get_compile_debugfile_args(compiler, target, rel_obj)
        if isinstance(src, File):
            if src.is_built:
                rel_src = src.fname
            else:
                rel_src = src.rel_to_builddir(self.build_to_src)
        else:
            raise InvalidArguments(f'Invalid source type: {src!r}')
        # Write the Ninja build command
        compiler_name = self.get_compiler_rule_name('llvm_ir', compiler.for_machine)
        element = NinjaBuildElement(self.all_outputs, rel_obj, compiler_name, rel_src)
        element.add_item('ARGS', commands)
        self.add_build(element)
        return (rel_obj, rel_src)

    @lru_cache(maxsize=None)
    def generate_inc_dir(self, compiler: 'Compiler', d: str, basedir: str, is_system: bool
                         ) -> T.Tuple[ImmutableListProtocol[str], ImmutableListProtocol[str]]:
        expdir = os.path.normpath(os.path.join(basedir, d))
        srctreedir = os.path.normpath(os.path.join(self.build_to_src, expdir))
        sargs = compiler.get_include_args(srctreedir, is_system)
        # There may be include dirs where a build directory has not been
        # created for some source dir. For example if someone does this:
        #
        # inc = include_directories('foo/bar/baz')
        #
        # But never subdir()s into the actual dir.
        if os.path.isdir(os.path.join(self.environment.get_build_dir(), expdir)):
            bargs = compiler.get_include_args(expdir, is_system)
        else:
            bargs = []
        return (sargs, bargs)

    def _generate_single_compile(self, target: build.BuildTarget, compiler: Compiler) -> CompilerArgs:
        commands = target.get_single_compile_base_args(compiler)
        commands += self._generate_single_compile_target_args(target, compiler)
        return commands

    @lru_cache(maxsize=None)
    def _generate_single_compile_target_args(self, target: build.BuildTarget, compiler: Compiler) -> ImmutableListProtocol[str]:
        # Add compiler args and include paths from several sources; defaults,
        # build options, external dependencies, etc.
        commands = self.generate_basic_compiler_args(target, compiler)
        # Add custom target dirs as includes automatically, but before
        # target-specific include directories.
        if target.implicit_include_directories:
            commands += self.get_custom_target_dir_include_args(target, compiler)
        # Add include dirs from the `include_directories:` kwarg on the target
        # and from `include_directories:` of internal deps of the target.
        #
        # Target include dirs should override internal deps include dirs.
        # This is handled in BuildTarget.process_kwargs()
        #
        # Include dirs from internal deps should override include dirs from
        # external deps and must maintain the order in which they are specified.
        # Hence, we must reverse the list so that the order is preserved.
        for i in reversed(target.get_include_dirs()):
            basedir = i.curdir
            # Each directory must be added to CompilerArgs individually
            # via a separate ``commands +=`` call, in reversed order.
            #
            # CompilerArgs.__iadd__ prepends ``-I`` args (which are in
            # CLikeCompilerArgs.prepend_prefixes) but appends ``-isystem``
            # args (which are not).  When adding one directory at a time
            # in reversed order, this produces the correct result for
            # both flag types:
            #
            # - For ``-I`` (prepended): each flag is inserted at the
            #   front, so reversed iteration produces the original
            #   order — first-listed directory appears first on the
            #   command line and is searched first.
            #
            # - For ``-isystem`` (appended): each flag is added at the
            #   back, so reversed iteration produces a reversed sequence
            #   on the command line — *last*-listed directory appears
            #   first and is searched first.
            #
            # Adding all directories in a single ``commands +=`` call
            # would break ``-isystem`` ordering: the whole list would be
            # appended at once in the original (unreversed) order, making
            # the *first*-listed directory appear first instead of last.
            for d in reversed(i.incdirs):
                sargs, bargs = self.generate_inc_dir(compiler, d, basedir, i.is_system)
                commands += sargs
                commands += bargs
            for d in i.extra_build_dirs:
                commands += compiler.get_include_args(d, i.is_system)
        # Add per-target compile args, f.ex, `c_args : ['-DFOO']`. We set these
        # near the end since these are supposed to override everything else.
        commands += self.escape_extra_args(target.get_extra_args(compiler.get_language()))

        # D specific additional flags
        if compiler.language == 'd':
            commands += compiler.get_feature_args(target.d_features, self.build_to_src)

        # Add source dir and build dir. Project-specific and target-specific
        # include paths must override per-target compile args, include paths
        # from external dependencies, internal dependencies, and from
        # per-target `include_directories:`
        #
        # We prefer headers in the build dir over the source dir since, for
        # instance, the user might have an srcdir == builddir Autotools build
        # in their source tree. Many projects that are moving to Meson have
        # both Meson and Autotools in parallel as part of the transition.
        if target.implicit_include_directories:
            commands += self.get_source_dir_include_args(target, compiler)
        if target.implicit_include_directories:
            commands += self.get_build_dir_include_args(target, compiler)
        # Finally add the private dir for the target to the include path. This
        # must override everything else and must be the final path added.
        commands += compiler.get_include_args(self.get_target_private_dir(target), False)
        if self._compile_needs_space_free_respell(target, compiler):
            # A header unit is named by the text of the include-path entry it
            # was resolved through, so a spaced entry must be respelled here --
            # the one place every consumer (compile, scan, header-unit edge and
            # BMI variant) draws its include path from. Through the target's own
            # class root when the machine is multi-class, so a spaced unit's
            # name embeds its class the way a non-spaced unit's scan name does;
            # None (a space-free spelling) otherwise. force_all is off: a
            # non-spaced entry keeps its real spelling here and is aliased only
            # on the scan.
            class_key = compiler.get_bmi_class_key(
                list(target.get_single_compile_base_args(compiler)) + list(commands))
            tag = self._header_unit_class_subdir_for(target.for_machine, compiler, class_key)
            return self._respell_include_args(list(commands), tag)
        return list(commands)

    # Returns a dictionary, mapping from each compiler src type (e.g. 'c', 'cpp', etc.) to a list of compiler arg strings
    # used for that respective src type.
    # Currently used for the purpose of populating VisualStudio intellisense fields but possibly useful in other scenarios.
    def generate_common_compile_args_per_src_type(self, target: build.BuildTarget) -> T.Dict[Language, T.List[str]]:
        src_type_to_args = {}

        use_pch = self.target_uses_pch(target)

        for src_type_str in target.compilers:
            compiler = target.compilers[src_type_str]
            commands = target.get_single_compile_base_args(compiler)

            # Include PCH header as first thing as it must be the first one or it will be
            # ignored by gcc https://gcc.gnu.org/bugzilla/show_bug.cgi?id=100462
            if use_pch and 'mw' not in compiler.id:
                commands += self.get_pch_include_args(compiler, target)

            commands += self._generate_single_compile_target_args(target, compiler)

            # Metrowerks compilers require PCH include args to come after intraprocedural analysis args
            if use_pch and 'mw' in compiler.id:
                commands += self.get_pch_include_args(compiler, target)

            commands = commands.compiler.compiler_args(commands)

            src_type_to_args[src_type_str] = commands.to_native()
        return src_type_to_args

    def order_deps_to_strings(self, target: build.BuildTarget, order_deps: T.List[File] | T.List[FileOrString]) -> T.List[str]:
        result: T.List[str] = []
        for d in order_deps:
            if isinstance(d, File):
                d = d.rel_to_builddir(self.build_to_src)
            elif not self.has_dir_part(d):
                d = os.path.join(self.get_target_private_dir(target), d)
            result.append(d)
        return result

    def generate_single_compile(self, target: build.BuildTarget, src: FileOrString,
                                is_generated: bool = False,
                                header_deps: T.Optional[T.List[FileOrString]] = None,
                                order_deps: T.Optional[T.List[File] | T.List[FileOrString]] = None,
                                extra_args: T.Optional[T.List[str]] = None,
                                unity_sources: list[File] | None = None,
                                ) -> T.Tuple[str, str]:
        """
        Compiles C/C++, ObjC/ObjC++, Fortran, and D sources
        """
        header_deps = header_deps if header_deps is not None else []
        order_deps = order_deps if order_deps is not None else []

        if isinstance(src, str) and src.endswith('.h'):
            raise AssertionError(f'BUG: sources should not contain headers {src!r}')

        compiler = get_compiler_for_source(target.compilers.values(), src)
        commands = target.get_single_compile_base_args(compiler)

        # Module interface units are detected by extension (or the explicit
        # kwarg), never by reading the source; several decisions below hang
        # off this. A declared internal partition is a BMI-producing module
        # unit too (so it is scanned, registered for BMI-class variants, and
        # compiled as a module unit), but MSVC flags it /internalPartition
        # rather than /interface.
        src_suffix = src.suffix if isinstance(src, File) else os.path.splitext(src)[1][1:].lower()
        # Whether this compile takes part in the module pipeline at all. A
        # module-enabled target can also hand the C++ compiler a source with
        # no modules in it (assembly), which is scanned by nothing and so must
        # carry none of the pipeline's flags, mappers or deps.
        is_module_edge = self.source_uses_p1689_cpp_modules(target, compiler, src)
        is_internal_partition = is_module_edge \
            and self._is_declared_internal_partition(target, src)
        # A source named only in cpp_private_module_interfaces (no
        # cppm/ixx extension) is still an interface unit that must be
        # compiled/scanned as one; being private only changes where its BMI
        # ends up, not whether it is an interface at all.
        is_private_interface = is_module_edge \
            and self._is_declared_private_interface(target, src)
        is_module_interface = is_module_edge \
            and (src_suffix in {'cppm', 'ixx'} or self._is_declared_module_interface(target, src)
                 or is_internal_partition or is_private_interface)

        # Include PCH header as first thing as it must be the first one or it will be
        # ignored by gcc https://gcc.gnu.org/bugzilla/show_bug.cgi?id=100462
        # A module interface unit gets no PCH at all (the forced include would
        # land before the module declaration, which is ill-formed), and some
        # compilers cannot combine PCH with modules in any TU of the target
        # (generate_pch skips the PCH edge and warns).
        use_pch = self.target_uses_pch(target) and not is_module_interface
        if use_pch and self.target_uses_p1689_cpp_modules_edge(target, compiler) \
                and not compiler.supports_pch_with_cpp_modules():
            use_pch = False
        if use_pch and 'mw' not in compiler.id:
            commands += self.get_pch_include_args(compiler, target)
            pch = target.pch.get(compiler.get_language())
            if pch and compiler.get_id() == 'msvc' \
                    and self.target_uses_p1689_cpp_modules_edge(target, compiler):
                # cl's /scanDependencies force-includes the PCH header (/FI) from
                # disk -- it does not resolve the forced include against the
                # baked .pch the way a compile does -- so a module target's scan
                # needs the header's own directory on the include path (Meson
                # requires the PCH header to live in a subdir, off the target's
                # implicit source include). A non-module compile resolves the
                # forced include from the .pch and never needs this; mirrors the
                # pch-header-dir include the PCH build edge already adds.
                pch_header_dir = os.path.dirname(
                    os.path.join(self.build_to_src, target.get_subdir(), pch[0]))
                commands += compiler.get_include_args(pch_header_dir, False)

        commands += self._generate_single_compile_target_args(target, compiler)

        # C++ named modules: compile every scanned TU of a module-enabled
        # target with the module flags, exactly as it will be scanned. The
        # module name and BMI path never appear on the command line -- ordering
        # is carried by the dyndep and BMIs are found by directory lookup in
        # the shared cache.
        if is_module_edge:
            private_dir = self._module_private_bmi_dir_for(target)
            private_output = private_dir is not None and self._is_private_module_source(target, src)
            modargs = compiler.get_module_compile_args(
                self._bmi_class_subdir_for(target), private_dir, private_output)
            if compiler.get_id() == 'clang' and '-fmodules' in commands:
                # The user turned on implicit Clang modules themselves (via
                # cpp_args, add_project_arguments, ...; all those channels are
                # in `commands` by now). Drop the ccache-defeating pair: its
                # trailing -fno-modules would silently cancel their flag, and
                # their -fmodules already makes ccache stand aside.
                modargs = [a for a in modargs if a not in {'-fmodules', '-fno-modules'}]
            commands += modargs
            # cl and clang need each module-interface unit flagged explicitly
            # (GCC infers it from the source). Detected by extension as
            # everywhere else, never by reading the source. The collator
            # enforces this contract for clang at build time (the
            # interface-extension check in depaccumulate.run_p1689): a scanned
            # provide from a source outside this set is an error there, not a
            # downstream "module not found".
            if is_module_interface:
                if compiler.get_id() == 'msvc':
                    # An internal partition is not an interface: cl rejects
                    # /interface (and an interface file extension) for it and
                    # wants /internalPartition. The flag rides `commands`, which
                    # the scan edge reuses, so scan and compile keep dialect
                    # parity. gcc infers the kind and clang's -x c++-module
                    # covers both, so only cl needs the split.
                    commands += ['/internalPartition', '/TP'] if is_internal_partition \
                        else ['/interface', '/TP']
                elif compiler.get_id() == 'clang':
                    # -x c++-module parses the TU as an interface unit (.ixx
                    # needs it; uniform for .cppm too); bare -fmodule-output
                    # writes the BMI next to the object, where the harvest
                    # edge picks it up. No BMI path on the command line.
                    commands += ['-x', 'c++-module', '-fmodule-output']
                elif compiler.get_id() == 'gcc' and mesonlib.version_compare(compiler.version, '<15'):
                    # GCC 14's driver does not know the module extensions and
                    # would treat the file as linker input (and the -E scan
                    # would silently emit nothing); hand it the language.
                    # Interface-ness is still inferred from the source. GCC 15
                    # taught the driver the extensions.
                    commands += ['-x', 'c++']
        elif compiler.get_language() == 'cpp' and compiler.get_id() == 'gcc' \
                and self.cpp_module_scanner_for_target(target) == 'regex' \
                and any(a in commands for a in compiler.get_cpp_modules_args()):
            # The regex escape hatch: the user's bare modules flag makes GCC
            # write its make-style module rules into the -MD depfile, which
            # Ninja's gcc-deps parser rejects ('inputs may not also have
            # inputs') -- the same shape get_module_compile_args suppresses on
            # the P1689 path. Module ordering is carried by the regex-scan
            # dyndep, so the rules are pure poison; -Mno-modules is accepted
            # by every GCC that accepts the bare flag.
            commands += ['-Mno-modules']

        # Metrowerks compilers require PCH include args to come after intraprocedural analysis args
        if use_pch and 'mw' in compiler.id:
            commands += self.get_pch_include_args(compiler, target)

        commands = commands.compiler.compiler_args(commands)

        # Create introspection information
        if is_generated is False:
            self.create_target_source_introspection(target, compiler, commands, [src], [], unity_sources)
        else:
            self.create_target_source_introspection(target, compiler, commands, [], [src], unity_sources)

        build_dir = self.environment.get_build_dir()
        if isinstance(src, File):
            if src.is_built and path_has_root(src.fname):
                raise MesonBugException('absolute file name for built file ' + src.relative_name())
            rel_src = src.rel_to_builddir(self.build_to_src)
        elif is_generated:
            raise AssertionError(f'BUG: broken generated source file handling for {src!r}')
        else:
            raise InvalidArguments(f'Invalid source type: {src!r}')
        if self._compile_needs_space_free_respell(target, compiler):
            # Respelling the include path is not enough: a quote-form import
            # searches the includer's own directory first, spelled the way the
            # source was spelled here, and that spelling becomes the unit's
            # name. Respell before the edge is built, so the compile's $in, the
            # scan and the recorded interface source all agree. Same class root
            # as the include respell above; a non-spaced source is untouched.
            tag = self._header_unit_class_subdir_for(
                target.for_machine, compiler, self._bmi_class_key_of(target))
            rel_src = self._respell_path(rel_src, tag) or rel_src
        obj_basename = self.object_filename_from_source(target, compiler, src)
        rel_obj = os.path.join(self.get_target_private_dir(target), obj_basename)
        dep_file = compiler.depfile_for_object(rel_obj)

        # Add MSVC debug file generation compile flags: /Fd /FS
        commands += self.get_compile_debugfile_args(compiler, target, rel_obj)

        # PCH handling. We only support PCH for C and C++
        if compiler.language in {'c', 'cpp'} and target.has_pch() and use_pch:
            pchlist = target.pch[compiler.language]
        else:
            pchlist = None
        pch_dep: T.List[str]
        if not pchlist:
            pch_dep = []
        elif compiler.id == 'intel':
            pch_dep = []
        else:
            arr = []
            i = os.path.join(self.get_target_private_dir(target), compiler.get_pch_name(pchlist[0]))
            arr.append(i)
            pch_dep = arr
        # If TASKING compiler family is used and MIL linking is enabled for the target,
        # then compilation rule name is a special one to output MIL files
        # instead of object files for .c files
        if compiler.get_id() == 'tasking':
            target_lto = self.get_target_option(target, OptionKey('b_lto', machine=target.for_machine, subproject=target.subproject))
            if ((isinstance(target, build.StaticLibrary) and target.prelink) or target_lto) and src.rsplit('.', 1)[1] in compilers.lang_suffixes['c']:
                compiler_name = self.get_compiler_rule_name('tasking_mil_compile', compiler.for_machine)
            else:
                compiler_name = self.compiler_to_rule_name(compiler)
        else:
            compiler_name = self.compiler_to_rule_name(compiler)
        extra_deps = self.get_target_depend_files(target).copy()
        if compiler.get_language() == 'fortran':
            # Can't read source file to scan for deps if it's generated later
            # at build-time. Skip scanning for deps, and just set the module
            # outdir argument instead.
            # https://github.com/mesonbuild/meson/issues/1348
            if not is_generated:
                abs_src = Path(build_dir) / rel_src
                extra_deps += self.get_fortran_deps(T.cast('FortranCompiler', compiler),
                                                    abs_src, target)
            if not self.use_dyndeps_for_fortran():
                # Dependency hack. Remove once multiple outputs in Ninja is fixed:
                # https://groups.google.com/forum/#!topic/ninja-build/j-2RfBIOd_8
                for modname, srcfile in self.fortran_deps[target.get_basename()].items():
                    modfile = os.path.join(self.get_target_private_dir(target),
                                           compiler.module_name_to_filename(modname))

                    if srcfile == src:
                        crstr = self.get_rule_suffix(target.for_machine)
                        depelem = NinjaBuildElement(self.all_outputs,
                                                    modfile,
                                                    'FORTRAN_DEP_HACK' + crstr,
                                                    rel_obj)
                        self.add_build(depelem)
            commands += compiler.get_module_outdir_args(self.get_target_private_dir(target))

        # C++ import std is complicated enough to get its own method.
        istd_args, istd_dep = self.handle_cpp_import_std(target, compiler)
        commands.extend(istd_args)
        header_deps += istd_dep
        if extra_args is not None:
            commands.extend(extra_args)

        element = NinjaBuildElement(self.all_outputs, rel_obj, compiler_name, rel_src)
        self.add_header_deps(target, element, header_deps)
        for d in extra_deps:
            element.add_dep(d)
        element.add_orderdep(self.order_deps_to_strings(target, order_deps))
        element.add_dep(pch_dep)
        if not self.use_dyndeps_for_fortran():
            for i in self.get_fortran_module_deps(target, compiler):
                element.add_dep(i)
        if dep_file:
            element.add_item('DEPFILE', dep_file)
        if compiler.get_language() == 'cuda':
            # for cuda, we manually escape target name ($out) as $CUDA_ESCAPED_TARGET because nvcc doesn't support `-MQ` flag
            def quote_make_target(targetName: str) -> str:
                # this escape implementation is taken from llvm
                result = ''
                for (i, c) in enumerate(targetName):
                    if c in {' ', '\t'}:
                        # Escape the preceding backslashes
                        for j in range(i - 1, -1, -1):
                            if targetName[j] == '\\':
                                result += '\\'
                            else:
                                break
                        # Escape the space/tab
                        result += '\\'
                    elif c == '$':
                        result += '$'
                    elif c == '#':
                        result += '\\'
                    result += c
                return result
            element.add_item('CUDA_ESCAPED_TARGET', quote_make_target(rel_obj))
        if self.ninja.should_use_rspfile(element) and compiler.rsp_file_syntax() == RSPFileSyntax.NASM:
            exe = compiler.get_exelist()
            # Add to commands the args created by generate_compile_rule_for().
            # commands remain separate from exelist because they must stay
            # a CompilerArgs.
            if dep_file:
                commands += compiler.get_dependency_gen_args(rel_obj, dep_file)
            commands += [*compiler.get_output_args(rel_obj), *compiler.get_compile_only_args(), rel_src]

            element.rulename = 'CUSTOM_COMMAND'
            meson_exe_cmd, reason = self.as_meson_exe_cmdline(exe[0],
                                                              exe[1:] + commands.to_native(),
                                                              separator='\n',
                                                              rsp_file_flag='-@',
                                                              can_use_rsp_file=True,
                                                              verbose=True)
            cmd_type = f' (wrapped by meson {reason})' if reason else ''
            element.add_item('COMMAND', meson_exe_cmd)
            element.add_item('description', f'Compiling {compiler.get_display_language()} object {rel_obj}{cmd_type}')
        else:
            compile_commands = commands
            if is_module_edge:
                # cl and clang consumers must name each imported header unit's
                # BMI explicitly (no directory lookup). On cl the flags go only
                # on the compile, never on the scan -- which reuses `commands`
                # and may run before the BMI exists; clang's scan needs them
                # too and gets its own copy in generate_cpp_module_scan.
                # Appending is non-mutating, so the scan's `commands` is
                # untouched.
                self.provision_header_units(target, compiler)
                hu_consumer = self._target_header_unit_consumer_args.get(target.get_id(), [])
                if hu_consumer:
                    compile_commands = commands + hu_consumer
                mapper_args = compiler.get_module_mapper_args(rel_obj + '.mapper')
                if mapper_args:
                    compile_commands = compile_commands + mapper_args
                    element.add_dep(rel_obj + '.mapper')
            element.add_item('ARGS', compile_commands)

        self.add_dependency_scanner_entries_to_element(target, compiler, element, src)
        # Module interface units are remembered per target so a BMI-only
        # variant can recompile the same set under another BMI class. A Clang
        # interface's compile also writes the source-keyed BMI
        # (-fmodule-output): declare it so Ninja knows its producer, and
        # publish it into the shared cache with a harvest edge. cl needs
        # neither -- its directory /ifcOutput writes the BMI straight into the
        # class cache and the dyndep declares it there.
        is_miu = is_module_interface and compiler.supports_bmi_classes()
        clang_miu = is_miu and compiler.get_id() == 'clang'
        if clang_miu:
            element.implicit_outfilenames.append(self.get_bmi_file_for(compiler, rel_obj))
        # Declared header units are implicit inputs: their BMIs must exist
        # before this compile (in gcm.cache for GCC, at the path we chose for
        # MSVC), and a source that imports one must recompile when its BMI
        # changes. See provision_header_units.
        if is_module_edge:
            element.add_dep(self.provision_header_units(target, compiler))
        self.add_build(element)
        # Emit the P1689 scan edge for GCC/MSVC/Clang module targets,
        # reusing the exact compile args so scan and compile see the same
        # dialect.
        self.generate_cpp_module_scan(target, compiler, rel_src, rel_obj, commands,
                                      header_deps=header_deps, order_deps=order_deps,
                                      pch_dep=pch_dep)
        if is_miu:
            self._target_module_interfaces[target.get_id()].append(ModuleInterfaceSource(
                rel_src, os.path.basename(rel_obj), tuple(header_deps), tuple(order_deps),
                is_internal_partition=is_internal_partition,
                is_private=self._is_private_module_source(target, src)))
        if clang_miu:
            private_dir = self._module_private_bmi_dir_for(target)
            harvest_dir = private_dir if (private_dir is not None
                                          and self._is_private_module_source(target, src)) \
                else self._module_shared_bmi_dir_for(target)
            self.generate_cpp_module_harvest(target, compiler, rel_obj, harvest_dir)
        assert isinstance(rel_obj, str)
        assert isinstance(rel_src, str)
        return (rel_obj, rel_src.replace('\\', '/'))

    def target_uses_import_std(self, target: build.BuildTarget) -> bool:
        if 'cpp' not in target.compilers:
            return False
        try:
            if self.environment.coredata.get_option_for_target(target, 'cpp_importstd') == 'true':
                return True
        except KeyError:
            pass
        return False

    def handle_cpp_import_std(self, target: build.BuildTarget, compiler: Compiler) -> T.Tuple[T.List[str], T.List[File]]:
        istd_args: T.List[str] = []
        istd_dep: T.List[File] = []
        if not self.target_uses_import_std(target):
            return istd_args, istd_dep
        mlog.warning('Import std support is experimental and might break compatibility in the future.')
        # At the time of writing, all three major compilers work
        # wildly differently. Keep this isolated here until things
        # consolidate.
        if compiler.id == 'gcc':
            if self.import_std is None:
                mod_file = 'gcm.cache/std.gcm'
                mod_obj_file = 'std.o'
                elem = NinjaBuildElement(self.all_outputs, [mod_file, mod_obj_file], 'CUSTOM_COMMAND', [])
                compile_args = compiler.get_option_compile_args(target, target.subproject)
                compile_args += compiler.get_option_std_args(target, target.subproject)
                compile_args += ['-c', '-fmodules', '-fsearch-include-path', 'bits/std.cc']
                elem.add_item('COMMAND', compiler.exelist + compile_args)
                self.add_build(elem)
                self.import_std = ImportStdInfo(elem, mod_file, [mod_obj_file])
            istd_args = ['-fmodules']
            istd_dep = [File(True, '', self.import_std.gen_module_file)]
            return istd_args, istd_dep
        elif compiler.id == 'msvc':
            if self.import_std is None:
                mod_file = 'std.ifc'
                mod_obj_file = 'std.obj'
                in_file = Path(os.environ['VCToolsInstallDir']) / 'modules/std.ixx'
                if not in_file.is_file():
                    raise SystemExit('VS std import header could not be located.')
                in_file_str = str(in_file)
                elem = NinjaBuildElement(self.all_outputs, [mod_file, mod_obj_file], 'CUSTOM_COMMAND', [in_file_str])
                compile_args = compiler.get_option_compile_args(target, target.subproject)
                compile_args += compiler.get_option_std_args(target, target.subproject)
                compile_args += ['/nologo', '/c', '/O2', in_file_str]
                elem.add_item('COMMAND', compiler.exelist + compile_args)
                self.add_build(elem)
                self.import_std = ImportStdInfo(elem, mod_file, [mod_obj_file])
            istd_dep = [File(True, '', self.import_std.gen_module_file)]
            return istd_args, istd_dep
        else:
            raise MesonException(f'Import std not supported on compiler {compiler.id} yet.')

    def add_dependency_scanner_entries_to_element(self, target: build.BuildTarget, compiler: Compiler, element: NinjaBuildElement, src: File) -> None:
        if not self.should_use_dyndeps_for_target(target):
            return
        if isinstance(target, build.CompileTarget):
            return
        if self.source_scan_language(src) is None:
            return
        dep_scan_file = self.get_dep_scan_file_for(target)[1]
        element.add_item('dyndep', dep_scan_file)
        element.add_orderdep(dep_scan_file)

    def get_dep_scan_file_for(self, target: build.BuildTarget) -> T.Tuple[str, str]:
        priv = self.get_target_private_dir(target)
        return os.path.join(priv, 'depscan.json'), os.path.join(priv, 'depscan.dd')

    def add_header_deps(self, target: build.BuildTarget, ninja_element: NinjaBuildElement, header_deps: T.List[FileOrString]) -> None:
        for d in header_deps:
            if isinstance(d, File):
                d = d.rel_to_builddir(self.build_to_src)
            elif not self.has_dir_part(d):
                d = os.path.join(self.get_target_private_dir(target), d)
            ninja_element.add_orderdep(d)

    def has_dir_part(self, fname: FileOrString) -> bool:
        # FIXME FIXME: The usage of this is a terrible and unreliable hack
        if isinstance(fname, File):
            return fname.subdir != ''
        return has_path_sep(fname)

    # Fortran is a bit weird (again). When you link against a library, just compiling a source file
    # requires the mod files that are output when single files are built. To do this right we would need to
    # scan all inputs and write out explicit deps for each file. That is too slow and too much effort so
    # instead just have a full dependency on the library. This ensures all required mod files are created.
    # The real deps are then detected via dep file generation from the compiler. This breaks on compilers that
    # produce incorrect dep files but such is life. A full dependency is
    # required to ensure that if a new module is added to an existing file that
    # we correctly rebuild
    def get_fortran_module_deps(self, target: build.BuildTarget, compiler: Compiler) -> T.List[str]:
        # If we have dyndeps then we don't need this, since the depscanner will
        # do all of things described above.
        if compiler.language != 'fortran' or self.use_dyndeps_for_fortran():
            return []
        return [
            os.path.join(self.get_target_dir(lt), lt.get_filename())
            for lt in itertools.chain(target.link_targets, target.link_whole_targets)
        ]

    def generate_msvc_pch_command(self, target: build.BuildTarget, compiler: Compiler, pch: T.Tuple[str, T.Optional[str]]) -> T.Tuple[T.List[str], str, str, T.List[str], str]:
        from ..compilers.mixins.visualstudio import VisualStudioLikeCompiler
        assert isinstance(compiler, VisualStudioLikeCompiler) # for mypy
        header = pch[0]
        pchname = compiler.get_pch_name(header)
        dst = os.path.join(self.get_target_private_dir(target), pchname)

        commands: T.List[str] = []
        commands += self.generate_basic_compiler_args(target, compiler)

        if pch[1] is None:
            # Auto generate PCH.
            source = self.create_msvc_pch_implementation(target, compiler.get_language(), pch[0])
            pch_header_dir = os.path.dirname(os.path.join(self.build_to_src, target.get_subdir(), header))
            commands += compiler.get_include_args(pch_header_dir, False)
        else:
            source = os.path.join(self.build_to_src, target.get_subdir(), pch[1])

        just_name = os.path.basename(header)
        (objname, pch_args) = compiler.gen_pch_args(just_name, source, dst)
        commands += pch_args
        commands += self._generate_single_compile(target, compiler)
        commands += self.get_compile_debugfile_args(compiler, target, objname)
        dep = dst + '.' + compiler.get_depfile_suffix()

        link_objects = [objname] if compiler.should_link_pch_object() else []

        return commands, dep, dst, link_objects, source

    def generate_gcc_pch_command(self, target: build.BuildTarget, compiler: Compiler, pch: str) -> T.Tuple[CompilerArgs, str, str, T.List[str]]:
        commands = self._generate_single_compile(target, compiler)
        if pch.split('.')[-1] == 'h' and compiler.language == 'cpp':
            # Explicitly compile pch headers as C++. If Clang is invoked in C++ mode, it actually warns if
            # this option is not set, and for gcc it also makes sense to use it.
            commands += ['-x', 'c++-header']
        dst = os.path.join(self.get_target_private_dir(target),
                           os.path.basename(pch) + '.' + compiler.get_pch_suffix())
        dep = dst + '.' + compiler.get_depfile_suffix()
        return commands, dep, dst, []  # Gcc does not create an object file during pch generation.

    def generate_mwcc_pch_command(self, target: build.BuildTarget, compiler: Compiler, pch: str) -> T.Tuple[CompilerArgs, str, str, T.List[str]]:
        commands = self._generate_single_compile(target, compiler)
        dst = os.path.join(self.get_target_private_dir(target),
                           os.path.basename(pch) + '.' + compiler.get_pch_suffix())
        dep = os.path.splitext(dst)[0] + '.' + compiler.get_depfile_suffix()
        return commands, dep, dst, []  # mwcc compilers do not create an object file during pch generation.

    def generate_pch(self, target: build.BuildTarget, header_deps: T.Optional[T.List[FileOrString]] = None) -> T.List[str]:
        header_deps = header_deps if header_deps is not None else []
        pch_objects = []
        for lang in T.cast('T.Tuple[Language, ...]', ('c', 'cpp')):
            pch = target.pch[lang]
            if not pch:
                continue
            if lang not in target.compilers:
                continue
            compiler: Compiler = target.compilers[lang]
            if lang == 'cpp' and not compiler.supports_pch_with_cpp_modules() \
                    and self.target_uses_p1689_cpp_modules_edge(target, compiler):
                mlog.warning(f'Target "{target.name}" uses both C++ modules and a C++ '
                             f'precompiled header, which {compiler.id} cannot combine; '
                             'the precompiled header is disabled for this target.')
                continue
            if compiler.get_argument_syntax() == 'msvc':
                (commands, dep, dst, objs, src) = self.generate_msvc_pch_command(target, compiler, pch)
                extradep = os.path.join(self.build_to_src, target.get_subdir(), pch[0])
            elif compiler.id == 'intel':
                # Intel generates on target generation
                continue
            elif 'mwcc' in compiler.id:
                src = os.path.join(self.build_to_src, target.get_subdir(), pch[0])
                (commands, dep, dst, objs) = self.generate_mwcc_pch_command(target, compiler, pch[0])
                extradep = None
            else:
                src = os.path.join(self.build_to_src, target.get_subdir(), pch[0])
                (commands, dep, dst, objs) = self.generate_gcc_pch_command(target, compiler, pch[0])
                extradep = None
            pch_objects += objs
            rulename = self.compiler_to_pch_rule_name(compiler)
            elem = NinjaBuildElement(self.all_outputs, objs + [dst], rulename, src)
            if extradep is not None:
                elem.add_dep(extradep)
            self.add_header_deps(target, elem, header_deps)
            elem.add_item('ARGS', commands)
            elem.add_item('DEPFILE', dep)
            self.add_build(elem)
            self.all_pch[compiler.id].update(objs + [dst])
        return pch_objects

    def get_target_shsym_filename(self, target: build.BuildTarget) -> str:
        # Always name the .symbols file after the primary build output because it always exists
        targetdir = self.get_target_private_dir(target)
        return os.path.join(targetdir, target.get_filename() + '.symbols')

    def generate_shsym(self, target: build.BuildTarget) -> None:
        # On OS/2, an import library is generated after linking a DLL, so
        # if a DLL is used as a target, import library is not generated.
        if self.environment.machines[target.for_machine].is_os2():
            target_file = self.get_target_filename_for_linking(target)
        else:
            target_file = self.get_target_filename(target)
        if isinstance(target, build.SharedLibrary) and target.aix_so_archive:
            if self.environment.machines[target.for_machine].is_aix():
                linker, stdlib_args = target.get_clink_dynamic_linker_and_stdlibs()
                target.get_outputs()[0] = linker.get_archive_name(target.get_outputs()[0])
                target_file = target.get_outputs()[0]
                target_file = os.path.join(self.get_target_dir(target), target_file)
        symname = self.get_target_shsym_filename(target)
        elem = NinjaBuildElement(self.all_outputs, symname, 'SHSYM', target_file)
        # The library we will actually link to, which is an import library on Windows (not the DLL)
        elem.add_item('IMPLIB', self.get_target_filename_for_linking(target))
        if self.environment.is_cross_build():
            elem.add_item('CROSS', '--cross-host=' + self.environment.machines[target.for_machine].system)
        self.add_build(elem)

    def get_import_filename(self, target: build.Executable | build.SharedLibrary) -> str:
        return os.path.join(self.get_target_dir(target), target.import_filename)

    def get_target_type_link_args(self, target: build.BuildTarget, linker: T.Union[StaticLinker, Compiler]) -> T.List[str]:
        if isinstance(target, build.StaticLibrary):
            produce_thin_archive = self.allow_thin_archives[target.for_machine] and not target.should_install()
            return linker.get_std_link_args(self.environment, produce_thin_archive)

        assert isinstance(linker, Compiler)
        commands: T.List[str] = []
        if isinstance(target, build.Executable):
            # Currently only used with the Swift compiler to add '-emit-executable'
            commands += linker.get_std_exe_link_args()
            # If export_dynamic, add the appropriate linker arguments
            if target.export_dynamic:
                commands += linker.gen_export_dynamic_link_args()
            # If implib, and that's significant on this platform (i.e. Windows using either GCC or Visual Studio)
            if target.import_filename:
                commands += linker.gen_import_library_args(self.get_import_filename(target))
            if target.pie:
                commands += linker.get_pie_link_args()
            if target.vs_module_defs:
                commands += linker.gen_vs_module_defs_args(target.vs_module_defs.rel_to_builddir(self.build_to_src))
        elif isinstance(target, build.SharedLibrary):
            if isinstance(target, build.SharedModule):
                commands += linker.get_std_shared_module_link_args(target)
            else:
                commands += linker.get_std_shared_lib_link_args()
            # All shared libraries are PIC
            commands += linker.get_pic_args()
            # Add -Wl,-soname arguments on Linux, -install_name on OS X
            if not isinstance(target, build.SharedModule) or target.force_soname:
                commands += linker.get_soname_args(
                    target.prefix, target.name, target.suffix,
                    target.soversion, target.darwin_versions)
            if target.vs_module_defs:
                commands += linker.gen_vs_module_defs_args(target.vs_module_defs.rel_to_builddir(self.build_to_src))
            # This is only visited when building for Windows using either GCC or Visual Studio
            if target.import_filename:
                commands += linker.gen_import_library_args(self.get_import_filename(target))
        else:
            raise RuntimeError('Unknown build target type.')
        return commands

    def get_target_type_link_args_post_dependencies(self, target: build.BuildTarget, linker: T.Union[Compiler, StaticLinker]) -> T.List[str]:
        commands: T.List[str] = []
        if isinstance(target, (build.Executable, build.SharedLibrary)):
            assert isinstance(linker, Compiler)

            # If win_subsystem is significant on this platform, add the appropriate linker arguments.
            # Unfortunately this can't be done in get_target_type_link_args, because some misguided
            # libraries (such as SDL2) add -mwindows to their link flags.
            m = self.environment.machines[target.for_machine]
            if m.is_windows() or m.is_cygwin():
                commands += linker.get_win_subsystem_args(target.win_subsystem)
        return commands

    def get_link_whole_args(self, linker: Compiler, target: build.BuildTarget) -> T.List[str]:
        use_custom = False
        if linker.id == 'msvc':
            # Expand our object lists manually if we are on pre-Visual Studio 2015 Update 2
            # (incidentally, the "linker" here actually refers to cl.exe)
            if mesonlib.version_compare(linker.version, '<19.00.23918'):
                use_custom = True

        if use_custom:
            objects_from_static_libs: T.List[str] = []
            for dep in target.link_whole_targets:
                if not isinstance(dep, build.BuildTarget):
                    raise MesonException(
                        f'Cannot extract objects from custom target {dep.name!r} to '
                        f'link_whole it into {target.name!r}: this is not supported '
                        'with versions of MSVC older than Visual Studio 2015 Update 2.')
                l = dep.extract_all_objects(False)
                objects_from_static_libs += self.determine_ext_objs(l)
                objects_from_static_libs.extend(self.flatten_object_list(dep)[0])

            return objects_from_static_libs
        else:
            target_args = self.build_target_link_arguments(linker, target.link_whole_targets)
            return linker.get_link_whole_for(target_args) if target_args else []

    @lru_cache(maxsize=None)
    def guess_library_absolute_path(self, linker: Compiler, libname: str, search_dirs: T.Tuple[str, ...], patterns: T.Tuple[str, ...]) -> T.Optional[Path]:
        from ..compilers.c import CCompiler
        for d in search_dirs:
            for p in patterns:
                trial = CCompiler._get_trials_from_pattern(p, d, libname)
                if not trial:
                    continue
                trial = CCompiler._get_file_from_list(self.environment, trial)
                if not trial:
                    continue
                # Return the first result
                return trial
        return None

    def guess_external_link_dependencies(self, linker: Compiler, target: build.BuildTarget, commands: CompilerArgs, internal: T.List[str]) -> T.List[str]:
        # Ideally the linker would generate dependency information that could be used.
        # But that has 2 problems:
        # * currently ld cannot create dependency information in a way that ninja can use:
        #   https://sourceware.org/bugzilla/show_bug.cgi?id=22843
        # * Meson optimizes libraries from the same build using the symbol extractor.
        #   Just letting ninja use ld generated dependencies would undo this optimization.
        search_dirs: OrderedSet[str] = OrderedSet()
        libs: OrderedSet[str] = OrderedSet()
        absolute_libs = []

        build_dir = self.environment.get_build_dir()
        # the following loop sometimes consumes two items from command in one pass
        it = iter(linker.native_args_to_unix(list(commands)))
        for item in it:
            if item in internal and not item.startswith('-'):
                continue

            if item.startswith('-L'):
                if len(item) > 2:
                    path = item[2:]
                else:
                    try:
                        path = next(it)
                    except StopIteration:
                        mlog.warning("Generated linker command has -L argument without following path")
                        break
                if not os.path.isabs(path):
                    path = os.path.join(build_dir, path)
                search_dirs.add(path)
            elif item.startswith('-l'):
                if len(item) > 2:
                    lib = item[2:]
                else:
                    try:
                        lib = next(it)
                    except StopIteration:
                        mlog.warning("Generated linker command has '-l' argument without following library name")
                        break
                libs.add(lib)
            elif os.path.isabs(item) and compilers.is_library(item) and os.path.isfile(item):
                absolute_libs.append(item)

        guessed_dependencies = []
        # TODO The get_library_naming requirement currently excludes link targets that use d or fortran as their main linker
        try:
            static_patterns = linker.get_library_naming(LibType.STATIC, strict=True)
            shared_patterns = linker.get_library_naming(LibType.SHARED, strict=True)
            search_dirs_tuple = tuple(search_dirs) + tuple(linker.get_library_dirs())
            for libname in libs:
                # be conservative and record most likely shared and static resolution, because we don't know exactly
                # which one the linker will prefer
                staticlibs = self.guess_library_absolute_path(linker, libname,
                                                              search_dirs_tuple, static_patterns)
                sharedlibs = self.guess_library_absolute_path(linker, libname,
                                                              search_dirs_tuple, shared_patterns)
                if staticlibs:
                    guessed_dependencies.append(staticlibs.resolve().as_posix())
                if sharedlibs:
                    guessed_dependencies.append(sharedlibs.resolve().as_posix())
        except (mesonlib.MesonException, AttributeError) as e:
            if 'get_library_naming' not in str(e):
                raise

        return guessed_dependencies + absolute_libs

    def generate_prelink(self, target: build.BuildTarget, obj_list: T.List[str]) -> T.List[str]:
        assert isinstance(target, build.StaticLibrary)
        prelink_name = os.path.join(self.get_target_private_dir(target), target.name + '-prelink.o')
        elem = NinjaBuildElement(self.all_outputs, [prelink_name], 'CUSTOM_COMMAND', obj_list)

        prelinker = target.get_prelinker()
        cmd = prelinker.exelist[:]
        obj_list, args = prelinker.get_prelink_args(prelink_name, obj_list)
        cmd += args
        if prelinker.get_prelink_append_compile_args():
            compile_args = target.get_single_compile_base_args(prelinker)
            compile_args += self._generate_single_compile_target_args(target, prelinker)
            compile_args = compile_args.compiler.compiler_args(compile_args)
            cmd += compile_args.to_native()

        cmd = self.replace_paths(target, cmd)
        elem.add_item('COMMAND', cmd)
        elem.add_item('description', f'Prelinking {prelink_name}')
        self.add_build(elem)
        return obj_list

    def get_build_rpath_args(self, target: build.BuildTarget, linker: T.Union[Compiler, StaticLinker]) -> T.List[str]:
        if has_path_sep(target.name):
            # Target names really should not have slashes in them, but
            # unfortunately we did not check for that and some downstream projects
            # now have them. Once slashes are forbidden, remove this bit.
            target_slashname_workaround_dir = os.path.join(os.path.dirname(target.name),
                                                           self.get_target_dir(target))
        else:
            target_slashname_workaround_dir = self.get_target_dir(target)
        (rpath_args, target.rpath_dirs_to_remove) = (
            linker.build_rpath_args(self.environment.get_build_dir(),
                                    target_slashname_workaround_dir,
                                    target))
        return rpath_args

    def generate_link(self, target: build.BuildTarget, outname: str, obj_list: T.List[str],
                      linker: T.Union[Compiler, StaticLinker],
                      extra_objs: T.Optional[T.List[str]] = None,
                      stdlib_args: T.Optional[T.List[str]] = None) -> NinjaBuildElement:
        extra_objs = extra_objs if extra_objs is not None else []
        stdlib_args = stdlib_args if stdlib_args is not None else []
        implicit_outs = []
        if isinstance(target, build.StaticLibrary):
            linker_base = 'STATIC'
        else:
            assert isinstance(linker, Compiler)
            linker_base = linker.get_language() # Fixme.
        if isinstance(target, build.SharedLibrary) and self.environment.machines[target.for_machine].is_os2():
            target_file = self.get_target_filename(target)
            import_name = self.get_import_filename(target)
            elem = NinjaBuildElement(self.all_outputs, import_name, 'IMPORTLIB', target_file)
            self.add_build(elem)
        crstr = self.get_rule_suffix(target.for_machine)
        linker_rule = linker_base + '_LINKER' + crstr
        # Create an empty commands list, and start adding link arguments from
        # various sources in the order in which they must override each other
        # starting from hard-coded defaults followed by build options and so on.
        #
        # Once all the linker options have been passed, we will start passing
        # libraries and library paths from internal and external sources.
        commands = linker.compiler_args()
        # First, the trivial ones that are impossible to override.
        #
        # Add linker args for linking this target derived from 'base' build
        # options passed on the command-line, in default_options, etc.
        # These have the lowest priority.
        if isinstance(target, build.StaticLibrary):
            assert isinstance(linker, StaticLinker)
            base_link_args = linker.get_base_link_args(target, linker, self.environment)
        else:
            assert isinstance(linker, Compiler)
            base_link_args = compilers.get_base_link_args(target,
                                                          linker,
                                                          self.environment)
        commands += self.transform_link_args(target, base_link_args)
        # Add -nostdlib if needed; can't be overridden
        commands += self.get_no_stdlib_link_args(target, linker)
        # Add things like /NOLOGO; usually can't be overridden
        commands += linker.get_linker_always_args()
        # Add buildtype linker args: optimization level, etc.
        optimization = self.get_target_option(target, 'optimization')
        assert isinstance(optimization, str)
        commands += linker.get_optimization_link_args(optimization)
        # Add /DEBUG and the pdb filename when using MSVC
        if self.get_target_option(target, 'debug'):
            commands += self.get_link_debugfile_args(linker, target)
            debugfile = self.get_link_debugfile_name(linker, target)
            if debugfile is not None:
                implicit_outs += [debugfile]
        # Add link args specific to this BuildTarget type, such as soname args,
        # PIC, import library generation, etc.
        commands += self.get_target_type_link_args(target, linker)
        if not isinstance(target, build.StaticLibrary):
            assert isinstance(linker, Compiler)
            # Archives that are copied wholesale in the result. Must be before any
            # other link targets so missing symbols from whole archives are found in those.
            commands += self.get_link_whole_args(linker, target)
            commands += linker.get_build_link_args(target, self.build)

        # Now we will add libraries and library paths from various sources

        # Set runtime-paths so we can run executables without needing to set
        # LD_LIBRARY_PATH, etc in the environment. Doesn't work on Windows.
        commands += self.get_build_rpath_args(target, linker)

        # Add link args to link to all internal libraries (link_with:) and
        # internal dependencies needed by this target.
        dep_targets: T.List[str] = []
        dependencies: T.Iterable[build.BuildTargetTypes]
        if isinstance(target, build.StaticLibrary):
            # Link arguments of static libraries are not put in the command
            # line of the library. They are instead appended to the command
            # line where the static library is used.
            dependencies = []
        else:
            # Only non-static built targets need link args and link dependencies
            assert isinstance(linker, Compiler)

            dependencies = target.get_dependencies()
            internal = self.build_target_link_arguments(linker, dependencies)
            commands += internal

            # For 'automagic' deps: Boost and GTest. Also dependency('threads').
            # pkg-config puts the thread flags itself via `Cflags:`
            commands += linker.get_target_link_args(target)
            # External deps must be last because target link libraries may depend on them.
            for dep in target.get_external_deps():
                # Extend without reordering or de-dup to preserve `-L -l` sets
                # https://github.com/mesonbuild/meson/issues/1718
                commands.extend_preserving_lflags(linker.get_dependency_link_args(dep))
            for d in target.get_dependencies():
                if isinstance(d, build.StaticLibrary):
                    for dep in d.get_external_deps():
                        link_args = linker.get_dependency_link_args(dep)
                        # Ensure that native static libraries use Unix-style naming if necessary.
                        # Depending on the target/linker, rustc --print native-static-libs may
                        # output MSVC-style names. Converting these to Unix-style is safe, as the
                        # list contains only native static libraries.
                        if dep.name == '_rust_native_static_libs' and linker.get_argument_syntax() != 'msvc':
                            from ..linkers.linkers import VisualStudioLikeLinker
                            link_args = VisualStudioLikeLinker.native_args_to_unix(link_args)
                        commands.extend_preserving_lflags(link_args)

            # Add link args specific to this BuildTarget type that must not be overridden by dependencies
            commands += self.get_target_type_link_args_post_dependencies(target, linker)

            # Add link args for c_* or cpp_* build options. Currently this only
            # adds c_winlibs and cpp_winlibs when building for Windows. This needs
            # to be after all internal and external libraries so that unresolved
            # symbols from those can be found here. This is needed when the
            # *_winlibs that we want to link to are static mingw64 libraries.
            #
            # The static linker doesn't know what language it is building, so we
            # don't know what option. Fortunately, it doesn't care to see the
            # language-specific options either.
            #
            # We shouldn't check whether we are making a static library, because
            # in the LTO case we do use a real compiler here.
            commands += linker.get_option_link_args(target)

            dep_targets.extend(self.guess_external_link_dependencies(linker, target, commands, internal))

        obj_list += self.get_import_std_object(target)

        # Add libraries generated by custom targets
        custom_target_libraries = self.get_custom_target_provided_libraries(target)
        commands += extra_objs
        commands += custom_target_libraries
        commands += stdlib_args # Standard library arguments go last, because they never depend on anything.
        dep_targets.extend([self.get_dependency_filename(t) for t in dependencies])
        dep_targets.extend([self.get_dependency_filename(t)
                            for t in target.link_depends])
        elem = NinjaBuildElement(self.all_outputs, outname, linker_rule, obj_list, implicit_outs=implicit_outs)
        all_deps = [*dep_targets, *extra_objs, *custom_target_libraries]
        elem.add_dep(all_deps)
        if linker.get_id() == 'tasking':
            if any(x.endswith('.ma') for x in all_deps) and not self.get_target_option(target, OptionKey('b_lto', target.subproject, target.for_machine)):
                raise MesonException(f'Tried to link the target named \'{target.name}\' with a MIL archive without LTO enabled! This causes the compiler to ignore the archive.')

        # Compiler args must be included in TI C28x linker commands.
        compile_args: T.List[str] = []
        if linker.get_id() in {'c2000', 'c6000', 'ti'}:
            for for_machine in MachineChoice:
                clist = self.environment.coredata.compilers[for_machine]
                for langname, compiler in clist.items():
                    if langname in {'c', 'cpp'} and compiler.get_id() in {'c2000', 'c6000', 'ti'}:
                        compile_args += self.generate_basic_compiler_args(target, compiler)

        # Add early arguments before any object files or libraries
        if not isinstance(target, build.StaticLibrary):
            assert isinstance(linker, Compiler)
            compile_args += linker.get_target_link_early_args(target)
        if compile_args:
            elem.add_item('ARGS', compile_args)
        elem.add_item('LINK_ARGS', commands)
        self.create_target_linker_introspection(target, linker, commands)
        return elem

    def get_import_std_object(self, target: build.BuildTarget) -> T.List[str]:
        if not self.target_uses_import_std(target):
            return []
        return self.import_std.gen_objects

    def get_dependency_filename(self, t: T.Union[File, build.BuildTargetTypes]) -> str:
        if isinstance(t, build.SharedLibrary):
            if t.uses_rust() and t.rust_crate_type == 'proc-macro':
                return self.get_target_filename(t)
            else:
                return self.get_target_shsym_filename(t)
        elif isinstance(t, mesonlib.File):
            if t.is_built:
                return t.relative_name()
            else:
                return t.absolute_path(self.environment.get_source_dir(),
                                       self.environment.get_build_dir())
        return self.get_target_filename(t)

    def generate_shlib_aliases(self, target: build.BuildTarget, outdir: str) -> None:
        for alias, to, tag in target.get_aliases():
            aliasfile = os.path.join(outdir, alias)
            abs_aliasfile = os.path.join(self.environment.get_build_dir(), outdir, alias)
            try:
                os.remove(abs_aliasfile)
            except Exception:
                pass
            try:
                os.symlink(to, abs_aliasfile)
            except NotImplementedError:
                mlog.debug("Library versioning disabled because symlinks are not supported.")
            except OSError:
                mlog.debug("Library versioning disabled because we do not have symlink creation privileges.")
            else:
                self.implicit_meson_outs.append(aliasfile)

    def generate_custom_target_clean(self, trees: T.List[str]) -> str:
        e = self.create_phony_target('clean-ctlist', 'CUSTOM_COMMAND', 'PHONY')
        d = CleanTrees(self.environment.get_build_dir(), trees)
        d_file = os.path.join(self.environment.get_scratch_dir(), 'cleantrees.dat')
        e.add_item('COMMAND', self.environment.get_build_command() + ['--internal', 'cleantrees', d_file])
        e.add_item('description', 'Cleaning custom target directories')
        self.add_build(e)
        # Write out the data file passed to the script
        with open(d_file, 'wb') as ofile:
            pickle.dump(d, ofile)
        return 'clean-ctlist'

    def generate_gcov_clean(self) -> None:
        gcno_elem = self.create_phony_target('clean-gcno', 'CUSTOM_COMMAND', 'PHONY')
        gcno_elem.add_item('COMMAND', mesonlib.get_meson_command() + ['--internal', 'delwithsuffix', '.', 'gcno'])
        gcno_elem.add_item('description', 'Deleting gcno files')
        self.add_build(gcno_elem)

        gcda_elem = self.create_phony_target('clean-gcda', 'CUSTOM_COMMAND', 'PHONY')
        gcda_elem.add_item('COMMAND', mesonlib.get_meson_command() + ['--internal', 'delwithsuffix', '.', 'gcda'])
        gcda_elem.add_item('description', 'Deleting gcda files')
        self.add_build(gcda_elem)

    def get_user_option_args(self) -> T.List[str]:
        cmds = []
        for k, v in self.environment.coredata.optstore.items():
            if self.environment.coredata.optstore.is_project_option(k):
                cmds.append('-D' + str(k) + '=' + (v.value if isinstance(v.value, str) else str(v.value).lower()))
        # The order of these arguments must be the same between runs of Meson
        # to ensure reproducible output. The order we pass them shouldn't
        # affect behavior in any other way.
        return sorted(cmds)

    def generate_dist(self) -> None:
        elem = self.create_phony_target('dist', 'CUSTOM_COMMAND', 'PHONY')
        elem.add_item('DESC', 'Creating source packages')
        elem.add_item('COMMAND', self.environment.get_build_command() + ['dist'])
        elem.add_item('pool', 'console')
        self.add_build(elem)

    def generate_clippy(self) -> None:
        if 'clippy' in self.all_outputs or not self.have_language('rust'):
            return

        cmd = self.environment.get_build_command() + \
            ['--internal', 'clippy', self.environment.build_dir]
        elem = self.create_phony_target('clippy', 'CUSTOM_COMMAND', 'PHONY')
        elem.add_item('COMMAND', cmd)
        elem.add_item('pool', 'console')
        for crate in self.rust_crates.values():
            if crate.crate_type in {'rlib', 'dylib', 'proc-macro'}:
                elem.add_dep(crate.target_name)
        elem.add_dep(list(self.all_structured_sources))
        self.add_build(elem)

    def generate_clippy_json_prereq(self) -> None:
        if 'clippy-json-prereq' in self.all_outputs or not self.have_language('rust'):
            return

        elem = self.create_phony_target('clippy-json-prereq', 'CUSTOM_COMMAND', 'PHONY')
        for crate in self.rust_crates.values():
            if crate.crate_type in {'rlib', 'dylib', 'proc-macro'}:
                elem.add_dep(crate.target_name)
        elem.add_dep(list(self.all_structured_sources))
        self.add_build(elem)

    def generate_clippy_json(self) -> None:
        if 'clippy-json' in self.all_outputs or not self.have_language('rust'):
            return

        cmd = self.environment.get_build_command() + \
            ['--internal', 'clippy', self.environment.build_dir, '--error-format=json']
        elem = self.create_phony_target('clippy-json', 'CUSTOM_COMMAND', 'PHONY')
        elem.add_item('COMMAND', cmd)
        elem.add_item('pool', 'console')
        self.add_build(elem)

    def generate_rustdoc(self) -> None:
        if 'rustdoc' in self.all_outputs or not self.have_language('rust'):
            return

        cmd = self.environment.get_build_command() + \
            ['--internal', 'rustdoc', self.environment.build_dir]
        elem = self.create_phony_target('rustdoc', 'CUSTOM_COMMAND', 'PHONY')
        elem.add_item('COMMAND', cmd)
        elem.add_item('pool', 'console')
        for crate in self.rust_crates.values():
            if crate.crate_type in {'rlib', 'dylib', 'proc-macro'}:
                elem.add_dep(crate.target_name)
        elem.add_dep(list(self.all_structured_sources))
        self.add_build(elem)

    def generate_scanbuild(self) -> None:
        if not tooldetect.detect_scanbuild():
            return
        if 'scan-build' in self.all_outputs:
            return
        cmd = self.environment.get_build_command() + \
            ['--internal', 'scanbuild', self.environment.source_dir, self.environment.build_dir, self.build.get_subproject_dir()] + \
            self.environment.get_build_command() + ['setup'] + self.get_user_option_args()
        elem = self.create_phony_target('scan-build', 'CUSTOM_COMMAND', 'PHONY')
        elem.add_item('COMMAND', cmd)
        elem.add_item('pool', 'console')
        self.add_build(elem)

    def generate_clangtool(self, name: str, extra_arg: T.Optional[str] = None, need_pch: bool = False) -> None:
        target_name = 'clang-' + name
        extra_args = []
        if extra_arg:
            target_name += f'-{extra_arg}'
            extra_args.append(f'--{extra_arg}')
        colorout = self.environment.coredata.optstore.get_value_for('b_colorout') \
            if OptionKey('b_colorout') in self.environment.coredata.optstore else 'always'
        assert isinstance(colorout, str), 'for mypy'
        extra_args.extend(['--color', colorout])
        if not os.path.exists(os.path.join(self.environment.source_dir, '.clang-' + name)) and \
                not os.path.exists(os.path.join(self.environment.source_dir, '_clang-' + name)):
            return
        if target_name in self.all_outputs:
            return
        if need_pch and not set(self.all_pch.keys()) <= {'clang'}:
            return

        cmd = self.environment.get_build_command() + \
            ['--internal', 'clang' + name, self.environment.source_dir, self.environment.build_dir] + \
            extra_args
        elem = self.create_phony_target(target_name, 'CUSTOM_COMMAND', 'PHONY')
        elem.add_item('COMMAND', cmd)
        elem.add_item('pool', 'console')
        if need_pch:
            elem.add_dep(list(self.all_pch['clang']))
        self.add_build(elem)

    def generate_clangformat(self) -> None:
        if not tooldetect.detect_clangformat():
            return
        self.generate_clangtool('format')
        self.generate_clangtool('format', 'check')

    def generate_clangtidy(self) -> None:
        if not tooldetect.detect_clangtidy():
            return
        self.generate_clangtool('tidy', need_pch=True)
        if not tooldetect.detect_clangapply():
            return
        self.generate_clangtool('tidy', 'fix', need_pch=True)

    def generate_tags(self, tool: str, target_name: str) -> None:
        import shutil
        if not shutil.which(tool):
            return
        if target_name in self.all_outputs:
            return
        cmd = self.environment.get_build_command() + \
            ['--internal', 'tags', tool, self.environment.source_dir]
        elem = self.create_phony_target(target_name, 'CUSTOM_COMMAND', 'PHONY')
        elem.add_item('COMMAND', cmd)
        elem.add_item('pool', 'console')
        self.add_build(elem)

    # For things like scan-build and other helper tools we might have.
    def generate_utils(self) -> None:
        self.generate_scanbuild()
        self.generate_clangformat()
        self.generate_clangtidy()
        self.generate_clippy()
        self.generate_clippy_json_prereq()
        self.generate_clippy_json()
        self.generate_rustdoc()
        self.generate_tags('etags', 'TAGS')
        self.generate_tags('ctags', 'ctags')
        self.generate_tags('cscope', 'cscope')
        cmd = self.environment.get_build_command() + ['--internal', 'uninstall']
        elem = self.create_phony_target('uninstall', 'CUSTOM_COMMAND', 'PHONY')
        elem.add_item('COMMAND', cmd)
        elem.add_item('pool', 'console')
        self.add_build(elem)

    def generate_ending(self) -> None:
        for targ, deps in [
                ('all', self.get_build_by_default_targets().values()),
                ('meson-test-prereq', self.get_testlike_targets()),
                ('meson-benchmark-prereq', self.get_testlike_targets(True))]:
            targetlist = []
            for t in deps:
                # Add the first output of each target to the 'all' target so that
                # they are all built
                #Add archive file if shared library in AIX for build all.
                if isinstance(t, build.SharedLibrary) and t.aix_so_archive:
                    if self.environment.machines[t.for_machine].is_aix():
                        linker, stdlib_args = t.get_clink_dynamic_linker_and_stdlibs()
                        t.get_outputs()[0] = linker.get_archive_name(t.get_outputs()[0])
                targetlist.append(os.path.join(self.get_target_dir(t), t.get_outputs()[0]))

                # Add an import library if shared library in OS/2 for build all
                if isinstance(t, build.SharedLibrary):
                    if self.environment.machines[t.for_machine].is_os2():
                        targetlist.append(os.path.join(self.get_target_dir(t), t.import_filename))

            elem = NinjaBuildElement(self.all_outputs, targ, 'phony', targetlist)
            self.add_build(elem)

        elem = self.create_phony_target('clean', 'CUSTOM_COMMAND', 'PHONY')
        elem.add_item('COMMAND', self.ninja_command + ['-t', 'clean'])
        elem.add_item('description', 'Cleaning')

        # If we have custom targets in this project, add all their outputs to
        # the list that is passed to the `cleantrees.py` script. The script
        # will manually delete all custom_target outputs that are directories
        # instead of files. This is needed because on platforms other than
        # Windows, Ninja only deletes directories while cleaning if they are
        # empty. https://github.com/mesonbuild/meson/issues/1220
        ctlist = []
        for t in self.build.get_targets().values():
            if isinstance(t, build.CustomTarget):
                # Create a list of all custom target outputs
                for o in t.get_outputs():
                    ctlist.append(os.path.join(self.get_target_dir(t), o))
        # Also clean meson-dist directory created by `meson dist`
        ctlist.append('meson-dist')
        if ctlist:
            elem.add_dep(self.generate_custom_target_clean(ctlist))

        if OptionKey('b_coverage') in self.environment.coredata.optstore and \
           self.environment.coredata.optstore.get_value_for('b_coverage'):
            self.generate_gcov_clean()
            elem.add_dep('clean-gcda')
            elem.add_dep('clean-gcno')
        self.add_build(elem)

        deps = self.get_regen_filelist()
        elem = NinjaBuildElement(self.all_outputs, 'build.ninja', 'REGENERATE_BUILD', deps)
        elem.add_item('pool', 'console')
        self.add_build(elem)

        # If these files used to be explicitly created, they need to appear on the build graph somehow,
        # otherwise cleandead deletes them. See https://github.com/ninja-build/ninja/issues/2299
        if self.implicit_meson_outs:
            elem = NinjaBuildElement(self.all_outputs, 'meson-implicit-outs', 'phony', self.implicit_meson_outs)
            self.add_build(elem)

        elem = NinjaBuildElement(self.all_outputs, 'reconfigure', 'REGENERATE_BUILD', 'PHONY')
        elem.add_item('pool', 'console')
        self.add_build(elem)

        elem = NinjaBuildElement(self.all_outputs, deps, 'phony', '')
        self.add_build(elem)

    def get_introspection_data(self, target_id: str, target: build.Target) -> T.List[TargetIntrospectionData]:
        data = self.introspection_data.get(target_id)
        if not data:
            return super().get_introspection_data(target_id, target)

        return list(data.values())


def _scan_fortran_file_deps(src: Path, srcdir: Path, dirname: Path, tdeps: T.Dict[str, File], compiler: FortranCompiler) -> T.List[str]:
    """
    scan a Fortran file for dependencies. Needs to be distinct from target
    to allow for recursion induced by `include` statements.er

    It makes a number of assumptions, including

    * `use`, `module`, `submodule` name is not on a continuation line

    Regex
    -----

    * `incre` works for `#include "foo.f90"` and `include "foo.f90"`
    * `usere` works for legacy and Fortran 2003 `use` statements
    * `submodre` is for Fortran >= 2008 `submodule`
    """

    incre = re.compile(FORTRAN_INCLUDE_PAT, re.IGNORECASE)
    usere = re.compile(FORTRAN_USE_PAT, re.IGNORECASE)
    submodre = re.compile(FORTRAN_SUBMOD_PAT, re.IGNORECASE)

    mod_files = []
    src = Path(src)
    with src.open(encoding='ascii', errors='ignore') as f:
        for line in f:
            # included files
            incmatch = incre.match(line)
            if incmatch is not None:
                incfile = src.parent / incmatch.group(1)
                # NOTE: src.parent is most general, in particular for CMake subproject with Fortran file
                # having an `include 'foo.f'` statement.
                if incfile.suffix.lower()[1:] in compiler.file_suffixes:
                    mod_files.extend(_scan_fortran_file_deps(incfile, srcdir, dirname, tdeps, compiler))
            # modules
            usematch = usere.match(line)
            if usematch is not None:
                usename = usematch.group(1).lower()
                if usename == 'intrinsic':  # this keeps the regex simpler
                    continue
                if usename not in tdeps:
                    # The module is not provided by any source file. This
                    # is due to:
                    #   a) missing file/typo/etc
                    #   b) using a module provided by the compiler, such as
                    #      OpenMP
                    # There's no easy way to tell which is which (that I
                    # know of) so just ignore this and go on. Ideally we
                    # would print a warning message to the user but this is
                    # a common occurrence, which would lead to lots of
                    # distracting noise.
                    continue
                srcfile = srcdir / tdeps[usename].fname
                if not srcfile.is_file():
                    if srcfile.name != src.name:  # generated source file
                        pass
                    else:  # subproject
                        continue
                elif srcfile.samefile(src):  # self-reference
                    continue

                mod_name = compiler.module_name_to_filename(usename)
                mod_files.append(str(dirname / mod_name))
            else:  # submodules
                submodmatch = submodre.match(line)
                if submodmatch is not None:
                    parents = submodmatch.group(1).lower().split(':')
                    assert len(parents) in {1, 2}, (
                        'submodule ancestry must be specified as'
                        f' ancestor:parent but Meson found {parents}')

                    ancestor_child = '_'.join(parents)
                    if ancestor_child not in tdeps:
                        raise MesonException("submodule {} relies on ancestor module {} that was not found.".format(submodmatch.group(2).lower(), ancestor_child.split('_', maxsplit=1)[0]))
                    submodsrcfile = srcdir / tdeps[ancestor_child].fname
                    if not submodsrcfile.is_file():
                        if submodsrcfile.name != src.name:  # generated source file
                            pass
                        else:  # subproject
                            continue
                    elif submodsrcfile.samefile(src):  # self-reference
                        continue
                    mod_name = compiler.module_name_to_filename(ancestor_child)
                    mod_files.append(str(dirname / mod_name))
    return mod_files
