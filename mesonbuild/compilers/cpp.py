# SPDX-License-Identifier: Apache-2.0
# Copyright 2012-2017 The Meson development team

from __future__ import annotations

import functools
import json
import os.path
import shutil
import tempfile
import typing as T

from .. import options
from .. import mlog
from ..mesonlib import MesonException, Popen_safe, version_compare, lazy_property

from .compilers import (
    gnu_winlibs,
    msvc_winlibs,
    Compiler,
    CompileCheckMode,
)
from .c_function_attributes import CXX_FUNC_ATTRIBUTES, C_FUNC_ATTRIBUTES
from .mixins.apple import AppleCompilerMixin, AppleCPPStdsMixin
from .mixins.clike import CLikeCompiler
from .mixins.ccrx import CcrxCompiler
from .mixins.ti import TICompiler
from .mixins.arm import ArmCompiler, ArmclangCompiler
from .mixins.visualstudio import MSVCCompiler, ClangClCompiler
from .mixins.gnu import GnuCompiler, GnuCPPStds, gnu_common_warning_args, gnu_cpp_warning_args
from .mixins.intel import IntelGnuLikeCompiler, IntelLLVMLikeCompiler, IntelVisualStudioLikeCompiler
from .mixins.clang import ClangCompiler, ClangCPPStds
from .mixins.elbrus import ElbrusCompiler
from .mixins.pgi import PGICompiler
from .mixins.emscripten import EmscriptenMixin
from .mixins.metrowerks import MetrowerksCompiler
from .mixins.metrowerks import mwccarm_instruction_set_args, mwcceppc_instruction_set_args
from .mixins.microchip import Xc32Compiler, Xc32CPPStds

if T.TYPE_CHECKING:
    from ..options import MutableKeyedOptionDictType
    from ..dependencies import Dependency
    from ..environment import Environment
    from ..linkers.linkers import DynamicLinker
    from ..mesonlib import MachineChoice
    from ..build import BuildTarget
    CompilerMixinBase = CLikeCompiler
else:
    CompilerMixinBase = object

# C++20 and newer as the base c++NN names (both the draft alias c++2a and the
# final c++20). Modules need C++20, so this is also the module-capable set;
# keeping it as the source ALL_STDS is built from means a future standard is one
# edit, here. Prefix-stripping (cpp_std_supports_modules) covers the gnu++/vc++
# spellings for the MODULE check only -- ALL_STDS membership is what option
# validation asserts against, so every advertised vc++ spelling must be listed.
CPP20_PLUS_STDS = ['c++2a', 'c++2b', 'c++2c', 'c++20', 'c++23', 'c++26']
ALL_STDS = ['c++98', 'c++0x', 'c++03', 'c++1y', 'c++1z', 'c++11', 'c++14', 'c++17']
ALL_STDS += CPP20_PLUS_STDS
ALL_STDS += [f'gnu{std[1:]}' for std in ALL_STDS]
ALL_STDS += ['vc++11', 'vc++14', 'vc++17', 'vc++20', 'vc++23', 'vc++latest', 'c++latest']

# Just the standard token (the part after the c++/gnu++/vc++ prefix) of each
# module-capable std, plus 'latest'. cpp_std_supports_modules compares this
# against std.rsplit('++', 1)[-1], so c++20, gnu++20 and vc++20 all match one
# entry -- every prefix spelling is covered without enumerating them.
_MODULE_STD_TOKENS = frozenset([s.split('++', 1)[1] for s in CPP20_PLUS_STDS] + ['latest'])


def cpp_std_supports_modules(std: str) -> bool:
    """Whether a cpp_std option value selects C++20 or later, which modules need.

    'none' (compiler default) and older standards are rejected, since no
    shipping compiler defaults to C++20 or later.
    """
    return std.rsplit('++', 1)[-1] in _MODULE_STD_TOKENS


def _parse_std_module_manifest(manifest: str) -> T.Tuple[T.Dict[str, str], T.List[str]]:
    """Parse a standard library's module manifest (*.modules.json).

    Both libstdc++'s and libc++'s manifests share the shape: a 'modules' list
    whose entries carry 'logical-name', a manifest-relative 'source-path' and
    an 'is-std-library' flag. Returns ({logical-name: absolute source path},
    [extra system include dirs]); the include dirs come from the entries'
    local-arguments.system-include-directories (libc++ needs them to compile
    its std.cppm; libstdc++ has none), manifest-relative too.
    """
    try:
        with open(manifest, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}, []
    base = os.path.dirname(manifest)
    sources: T.Dict[str, str] = {}
    incdirs: T.List[str] = []
    for mod in data.get('modules', []):
        if mod.get('is-std-library') and 'source-path' in mod:
            sources[mod['logical-name']] = os.path.normpath(
                os.path.join(base, mod['source-path']))
            for d in mod.get('local-arguments', {}).get('system-include-directories', []):
                d = os.path.normpath(os.path.join(base, d))
                if d not in incdirs:
                    incdirs.append(d)
    return sources, incdirs


def non_msvc_eh_options(eh: str, args: T.List[str]) -> None:
    if eh == 'none':
        args.append('-fno-exceptions')
    elif eh in {'s', 'c'}:
        mlog.warning(f'non-MSVC compilers do not support {eh} exception handling. '
                     'You may want to set eh to \'default\'.', fatal=False)

class CPPCompiler(CLikeCompiler, Compiler):
    def attribute_check_func(self, name: str) -> str:
        try:
            return CXX_FUNC_ATTRIBUTES.get(name, C_FUNC_ATTRIBUTES[name])
        except KeyError:
            raise MesonException(f'Unknown function attribute "{name}"')

    language = 'cpp'

    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        # If a child ObjCPP class has already set it, don't set it ourselves
        Compiler.__init__(self, ccache, exelist, version, for_machine, env,
                          linker=linker, full_version=full_version)
        CLikeCompiler.__init__(self)

    @classmethod
    def get_display_language(cls) -> str:
        return 'C++'

    def get_no_stdinc_args(self) -> T.List[str]:
        return ['-nostdinc++']

    def get_no_stdlib_link_args(self) -> T.List[str]:
        return ['-nostdlib++']

    def get_cpp_modules_args(self) -> T.List[str]:
        return []

    def _sanity_check_source_code(self) -> str:
        return '#include <stddef.h>\nclass breakCCompiler;int main(void) { return 0; }\n'

    def get_compiler_check_args(self, mode: CompileCheckMode) -> T.List[str]:
        # -fpermissive allows non-conforming code to compile which is necessary
        # for many C++ checks. Particularly, the has_header_symbol check is
        # too strict without this and always fails.
        return super().get_compiler_check_args(mode) + ['-fpermissive']

    def has_header_symbol(self, hname: str, symbol: str, prefix: str, *,
                          extra_args: T.Union[None, T.List[str], T.Callable[[CompileCheckMode], T.List[str]]] = None,
                          dependencies: T.Optional[T.List['Dependency']] = None) -> T.Tuple[bool, bool]:
        # Check if it's a C-like symbol
        found, cached = super().has_header_symbol(hname, symbol, prefix,
                                                  extra_args=extra_args,
                                                  dependencies=dependencies)
        if found:
            return True, cached
        # Check if it's a class or a template
        if extra_args is None:
            extra_args = []
        t = f'''{prefix}
        #include <{hname}>
        using {symbol};
        int main(void) {{ return 0; }}'''
        return self.compiles(t, extra_args=extra_args,
                             dependencies=dependencies)

    def _test_cpp_std_arg(self, cpp_std_value: str) -> bool:
        # Test whether the compiler understands a -std=XY argument
        assert cpp_std_value.startswith('-std=')

        # This test does not use has_multi_arguments() for two reasons:
        # 1. has_multi_arguments() requires an env argument, which the compiler
        #    object does not have at this point.
        # 2. even if it did have an env object, that might contain another more
        #    recent -std= argument, which might lead to a cascaded failure.
        CPP_TEST = 'int i = static_cast<int>(0);'
        with self.compile(CPP_TEST, extra_args=[cpp_std_value], mode=CompileCheckMode.COMPILE) as p:
            if p.returncode == 0:
                mlog.debug(f'Compiler accepts {cpp_std_value}:', 'YES')
                return True
            else:
                mlog.debug(f'Compiler accepts {cpp_std_value}:', 'NO')
                return False

    @functools.lru_cache()
    def _find_best_cpp_std(self, cpp_std: str) -> str:
        # The initial version mapping approach to make falling back
        # from '-std=c++14' to '-std=c++1y' was too brittle. For instance,
        # Apple's Clang uses a different versioning scheme to upstream LLVM,
        # making the whole detection logic awfully brittle. Instead, let's
        # just see if feeding GCC or Clang our '-std=' setting works, and
        # if not, try the fallback argument.
        CPP_FALLBACKS = {
            'c++11': 'c++0x',
            'gnu++11': 'gnu++0x',
            'c++14': 'c++1y',
            'gnu++14': 'gnu++1y',
            'c++17': 'c++1z',
            'gnu++17': 'gnu++1z',
            'c++20': 'c++2a',
            'gnu++20': 'gnu++2a',
            'c++23': 'c++2b',
            'gnu++23': 'gnu++2b',
            'c++26': 'c++2c',
            'gnu++26': 'gnu++2c',
        }

        # Currently, remapping is only supported for Clang, Elbrus and GCC
        assert self.id in frozenset(['clang', 'lcc', 'gcc', 'emscripten', 'armltdclang', 'intel-llvm', 'nvidia_hpc', 'xc32-gcc'])

        if cpp_std not in CPP_FALLBACKS:
            # 'c++03' and 'c++98' don't have fallback types
            return '-std=' + cpp_std

        for i in (cpp_std, CPP_FALLBACKS[cpp_std]):
            cpp_std_value = '-std=' + i
            if self._test_cpp_std_arg(cpp_std_value):
                return cpp_std_value

        raise MesonException(f'C++ Compiler does not support -std={cpp_std}')

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()
        key = self.form_compileropt_key('std')
        opts.update({
            key: options.UserStdOption('cpp', ALL_STDS),
        })
        return opts


class _StdCPPLibMixin(CompilerMixinBase):

    """Detect whether to use libc++ or libstdc++."""

    @lazy_property
    def language_stdlib_provider(self) -> str:
        # https://stackoverflow.com/a/31658120
        header = 'version' if self.has_header('version', '')[0] else 'ciso646'
        is_libcxx = self.has_header_symbol(header, '_LIBCPP_VERSION', '')[0]
        lib = 'c++' if is_libcxx else 'stdc++'
        return lib

    @functools.lru_cache(None)
    def language_stdlib_only_link_flags(self) -> T.List[str]:
        """Detect the C++ stdlib and default search dirs

        As an optimization, this method will cache the value, to avoid building the same values over and over

        :raises MesonException: If a stdlib cannot be determined
        """

        # We need to apply the search prefix here, as these link arguments may
        # be passed to a different compiler with a different set of default
        # search paths, such as when using Clang for C/C++ and gfortran for
        # fortran.
        search_dirs = [f'-L{d}' for d in self.get_compiler_dirs('libraries')]

        lib = self.language_stdlib_provider
        if self.find_library(lib, []) is not None:
            return search_dirs + [f'-l{lib}']

        # TODO: maybe a bug exception?
        raise MesonException('Could not detect either libc++ or libstdc++ as your C++ stdlib implementation.')


class ClangCPPCompiler(_StdCPPLibMixin, ClangCPPStds, ClangCompiler, CPPCompiler):

    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 defines: T.Optional[T.Dict[str, str]] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        ClangCompiler.__init__(self, defines)
        default_warn_args = ['-Wall', '-Winvalid-pch']
        self.warn_args = {'0': [],
                          '1': default_warn_args,
                          '2': default_warn_args + ['-Wextra'],
                          '3': default_warn_args + ['-Wextra', '-Wpedantic'],
                          'everything': ['-Weverything']}

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()

        key = self.form_compileropt_key('eh')
        opts[key] = options.UserComboOption(
            self.make_option_name(key),
            'C++ exception handling type.',
            'default',
            choices=['none', 'default', 'a', 's', 'sc'])

        key = self.form_compileropt_key('rtti')
        opts[key] = options.UserBooleanOption(
            self.make_option_name(key),
            'Enable RTTI',
            True)

        key = self.form_compileropt_key('debugstl')
        opts[key] = options.UserBooleanOption(
            self.make_option_name(key),
            'STL debug mode',
            False)

        if self.info.is_windows() or self.info.is_cygwin():
            key = self.form_compileropt_key('winlibs')
            opts[key] = options.UserStringArrayOption(
                self.make_option_name(key),
                'Standard Win libraries to link against',
                gnu_winlibs)
        return opts

    def get_option_compile_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []

        rtti = self.get_compileropt_value('rtti', target, subproject)
        debugstl = self.get_compileropt_value('debugstl', target, subproject)
        eh = self.get_compileropt_value('eh', target, subproject)

        assert isinstance(rtti, bool)
        assert isinstance(eh, str)
        assert isinstance(debugstl, bool)

        non_msvc_eh_options(eh, args)

        if debugstl:
            args.append('-D_GLIBCXX_DEBUG=1')

            # We can't do _LIBCPP_DEBUG because it's unreliable unless libc++ was built with it too:
            # https://discourse.llvm.org/t/building-a-program-with-d-libcpp-debug-1-against-a-libc-that-is-not-itself-built-with-that-define/59176/3
            # Note that unlike _GLIBCXX_DEBUG, _MODE_DEBUG doesn't break ABI. It's just slow.
            if version_compare(self.version, '>=18'):
                args.append('-U_LIBCPP_HARDENING_MODE')
                args.append('-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_DEBUG')

        if not rtti:
            args.append('-fno-rtti')

        return args

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std != 'none':
            args.append(self._find_best_cpp_std(std))
        return args

    def get_option_link_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        if self.info.is_windows() or self.info.is_cygwin():
            # without a typedict mypy can't understand this.
            retval = self.get_compileropt_value('winlibs', target, subproject)
            assert isinstance(retval, list)
            libs = retval[:]
            for l in libs:
                assert isinstance(l, str)
            return libs
        return []

    def get_assert_args(self, disable: bool) -> T.List[str]:
        if disable:
            return ['-DNDEBUG']

        # Don't inject the macro if the compiler already has it pre-defined.
        for macro in ['_GLIBCXX_ASSERTIONS', '_LIBCPP_HARDENING_MODE', '_LIBCPP_ENABLE_ASSERTIONS']:
            if self.defines.get(macro) is not None:
                return []

        if self.language_stdlib_provider == 'stdc++':
            return ['-D_GLIBCXX_ASSERTIONS=1']

        return ['-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST']

    def get_pch_use_args(self, pch_dir: str, header: str) -> T.List[str]:
        args = super().get_pch_use_args(pch_dir, header)
        if version_compare(self.version, '>=11'):
            return ['-fpch-instantiate-templates'] + args
        return args

    def get_cpp_modules_args(self) -> T.List[str]:
        # Although -fmodules-ts is removed in LLVM 17, we keep this in for compatibility with old compilers.
        return ['-fmodules', '-fmodules-ts']

    @lazy_property
    def _clang_scan_deps(self) -> T.Optional[str]:
        """Path to a clang-scan-deps proven to support P1689, else None.

        Clang's module scanner is the separate clang-scan-deps binary, and its
        P1689 support is feature-tested by running it rather than inferred from
        a version number: the candidate next to this clang binary is preferred
        (the matching install), then PATH candidates in get_llvm_tool_names
        order. A candidate is accepted only if the exact invocation shape the
        scan rule uses -- -format=p1689 with -o and
        -resource-dir-recipe=invoke-compiler, wrapping this compiler --
        succeeds on an import plus a system #include (exercising the
        scanner's resource-directory resolution) and yields JSON with the
        P1689 'rules' shape, so acceptance implies the build-time contract
        works.
        """
        from .. import tooldetect
        candidates: T.List[str] = []
        exe = self.get_exelist(ccache=False)[0]
        exe_path = shutil.which(exe) or exe
        bindir = os.path.dirname(os.path.realpath(exe_path))
        candidates.append(os.path.join(bindir, 'clang-scan-deps'))
        for name in tooldetect.get_llvm_tool_names('clang-scan-deps'):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        tried: T.Set[str] = set()
        with tempfile.TemporaryDirectory() as tdir:
            src = os.path.join(tdir, 'probe.cpp')
            with open(src, 'w', encoding='utf-8') as f:
                f.write('#include <cstddef>\nimport probe;\n')
            ddi = os.path.join(tdir, 'probe.ddi')
            obj = os.path.join(tdir, 'probe.o')
            depfile = os.path.join(tdir, 'probe.d')
            for cand in candidates:
                cand = os.path.realpath(cand)
                if cand in tried or not os.path.isfile(cand):
                    continue
                tried.add(cand)
                cmd = [cand, '-format=p1689', '-resource-dir-recipe=invoke-compiler',
                       '-o', ddi, '--',
                       *self.get_exelist(ccache=False), '-std=c++20', '-c', src, '-o', obj,
                       '-MD', '-MF', depfile]
                try:
                    p, _, err = Popen_safe(cmd)
                except OSError:
                    continue
                if p.returncode != 0:
                    mlog.debug(f'clang-scan-deps P1689 probe failed for {cand}: {err}')
                    continue
                try:
                    with open(ddi, encoding='utf-8') as f:
                        result = json.load(f)
                except (OSError, ValueError):
                    continue
                if 'rules' in result:
                    mlog.debug(f'Using clang-scan-deps for C++ modules: {cand}')
                    return cand
        return None

    def supports_cpp_modules_p1689(self) -> bool:
        # Feature-tested, not version-gated: True iff a working clang-scan-deps
        # was found (see _clang_scan_deps).
        return self._clang_scan_deps is not None

    def cpp_module_family(self) -> T.Literal['none', 'gcc', 'clang', 'msvc']:
        # Inherited by every clang-derived compiler (intel-llvm, armltdclang,
        # emscripten, appleclang); clang-cl is not a subclass and stays 'none'.
        return 'clang'

    def get_module_scanner_exelist(self) -> T.List[str]:
        assert self._clang_scan_deps is not None, 'only valid when supports_cpp_modules_p1689()'
        return [self._clang_scan_deps]

    def get_module_cache_dir(self, class_subdir: T.Optional[str] = None) -> str:
        return 'pcm.cache' if class_subdir is None else f'pcm.cache/{class_subdir}'

    def get_module_bmi_suffix(self) -> str:
        return '.pcm'

    def supports_bmi_classes(self) -> bool:
        # clang-cl never reaches the P1689 pipeline, and AppleClang inherits
        # this deliberately. Consumers name each unit's BMI with -fmodule-file=
        # (see get_header_unit_consumer_args), so per-class units need no new
        # resolution machinery either.
        return True

    def get_module_compile_args(self, class_subdir: T.Optional[str] = None,
                                private_dir: T.Optional[str] = None,
                                private_output: bool = False) -> T.List[str]:
        # Imports resolve by name lookup in the shared cache (the target's
        # class subdir of it, when BMI classes are in play). Producers write
        # their BMI next to the object (-fmodule-output, added per interface
        # unit by the backend) and a harvest edge publishes it into the cache;
        # no module name or BMI path ever appears on a command line.
        #
        # private_dir, set whenever the target has any private module of its
        # own, is searched first: a private import resolves there, never in
        # the shared cache. The shared class cache is still listed, since the
        # target may also import its dependencies' public modules, or (for a
        # library) have public modules of its own. private_output is unused
        # here: unlike MSVC's directory-addressed /ifcOutput, Clang's own BMI
        # never has a compile-time output directory to steer -- the harvest
        # edge decides per-source, private or shared, where a Clang interface's
        # BMI is published (see the backend's Clang harvest call site).
        #
        # -fmodules -fno-modules exists to defeat ccache. ccache does not
        # track the contents of BMIs (they never appear in preprocessed
        # output), so it serves stale objects when an imported module or
        # header unit changes -- including via distro PATH masquerade such as
        # Fedora's /usr/lib64/ccache. It refuses to cache anything compiled
        # with -fmodules, which on GCC protects module builds as a side
        # effect; this pair buys Clang the same refusal. The pair is a no-op
        # to Clang itself: the driver cancels it before cc1. Do not pass
        # -fmodules alone -- that enables the unrelated implicit
        # Clang-header-modules feature. The backend drops the pair when the
        # user passed -fmodules themselves: their flag must stay in effect,
        # and it keeps ccache away on its own.
        paths = [private_dir] if private_dir is not None else []
        paths.append(self.get_module_cache_dir(class_subdir))
        return [f'-fprebuilt-module-path={p}' for p in paths] + ['-fmodules', '-fno-modules']

    def get_bmi_irrelevant_args(self) -> T.Tuple[T.FrozenSet[str], T.FrozenSet[str], T.FrozenSet[str], T.FrozenSet[str]]:
        # After xmake's speculative Clang strip list. Defines are deliberately
        # absent: a -D difference must split the BMI class. fPIC/fPIE are
        # listed because Meson injects them asymmetrically (library vs
        # executable). A user-passed -fmodules is intentionally NOT stripped:
        # implicit Clang header modules genuinely change the compile.
        #
        # Protected against the four family prefixes above (checked against
        # `clang --help-hidden`): -Wa,/-Wp,/-Wl, forward opaque, comma-
        # delimited content to another tool stage (-Wp, can carry a real -D)
        # and must not be swallowed by the bare 'W' meant for -Wall et al.;
        # -ObjC/-ObjC++ select a different source language, not an
        # optimization level, despite sharing 'O'; --gcc-toolchain= picks an
        # entirely different GCC install (headers/libs/macros), despite
        # sharing 'g' with -g/-gdwarf/...; -working-directory changes how
        # relative #includes resolve, despite sharing 'w' with -w.
        return (frozenset(),
                frozenset({'g', 'O', 'W', 'w', 'Q', 'fmodule-file', 'fPIC',
                           'fpic', 'fPIE', 'fpie', 'fsanitize', 'embed-dir'}),
                frozenset({'I', 'isystem', 'cxx-isystem', 'framework'}),
                frozenset({'Wa,', 'Wp,', 'Wl,', 'ObjC', 'gcc-toolchain',
                           'working-directory'}))

    def get_header_unit_consumer_args(self, mode: str, spelling: str, bmi_path: str) -> T.List[str]:
        # Clang has no directory lookup for header units (-fprebuilt-module-path
        # does not apply to them): a consumer must name each unit's BMI with a
        # bare -fmodule-file=<pcm>. No spelling on the flag -- the pcm records
        # the resolved header's identity and matching is by that identity; the
        # declared mode only selects -xc++-{user,system}-header at build time.
        return [f'-fmodule-file={bmi_path}']

    @functools.lru_cache(maxsize=None)
    def _std_module_info(self, extra_args: T.Tuple[str, ...]) -> T.Tuple[T.Dict[str, str], T.List[str]]:
        """(std module sources, extra -isystem dirs) for the selected stdlib.

        Unlike GCC, Clang serves two standard libraries, so which manifest to
        read is decided by probing which stdlib these compiles will actually
        use: preprocess a '#include <cstddef>' and look for the identifying
        macro the stdlib's config header defines. <cstddef> rather than
        <version> because any standard header carries the identification at
        every -std level, with no C++20 assumption -- this runs at configure
        time, before any cpp_std gate. The probe includes the build's
        configure-time cpp args (options + global + project), so a
        -stdlib=libc++ from any of them -- or from a clang config file or the
        platform default -- is honored. Per-target -stdlib divergence is not
        (and cannot be) supported: a BMI bakes in its stdlib.
        """
        with tempfile.TemporaryDirectory() as tdir:
            src = os.path.join(tdir, 'probe.cpp')
            with open(src, 'w', encoding='utf-8') as f:
                f.write('#include <cstddef>\n')
            cmd = self.get_exelist(ccache=False) + list(extra_args) + ['-x', 'c++', '-E', '-dM', src]
            try:
                p, out, _ = Popen_safe(cmd)
            except OSError:
                return {}, []
        if p.returncode != 0:
            return {}, []
        manifest_name = 'libc++.modules.json' if '_LIBCPP_VERSION' in out else 'libstdc++.modules.json'
        try:
            _, out, _ = Popen_safe(self.get_exelist(ccache=False) + list(extra_args)
                                   + [f'-print-file-name={manifest_name}'])
        except OSError:
            return {}, []
        manifest = out.strip()
        if not manifest or not os.path.isfile(manifest):
            return {}, []
        return _parse_std_module_manifest(manifest)

    def get_std_module_sources(self, extra_args: T.Tuple[str, ...] = ()) -> T.Dict[str, str]:
        """{logical-name: source path} for auto-provisioned stdlib modules."""
        return self._std_module_info(tuple(extra_args))[0]

    def get_std_module_extra_args(self, extra_args: T.Tuple[str, ...] = ()) -> T.List[str]:
        sources, incdirs = self._std_module_info(tuple(extra_args))
        if not sources:
            return []
        # The std interface sources declare the reserved module name on
        # purpose; libc++'s additionally need their own support headers on the
        # include path (the manifest's system-include-directories).
        args = ['-Wno-reserved-module-identifier']
        for d in incdirs:
            args += ['-isystem', d]
        return args


class ArmLtdClangCPPCompiler(ClangCPPCompiler):

    id = 'armltdclang'


class AppleClangCPPCompiler(AppleCompilerMixin, AppleCPPStdsMixin, ClangCPPCompiler):
    pass


class EmscriptenCPPCompiler(EmscriptenMixin, ClangCPPCompiler):

    id = 'emscripten'

    # Emscripten uses different version numbers than Clang; `emcc -v` will show
    # the Clang version number used as well (but `emcc --version` does not).
    # See https://github.com/pyodide/pyodide/discussions/4762 for more on
    # emcc <--> clang versions. Note, although earlier versions claim to be the
    # Clang versions 12.0.0 and 17.0.0 required for these C++ standards, they
    # only accept the flags in the later versions below.
    _CPP23_VERSION = '>=2.0.10'
    _CPP26_VERSION = '>=3.1.39'

    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 defines: T.Optional[T.Dict[str, str]] = None,
                 full_version: T.Optional[str] = None):
        if not env.is_cross_build(for_machine):
            raise MesonException('Emscripten compiler can only be used for cross compilation.')
        if not version_compare(version, '>=1.39.19'):
            raise MesonException('Meson requires Emscripten >= 1.39.19')
        ClangCPPCompiler.__init__(self, ccache, exelist, version, for_machine, env,
                                  linker=linker, defines=defines, full_version=full_version)

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std != 'none':
            args.append(self._find_best_cpp_std(std))
        return args


class ArmclangCPPCompiler(ArmclangCompiler, CPPCompiler):
    '''
    Keil armclang
    '''

    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        ArmclangCompiler.__init__(self)
        default_warn_args = ['-Wall', '-Winvalid-pch']
        self.warn_args = {'0': [],
                          '1': default_warn_args,
                          '2': default_warn_args + ['-Wextra'],
                          '3': default_warn_args + ['-Wextra', '-Wpedantic'],
                          'everything': ['-Weverything']}

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()

        key = self.form_compileropt_key('eh')
        opts[key] = options.UserComboOption(
            self.make_option_name(key),
            'C++ exception handling type.',
            'default',
            choices=['none', 'default', 'a', 's', 'sc'])

        key = self.form_compileropt_key('std')
        std_opt = opts[key]
        assert isinstance(std_opt, options.UserStdOption), 'for mypy'
        std_opt.set_versions(['c++98', 'c++03', 'c++11', 'c++14', 'c++17'], gnu=True)
        return opts

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std != 'none':
            args.append('-std=' + std)

        eh = self.get_compileropt_value('eh', target, subproject)
        assert isinstance(eh, str)
        non_msvc_eh_options(eh, args)

        return args

    def get_option_link_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        return []


class GnuCPPCompiler(_StdCPPLibMixin, GnuCPPStds, GnuCompiler, CPPCompiler):
    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 defines: T.Optional[T.Dict[str, str]] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        GnuCompiler.__init__(self, defines)
        default_warn_args = ['-Wall', '-Winvalid-pch']
        self.warn_args = {'0': [],
                          '1': default_warn_args,
                          '2': default_warn_args + ['-Wextra'],
                          '3': default_warn_args + ['-Wextra', '-Wpedantic'],
                          'everything': (default_warn_args + ['-Wextra', '-Wpedantic'] +
                                         self.supported_warn_args(gnu_common_warning_args) +
                                         self.supported_warn_args(gnu_cpp_warning_args))}

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()

        key = self.form_compileropt_key('eh')
        opts[key] = options.UserComboOption(
            self.make_option_name(key),
            'C++ exception handling type.',
            'default',
            choices=['none', 'default', 'a', 's', 'sc'])

        key = self.form_compileropt_key('rtti')
        opts[key] = options.UserBooleanOption(
            self.make_option_name(key),
            'Enable RTTI',
            True)

        key = self.form_compileropt_key('debugstl')
        opts[key] = options.UserBooleanOption(
            self.make_option_name(key),
            'STL debug mode',
            False)

        if self.info.is_windows() or self.info.is_cygwin():
            key = key.evolve(name='cpp_winlibs')
            opts[key] = options.UserStringArrayOption(
                self.make_option_name(key),
                'Standard Win libraries to link against',
                gnu_winlibs)

        if version_compare(self.version, '>=15.1'):
            key = key.evolve(name='cpp_importstd')
            opts[key] = options.UserComboOption(self.make_option_name(key),
                                                'Use #import std.',
                                                'false',
                                                choices=['false', 'true'])

        return opts

    def get_option_compile_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []

        rtti = self.get_compileropt_value('rtti', target, subproject)
        debugstl = self.get_compileropt_value('debugstl', target, subproject)
        eh = self.get_compileropt_value('eh', target, subproject)

        assert isinstance(rtti, bool)
        assert isinstance(eh, str)
        assert isinstance(debugstl, bool)

        non_msvc_eh_options(eh, args)

        if not rtti:
            args.append('-fno-rtti')

        # We may want to handle libc++'s debugstl mode here too
        if debugstl:
            args.append('-D_GLIBCXX_DEBUG=1')
        return args

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std != 'none':
            args.append(self._find_best_cpp_std(std))
        return args

    def get_option_link_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        if self.info.is_windows() or self.info.is_cygwin():
            # without a typedict mypy can't understand this.
            retval = self.get_compileropt_value('winlibs', target, subproject)
            assert isinstance(retval, list)
            libs: T.List[str] = retval[:]
            for l in libs:
                assert isinstance(l, str)
            return libs
        return []

    def get_assert_args(self, disable: bool) -> T.List[str]:
        if disable:
            return ['-DNDEBUG']

        # Don't inject the macro if the compiler already has it pre-defined.
        for macro in ['_GLIBCXX_ASSERTIONS', '_LIBCPP_HARDENING_MODE', '_LIBCPP_ENABLE_ASSERTIONS']:
            if self.defines.get(macro) is not None:
                return []

        # For GCC, we can assume that the libstdc++ version is the same as
        # the compiler itself. Anything else isn't supported.
        if self.language_stdlib_provider == 'stdc++':
            return ['-D_GLIBCXX_ASSERTIONS=1']
        else:
            # One can use -stdlib=libc++ with GCC, it just (as of 2025) requires
            # an experimental configure arg to expose that. libc++ supports "multiple"
            # versions of GCC (only ever one version of GCC per libc++ version), but
            # that is "multiple" for our purposes as we can't assume a mapping.
            if version_compare(self.version, '>=18'):
                return ['-D_LIBCPP_HARDENING_MODE=_LIBCPP_HARDENING_MODE_FAST']

        return []

    def get_pch_use_args(self, pch_dir: str, header: str) -> T.List[str]:
        return ['-fpch-preprocess', '-include', os.path.basename(header)]

    def get_cpp_modules_args(self) -> T.List[str]:
        return ['-fmodules', '-fmodules-ts']

    def supports_cpp_modules_p1689(self) -> bool:
        # GCC gained -fdeps-format=p1689r5 (the scanner the pipeline needs) in
        # GCC 14.
        return version_compare(self.version, '>=14')

    def cpp_module_family(self) -> T.Literal['none', 'gcc', 'clang', 'msvc']:
        return 'gcc'

    def get_module_cache_dir(self, class_subdir: T.Optional[str] = None) -> str:
        return 'gcm.cache' if class_subdir is None else f'gcm.cache/{class_subdir}'

    def get_module_bmi_suffix(self) -> str:
        return '.gcm'

    def _named_modules_flag(self) -> str:
        # GCC 15 renamed -fmodules-ts to -fmodules and deprecated the old
        # spelling; GCC 14, the first P1689 release, accepts only
        # -fmodules-ts.
        return '-fmodules' if version_compare(self.version, '>=15') else '-fmodules-ts'

    def get_module_compile_args(self, class_subdir: T.Optional[str] = None,
                                private_dir: T.Optional[str] = None,
                                private_output: bool = False) -> T.List[str]:
        # The modules flag enables named modules. -Mno-modules stops GCC from
        # writing its make-style module dependency rules (phony
        # '<name>.c++-module' targets and an order-only
        # 'gcm.cache/<name>.gcm:| <obj>' line) into the -MD depfile: Ninja's
        # gcc-deps parser cannot handle that shape, and module ordering is
        # carried by the dyndep instead. BMI generation is unaffected.
        # class_subdir/private_dir/private_output are all unused: GCC carries
        # no cache dir on the command line. Every BMI a compile resolves, in
        # any class or a target-private directory, is named by the per-TU
        # module mapper instead (get_module_mapper_args), whose contents the
        # collator resolves (--private-bmi-dir/--private-interface-object at the
        # collator, in lockstep with this target's own privacy), which the
        # backend adds to compile edges only -- a scan resolves no named
        # modules, and header units stay in the default-named cache the
        # mapper-less scan already finds.
        return [self._named_modules_flag(), '-Mno-modules']

    def get_module_mapper_args(self, mapper_path: str) -> T.List[str]:
        # The only way to steer GCC's BMI lookup: gcm.cache is resolved
        # relative to the working directory, and there is no search-path
        # flag. The mapper file must enumerate the TU's provides and direct
        # imports (a mapping file disables the default module->CMI naming
        # and has no wildcard form), so its contents are scan-derived and
        # written by the collate; only this static path is on the command
        # line.
        return [f'-fmodule-mapper={mapper_path}']

    def supports_bmi_classes(self) -> bool:
        # Divergent classes work through the per-TU module mapper. Only
        # consulted on the P1689 path (GCC >= 14); -fmodule-mapper is far
        # older than that. The mapper names a header unit's CMI too, so a unit
        # can stand at a per-class path with each consumer sent to its own
        # class's, and no BMI path on any command line: the backend computes
        # what GCC calls a unit (the header as resolved on the include path) to
        # write those lines.
        return True

    def supports_pch_with_cpp_modules(self) -> bool:
        # Mutually exclusive: any -fmodules compile rejects a .gch built
        # without -fmodules as invalid, and building the .gch with -fmodules
        # is impossible (-x c++-header then emits a header-unit CMI and no
        # .gch at all). GCC's position is that header units subsume PCH.
        return False

    def get_bmi_irrelevant_args(self) -> T.Tuple[T.FrozenSet[str], T.FrozenSet[str], T.FrozenSet[str], T.FrozenSet[str]]:
        # After xmake's speculative GCC strip list. Defines are deliberately
        # absent: a -D difference must split the BMI class. fPIC/fPIE are
        # listed because Meson injects them asymmetrically (library vs
        # executable); -Mno-modules shapes only the depfile, never the BMI.
        #
        # Protected against the two family prefixes above (checked against
        # `man gcc`): -Wa,/-Wp,/-Wl, forward opaque, comma-delimited content
        # to another tool stage (-Wp, can carry a real -D) and must not be
        # swallowed by the bare 'W' meant for -Wall et al.; -ObjC/-ObjC++
        # select a different source language, not an optimization level,
        # despite sharing 'O'; -wrapper forwards an opaque, comma-separated
        # program+args list (same shape as -Wa,/-Wp,/-Wl,), despite sharing
        # 'w' with -w.
        return (frozenset(),
                frozenset({'O', 'W', 'w', 'Q', 'fmodule-mapper', 'fmodules-ts',
                           'fmodules', 'fPIC', 'fpic', 'fPIE', 'fpie',
                           'fsanitize', 'embed-dir', 'Mno-modules'}),
                frozenset({'I', 'isystem', 'cxx-isystem', 'framework'}),
                frozenset({'Wa,', 'Wp,', 'Wl,', 'ObjC', 'wrapper'}))

    def get_module_scanner_args(self, outfile: str, target: str, depfile: str) -> T.List[str]:
        # A P1689 scan runs before any BMI exists, so it must not compile:
        # preprocess only (-E, output discarded to a .pp beside the .ddi).
        # It must be -E -MD, not a plain -M make-rule pass: GCC 14 only
        # populates the -fdeps-* P1689 provides when really preprocessing
        # ("module dependencies require preprocessing"). The modules flag
        # matches the compile so the scan sees the same dialect; -Mno-modules
        # keeps the header depfile plain (module info goes to the .ddi).
        return [self._named_modules_flag(), '-Mno-modules', '-fdeps-format=p1689r5',
                f'-fdeps-file={outfile}', f'-fdeps-target={target}',
                '-E', '-MD', '-MQ', target, '-MF', depfile, '-o', f'{outfile}.pp']

    @lazy_property
    def _std_module_sources(self) -> T.Dict[str, str]:
        # Locate the standard library's module-interface sources from the
        # selected libstdc++'s manifest. The manifest maps std ->
        # bits/std.cc and std.compat -> bits/std.compat.cc, each flagged
        # is-std-library; source paths are relative to the manifest. Empty when
        # the toolchain is too old to ship the std module (GCC < 15) or has no
        # manifest.
        if version_compare(self.version, '<15'):
            return {}
        try:
            _, out, _ = Popen_safe(
                self.get_exelist(ccache=False) + ['-print-file-name=libstdc++.modules.json'])
        except OSError:
            return {}
        manifest = out.strip()
        if not manifest or not os.path.isfile(manifest):
            return {}
        return _parse_std_module_manifest(manifest)[0]

    def get_std_module_sources(self, extra_args: T.Tuple[str, ...] = ()) -> T.Dict[str, str]:
        """{logical-name: source path} for auto-provisioned stdlib modules."""
        return self._std_module_sources


class PGICPPCompiler(PGICompiler, CPPCompiler):
    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        PGICompiler.__init__(self)


class NvidiaHPC_CPPCompiler(PGICompiler, CPPCompiler):

    id = 'nvidia_hpc'

    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        PGICompiler.__init__(self)

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()
        cppstd_choices = [
            'c++98', 'c++03', 'c++11', 'c++14', 'c++17', 'c++20', 'c++23',
            'gnu++98', 'gnu++03', 'gnu++11', 'gnu++14', 'gnu++17', 'gnu++20'
        ]
        std_opt = opts[self.form_compileropt_key('std')]
        assert isinstance(std_opt, options.UserStdOption), 'for mypy'
        std_opt.set_versions(cppstd_choices)
        return opts

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std != 'none':
            args.append(self._find_best_cpp_std(std))
        return args


class ElbrusCPPCompiler(ElbrusCompiler, CPPCompiler):
    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 defines: T.Optional[T.Dict[str, str]] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        ElbrusCompiler.__init__(self)

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()

        key = self.form_compileropt_key('eh')
        opts[key] = options.UserComboOption(
            self.make_option_name(key),
            'C++ exception handling type.',
            'default',
            choices=['none', 'default', 'a', 's', 'sc'])

        key = self.form_compileropt_key('debugstl')
        opts[key] = options.UserBooleanOption(
            self.make_option_name(key),
            'STL debug mode',
            False)

        cpp_stds = ['c++98']
        if version_compare(self.version, '>=1.20.00'):
            cpp_stds += ['c++03', 'c++0x', 'c++11']
        if version_compare(self.version, '>=1.21.00') and version_compare(self.version, '<1.22.00'):
            cpp_stds += ['c++14', 'c++1y']
        if version_compare(self.version, '>=1.22.00'):
            cpp_stds += ['c++14']
        if version_compare(self.version, '>=1.23.00'):
            cpp_stds += ['c++1y']
        if version_compare(self.version, '>=1.24.00'):
            cpp_stds += ['c++1z', 'c++17']
        if version_compare(self.version, '>=1.25.00'):
            cpp_stds += ['c++2a']
        if version_compare(self.version, '>=1.26.00'):
            cpp_stds += ['c++20']
        if version_compare(self.version, '>=1.28.00'):
            cpp_stds += ['c++2b', 'c++23']

        key = self.form_compileropt_key('std')
        std_opt = opts[key]
        assert isinstance(std_opt, options.UserStdOption), 'for mypy'
        std_opt.set_versions(cpp_stds, gnu=True)
        return opts

    # Elbrus C++ compiler does not have lchmod, but there is only linker warning, not compiler error.
    # So we should explicitly fail at this case.
    def has_function(self, funcname: str, prefix: str, *,
                     extra_args: T.Optional[T.List[str]] = None,
                     dependencies: T.Optional[T.List['Dependency']] = None) -> T.Tuple[bool, bool]:
        if funcname == 'lchmod':
            return False, False
        return super().has_function(funcname, prefix, extra_args=extra_args, dependencies=dependencies)

    # Elbrus C++ compiler does not support RTTI, so don't check for it.
    def get_option_compile_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        eh = self.get_compileropt_value('eh', target, subproject)
        assert isinstance(eh, str)

        non_msvc_eh_options(eh, args)

        debugstl = self.get_compileropt_value('debugstl', target, subproject)
        assert isinstance(debugstl, bool)
        if debugstl:
            args.append('-D_GLIBCXX_DEBUG=1')
        return args

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std != 'none':
            args.append(self._find_best_cpp_std(std))
        return args


class IntelCPPCompiler(IntelGnuLikeCompiler, CPPCompiler):
    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        IntelGnuLikeCompiler.__init__(self)
        self.lang_header = 'c++-header'
        default_warn_args = ['-Wall', '-w3', '-Wpch-messages']
        self.warn_args = {'0': [],
                          '1': default_warn_args + ['-diag-disable:remark'],
                          '2': default_warn_args + ['-Wextra', '-diag-disable:remark'],
                          '3': default_warn_args + ['-Wextra', '-diag-disable:remark'],
                          'everything': default_warn_args + ['-Wextra']}

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()

        key = self.form_compileropt_key('eh')
        opts[key] = options.UserComboOption(
            self.make_option_name(key),
            'C++ exception handling type.',
            'default',
            choices=['none', 'default', 'a', 's', 'sc'])

        key = self.form_compileropt_key('rtti')
        opts[key] = options.UserBooleanOption(
            self.make_option_name(key),
            'Enable RTTI',
            True)

        key = self.form_compileropt_key('debugstl')
        opts[key] = options.UserBooleanOption(
            self.make_option_name(key),
            'STL debug mode',
            False)

        # Every Unix compiler under the sun seems to accept -std=c++03,
        # with the exception of ICC. Instead of preventing the user from
        # globally requesting C++03, we transparently remap it to C++98
        c_stds = ['c++98', 'c++03']
        g_stds = ['gnu++98', 'gnu++03']
        if version_compare(self.version, '>=15.0.0'):
            c_stds += ['c++11', 'c++14']
            g_stds += ['gnu++11']
        if version_compare(self.version, '>=16.0.0'):
            c_stds += ['c++17']
        if version_compare(self.version, '>=17.0.0'):
            g_stds += ['gnu++14']
        if version_compare(self.version, '>=19.1.0'):
            c_stds += ['c++2a']
            g_stds += ['gnu++2a']

        self._update_language_stds(opts, c_stds + g_stds)
        return opts

    def get_option_compile_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []

        rtti = self.get_compileropt_value('rtti', target, subproject)
        debugstl = self.get_compileropt_value('debugstl', target, subproject)
        eh = self.get_compileropt_value('eh', target, subproject)

        assert isinstance(rtti, bool)
        assert isinstance(eh, str)
        assert isinstance(debugstl, bool)

        if eh == 'none':
            args.append('-fno-exceptions')
        if not rtti:
            args.append('-fno-rtti')
        if debugstl:
            args.append('-D_GLIBCXX_DEBUG=1')
        return args

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std != 'none':
            remap_cpp03 = {
                'c++03': 'c++98',
                'gnu++03': 'gnu++98'
            }
            args.append('-std=' + remap_cpp03.get(std, std))

        return args

    def get_option_link_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        return []


class IntelLLVMCPPCompiler(IntelLLVMLikeCompiler, ClangCPPCompiler):

    id = 'intel-llvm'


class VisualStudioLikeCPPCompilerMixin(CompilerMixinBase):

    """Mixin for C++ specific method overrides in MSVC-like compilers."""

    VC_VERSION_MAP = {
        'none': (True, None),
        'vc++11': (True, 11),
        'vc++14': (True, 14),
        'vc++17': (True, 17),
        'vc++20': (True, 20),
        'vc++latest': (True, "latest"),
        'c++11': (False, 11),
        'c++14': (False, 14),
        'c++17': (False, 17),
        'c++20': (False, 20),
        'c++latest': (False, "latest"),
    }

    def get_option_link_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        # need a typeddict for this
        key = self.form_compileropt_key('winlibs').evolve(subproject=subproject)
        if target:
            value = self.environment.coredata.get_option_for_target(target, key)
        else:
            value = self.environment.coredata.optstore.get_value_for(key)
        return T.cast('T.List[str]', value)[:]

    def _get_options_impl(self, opts: 'MutableKeyedOptionDictType', cpp_stds: T.List[str]) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()

        key = self.form_compileropt_key('eh')
        opts[key] = options.UserComboOption(
            self.make_option_name(key),
            'C++ exception handling type.',
            'default',
            choices=['none', 'default', 'a', 's', 'sc'])

        key = self.form_compileropt_key('rtti')
        opts[key] = options.UserBooleanOption(
            self.make_option_name(key),
            'Enable RTTI',
            True)

        key = self.form_compileropt_key('winlibs')
        opts[key] = options.UserStringArrayOption(
            self.make_option_name(key),
            'Standard Win libraries to link against',
            msvc_winlibs)

        std_opt = opts[self.form_compileropt_key('std')]
        assert isinstance(std_opt, options.UserStdOption), 'for mypy'
        std_opt.set_versions(cpp_stds)

        if version_compare(self.version, '>=19.44.35219'):
            key = self.form_compileropt_key('importstd')
            opts[key] = options.UserComboOption(self.make_option_name(key),
                                                'Use #import std.',
                                                'false',
                                                choices=['false', 'true'])
        return opts

    def get_option_compile_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []

        eh = self.get_compileropt_value('eh', target, subproject)
        rtti = self.get_compileropt_value('rtti', target, subproject)

        assert isinstance(rtti, bool)
        assert isinstance(eh, str)

        if eh == 'default':
            args.append('/EHsc')
        elif eh == 'none':
            args.append('/EHs-c-')
        else:
            args.append('/EH' + eh)

        if not rtti:
            args.append('/GR-')

        return args

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)

        permissive, ver = self.VC_VERSION_MAP[std]
        if ver is not None:
            args.append(f'/std:c++{ver}')
        if not permissive:
            args.append('/permissive-')
        return args

    def get_compiler_check_args(self, mode: CompileCheckMode) -> T.List[str]:
        # XXX: this is a hack because so much GnuLike stuff is in the base CPPCompiler class.
        return Compiler.get_compiler_check_args(self, mode)

class CPP11AsCPP14Mixin(CompilerMixinBase):

    """Mixin class for VisualStudio and ClangCl to replace C++11 std with C++14.

    This is a limitation of Clang and MSVC that ICL doesn't share.
    """

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        # Note: there is no explicit flag for supporting C++11; we attempt to do the best we can
        # which means setting the C++ standard version to C++14, in compilers that support it
        # (i.e., after VS2015U3)
        # if one is using anything before that point, one cannot set the standard.
        stdkey = self.form_compileropt_key('std').evolve(subproject=subproject)
        if target is not None:
            std = self.environment.coredata.get_option_for_target(target, stdkey)
        else:
            std = self.environment.coredata.optstore.get_value_for(stdkey)
        if std in {'vc++11', 'c++11'}:
            mlog.warning(self.id, 'does not support C++11;',
                         'attempting best effort; setting the standard to C++14',
                         once=True, fatal=False)
        original_args = super().get_option_std_args(target, subproject)
        std_mapping = {'/std:c++11': '/std:c++14'}
        processed_args = [std_mapping.get(x, x) for x in original_args]
        return processed_args


class VisualStudioCPPCompiler(CPP11AsCPP14Mixin, VisualStudioLikeCPPCompilerMixin, MSVCCompiler, CPPCompiler):

    id = 'msvc'

    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, target: str,
                 linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        MSVCCompiler.__init__(self, target)

        # By default, MSVC has a broken __cplusplus define that pretends to be c++98:
        # https://docs.microsoft.com/en-us/cpp/build/reference/zc-cplusplus?view=msvc-160
        # Pass the flag to enable a truthful define, if possible.
        if version_compare(self.version, '>= 19.14.26428'):
            self.always_args = self.always_args + ['/Zc:__cplusplus']

    def get_options(self) -> 'MutableKeyedOptionDictType':
        cpp_stds = ['none', 'c++11', 'vc++11']
        # Visual Studio 2015 and later
        if version_compare(self.version, '>=19'):
            cpp_stds.extend(['c++14', 'c++latest', 'vc++latest'])
        # Visual Studio 2017 and later
        if version_compare(self.version, '>=19.11'):
            cpp_stds.extend(['vc++14', 'c++17', 'vc++17'])
        if version_compare(self.version, '>=19.29'):
            cpp_stds.extend(['c++20', 'vc++20'])
        return self._get_options_impl(super().get_options(), cpp_stds)

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        std = self.get_compileropt_value('std', target, subproject)
        if std != 'none' and version_compare(self.version, '<19.00.24210'):
            mlog.warning('This version of MSVC does not support cpp_std arguments', fatal=False)

        args = super().get_option_std_args(target, subproject)

        if version_compare(self.version, '<19.11'):
            try:
                i = args.index('/permissive-')
            except ValueError:
                return args
            del args[i]
        return args

    def get_cpp_modules_args(self) -> T.List[str]:
        return ['/interface']

    def supports_cpp_modules_p1689(self) -> bool:
        # cl ships /scanDependencies (the P1689 scanner the pipeline needs)
        # from VS 2022 17.2, i.e. cl 19.32.
        return version_compare(self.version, '>=19.32')

    def cpp_module_family(self) -> T.Literal['none', 'gcc', 'clang', 'msvc']:
        return 'msvc'

    def get_module_cache_dir(self, class_subdir: T.Optional[str] = None) -> str:
        return 'ifc.cache' if class_subdir is None else f'ifc.cache/{class_subdir}'

    def get_module_bmi_suffix(self) -> str:
        return '.ifc'

    def get_module_compile_args(self, class_subdir: T.Optional[str] = None,
                                private_dir: T.Optional[str] = None,
                                private_output: bool = False) -> T.List[str]:
        # Read and write BMIs by directory, shared by every compile. /interface
        # is per interface-unit and is added at the compile site, not here. The
        # trailing slash marks /ifcOutput as a directory; a forward slash avoids
        # backslash-escaping in the generated ninja file (cl accepts either).
        #
        # private_dir, set whenever the target has any private module of its
        # own, is always an extra search dir, alongside the shared class
        # cache: a private import resolves there, and the target may also
        # import public modules (its own, or a dependency's) from the shared
        # cache. /ifcOutput, cl's single write destination for this compile,
        # is the one place the two cannot both be listed -- private_output
        # picks which of the two this specific compile's BMI (if it produces
        # one) is written to. A wholly-private executable (Stage 7) always
        # passes private_output=True for its own compiles; a library mixing
        # public and private interfaces passes it per source.
        cache = self.get_module_cache_dir(class_subdir)
        if private_dir is not None:
            out = private_dir if private_output else cache
            args = ['/ifcSearchDir', private_dir, '/ifcSearchDir', cache]
        else:
            out = cache
            args = ['/ifcSearchDir', out]
        args += ['/ifcOutput', f'{out}/']
        return args

    def get_bmi_irrelevant_args(self) -> T.Tuple[T.FrozenSet[str], T.FrozenSet[str], T.FrozenSet[str], T.FrozenSet[str]]:
        # After xmake's speculative MSVC strip list. Defines are deliberately
        # absent (a -D difference must split the BMI class) and so is /O
        # (conservative: optimization divergence splits here). 'isystem' is
        # listed because Meson emits system includes as unix-form two-token
        # ['-isystem', dir] until native conversion at write time. /EH* (which
        # cpp_eh controls) is deliberately NOT stripped: it changes
        # _CPPUNWIND, an ABI-relevant macro that can be baked into
        # constexpr-evaluated BMI content, so a divergence there must split
        # the class -- the key is not advisory here (supports_bmi_classes()
        # is unconditionally True for this compiler). 'E' therefore lives in
        # the exact set below, not prefix: as a prefix it used to also match
        # /EHsc and /EHs-c-, silently merging BMIs built under different
        # exception-handling models. No protected prefixes here: cl has no
        # GCC-style comma-forwarding convention, and the family prefixes
        # below have not been audited against the cl docs the way GCC/Clang
        # were (see mesonbuild/compilers/compilers.py's get_bmi_irrelevant_args
        # docstring).
        #
        # cl spells several of its argument-taking flags with a suffix and
        # still takes the argument as a separate token (/headerUnit:quote
        # NAME=IFC, /headerName:angle vector, /external:I DIR), so each such
        # spelling is enumerated as consuming, not just the bare flag. The
        # F-family (/Fd, /Fp, ...) stays a prefix: its argument is attached
        # and optional, and a consuming entry would swallow the next flag
        # after a bare /Fd. 'external' likewise stays a prefix for the
        # /external:W0, /external:anglebrackets, /external:env:V family, whose
        # members take no detached argument.
        return (frozenset({'E', 'EP', 'TP', 'nologo', 'internalPartition',
                           'interface', 'help', 'exportHeader', 'C', '?'}),
                frozenset({'errorReport', 'W', 'w', 'PD', 'MP',
                           'Fp', 'Fm', 'Fe', 'Fd', 'FC',
                           'doc', 'diagnostics', 'cgthreads', 'analyze',
                           'external', 'fsanitize'}),
                frozenset({'Fo', 'I', 'reference', 'isystem', 'ifcSearchDir',
                           'ifcOutput', 'sourceDependencies', 'scanDependencies',
                           'headerUnit', 'headerUnit:quote', 'headerUnit:angle',
                           'headerName:quote', 'headerName:angle', 'external:I'}),
                frozenset())

    def get_module_scanner_args(self, outfile: str, target: str, depfile: str) -> T.List[str]:
        # P1689 scan. No /c: cl only scans and writes no object. /Fo sets
        # the scan's primary-output to the eventual object so the collator can
        # match it. Module info goes to outfile; there is no make-style depfile.
        return ['/scanDependencies', outfile, '/Fo' + target]

    @lazy_property
    def _std_module_sources(self) -> T.Dict[str, str]:
        # cl ships the standard library's module-interface sources under
        # %VCToolsInstallDir%\modules with a modules.json manifest listing their
        # filenames; the logical name is the filename without its .ixx suffix
        # (std.ixx -> std, std.compat.ixx -> std.compat). Empty when the toolset
        # is too old to ship the manifest.
        root = os.environ.get('VCToolsInstallDir')
        if not root:
            return {}
        moddir = os.path.join(root, 'modules')
        manifest = os.path.join(moddir, 'modules.json')
        if not os.path.isfile(manifest):
            return {}
        try:
            with open(manifest, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        result: T.Dict[str, str] = {}
        for src in data.get('module-sources', []):
            path = os.path.normpath(os.path.join(moddir, src))
            if os.path.isfile(path):
                name = os.path.basename(src)
                if name.endswith('.ixx'):
                    name = name[:-len('.ixx')]
                result[name] = path
        return result

    def get_std_module_sources(self, extra_args: T.Tuple[str, ...] = ()) -> T.Dict[str, str]:
        """{logical-name: source path} for auto-provisioned stdlib modules."""
        return self._std_module_sources

    def get_header_unit_consumer_args(self, mode: str, spelling: str, bmi_path: str) -> T.List[str]:
        # cl has no directory lookup for header units: a consumer must name each
        # unit's BMI explicitly as <spelling>=<ifc>. Quote vs angle spelling
        # selects /headerUnit:quote or :angle, matching the import syntax.
        flag = '/headerUnit:quote' if mode == 'user' else '/headerUnit:angle'
        return [flag, f'{spelling}={bmi_path}']

    def supports_bmi_classes(self) -> bool:
        # Only consulted inside the P1689 pipeline (cl >= 19.32), where
        # /ifcSearchDir and /ifcOnly are always available. clang-cl never
        # reaches P1689. Header units use the same explicit-path resolution as
        # clang (/headerUnit above), so the per-class unit path works unchanged.
        return True

class ClangClCPPCompiler(VisualStudioLikeCPPCompilerMixin, ClangClCompiler, CPPCompiler):

    id = 'clang-cl'

    def __init__(self, exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, target: str,
                 linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, [], exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        ClangClCompiler.__init__(self, target)

    def get_options(self) -> 'MutableKeyedOptionDictType':
        cpp_stds = list(self.VC_VERSION_MAP) + ['c++23', 'vc++23']
        return self._get_options_impl(super().get_options(), cpp_stds)

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std == 'none':
            return []
        # c++latest and vc++latest have no /clang:-std= equivalent
        if std in {'c++latest', 'vc++latest'}:
            args = ['/std:c++latest']
            if std == 'c++latest':
                args.append('/permissive-')
            return args
        # vc++ variants: permissive mode, strip 'vc++' prefix to get clang std name
        if std.startswith('vc++'):
            return [f'/clang:-std=c++{std[4:]}']
        # c++ variants: strict conformance mode
        return [f'/clang:-std={std}', '/permissive-']

    def get_cpp_modules_args(self) -> T.List[str]:
        # clang-cl does not support /interface.
        return ['-fmodules', '-fmodules-ts']


class IntelClCPPCompiler(VisualStudioLikeCPPCompilerMixin, IntelVisualStudioLikeCompiler, CPPCompiler):

    def __init__(self, exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, target: str,
                 linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, [], exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        IntelVisualStudioLikeCompiler.__init__(self, target)

    def get_options(self) -> 'MutableKeyedOptionDictType':
        # This has only been tested with version 19.0, 2021.2.1, 2024.4.2 and 2025.0.1
        if version_compare(self.version, '<2021.1.0'):
            cpp_stds = ['none', 'c++11', 'vc++11', 'c++14', 'vc++14', 'c++17', 'vc++17', 'c++latest']
        else:
            cpp_stds = ['none', 'c++14', 'c++17', 'c++latest']
        if version_compare(self.version, '>=2024.1.0'):
            cpp_stds += ['c++20']
        return self._get_options_impl(super().get_options(), cpp_stds)

    def get_compiler_check_args(self, mode: CompileCheckMode) -> T.List[str]:
        # XXX: this is a hack because so much GnuLike stuff is in the base CPPCompiler class.
        return IntelVisualStudioLikeCompiler.get_compiler_check_args(self, mode)


class IntelLLVMClCPPCompiler(IntelClCPPCompiler):

    id = 'intel-llvm-cl'


class ArmCPPCompiler(ArmCompiler, CPPCompiler):
    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        ArmCompiler.__init__(self)

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()
        std_opt = self.form_compileropt_key('std')
        assert isinstance(std_opt, options.UserStdOption), 'for mypy'
        std_opt.set_versions(['c++03', 'c++11'])
        return opts

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std == 'c++11':
            args.append('--cpp11')
        elif std == 'c++03':
            args.append('--cpp')
        return args

    def get_option_link_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        return []

    def get_compiler_check_args(self, mode: CompileCheckMode) -> T.List[str]:
        return []


class CcrxCPPCompiler(CcrxCompiler, CPPCompiler):
    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        CcrxCompiler.__init__(self)

    # Override CCompiler.get_always_args
    def get_always_args(self) -> T.List[str]:
        return ['-nologo', '-lang=cpp']

    def get_compile_only_args(self) -> T.List[str]:
        return []

    def get_output_args(self, outputname: str) -> T.List[str]:
        return [f'-output=obj={outputname}']

    def get_option_link_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        return []

    def get_compiler_check_args(self, mode: CompileCheckMode) -> T.List[str]:
        return []

class TICPPCompiler(TICompiler, CPPCompiler):
    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        TICompiler.__init__(self)

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()
        key = self.form_compileropt_key('std')
        std_opt = opts[key]
        assert isinstance(std_opt, options.UserStdOption), 'for mypy'
        std_opt.set_versions(['c++03'])
        return opts

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std != 'none':
            args.append('--' + std)
        return args

    def get_always_args(self) -> T.List[str]:
        return []

    def get_option_link_args(self, target: 'BuildTarget', subproject: T.Optional[str] = None) -> T.List[str]:
        return []

class C2000CPPCompiler(TICPPCompiler):
    # Required for backwards compat with projects created before ti-cgt support existed
    id = 'c2000'

class C6000CPPCompiler(TICPPCompiler):
    id = 'c6000'

class MetrowerksCPPCompilerARM(MetrowerksCompiler, CPPCompiler):
    id = 'mwccarm'

    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        MetrowerksCompiler.__init__(self)

    def get_instruction_set_args(self, instruction_set: str) -> T.Optional[T.List[str]]:
        return mwccarm_instruction_set_args.get(instruction_set, None)

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()
        self._update_language_stds(opts, [])
        return opts

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std != 'none':
            args.append('-lang')
            args.append(std)
        return args

class MetrowerksCPPCompilerEmbeddedPowerPC(MetrowerksCompiler, CPPCompiler):
    id = 'mwcceppc'

    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional['DynamicLinker'] = None,
                 full_version: T.Optional[str] = None):
        CPPCompiler.__init__(self, ccache, exelist, version, for_machine,
                             env, linker=linker, full_version=full_version)
        MetrowerksCompiler.__init__(self)

    def get_instruction_set_args(self, instruction_set: str) -> T.Optional[T.List[str]]:
        return mwcceppc_instruction_set_args.get(instruction_set, None)

    def get_options(self) -> 'MutableKeyedOptionDictType':
        opts = super().get_options()
        self._update_language_stds(opts, [])
        return opts

    def get_option_std_args(self, target: BuildTarget, subproject: T.Optional[str] = None) -> T.List[str]:
        args: T.List[str] = []
        std = self.get_compileropt_value('std', target, subproject)
        assert isinstance(std, str)
        if std != 'none':
            args.append('-lang ' + std)
        return args


class Xc32CPPCompiler(Xc32CPPStds, Xc32Compiler, GnuCPPCompiler):

    """Microchip XC32 C++ compiler."""

    def __init__(self, ccache: T.List[str], exelist: T.List[str], version: str, for_machine: MachineChoice,
                 env: Environment, linker: T.Optional[DynamicLinker] = None,
                 defines: T.Optional[T.Dict[str, str]] = None,
                 full_version: T.Optional[str] = None):
        GnuCPPCompiler.__init__(self, ccache, exelist, version, for_machine, env,
                                linker=linker, full_version=full_version, defines=defines)
        Xc32Compiler.__init__(self)

    def cpp_module_family(self) -> T.Literal['none', 'gcc', 'clang', 'msvc']:
        # XC32 is GCC-based but its module pipeline is untested; keep it out,
        # matching the id gate this classifier replaces.
        return 'none'
