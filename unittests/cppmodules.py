# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Meson development team

"""Capability-token framework for the C++ module unittests.

Every C++ module test is gated on the *capability tokens* its fixture needs,
probed once per (compiler id, version) from the detected host C++ compiler.
CI covers the compiler axis by running the suite once per toolchain (CXX set
per job); within one process the tokens subtract the tests the toolchain
cannot run, so no test hand-rolls compiler-version logic.

Token vocabulary (the API for the C++ module test series):

``modules``
    Named modules via the P1689 pipeline are usable: the compiler is one of
    gcc/clang/msvc and ``supports_cpp_modules_p1689()`` holds (GCC >= 14,
    cl >= 19.32, Clang with a feature-probed P1689-capable clang-scan-deps).
    The Ninja-backend requirement is enforced by the gate itself, since the
    backend is per-test-process state, not a compiler capability.
``partitions``, ``header_units``, ``module_interfaces``
    Module partitions, the ``cpp_header_units`` kwarg, and the
    ``cpp_module_interfaces`` kwarg. All currently coincide with ``modules``
    on every supported compiler; they are separate tokens so that a future
    toolchain that splits them only needs a probe change, not test edits.
``import_std``
    ``dependency('std')`` resolves: ``get_std_module_sources()`` finds a
    stdlib module manifest (GCC >= 15 libstdc++.modules.json, Clang with a
    libc++/libstdc++ manifest, an MSVC toolset modules.json) -- except GCC
    15.0-15.2, whose std module is too unreliable for the test suite.
``regex_scanner``
    The legacy regex fallback scanner (mesonbuild/scripts/depscan.py) can
    build a flat named module: the compiler compiles a probe interface unit
    and its importer with one of its bare modules flags
    (``get_cpp_modules_args()``). Probed by actually compiling, not by
    version: in practice GCC qualifies, Clang does not (an interface unit
    needs ``-x c++-module`` there, which the regex path never passes).
``bmionly``
    The compiler can emit a BMI with no object file (GCC ``-fmodule-only``,
    Clang ``--precompile``, cl ``/ifcOnly``). Probed by compiling a trivial
    interface unit and checking a BMI appears and no object does. The
    prerequisite for BMI-only variants.
``bmi_classes``
    Divergent BMI equivalence classes work rather than warn: Meson gives
    each class its own cache subdirectory and synthesizes BMI-only variants
    of shared module providers (``Compiler.supports_bmi_classes()``, plus
    the ``bmionly`` probe). Clang, MSVC from cl 19.32, and GCC (from 14,
    the P1689 floor; the class cache rides the per-TU module mapper there).
"""

from __future__ import annotations
import functools
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import typing as T
import unittest

from mesonbuild.backend.ninjabackend import HEADER_UNIT_ALIAS_ROOT, HEADER_UNIT_ALIAS_DIGEST_LEN
from mesonbuild.compilers import detect_cpp_compiler
from mesonbuild.mesonlib import MachineChoice, is_windows, version_compare, EnvironmentException
from run_tests import get_fake_env, Backend

if T.TYPE_CHECKING:
    from typing_extensions import ParamSpec

    from mesonbuild.compilers.cpp import CPPCompiler
    from .baseplatformtests import BasePlatformTests

    P = ParamSpec('P')
    R = T.TypeVar('R')

CPP_MODULE_CAPS = frozenset({
    'modules', 'partitions', 'header_units', 'module_interfaces',
    'import_std', 'regex_scanner', 'bmionly', 'bmi_classes',
})

_caps_cache: T.Dict[T.Tuple[str, str], T.FrozenSet[str]] = {}
_regex_flag_cache: T.Dict[T.Tuple[str, str], T.Optional[str]] = {}
_bmionly_cache: T.Dict[T.Tuple[str, str], bool] = {}


# Binaries that wear a compiler driver's name while being a caching front end
# for one. A path resolving to one of these names is not a compiler and its
# directory is not a toolchain root.
_COMPILER_CACHE_STEMS = frozenset({'ccache', 'distcc', 'sccache', 'buildcache'})


def _is_compiler_cache(exe: str) -> bool:
    """Whether `exe` ultimately resolves to a compiler-cache binary."""
    stem = os.path.splitext(os.path.basename(os.path.realpath(exe)))[0]
    return stem.lower() in _COMPILER_CACHE_STEMS


def _which_real_driver(name: str) -> T.Optional[str]:
    """The first `name` on PATH that is not a compiler-cache masquerade."""
    for d in os.get_exec_path():
        found = shutil.which(name, path=d)
        if found and not _is_compiler_cache(found):
            return found
    return None


@functools.lru_cache(maxsize=None)
def _host_cpp_compiler() -> T.Optional[CPPCompiler]:
    try:
        return T.cast('CPPCompiler', detect_cpp_compiler(get_fake_env(), MachineChoice.HOST))
    except EnvironmentException:
        return None


def _regex_probe_builds(cpp: CPPCompiler, flag: str) -> bool:
    """Compile a two-TU named-module hello with only the given bare flag,
    the way the regex fallback path would (plain compiles, default module
    mapper/cache in cwd)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, 'mod.cpp'), 'w', encoding='utf-8') as f:
            f.write('export module regexprobe;\nexport int probefunc() { return 42; }\n')
        with open(os.path.join(tmpdir, 'use.cpp'), 'w', encoding='utf-8') as f:
            f.write('import regexprobe;\nint main() { return probefunc() - 42; }\n')
        exelist = cpp.get_exelist(ccache=False)
        if cpp.get_argument_syntax() == 'msvc':
            std_args = ['/std:c++20', '/c']
            consumer_flags = []
        else:
            std_args = ['-std=c++20', '-c']
            consumer_flags = [flag]
        for src, flags in (('mod.cpp', [flag]), ('use.cpp', consumer_flags)):
            try:
                p = subprocess.run(exelist + std_args + flags + [src],
                                   cwd=tmpdir, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if p.returncode != 0:
                return False
    return True


def regex_scanner_flag(cpp: CPPCompiler) -> T.Optional[str]:
    """The bare modules flag with which this compiler can build a flat named
    module on the regex fallback path, or None if none works."""
    key = (cpp.get_id(), cpp.version)
    try:
        return _regex_flag_cache[key]
    except KeyError:
        pass
    flag = next((f for f in cpp.get_cpp_modules_args() if _regex_probe_builds(cpp, f)), None)
    _regex_flag_cache[key] = flag
    return flag


def _bmionly_probe_builds(cpp: CPPCompiler) -> bool:
    """Compile a trivial interface unit in the compiler's BMI-only mode and
    check that a BMI appears and no object does (a failing rc, a missing BMI
    or a stray object all mean the mode is unusable)."""
    family = cpp.cpp_module_family()
    if family == 'msvc':
        src = 'mod.ixx'
        attempts = [['/nologo', '/std:c++20', '/c', '/interface', '/ifcOnly', src]]
    elif family == 'clang':
        src = 'mod.cppm'
        attempts = [['-std=c++20', '--precompile', src, '-o', 'mod.pcm']]
    else:
        src = 'mod.cpp'
        attempts = [[flag, '-std=c++20', '-fmodule-only', '-c', src]
                    for flag in cpp.get_cpp_modules_args()]
    exelist = cpp.get_exelist(ccache=False)
    for args in attempts:
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, src), 'w', encoding='utf-8') as f:
                f.write('export module bmiprobe;\nexport int probefunc() { return 42; }\n')
            try:
                p = subprocess.run(exelist + args, cwd=tmpdir,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if p.returncode != 0:
                continue
            produced = [name for root, _, files in os.walk(tmpdir) for name in files]
            if any(name.endswith(cpp.get_module_bmi_suffix()) for name in produced) \
                    and not any(name.endswith(('.o', '.obj')) for name in produced):
                return True
    return False


def bmionly_works(cpp: CPPCompiler) -> bool:
    """Whether this compiler can emit a BMI without an object file."""
    key = (cpp.get_id(), cpp.version)
    try:
        return _bmionly_cache[key]
    except KeyError:
        pass
    result = _bmionly_probe_builds(cpp)
    _bmionly_cache[key] = result
    return result


def cpp_module_caps(cpp: CPPCompiler) -> T.FrozenSet[str]:
    """The capability tokens of the given compiler; see the module docstring
    for the vocabulary. Memoized per (id, version); import_std, regex_scanner
    and bmionly probe by invoking the compiler."""
    key = (cpp.get_id(), cpp.version)
    try:
        return _caps_cache[key]
    except KeyError:
        pass
    caps = set()
    # Deliberately not gated on current_vs_supports_modules() or the
    # >= 19.28.28617 floor: no test depends on them and a broken developer
    # prompt should fail loudly, not skip.
    if cpp.cpp_module_family() != 'none' and cpp.supports_cpp_modules_p1689():
        caps.update(('modules', 'partitions', 'header_units', 'module_interfaces'))
        if cpp.get_std_module_sources():
            # GCC 15.0-15.2 ship the manifest but are too unreliable for the
            # std module.
            if cpp.cpp_module_family() != 'gcc' or version_compare(cpp.version, '>=15.3'):
                caps.add('import_std')
    if regex_scanner_flag(cpp) is not None:
        caps.add('regex_scanner')
    if 'modules' in caps and bmionly_works(cpp):
        caps.add('bmionly')
        if cpp.supports_bmi_classes():
            caps.add('bmi_classes')
    result = frozenset(caps)
    _caps_cache[key] = result
    return result


def require_cpp_module_caps(test: BasePlatformTests, *tokens: str,
                            compiler: T.Union[str, T.Tuple[str, ...], None] = None) -> CPPCompiler:
    """Skip the running test unless the Ninja backend is in use, the host C++
    compiler is one of `compiler` (id string or tuple of ids, if given), and
    it has every requested capability token. Returns the compiler."""
    unknown = set(tokens) - CPP_MODULE_CAPS
    if unknown:
        raise ValueError(f'Unknown C++ module capability token(s): {sorted(unknown)}')
    if test.backend is not Backend.ninja:
        raise unittest.SkipTest(f'C++ modules only work with the Ninja backend (not {test.backend.name}).')
    cpp = _host_cpp_compiler()
    if cpp is None:
        raise unittest.SkipTest('No C++ compiler found.')
    if compiler is not None:
        ids = (compiler,) if isinstance(compiler, str) else tuple(compiler)
        if cpp.get_id() not in ids:
            raise unittest.SkipTest(f'Test only applies to {"/".join(ids)} (found {cpp.get_id()}).')
    caps = cpp_module_caps(cpp)
    missing = [t for t in tokens if t not in caps]
    if missing:
        raise unittest.SkipTest(f'C++ module capability {", ".join(repr(m) for m in missing)} '
                                f'not available with {cpp.get_id()} {cpp.version}.')
    return cpp


def requires_cpp_module_caps(*tokens: str,
                             compiler: T.Union[str, T.Tuple[str, ...], None] = None
                             ) -> T.Callable[[T.Callable[P, R]], T.Callable[P, R]]:
    """Decorator form of require_cpp_module_caps."""
    def wrapper(func: T.Callable[P, R]) -> T.Callable[P, R]:
        @functools.wraps(func)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            require_cpp_module_caps(T.cast('BasePlatformTests', args[0]), *tokens, compiler=compiler)
            return func(*args, **kwargs)
        return wrapped
    return wrapper


# The flag that names a specific BMI on a consumer command line. Commands must
# never carry one: modules are mapped via the module mapper / prebuilt-path
# machinery, not per-BMI flags, or the command lines would change with the
# module graph. Keyed by cpp_module_family(), not by compiler id: the families
# are exactly the three that have such a flag ('none' never reaches a module
# test), while an id would raise KeyError on every clang-derived compiler that
# carries an id of its own -- intel-llvm, armltdclang, emscripten.
_BMI_MAP_FLAG = {'gcc': '-fmodule-file', 'clang': '-fmodule-file', 'msvc': '/reference'}


class CppModulesTestMixin:

    """Shared driver for C++ module tests; mix into a BasePlatformTests
    subclass. Test bodies read the detected compiler's BMI cache layout
    (get_module_cache_dir/get_module_bmi_suffix) so the same method works
    under whichever single compiler the process was launched with."""

    # Rebuild tests exercise Meson's dependency graph, so ccache must not
    # answer for the compiler: it does not track the contents of BMIs, so a
    # cached hit can hand back an object compiled against the pre-edit
    # module -- including via distro PATH masquerade (/usr/lib64/ccache).
    NO_CCACHE = {'CCACHE_DISABLE': '1'}

    def host_cpp_compiler(self) -> CPPCompiler:
        cpp = _host_cpp_compiler()
        assert cpp is not None, 'gate the test with require(s)_cpp_module_caps first'
        return cpp

    def bmi_path(self, module_name: str) -> str:
        """Where the BMI of the given module lands in the shared per-build
        cache; partition ':' maps to '-' as in the documented scheme."""
        cpp = self.host_cpp_compiler()
        return os.path.join(self.builddir, cpp.get_module_cache_dir(),
                            module_name.replace(':', '-') + cpp.get_module_bmi_suffix())

    def build_and_check_modules(self, testdir_name: str, *,
                                srcdir: T.Optional[str] = None,
                                extra_args: T.Sequence[str] = (),
                                setup_contains: T.Sequence[str] = (),
                                setup_not_contains: T.Sequence[str] = (),
                                build_contains: T.Sequence[str] = (),
                                build_not_contains: T.Sequence[str] = (),
                                run_tests: bool = True,
                                bmis: T.Sequence[str] = (),
                                ninja_args_not_contains: T.Optional[T.Sequence[str]] = None,
                                ninja_not_contains: T.Sequence[str] = (),
                                noop_check: bool = True,
                                no_ccache: bool = False) -> str:
        """The common spine of a module test: configure, build, run the
        tests, then check the BMI cache and build.ninja.

        srcdir: a pre-copied fixture path to configure from directly (e.g. a
            deterministically spaced tmp copy); the name still labels messages.
            When None the name resolves under the unit-test dir as usual.
        bmis: logical module names whose BMIs must exist in the shared cache.
        ninja_args_not_contains: needles no 'ARGS =' line may contain; None
            selects the compiler default (the BMI suffix and the flag that
            names a BMI), () disables the check (header units legitimately
            map BMIs on consumer command lines).
        ninja_not_contains: needles the whole of build.ninja may not contain.
        noop_check: an untouched follow-up build must do no work at all --
            catches any module output rewritten without copy-if-different.

        Returns the build output.
        """
        cpp = self.host_cpp_compiler()
        testdir = srcdir if srcdir is not None else os.path.join(self.unit_test_dir, testdir_name)
        out = self.init(testdir, extra_args=list(extra_args))
        for needle in setup_contains:
            self.assertIn(needle, out)
        for needle in setup_not_contains:
            self.assertNotIn(needle, out)
        built = self.build(override_envvars=self.NO_CCACHE if no_ccache else None)
        for needle in build_contains:
            self.assertIn(needle, built)
        for needle in build_not_contains:
            self.assertNotIn(needle, built)
        if run_tests:
            self.run_tests()
        for name in bmis:
            path = self.bmi_path(name)
            self.assertTrue(os.path.isfile(path), f'missing BMI {path}')
        if ninja_args_not_contains is None:
            ninja_args_not_contains = (cpp.get_module_bmi_suffix(),
                                       _BMI_MAP_FLAG[cpp.cpp_module_family()])
        if ninja_args_not_contains or ninja_not_contains:
            with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
                contents = f.read()
            for line in contents.splitlines():
                if line.strip().startswith('ARGS ='):
                    for needle in ninja_args_not_contains:
                        self.assertNotIn(needle, line)
            for needle in ninja_not_contains:
                self.assertNotIn(needle, contents)
        if noop_check:
            self.assertBuildIsNoop()
        return built

    def check_module_rebuild(self, testdir_name: str, *, edit_file: str,
                             srcdir: T.Optional[str] = None,
                             expect_in_rebuild: T.Sequence[str] = ('Linking target prog',),
                             prog: str = 'prog') -> None:
        """Editing a module interface (or a header behind a header unit) must
        rebuild the BMI and recompile every importer, exactly as editing a
        normally #included header does -- otherwise consumers link against a
        stale BMI. The edit bumps the exported value, so a stale build makes
        prog exit nonzero.

        srcdir: a pre-copied, writable fixture path to edit and build in place
            (a fresh copytree, so editing it never touches the original); the
            name still labels messages. When None the name is resolved under the
            unit-test dir and copied here as usual."""
        if srcdir is None:
            srcdir = self.copy_srcdir(os.path.join(self.unit_test_dir, testdir_name))
        self.init(srcdir)
        self.build(override_envvars=self.NO_CCACHE)
        self.run_tests()
        path = os.path.join(srcdir, edit_file)
        with open(path, encoding='utf-8') as f:
            content = f.read()
        newcontent = content.replace('return ', 'return 1000 + ', 1)
        self.assertNotEqual(content, newcontent, 'edit was a no-op')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(newcontent)
        out = self.build(override_envvars=self.NO_CCACHE)
        for needle in expect_in_rebuild:
            self.assertIn(needle, out)
        exe = os.path.join(self.builddir, prog + ('.exe' if is_windows() else ''))
        self.assertNotEqual(0, subprocess.run([exe]).returncode,
                            'importer did not pick up the module change (stale BMI)')

    def check_module_graph_mutation(self, testdir_name: str) -> None:
        """Mutate the module graph across rebuilds: add a second interface
        source, rename its file (module name unchanged, also covering
        file name != module name on the kwarg path), then remove it. Each
        phase is a reconfigure -- source lists changed -- and must yield a
        correct program and a clean no-op follow-up build. This exercises the
        collator's provider-claim handling as providers appear, move and
        disappear. The removed module's stale BMI is deliberately not
        asserted gone: nothing cleans the harvest cache."""
        testdir = os.path.join(self.unit_test_dir, testdir_name)
        srcdir = self.copy_srcdir(testdir)
        build_template = textwrap.dedent('''\
            project('cpp module graph mutation', 'cpp', default_options: ['cpp_std=c++20'])

            modlib = static_library('modlib', {srcs}, cpp_module_interfaces: [{srcs}])
            prog = executable('prog', 'main.cpp', link_with: modlib)
            test('prog', prog)
            ''')
        base_main = 'import mymod;\n\nint main() {\n    return modfunc() - 42;\n}\n'
        extra_main = ('import mymod;\nimport extra;\n\n'
                      'int main() {\n    return modfunc() - 42 + extraval();\n}\n')
        extra_src = 'export module extra;\n\nexport int extraval() {\n    return 0;\n}\n'

        def write(name: str, content: str) -> None:
            with open(os.path.join(srcdir, name), 'w', encoding='utf-8') as f:
                f.write(content)

        def build_and_verify() -> str:
            out = self.build(override_envvars=self.NO_CCACHE)
            self.run_tests()
            self.assertBuildIsNoop()
            return out

        self.init(srcdir)
        build_and_verify()

        write('extra.cc', extra_src)
        write('main.cpp', extra_main)
        write('meson.build', build_template.format(srcs="'mod.cc', 'extra.cc'"))
        out = build_and_verify()
        self.assertIn('Linking target prog', out)

        os.rename(os.path.join(srcdir, 'extra.cc'), os.path.join(srcdir, 'other.cc'))
        write('meson.build', build_template.format(srcs="'mod.cc', 'other.cc'"))
        build_and_verify()

        os.unlink(os.path.join(srcdir, 'other.cc'))
        write('main.cpp', base_main)
        write('meson.build', build_template.format(srcs="'mod.cc'"))
        build_and_verify()

    def link_edge(self, target: str) -> str:
        """The given output's build.ninja edge block (its 'build <target>:'
        line plus indented continuations), or '' when absent. The space form
        covers implicit outputs ('build prog.exe | prog.pdb: ...')."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            lines = f.read().splitlines()
        for i, line in enumerate(lines):
            if line.startswith(f'build {target}:') or line.startswith(f'build {target} '):
                block = [line]
                for nxt in lines[i + 1:]:
                    if not nxt.startswith((' ', '\t')):
                        break
                    block.append(nxt)
                return '\n'.join(block)
        return ''

    def assert_std_link_edges(self, linked: T.Sequence[str], not_linked: T.Sequence[str]) -> None:
        """dependency('std') synthesizes one static library carrying the std
        module objects; it must be on the link line of every target that
        consumes it, directly or transitively, and of nothing else."""
        stdlib = 'lib__meson_cxx_std.a'
        self.assertTrue(os.path.isfile(os.path.join(self.builddir, stdlib)),
                        'std module static library not built')
        for target in linked:
            self.assertIn(stdlib, self.link_edge(target), f'{target} does not link the std library')
        for target in not_linked:
            self.assertNotIn(stdlib, self.link_edge(target))

    def count_on_link_line(self, target: str, needle: str) -> int:
        """Occurrences of needle on the given output's LINK_ARGS line (the
        edge block also names archives as ninja inputs, which is not the
        link command)."""
        return sum(line.count(needle) for line in self.link_edge(target).splitlines()
                   if line.strip().startswith('LINK_ARGS ='))

    def bmi_variant_ids(self) -> T.Set[str]:
        """The distinct BMI-only variant private dirs named in build.ninja
        (one per provider x divergent class). Tolerates either path separator
        and normalizes to '/' so counts are separator-independent."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            contents = f.read()
        return {m.replace('\\', '/')
                for m in re.findall(r'meson-private[/\\][^/\\\s]*@bmi@[0-9a-f]{12}', contents)}

    def provided_modules(self, target: str) -> T.Set[str]:
        """The module names a target publishes to targets linking it, from its
        provided-modules.json. A private module is never in here (it has no
        importer outside the target), whether it is private by declaration or
        because nothing can link the target at all."""
        # The private dir carries the target's output name (app.p / app.exe.p
        # for an executable, libmylib.a.p / mylib.lib.p for a static library) --
        # glob rather than hardcode any of them, as assert_alias_mapper_key does.
        patterns = [os.path.join(self.builddir, f'{target}.p', 'provided-modules.json'),
                    os.path.join(self.builddir, f'{target}.*.p', 'provided-modules.json'),
                    os.path.join(self.builddir, f'lib{target}.*.p', 'provided-modules.json')]
        matches = [m for pat in patterns for m in glob.glob(pat)]
        self.assertEqual(len(matches), 1,
                         f'{" or ".join(patterns)}: expected one provmap, got {matches}')
        with open(matches[0], encoding='utf-8') as f:
            return set(json.load(f))

    def private_bmi_dirs(self) -> T.Set[str]:
        """The distinct module-providing-executable private BMI dirs named in
        build.ninja (one per such executable). Tolerates either path
        separator and normalizes to '/' so counts are separator-independent."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            contents = f.read()
        return {m.replace('\\', '/')
                for m in re.findall(r'meson-private[/\\][^/\\\s]*@bmi-private', contents)}

    def header_unit_digests(self, basename: str, *, edges: str = '') -> T.Set[str]:
        """The distinct digests of 'meson-private/header-units/<basename>.<digest>.*'
        paths in build.ninja, one per (mode, spelling) and, on per-class
        compilers, per BMI class. `edges` restricts the search to edge blocks
        whose 'build' line contains it (e.g. 'prog.p/' for one target's
        compiles and scans, '@bmi@' for BMI-variant edges)."""
        pattern = re.compile(r'header-units[/\\]' + re.escape(basename) + r'\.([0-9a-f]{16})\.')
        digests: T.Set[str] = set()
        wanted = False
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('build '):
                    wanted = edges in line
                if wanted:
                    digests.update(pattern.findall(line))
        return digests

    def header_unit_bmis(self, basename: str) -> T.Set[str]:
        """Every header-unit BMI the build declares for `basename`, from the
        outputs of the header-unit rule. Unlike header_unit_digests this sees
        GCC's too, whose per-class BMIs stand at the compiler's default-named
        paths under the shared cache rather than under meson-private/header-units."""
        outputs: T.Set[str] = set()
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if not line.startswith('build ') or 'HEADER_UNIT' not in line:
                    continue
                out = line[len('build '):].partition(': ')[0].strip()
                if basename in os.path.basename(out):
                    outputs.add(out.replace('\\', '/'))
        return outputs

    def header_unit_edge_orderdeps(self, basename: str) -> T.Tuple[T.Optional[str], T.List[str]]:
        """The (output, order-only deps) of the header-unit edge for `basename`.
        A ninja build line's order-only inputs follow the '||'; the header
        basename here contains no space, so a plain split is enough."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if not line.startswith('build ') or 'HEADER_UNIT' not in line:
                    continue
                out, _, rest = line[len('build '):].partition(': ')
                if basename not in os.path.basename(out.strip()):
                    continue
                _, _, orderonly = rest.partition('||')
                return out.strip(), orderonly.split()
        return None, []

    def check_generated_header_unit_ordered(self) -> None:
        """A header unit whose header a custom_target writes during the build.
        GCC cannot name such a unit (it is named by the path it resolves to,
        which does not exist yet) and rejects it at setup; cl and clang have no
        such gate and build it. The unit edge reads the header off disk but
        names no source, so it must order behind the generator -- the bug this
        covers is an edge with no input at all, which ninja is free to run
        before the generator, failing to open the header."""
        testdir = os.path.join(self.unit_test_dir, '198 unresolvable header unit')
        self.init(testdir, extra_args=['-Dmode=generated'])
        out, orderdeps = self.header_unit_edge_orderdeps('generated.h')
        self.assertIsNotNone(out, 'no header-unit edge emitted for generated.h')
        self.assertIn('generated.h', orderdeps,
                      'the header-unit edge does not order behind its generator')
        # Building just the unit from a clean tree must run the generator
        # first: the deterministic form of the race a full -j build only
        # sometimes lost (the generator usually beat the compiler's startup).
        self.build(target=out)
        self.assertTrue(os.path.isfile(os.path.join(self.builddir, 'generated.h')),
                        'building the unit did not run its generator first')

    def assert_alias_mapper_key(self, target: str, obj: str, alias: bool = True) -> str:
        """The BMI a GCC TU resolves header.hpp to, checking the mapper carries a
        line keyed by the spelling that TU's compile computes -- textually, '..'
        and all, since that is what the compiler keys a unit by. The collate
        joins every name of one BMI into the mapper, so other header.hpp lines
        (the file's other spellings, pointing at the same BMI) may sit alongside;
        this asserts the one the TU asks for is present and returns the BMI."""
        # The private dir carries the target's output suffix (prog.p on POSIX,
        # prog.exe.p on Windows) and so does the object (.o vs .obj) -- glob
        # rather than hardcode either. The dot before each wildcard keeps
        # 'prog' from also matching 'progfoo's directory.
        patterns = [os.path.join(self.builddir, f'{target}.p', f'{obj}.*.mapper'),
                    os.path.join(self.builddir, f'{target}.*.p', f'{obj}.*.mapper')]
        matches = [m for pat in patterns for m in glob.glob(pat)]
        self.assertEqual(len(matches), 1,
                         f'{" or ".join(patterns)}: expected one mapper, got {matches}')
        path = matches[0]
        keyre = re.compile(r'^\./meson-private/i/[0-9a-z]+/foo/\.\./header\.hpp$'
                           if alias else r'^\./meson-private/i/[0-9a-z]+/header\.hpp$')
        with open(path, encoding='utf-8') as f:
            lines = [ln for ln in f.read().splitlines() if 'header.hpp' in ln]
        keyed = [ln for ln in lines if keyre.match(ln.partition(' ')[0])]
        self.assertEqual(len(keyed), 1,
                         f'{path}: expected one line keyed by the compile spelling, got {lines}')
        _, _, bmi = keyed[0].partition(' ')
        self.assertTrue(os.path.isfile(os.path.join(self.builddir, bmi)),
                        f'mapper names {bmi}, which nothing produced')
        return bmi

    def assert_header_unit_alias_link(self, canonical_bmi: str, count: int = 1) -> None:
        """An alias spelling's own default-named path holds the class's BMI
        itself, not a second build of it: scans carry no mapper, so a TU
        importing through the alias reaches the unit only there. One link edge
        per aliased spelling per class, so `count` is the total; the given
        canonical BMI must be the target of one of them."""
        outs = []
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('build ') and 'cpp_header_unit_alias' in line:
                    outs.append(line[len('build '):].partition(': ')[0].strip())
        self.assertEqual(len(outs), count, f'expected {count} alias link edge(s), got {outs}')
        for out in outs:
            alias_path = os.path.join(self.builddir, out)
            self.assertTrue(os.path.isfile(alias_path), f'alias link {out} was not built')
        self.assertTrue(
            any(os.path.samefile(os.path.join(self.builddir, out),
                                 os.path.join(self.builddir, canonical_bmi)) for out in outs),
            f'none of {outs} is the canonical BMI {canonical_bmi}')

    def header_unit_bmis_of(self, target: str, basename: str) -> T.Set[str]:
        """The header-unit BMIs `target`'s own compiles resolve for `basename`.
        Read from its per-TU module mappers on GCC, where no unit BMI path
        reaches a command line; from the compile edge's flags elsewhere."""
        if self.host_cpp_compiler().cpp_module_family() != 'gcc':
            return self.header_unit_digests(basename, edges=f'{target}.p/')
        bmis: T.Set[str] = set()
        # The private dir carries the target's output suffix (prog.p on
        # POSIX, prog.exe.p on Windows) -- glob rather than hardcode it. The
        # dot before the wildcard keeps 'prog' from also matching 'progfoo's
        # directory.
        patterns = [os.path.join(self.builddir, f'{target}.p'),
                    os.path.join(self.builddir, f'{target}.*.p')]
        matches = [m for pat in patterns for m in glob.glob(pat)]
        self.assertEqual(len(matches), 1,
                         f'{" or ".join(patterns)}: expected one private dir, got {matches}')
        tdir = matches[0]
        for entry in sorted(os.listdir(tdir)):
            if not entry.endswith('.mapper'):
                continue
            with open(os.path.join(tdir, entry), encoding='utf-8') as f:
                for line in f:
                    _, _, bmi = line.strip().partition(' ')
                    if bmi and basename in os.path.basename(bmi):
                        bmis.add(bmi.replace('\\', '/'))
        return bmis

    def bmi_class_dirs(self) -> T.List[str]:
        """The BMI class subdirectories of the shared module cache, sorted."""
        cpp = self.host_cpp_compiler()
        cache = os.path.join(self.builddir, cpp.get_module_cache_dir())
        return sorted(e for e in os.listdir(cache)
                      if os.path.isdir(os.path.join(cache, e)))

    def assert_variants_emit_no_objects(self) -> None:
        """A BMI-only variant must never produce an object file: consumers
        link the original provider's objects, and a second copy of a module's
        initializer or vague-linkage entities in one link is an ODR
        violation."""
        for root, _, files in os.walk(os.path.join(self.builddir, 'meson-private')):
            if '@bmi@' not in root:
                continue
            objs = [f for f in files if f.endswith(('.o', '.obj'))]
            self.assertEqual(objs, [], f'BMI variant dir {root} must not contain objects')

    def check_bmi_classes(self, testdir_name: str, *, module_name: str,
                          provider_lib: str, consumers: T.Sequence[str],
                          expected_targets: T.Sequence[str],
                          extra_args: T.Sequence[str] = ()) -> None:
        """The canonical two-class fixture: one module provider, one consumer
        in the provider's own BMI class, one divergent consumer. Divergence
        must build and run without the Stage-1 warning, with exactly one
        object-producing provider (the divergent consumer resolves against a
        BMI-only variant), one BMI per class dir, no flat BMI, and exactly
        one variant build-wide -- the compatible consumer must reuse the
        provider's BMI, not spawn a redundant variant."""
        cpp = self.host_cpp_compiler()
        self.build_and_check_modules(testdir_name, extra_args=extra_args,
                                     setup_not_contains=['divergent dialects'])
        bmi = module_name.replace(':', '-') + cpp.get_module_bmi_suffix()
        cache = os.path.join(self.builddir, cpp.get_module_cache_dir())
        class_dirs = self.bmi_class_dirs()
        self.assertEqual(len(class_dirs), 2, f'expected two BMI class dirs, got {class_dirs}')
        for d in class_dirs:
            self.assertTrue(os.path.isfile(os.path.join(cache, d, bmi)),
                            f'missing {bmi} in class dir {d}')
            # The collator's module-name claim is keyed by the BMI path, so
            # per-class dirs make claims per-class: both classes of one
            # module coexist without the duplicate-provider error.
            self.assertTrue(os.path.isfile(os.path.join(cache, d, bmi + '.owner')),
                            f'missing per-class owner claim in {d}')
        self.assertFalse(os.path.exists(os.path.join(cache, bmi)),
                         'flat BMI must not exist in a multi-class build')
        targets = sorted(t['name'] for t in self.introspect('--targets'))
        self.assertEqual(targets, sorted(expected_targets),
                         'variants must not appear as targets')
        variants = self.bmi_variant_ids()
        self.assertEqual(len(variants), 1,
                         f'expected exactly one BMI variant, got {sorted(variants)}')
        for t in consumers:
            self.assertEqual(self.count_on_link_line(t, provider_lib), 1,
                             f'{t} must link the real provider exactly once')
            self.assertNotIn('@bmi@', self.link_edge(t), f'{t} must not link variant artifacts')
        self.assert_variants_emit_no_objects()

    def check_import_std_bmi_classes(self, testdir_name: str, *, progs: T.Sequence[str],
                                     compat_progs: T.Sequence[str] = (),
                                     compat_in_all_classes: bool = False,
                                     extra_args: T.Sequence[str] = ()) -> None:
        """Two dialects sharing dependency('std') in one build: one
        __meson_cxx_std target provides the objects for both, each class dir
        carries its own std BMI set (std.compat only where imported, except
        compat_in_all_classes below), and exactly one variant exists -- all
        through the generic variant rule, with no std-specific machinery.

        compat_in_all_classes: on MSVC the provider's compile writes its
        class's BMI eagerly (directory /ifcOutput), so std.compat appears in
        the provider's class dir whether or not anything there imports it;
        Clang's harvest is lazily pulled by importers only.
        """
        cpp = self.host_cpp_compiler()
        suffix = cpp.get_module_bmi_suffix()
        self.build_and_check_modules(testdir_name, extra_args=extra_args,
                                     setup_not_contains=['divergent dialects'])
        self.assert_std_link_edges(progs, ())
        for t in progs:
            self.assertEqual(self.count_on_link_line(t, 'lib__meson_cxx_std.a'), 1,
                             f'{t} must link the std library exactly once')
        std_targets = [t for t in self.introspect('--targets') if t['name'] == '__meson_cxx_std']
        self.assertEqual(len(std_targets), 1, 'exactly one std module target must exist')
        cache = os.path.join(self.builddir, cpp.get_module_cache_dir())
        class_dirs = self.bmi_class_dirs()
        self.assertEqual(len(class_dirs), 2, f'expected two BMI class dirs, got {class_dirs}')
        for d in class_dirs:
            self.assertTrue(os.path.isfile(os.path.join(cache, d, 'std' + suffix)),
                            f'missing std{suffix} in class dir {d}')
        if compat_progs:
            compat = [d for d in class_dirs
                      if os.path.isfile(os.path.join(cache, d, 'std.compat' + suffix))]
            if compat_in_all_classes:
                self.assertEqual(compat, class_dirs,
                                 'std.compat must be published in every class dir')
            else:
                self.assertEqual(len(compat), 1,
                                 'std.compat must be published only in the class that imports it')
        self.assertEqual(len(self.bmi_variant_ids()), 1,
                         'expected exactly one BMI variant of the std target')
        self.assert_variants_emit_no_objects()

    def check_gcc_module_mappers(self) -> None:
        """GCC builds resolve modules through mappers, whatever their class
        topology: every module compile edge names its own object's mapper (a
        static path whose scan-derived contents the collate writes), every
        header-unit edge names one written at setup, the mapper files exist on
        disk -- and scan edges carry no mapper at all.

        That last one is load-bearing. A mapper file switches off GCC's default
        naming outright, named modules included, and their names come from the
        very scan the mapper would serve: a scan carrying one dies on the first
        `import some_module;`. So a scan resolves a unit only at a default-named
        path, and each class reaches its own by scanning through its class root."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            lines = f.read().splitlines()
        rule = out = ''
        compile_edges = 0
        unit_edges = 0
        for line in lines:
            if line.startswith('build '):
                head, _, tail = line.partition(': ')
                out = head[len('build '):].split(' ')[0]
                rule = tail.split(' ')[0]
                continue
            s = line.strip()
            if 'MODULE_SCAN' in rule and s.startswith(('ARGS =', 'HUMAPPER =')):
                self.assertNotIn('-fmodule-mapper', s,
                                 f'scan edge for {out} must not carry a mapper')
            elif 'HEADER_UNIT' in rule and s.startswith('HUMAPPER ='):
                unit_edges += 1
                # Windows quotes the whole arg ('"-fmodule-mapper=path"'), so the
                # closing quote lands in the partitioned tail; POSIX does not.
                mapper = s.partition('-fmodule-mapper=')[2].strip().rstrip('"')
                self.assertTrue(mapper, f'header-unit edge for {out} must carry a mapper')
                self.assertTrue(os.path.isfile(os.path.join(self.builddir, mapper)),
                                f'missing header-unit mapper file {mapper}')
            elif rule.startswith('cpp_COMPILER') and s.startswith('ARGS ='):
                compile_edges += 1
                # The mapper path is spelled OS-natively in ARGS (backslashes on
                # Windows) while the build-target 'out' uses forward slashes;
                # compare separator-insensitively.
                self.assertIn(f'-fmodule-mapper={out}.mapper'.replace('\\', '/'),
                              s.replace('\\', '/'),
                              f'compile edge for {out} must resolve through its mapper')
                mapper = os.path.join(self.builddir, out + '.mapper')
                self.assertTrue(os.path.isfile(mapper), f'missing mapper file {mapper}')
        self.assertGreater(compile_edges, 0, 'no module compile edges found')

    # --- Spaced-path header-unit test fabrication -------------------------
    #
    # "A header unit on a path with a space" is one feature contract, exercised
    # across every module family and against both a spaced and an unspaced
    # path, with each family asserting its own contracted outcome. The repo's
    # own 'test cases' dir has a space, so the *unspaced* side never occurs
    # naturally in CI and the *spaced-compiler-prefix* side occurs only where
    # the toolchain happens to sit under one. These helpers establish the
    # property a cell needs rather than assuming the checkout's layout: they
    # probe for it, fabricate it when absent, and skip with the probe evidence
    # only when fabrication is impossible.

    @staticmethod
    def _gcc_abs_exe(cpp: 'CPPCompiler') -> T.Optional[str]:
        """The absolute path of the (GNU-driver) compiler binary, past any
        ccache wrapper meson detected in front of it -- what the fabrication
        rungs need to find the install root and write a native file."""
        # get_exelist strips only the ccache wrapper; what remains is the
        # driver followed by any baked-in arguments (CXX='ccache g++ -m32'
        # leaves ['g++', '-m32']), so the driver is the first entry.
        cmd = cpp.get_exelist(ccache=False)
        exe = cmd[0] if cmd else None
        if not exe:
            return None
        if not (os.path.isabs(exe) and os.path.isfile(exe)):
            exe = shutil.which(exe)
        if exe is None or not _is_compiler_cache(exe):
            return exe
        # A compiler-cache masquerade: distros put a directory of links named
        # after the drivers early on PATH (/usr/lib64/ccache/g++), each
        # pointing at the cache binary. Its realpath's directory is the
        # cache's, so the caller would derive a "toolchain root" of /usr and
        # the rungs would go on to link and probe ccache itself instead of a
        # compiler. Look further down PATH for a real driver of the same
        # name; None if there is none, so the rung skips honestly.
        return _which_real_driver(os.path.basename(exe))

    def _probe_system_header(self, cpp: 'CPPCompiler', header: str) -> T.Optional[str]:
        """The path the compiler resolves ``#include <header>`` to, read from
        its own diagnostic output. None when the probe cannot resolve it."""
        exe = cpp.get_exelist(ccache=False)
        if cpp.get_argument_syntax() == 'msvc':
            with tempfile.TemporaryDirectory() as d:
                src = os.path.join(d, 'p.cpp')
                with open(src, 'w', encoding='utf-8') as f:
                    f.write(f'#include <{header}>\n')
                p = subprocess.run(exe + ['/nologo', '/EP', '/showIncludes', '/TP', src],
                                   capture_output=True, text=True)
                for line in (p.stdout + p.stderr).splitlines():
                    marker = line.partition('including file:')[2].strip()
                    if marker and os.path.basename(marker) == header:
                        return marker
            return None
        p = subprocess.run(exe + ['-E', '-H', '-x', 'c++', '-'],
                           input=f'#include <{header}>\n', capture_output=True, text=True)
        for line in p.stderr.splitlines():
            if line.startswith('. '):
                return line[2:].strip()
        return None

    def probe_compiler_owned_space(self, cpp: 'CPPCompiler',
                                   header: str = 'vector') -> T.Tuple[bool, T.Optional[str]]:
        """(whether a compiler-owned system header resolves to a spaced path,
        that path). ``import <vector>;`` is named by the compiler's own search
        path, so a spaced install prefix is what makes such a unit unmappable
        on GCC -- and what the immune families must still handle."""
        resolved = self._probe_system_header(cpp, header)
        if resolved is None:
            return False, None
        return (any(c.isspace() for c in resolved), resolved)

    # The build-relative alias root Meson routes a spaced user header unit
    # through (HEADER_UNIT_ALIAS_ROOT + '/' + a base36 digest). GCC compares a
    # user header's absolute real path against the spelling it was reached by
    # and keeps the shorter, so shorter-wins fires only where the real fixture
    # root is shorter than this. Derived from the backend's own alias-root
    # spelling, not a magic number, so it tracks a re-spelling of the scheme;
    # the digest length matches _dir_alias_root's base length.
    _ALIAS_ROOT_SPELLING_LEN = len(HEADER_UNIT_ALIAS_ROOT + '/') + HEADER_UNIT_ALIAS_DIGEST_LEN
    # The spaced leaf a shallow fixture is copied into (the header's real path
    # tail is common to both sides of GCC's comparison and cancels, so only the
    # root dir length -- base + this leaf -- decides the defeat).
    _SHORT_SPACED_LEAF = 's p'

    def _short_fixture_base(self) -> str:
        """A base directory for a shallow spaced fixture, short enough that the
        fixture root (base + the spaced leaf) is shorter than the alias root --
        the only way GCC's shorter-of-two canonicalization discards the alias
        and shorter-wins fires. The ladder: the platform temp dir first (managed
        mkdtemp space) when its layout is already short enough, else a
        distinctively named base created near the filesystem root. Neither
        working is a skip with the computed lengths, never a long-path fallback
        (a long path cannot provoke the defeat). The chosen base registers its
        own cleanup the moment it exists, so every exit path removes it, and the
        near-root base is created (exist_ok=False), never adopted."""
        from mesonbuild.mesonlib import windows_proof_rmtree
        limit = self._ALIAS_ROOT_SPELLING_LEN
        margin = 2

        def root_len(base: str) -> int:
            return len(os.path.abspath(os.path.join(base, self._SHORT_SPACED_LEAF)))

        diags: T.List[str] = []
        # Rung 1: the platform temp dir. Managed and collision-safe; used only
        # when its layout already clears the limit (true on a short tmpdir like
        # /tmp, not on Windows' deep AppData\Local\Temp).
        tmp = tempfile.mkdtemp(prefix='m-meson-')
        self.addCleanup(windows_proof_rmtree, tmp)
        n = root_len(tmp)
        diags.append(f'temp base {tmp!r}: fixture root len {n}')
        if n + margin <= limit:
            return tmp

        # Rung 2: a distinctively named, collision-resistant base created near
        # the filesystem root. Created, never adopted; cleanup registered the
        # instant it exists; an unwritable root is a skip, not a force-remove.
        marker = f'm-meson-{os.getpid()}-{os.urandom(2).hex()}'
        if os.name == 'nt':
            drive = os.path.splitdrive(tempfile.gettempdir())[0]
            near = os.path.join(drive + os.sep, marker) if drive else None
        else:
            # No POSIX near-root rung: GCC shortens a USER header only under
            # HAVE_DOS_BASED_FILE_SYSTEM, which is Windows-only, so a user
            # header on POSIX is never canonicalized to its real path -- the
            # alias is kept whatever the fixture root's length, and shorter-wins
            # cannot be provoked at all. A near-root spaced fixture here would
            # root a tree that can never exercise the defeat, so skip with the
            # reason rather than fabricate a misleading one. (The only caller is
            # the Windows-only shorter-wins cell; this arm just refuses to lie
            # if it is ever reached off Windows.)
            raise unittest.SkipTest(
                'shorter-wins is not provocable on POSIX: a user header is '
                'never shortened to its real path (the shortening is guarded by '
                'HAVE_DOS_BASED_FILE_SYSTEM, a Windows-only macro), so no '
                'fixture-root length can defeat the space-free alias here; '
                + '; '.join(diags))
        if near is not None:
            try:
                os.makedirs(near, exist_ok=False)
            except OSError as e:
                diags.append(f'near-root {near!r} not creatable: {e}')
            else:
                self.addCleanup(windows_proof_rmtree, near)
                n = root_len(near)
                diags.append(f'near-root base {near!r}: fixture root len {n}')
                if n + margin <= limit:
                    return near
        raise unittest.SkipTest(
            f'cannot root a spaced fixture shorter than the {limit}-char alias '
            'root to provoke shorter-wins; ' + '; '.join(diags))

    def copy_fixture(self, name: str, *, spaced: bool, deep: bool = True) -> str:
        """A copy of unit fixture `name` under a tmp root whose path is
        deterministically spaced or space-free, independent of the checkout
        path. `deep` (spaced only) pads the root so it stays longer than the
        meson-private/i alias root, keeping the alias the shorter spelling GCC
        prefers; a shallow spaced root (deep=False) is rooted short enough that
        GCC discards the alias, which is how shorter-wins is provoked. The tree
        is removed on cleanup."""
        from mesonbuild.mesonlib import windows_proof_rmtree
        if spaced and not deep:
            # The short base registers its own cleanup (or skips). The space
            # lives in the leaf copied straight into -- no extra component, so
            # the root stays under the alias length computed above.
            dest = os.path.join(self._short_fixture_base(), self._SHORT_SPACED_LEAF)
        else:
            base = tempfile.mkdtemp()
            self.addCleanup(windows_proof_rmtree, base)
            leaf = ('deep padded root ' * 2 + 'with space') if spaced else 'nospace'
            # A space-free destination leaf, so the only space in an unspaced
            # copy is none at all -- the fixture's own directory name (e.g. '197
            # header unit forced include') holds spaces that would leak in.
            dest = os.path.join(base, leaf, 'fixture')
            os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copytree(os.path.join(self.unit_test_dir, name), dest)
        assert (' ' in dest) == spaced, dest
        return dest

    def fabricate_spaced_gcc_prefix(self, cpp: 'CPPCompiler') -> str:
        """A g++.exe whose install prefix contains a space, for the
        compiler-owned-spaced cell. The ladder, cheapest first:

          1. the host prefix is already spaced -> use it;
          2. link the compiler root under a spaced path and RE-PROBE through
             the link -- some platforms keep the link's spelling, others
             canonicalize it back to the real prefix (GCC resolves the reparse
             point); the probe decides, never an assumption;
          3. the optional minimal-copy rung (unittests/spaced_toolchain_copy),
             off by default -- without it the ladder ends at step 2;
          4. SkipTest carrying the probe transcript.

        Returns the g++.exe path, or raises SkipTest.
        """
        from mesonbuild.backend.ninjabackend import NinjaBackend
        transcript: T.List[str] = []
        spaced, resolved = self.probe_compiler_owned_space(cpp)
        transcript.append(f'host <vector> resolves to {resolved!r} (spaced={spaced})')
        gxx = self._gcc_abs_exe(cpp)
        if gxx is None:
            raise unittest.SkipTest('could not locate the host GCC binary')
        if spaced:
            return gxx
        # Rung 2: link the compiler root under a spaced tmp path, re-probe.
        from mesonbuild.mesonlib import windows_proof_rmtree
        try:
            from .spaced_toolchain_copy import minimal_gcc_copy, GATE_ENV, available
        except ImportError:
            # Rung 3 is severable: deleting unittests/spaced_toolchain_copy.py
            # must leave the ladder ending at the link probe and the affected
            # cells skipping, never failing on the import.
            minimal_gcc_copy, GATE_ENV, available = None, None, lambda: False
        root = os.path.dirname(os.path.dirname(os.path.realpath(gxx)))
        base = tempfile.mkdtemp()
        self.addCleanup(windows_proof_rmtree, base)
        link_root = os.path.join(base, 'sp ace')
        # Spell the driver under the link the way it is spelled under the real
        # root -- the stem carries a triple/version on many installs
        # (x86_64-w64-mingw32-g++, g++-13) and the layout need not be bin/.
        linked_gxx = os.path.join(link_root, os.path.basename(root),
                                  os.path.relpath(os.path.realpath(gxx), root))
        os.makedirs(link_root, exist_ok=True)
        made = NinjaBackend._make_dir_link(
            root, os.path.join(link_root, os.path.basename(root)))
        if made and os.path.isfile(linked_gxx):
            # Re-probe by shelling out to the linked driver directly: whether
            # GCC keeps the link's spaced spelling or canonicalizes it back to
            # the real prefix is the runtime fact the decision rests on.
            r2 = self._shell_probe_vector(linked_gxx)
            sp2 = r2 is not None and any(c.isspace() for c in r2)
            transcript.append(f'through link {linked_gxx!r}: <vector> -> {r2!r} (spaced={sp2})')
            if sp2:
                return linked_gxx
        else:
            transcript.append(f'compiler root not linkable under a spaced path (made={made})')
        # Rung 3: the optional copy. Absent unless the runner opted in.
        if available():
            copy_root = os.path.join(base, 'sp ace copy', os.path.basename(root))
            copied = minimal_gcc_copy(gxx, copy_root)
            r3 = self._shell_probe_vector(copied) if copied else None
            sp3 = r3 is not None and any(c.isspace() for c in r3)
            transcript.append(f'minimal copy at {copied!r}: <vector> -> {r3!r} (spaced={sp3})')
            if sp3:
                return copied
        else:
            why = (f'set {GATE_ENV}=1 to enable' if GATE_ENV
                   else 'unittests/spaced_toolchain_copy.py is absent')
            transcript.append(f'copy rung disabled ({why})')
        raise unittest.SkipTest(
            'could not fabricate a spaced GCC prefix:\n  ' + '\n  '.join(transcript))

    def _shell_probe_vector(self, gxx: T.Optional[str]) -> T.Optional[str]:
        if not gxx or not os.path.isfile(gxx):
            return None
        p = subprocess.run([gxx, '-E', '-H', '-x', 'c++', '-'],
                           input='#include <vector>\n', capture_output=True, text=True)
        for line in p.stderr.splitlines():
            if line.startswith('. '):
                return line[2:].strip()
        return None

    def gcc_mapper_keys(self, builddir: T.Optional[str] = None) -> T.List[str]:
        """Every key (first whitespace-delimited field) of every GCC mapper
        file in the build tree."""
        builddir = builddir or self.builddir
        keys: T.List[str] = []
        for dirpath, _dirs, files in os.walk(builddir):
            for fn in files:
                if not fn.endswith('.mapper'):
                    continue
                with open(os.path.join(dirpath, fn), encoding='utf-8') as f:
                    for line in f:
                        line = line.rstrip('\n')
                        if line:
                            keys.append(line)
        return keys

    def assert_no_spaced_mapper_key(self, spaced_marker: str,
                                    builddir: T.Optional[str] = None) -> None:
        """No GCC mapper carries a line naming a unit under `spaced_marker` (the
        known spaced fragment of the source or compiler tree). A spaced key ends
        at the first space, so GCC would bind the wrong name and leave the real
        one a hard 'no such module' -- the degrade drops the line entirely, so
        the marker must appear in no mapper at all."""
        for line in self.gcc_mapper_keys(builddir):
            self.assertNotIn(spaced_marker, line,
                             f'a mapper carries a spaced-path unit line: {line!r}')

    def _header_unit_edge_args(self, basename: str) -> T.Tuple[T.List[str], str]:
        """The (ARGS, SPELLING) of the GCC header-unit build edge for `basename`,
        read from build.ninja -- the include path and spelling GCC resolves the
        unit through."""
        args: T.List[str] = []
        spelling = ''
        in_edge = False
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('build ') and 'HEADER_UNIT' in line:
                    out = line[len('build '):].partition(': ')[0].strip()
                    in_edge = basename in os.path.basename(out)
                elif in_edge and line.startswith(' ARGS ='):
                    val = line.partition('=')[2].strip()
                    args = re.findall(r'"([^"]*)"', val) or val.split()
                elif in_edge and line.startswith(' SPELLING ='):
                    spelling = line.partition('=')[2].strip().strip('"')
                elif in_edge and not line.startswith(' '):
                    in_edge = False
        return args, spelling

    def assert_alias_carried(self, basename: str) -> None:
        """GCC actually resolves the spaced user header unit `basename` through
        its space-free alias root, not the header's real path. GCC keeps the
        shorter of the two, so a deep checkout keeps the alias but a short one
        discards it for the shorter real path (shorter-wins) -- which the
        mapper-keyed build cannot survive. Probing under the unit's own build
        args, require the alias to be carried, so a short-checkout box fails
        here with a pointed message rather than a mysterious downstream error.
        A no-op where no alias is in play (an unspaced checkout)."""
        args, spelling = self._header_unit_edge_args(basename)
        if not any('meson-private/i/' in a.replace('\\', '/') for a in args):
            return
        cpp = self.host_cpp_compiler()
        cmd = cpp.get_exelist(ccache=False) + args + ['-E', '-H', '-x', 'c++', '-']
        p = subprocess.run(cmd, input=f'#include "{spelling}"\n', capture_output=True,
                           text=True, cwd=self.builddir)
        resolved = next((l[2:].strip() for l in p.stderr.splitlines()
                         if l.startswith('. ')), None)
        self.assertIsNotNone(resolved, f'could not probe the resolved name of {spelling!r}')
        self.assertNotIn(' ', resolved,
            f'the space-free alias for header unit {basename!r} was defeated by a '
            f'shorter real path at this checkout depth (GCC resolved it to '
            f'{resolved!r}) -- see the shorter-wins degrade. Move the checkout to a '
            'deeper path, or one without spaces.')
        self.assertIn('meson-private/i/', resolved.replace('\\', '/'),
                      f'header unit {basename!r} is not resolved through its '
                      f'alias root (got {resolved!r})')

    def assert_space_machinery_dormant(self, out: str,
                                       builddir: T.Optional[str] = None) -> None:
        """The negative contract: on an unspaced path the space machinery must
        not stir. No alias roots, no whitespace/degrade warnings, and every GCC
        mapper key an unaliased name."""
        builddir = builddir or self.builddir
        self.assertNotIn('cannot name a header unit', out)
        self.assertNotIn('a path containing whitespace', out)
        self.assertNotIn('divergent dialects', out)
        self.assertFalse(os.path.isdir(os.path.join(builddir, 'meson-private', 'i')),
                         'an alias root was placed on an unspaced path')
        with open(os.path.join(builddir, 'build.ninja'), encoding='utf-8') as f:
            self.assertNotIn('meson-private/i/', f.read().replace('\\', '/'))

    # --- The (user|system) x (spaced|unspaced) feature matrix -------------
    #
    # "A header unit works whether its directory has a space or not" is the
    # feature; the mapper-whitespace limit that the alias exists to route around
    # is platform-independent (a mapper line ends at the first whitespace on
    # every platform), so every cell of the matrix runs on Linux and on Windows
    # with the same assertions. The repo's own path is spaced, so the spaced
    # cells run in place (test_gcc_header_units / test_gcc_stl_header_units) and
    # the unspaced cells fabricate an unspaced tree with copy_fixture. Only the
    # alias-LENGTH sensitivity (GCC's shorter-of-two canonicalization defeating
    # the alias) is genuinely Windows-only and stays in WindowsTests.

    def check_gcc_user_header_unit_unspaced(self) -> None:
        """(user header unit) x (UNSPACED path): the space machinery stays
        dormant. Nothing needs an alias, so none is placed, no shorter-wins
        probe runs (the detector only probes an aliased name), and the unit
        builds and runs untouched -- the same outcome on Linux and Windows,
        because the whitespace limit the alias routes around is the same on
        both. The repo path is spaced, so an unspaced tree is fabricated."""
        from unittest import mock
        from mesonbuild.backend.ninjabackend import NinjaBackend
        src = self.copy_fixture('173 header unit aliasing', spaced=False)
        orig = NinjaBackend._run_header_probe
        calls: T.List[T.Tuple[str, str]] = []

        def counting(be: T.Any, compiler: T.Any, arglist: T.Any,
                     mode: str, spelling: str) -> T.Tuple[T.Optional[str], bool]:
            calls.append((mode, spelling))
            return orig(be, compiler, arglist, mode, spelling)
        with mock.patch.object(NinjaBackend, '_run_header_probe', counting):
            out = self.init(src, extra_args=['-Dmode=plain'], inprocess=True)
        self.assert_space_machinery_dormant(out)
        self.assertEqual(calls, [],
                         f'an unspaced header-unit build probed the compiler: {calls}')
        self.build()
        self.run_tests()

    def check_gcc_system_header_unit_unspaced(self) -> None:
        """(system header unit) x (UNSPACED path): a two-class build importing
        <vector> rooted on an unspaced tree. The system include chain is
        class-aliased for per-class naming -- a BMI-class need with nothing to
        do with whitespace -- so the unit is named and imported exactly as in
        the spaced in-place fixture: the same real-name -> class-aliased-BMI
        mapper line. Proves the system-unit feature does not lean on the build
        tree carrying a space. The same assertion on Linux and Windows."""
        src = self.copy_fixture('171 stl header units', spaced=False)
        out = self.init(src)
        self.assertNotIn('cannot name a header unit', out)
        self.assertNotIn('divergent dialects', out)
        self.build()
        self.run_tests()
        self.check_gcc_module_mappers()
        lines: T.List[str] = []
        for dirpath, _dirs, files in os.walk(self.builddir):
            for fn in files:
                if fn.endswith('.mapper'):
                    with open(os.path.join(dirpath, fn), encoding='utf-8') as f:
                        lines += f.read().splitlines()
        pat = re.compile(r'\S+/vector gcm\.cache/,/meson-private/i/[0-9a-z]+/vector\.gcm$')
        self.assertTrue(any(pat.search(line) for line in lines),
                        'no real-name mapping onto a class-aliased <vector> BMI '
                        f'in an unspaced tree: {lines}')
