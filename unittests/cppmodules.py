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
import os
import re
import subprocess
import tempfile
import textwrap
import typing as T
import unittest

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
    if cpp.get_id() == 'msvc':
        src = 'mod.ixx'
        attempts = [['/nologo', '/std:c++20', '/c', '/interface', '/ifcOnly', src]]
    elif cpp.get_id() == 'clang':
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
            if any(name.endswith(('.gcm', '.pcm', '.ifc')) for name in produced) \
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
    if cpp.get_id() in {'gcc', 'clang', 'msvc'} and cpp.supports_cpp_modules_p1689():
        caps.update(('modules', 'partitions', 'header_units', 'module_interfaces'))
        if cpp.get_std_module_sources():
            # GCC 15.0-15.2 ship the manifest but are too unreliable for the
            # std module.
            if cpp.get_id() != 'gcc' or version_compare(cpp.version, '>=15.3'):
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
# module graph.
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
        testdir = os.path.join(self.unit_test_dir, testdir_name)
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
            ninja_args_not_contains = (cpp.get_module_bmi_suffix(), _BMI_MAP_FLAG[cpp.get_id()])
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
                             expect_in_rebuild: T.Sequence[str] = ('Linking target prog',),
                             prog: str = 'prog') -> None:
        """Editing a module interface (or a header behind a header unit) must
        rebuild the BMI and recompile every importer, exactly as editing a
        normally #included header does -- otherwise consumers link against a
        stale BMI. The edit bumps the exported value, so a stale build makes
        prog exit nonzero."""
        testdir = os.path.join(self.unit_test_dir, testdir_name)
        srcdir = self.copy_srcdir(testdir)
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
                                     setup_not_contains=['divergent BMI-affecting flags'])
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
                                     setup_not_contains=['divergent BMI-affecting flags'])
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
        """Multi-class GCC builds resolve modules through per-TU mappers:
        every module compile edge names its own object's mapper (a static
        path whose scan-derived contents the collate writes), scan edges
        carry none -- their command lines stay identical to a single-class
        build -- and the mapper files exist on disk."""
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            lines = f.read().splitlines()
        rule = out = ''
        compile_edges = 0
        for line in lines:
            if line.startswith('build '):
                head, _, tail = line.partition(': ')
                out = head[len('build '):].split(' ')[0]
                rule = tail.split(' ')[0]
                continue
            s = line.strip()
            if not s.startswith('ARGS ='):
                continue
            if 'MODULE_SCAN' in rule:
                self.assertNotIn('-fmodule-mapper', s,
                                 f'scan edge for {out} must not carry a mapper')
            elif rule.startswith('cpp_COMPILER'):
                compile_edges += 1
                self.assertIn(f'-fmodule-mapper={out}.mapper', s,
                              f'compile edge for {out} must resolve through its mapper')
                mapper = os.path.join(self.builddir, out + '.mapper')
                self.assertTrue(os.path.isfile(mapper), f'missing mapper file {mapper}')
        self.assertGreater(compile_edges, 0, 'no module compile edges found')
