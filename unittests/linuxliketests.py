# SPDX-License-Identifier: Apache-2.0
# Copyright 2016-2022 The Meson development team

import json
import stat
import subprocess
import re
import tempfile
import textwrap
import os
import shutil
import hashlib
from unittest import mock, skipUnless, SkipTest
from glob import glob
from pathlib import Path
import typing as T

import mesonbuild.mlog
import mesonbuild.depfile
import mesonbuild.dependencies.base
import mesonbuild.dependencies.factory
import mesonbuild.envconfig
import mesonbuild.environment
import mesonbuild.coredata
import mesonbuild.modules.gnome
from mesonbuild.mesonlib import (
    MachineChoice, is_windows, is_osx, is_cygwin, is_openbsd, is_haiku,
    is_sunos, windows_proof_rmtree, version_compare, is_linux,
    EnvironmentException
)
from mesonbuild.options import OptionKey
from mesonbuild.compilers import (
    detect_c_compiler, detect_cpp_compiler, compiler_from_language,
)
from mesonbuild.compilers.c import AppleClangCCompiler, ElbrusCompiler
from mesonbuild.compilers.cpp import AppleClangCPPCompiler
from mesonbuild.compilers.objc import AppleClangObjCCompiler
from mesonbuild.compilers.objcpp import AppleClangObjCPPCompiler
from mesonbuild.dependencies.pkgconfig import PkgConfigDependency, PkgConfigCLI, PkgConfigInterface
from mesonbuild.programs import NonExistingExternalProgram
import mesonbuild.modules.pkgconfig

PKG_CONFIG = os.environ.get('PKG_CONFIG', 'pkg-config')


from run_tests import (
    get_fake_env, Backend,
)

from .baseplatformtests import BasePlatformTests
from .cppmodules import CppModulesTestMixin, regex_scanner_flag, requires_cpp_module_caps
from .helpers import *

def _prepend_pkg_config_path(path: str) -> str:
    """Prepend a string value to pkg_config_path

    :param path: The path to prepend
    :return: The path, followed by any PKG_CONFIG_PATH already in the environment
    """
    pkgconf = os.environ.get('PKG_CONFIG_PATH')
    if pkgconf:
        return f'{path}{os.path.pathsep}{pkgconf}'
    return path


def _clang_at_least(compiler: 'Compiler', minver: str, apple_minver: T.Optional[str]) -> bool:
    """
    check that Clang compiler is at least a specified version, whether AppleClang or regular Clang

    Parameters
    ----------
    compiler:
        Meson compiler object
    minver: str
        Clang minimum version
    apple_minver: str
        AppleCLang minimum version

    Returns
    -------
    at_least: bool
        Clang is at least the specified version
    """
    if isinstance(compiler, (AppleClangCCompiler, AppleClangCPPCompiler)):
        if apple_minver is None:
            return False
        return version_compare(compiler.version, apple_minver)
    return version_compare(compiler.version, minver)

@skipUnless(not is_windows(), "requires something Unix-like")
class LinuxlikeTests(CppModulesTestMixin, BasePlatformTests):
    '''
    Tests that should run on Linux, macOS, and *BSD
    '''

    def test_basic_soname(self):
        '''
        Test that the soname is set correctly for shared libraries. This can't
        be an ordinary test case because we need to run `readelf` and actually
        check the soname.
        https://github.com/mesonbuild/meson/issues/785
        '''
        testdir = os.path.join(self.common_test_dir, '4 shared')
        self.init(testdir)
        self.build()
        lib1 = os.path.join(self.builddir, 'libmylib.so')
        soname = get_soname(lib1)
        self.assertEqual(soname, 'libmylib.so')

    @skip_if_not_language('rust')
    def test_rust_soname(self):
        '''
        Test that the soname is set correctly for shared libraries. This can't
        be an ordinary test case because we need to run `readelf` and actually
        check the soname.
        https://github.com/mesonbuild/meson/issues/785
        '''
        testdir = os.path.join(self.rust_test_dir, '2 sharedlib')
        self.init(testdir)
        self.build()
        lib1 = os.path.join(self.builddir, 'cdylib/libnot_so_rusty.so')
        soname = get_soname(lib1)
        self.assertEqual(soname, 'libnot_so_rusty.so')

    def test_custom_soname(self):
        '''
        Test that the soname is set correctly for shared libraries when
        a custom prefix and/or suffix is used. This can't be an ordinary test
        case because we need to run `readelf` and actually check the soname.
        https://github.com/mesonbuild/meson/issues/785
        '''
        testdir = os.path.join(self.common_test_dir, '24 library versions')
        self.init(testdir)
        self.build()
        lib1 = os.path.join(self.builddir, 'prefixsomelib.suffix')
        soname = get_soname(lib1)
        self.assertEqual(soname, 'prefixsomelib.suffix')

    def test_pic(self):
        '''
        Test that -fPIC is correctly added to static libraries when b_staticpic
        is true and not when it is false. This can't be an ordinary test case
        because we need to inspect the compiler database.
        '''
        if is_windows() or is_cygwin() or is_osx():
            raise SkipTest('PIC not relevant')

        testdir = os.path.join(self.common_test_dir, '3 static')
        self.init(testdir)
        compdb = self.get_compdb()
        self.assertIn('-fPIC', compdb[0]['command'])
        self.setconf('-Db_staticpic=false')
        # Regenerate build
        self.build()
        compdb = self.get_compdb()
        self.assertNotIn('-fPIC', compdb[0]['command'])

    @mock.patch.dict(os.environ)
    def test_pkgconfig_gen(self):
        '''
        Test that generated pkg-config files can be found and have the correct
        version and link args. This can't be an ordinary test case because we
        need to run pkg-config outside of a Meson build file.
        https://github.com/mesonbuild/meson/issues/889
        '''
        testdir = os.path.join(self.common_test_dir, '44 pkgconfig-gen')
        self.init(testdir)
        env = get_fake_env(testdir, self.builddir, self.prefix)
        kwargs = {'required': True, 'silent': True, 'native': MachineChoice.HOST}
        os.environ['PKG_CONFIG_LIBDIR'] = self.privatedir
        foo_dep = PkgConfigDependency('libfoo', env, kwargs)
        self.assertTrue(foo_dep.found())
        self.assertEqual(foo_dep.get_version(), '1.0')
        self.assertIn('-lfoo', foo_dep.get_link_args())
        self.assertEqual(foo_dep.get_variable(pkgconfig='foo'), 'bar')
        self.assertPathEqual(foo_dep.get_variable(pkgconfig='datadir'), '/usr/data')

        libhello_nolib = PkgConfigDependency('libhello_nolib', env, kwargs)
        self.assertTrue(libhello_nolib.found())
        self.assertEqual(libhello_nolib.get_link_args(), [])
        self.assertEqual(libhello_nolib.get_compile_args(), [])
        self.assertEqual(libhello_nolib.get_variable(pkgconfig='foo'), 'bar')
        self.assertEqual(libhello_nolib.get_variable(pkgconfig='prefix'), self.prefix)
        impl = libhello_nolib.pkgconfig
        if not isinstance(impl, PkgConfigCLI) or version_compare(impl.pkgbin_version, ">=0.29.1"):
            self.assertEqual(libhello_nolib.get_variable(pkgconfig='escaped_var'), r'hello\ world')
        self.assertEqual(libhello_nolib.get_variable(pkgconfig='unescaped_var'), 'hello world')

        cc = detect_c_compiler(env, MachineChoice.HOST)
        if cc.get_id() in {'gcc', 'clang'}:
            for name in {'ct', 'ct0'}:
                ct_dep = PkgConfigDependency(name, env, kwargs)
                self.assertTrue(ct_dep.found())
                self.assertIn('-lct', ct_dep.get_link_args(raw=True))

    def test_pkgconfig_gen_deps(self):
        '''
        Test that generated pkg-config files correctly handle dependencies
        '''
        testdir = os.path.join(self.common_test_dir, '44 pkgconfig-gen')
        self.init(testdir)
        privatedir1 = self.privatedir

        self.new_builddir()
        testdir = os.path.join(self.common_test_dir, '44 pkgconfig-gen', 'dependencies')
        self.init(testdir, override_envvars={'PKG_CONFIG_LIBDIR': privatedir1})
        privatedir2 = self.privatedir

        env = {
            'PKG_CONFIG_LIBDIR': os.pathsep.join([privatedir1, privatedir2]),
            'PKG_CONFIG_SYSTEM_LIBRARY_PATH': '/usr/lib',
        }
        self._run([PKG_CONFIG, 'dependency-test', '--validate'], override_envvars=env)

        # pkg-config strips some duplicated flags so we have to parse the
        # generated file ourself.
        expected = {
            'Requires': 'libexposed',
            'Requires.private': 'libfoo >= 1.0',
            'Libs': '-L${libdir} -llibmain -pthread -lcustom',
            'Libs.private': '-lcustom2 -L${libdir} -llibinternal',
            'Cflags': '-I${includedir} -pthread -DCUSTOM',
        }
        if is_osx() or is_haiku():
            expected['Cflags'] = expected['Cflags'].replace('-pthread ', '')
        with open(os.path.join(privatedir2, 'dependency-test.pc'), encoding='utf-8') as f:
            matched_lines = 0
            for line in f:
                parts = line.split(':', 1)
                if parts[0] in expected:
                    key = parts[0]
                    val = parts[1].strip()
                    expected_val = expected[key]
                    self.assertEqual(expected_val, val)
                    matched_lines += 1
            self.assertEqual(len(expected), matched_lines)

        cmd = [PKG_CONFIG, 'requires-test']
        out = self._run(cmd + ['--print-requires'], override_envvars=env).strip().split('\n')
        if not is_openbsd():
            self.assertEqual(sorted(out), sorted(['libexposed', 'libfoo >= 1.0', 'libhello']))
        else:
            self.assertEqual(sorted(out), sorted(['libexposed', 'libfoo>=1.0', 'libhello']))

        cmd = [PKG_CONFIG, 'requires-private-test']
        out = self._run(cmd + ['--print-requires-private'], override_envvars=env).strip().split('\n')
        if not is_openbsd():
            self.assertEqual(sorted(out), sorted(['libexposed', 'libfoo >= 1.0', 'libhello']))
        else:
            self.assertEqual(sorted(out), sorted(['libexposed', 'libfoo>=1.0', 'libhello']))

        cmd = [PKG_CONFIG, 'pub-lib-order']
        out = self._run(cmd + ['--libs'], override_envvars=env).strip().split()
        self.assertEqual(out, ['-llibmain2', '-llibinternal'])

        # See common/44 pkgconfig-gen/meson.build for description of the case this test
        with open(os.path.join(privatedir1, 'simple2.pc'), encoding='utf-8') as f:
            content = f.read()
            self.assertIn('Libs: -L${libdir} -lsimple2 -lsimple1', content)
            self.assertIn('Libs.private: -lz', content)

        with open(os.path.join(privatedir1, 'simple3.pc'), encoding='utf-8') as f:
            content = f.read()
            self.assertEqual(1, content.count('-lsimple3'))

        with open(os.path.join(privatedir1, 'simple5.pc'), encoding='utf-8') as f:
            content = f.read()
            self.assertNotIn('-lstat2', content)

    @mock.patch.dict(os.environ)
    def test_pkgconfig_uninstalled(self):
        testdir = os.path.join(self.common_test_dir, '44 pkgconfig-gen')
        self.init(testdir)
        self.build()

        os.environ['PKG_CONFIG_LIBDIR'] = os.path.join(self.builddir, 'meson-uninstalled')
        if is_cygwin():
            os.environ['PATH'] += os.pathsep + self.builddir

        self.new_builddir()
        testdir = os.path.join(self.common_test_dir, '44 pkgconfig-gen', 'dependencies')
        self.init(testdir)
        self.build()
        self.run_tests()

    def test_pkg_unfound(self):
        testdir = os.path.join(self.unit_test_dir, '23 unfound pkgconfig')
        self.init(testdir)
        with open(os.path.join(self.privatedir, 'somename.pc'), encoding='utf-8') as f:
            pcfile = f.read()
        self.assertNotIn('blub_blob_blib', pcfile)

    def test_symlink_builddir(self) -> None:
        '''
        Test using a symlink as either the builddir for "setup" or
        the argument for "-C".
        '''
        testdir = os.path.join(self.common_test_dir, '1 trivial')

        symdir = f'{self.builddir}-symlink'
        os.symlink(self.builddir, symdir)
        self.change_builddir(symdir)

        self.init(testdir)
        self.build()
        self._run(self.mtest_command)

    @skipIfNoPkgconfig
    def test_qtdependency_pkgconfig_detection(self):
        '''
        Test that qt4 and qt5 detection with pkgconfig works.
        '''
        # Verify Qt4 or Qt5 can be found with pkg-config
        qt4 = subprocess.call([PKG_CONFIG, '--exists', 'QtCore'])
        qt5 = subprocess.call([PKG_CONFIG, '--exists', 'Qt5Core'])
        testdir = os.path.join(self.framework_test_dir, '4 qt')
        self.init(testdir, extra_args=['-Dmethod=pkg-config'])
        # Confirm that the dependency was found with pkg-config
        mesonlog = self.get_meson_log_raw()
        if qt4 == 0:
            self.assertRegex(mesonlog,
                             r'Run-time dependency qt4 \(modules: Core\) found: YES 4.* \(pkg-config\)')
        if qt5 == 0:
            self.assertRegex(mesonlog,
                             r'Run-time dependency qt5 \(modules: Core\) found: YES 5.* \(pkg-config\)')

    @skip_if_not_base_option('b_sanitize')
    def test_generate_gir_with_address_sanitizer(self):
        if is_cygwin():
            raise SkipTest('asan not available on Cygwin')
        if is_openbsd():
            raise SkipTest('-fsanitize=address is not supported on OpenBSD')

        testdir = os.path.join(self.framework_test_dir, '7 gnome')
        self.init(testdir, extra_args=['-Db_sanitize=address', '-Db_lundef=false'])
        self.build()

    def test_qt5dependency_no_lrelease(self):
        '''
        Test that qt5 detection with qmake works. This can't be an ordinary
        test case because it involves setting the environment.
        '''
        testdir = os.path.join(self.framework_test_dir, '4 qt')
        def _no_lrelease(self, prog, *args, **kwargs):
            if 'lrelease' in prog:
                return NonExistingExternalProgram(prog)
            return self._interpreter.find_program_impl(prog, *args, **kwargs)
        with mock.patch.object(mesonbuild.modules.ModuleState, 'find_program', _no_lrelease):
            self.init(testdir, inprocess=True, extra_args=['-Dmethod=qmake', '-Dexpect_lrelease=false'])

    def test_qt5dependency_qmake_detection(self):
        '''
        Test that qt5 detection with qmake works. This can't be an ordinary
        test case because it involves setting the environment.
        '''
        # Verify that qmake is for Qt5
        if not shutil.which('qmake-qt5'):
            if not shutil.which('qmake'):
                raise SkipTest('QMake not found')
            output = subprocess.getoutput('qmake --version')
            if 'Qt version 5' not in output:
                raise SkipTest('Qmake found, but it is not for Qt 5.')
        # Disable pkg-config codepath and force searching with qmake/qmake-qt5
        testdir = os.path.join(self.framework_test_dir, '4 qt')
        self.init(testdir, extra_args=['-Dmethod=qmake'])
        # Confirm that the dependency was found with qmake
        mesonlog = self.get_meson_log_raw()
        self.assertRegex(mesonlog,
                         r'Run-time dependency qt5 \(modules: Core\) found: YES .* \(qmake\)\n')

    def test_qt6dependency_qmake_detection(self):
        '''
        Test that qt6 detection with qmake works. This can't be an ordinary
        test case because it involves setting the environment.
        '''
        # Verify that qmake is for Qt6
        if not shutil.which('qmake6'):
            if not shutil.which('qmake'):
                raise SkipTest('QMake not found')
            output = subprocess.getoutput('qmake --version')
            if 'Qt version 6' not in output:
                raise SkipTest('Qmake found, but it is not for Qt 6.')
        # Disable pkg-config codepath and force searching with qmake/qmake-qt6
        testdir = os.path.join(self.framework_test_dir, '4 qt')
        self.init(testdir, extra_args=['-Dmethod=qmake'])
        # Confirm that the dependency was found with qmake
        mesonlog = self.get_meson_log_raw()
        self.assertRegex(mesonlog,
                         r'Run-time dependency qt6 \(modules: Core\) found: YES .* \(qmake\)\n')

    def glob_sofiles_without_privdir(self, g):
        files = glob(g)
        return [f for f in files if not f.endswith('.p')]

    def _test_soname_impl(self, libpath, install):
        if is_cygwin() or is_osx():
            raise SkipTest('Test only applicable to ELF and linuxlike sonames')

        testdir = os.path.join(self.unit_test_dir, '1 soname')
        self.init(testdir)
        self.build()
        if install:
            self.install()

        # File without aliases set.
        nover = os.path.join(libpath, 'libnover.so')
        self.assertPathExists(nover)
        self.assertFalse(os.path.islink(nover))
        self.assertEqual(get_soname(nover), 'libnover.so')
        self.assertEqual(len(self.glob_sofiles_without_privdir(nover[:-3] + '*')), 1)

        # File with version set
        verset = os.path.join(libpath, 'libverset.so')
        self.assertPathExists(verset + '.4.5.6')
        self.assertEqual(os.readlink(verset), 'libverset.so.4')
        self.assertEqual(get_soname(verset), 'libverset.so.4')
        self.assertEqual(len(self.glob_sofiles_without_privdir(verset[:-3] + '*')), 3)

        # File with soversion set
        soverset = os.path.join(libpath, 'libsoverset.so')
        self.assertPathExists(soverset + '.1.2.3')
        self.assertEqual(os.readlink(soverset), 'libsoverset.so.1.2.3')
        self.assertEqual(get_soname(soverset), 'libsoverset.so.1.2.3')
        self.assertEqual(len(self.glob_sofiles_without_privdir(soverset[:-3] + '*')), 2)

        # File with version and soversion set to same values
        settosame = os.path.join(libpath, 'libsettosame.so')
        self.assertPathExists(settosame + '.7.8.9')
        self.assertEqual(os.readlink(settosame), 'libsettosame.so.7.8.9')
        self.assertEqual(get_soname(settosame), 'libsettosame.so.7.8.9')
        self.assertEqual(len(self.glob_sofiles_without_privdir(settosame[:-3] + '*')), 2)

        # File with version and soversion set to different values
        bothset = os.path.join(libpath, 'libbothset.so')
        self.assertPathExists(bothset + '.1.2.3')
        self.assertEqual(os.readlink(bothset), 'libbothset.so.1.2.3')
        self.assertEqual(os.readlink(bothset + '.1.2.3'), 'libbothset.so.4.5.6')
        self.assertEqual(get_soname(bothset), 'libbothset.so.1.2.3')
        self.assertEqual(len(self.glob_sofiles_without_privdir(bothset[:-3] + '*')), 3)

        # A shared_module that is not linked to anything
        module = os.path.join(libpath, 'libsome_module.so')
        self.assertPathExists(module)
        self.assertFalse(os.path.islink(module))
        self.assertEqual(get_soname(module), None)

        # A shared_module that is not linked to an executable with link_with:
        module = os.path.join(libpath, 'liblinked_module1.so')
        self.assertPathExists(module)
        self.assertFalse(os.path.islink(module))
        self.assertEqual(get_soname(module), 'liblinked_module1.so')

        # A shared_module that is not linked to an executable with dependencies:
        module = os.path.join(libpath, 'liblinked_module2.so')
        self.assertPathExists(module)
        self.assertFalse(os.path.islink(module))
        self.assertEqual(get_soname(module), 'liblinked_module2.so')

    def test_soname(self):
        self._test_soname_impl(self.builddir, False)

    def test_installed_soname(self):
        libdir = self.installdir + os.path.join(self.prefix, self.libdir)
        self._test_soname_impl(libdir, True)

    @skip_if_not_base_option('b_sanitize')
    def test_c_link_args_and_env(self):
        '''
        Test that the CFLAGS / CXXFLAGS environment variables are
        included on the linker command line when c_link_args is
        set but c_args is not.
        '''
        if is_cygwin():
            raise SkipTest('asan not available on Cygwin')
        if is_openbsd():
            raise SkipTest('-fsanitize=address is not supported on OpenBSD')
        if is_sunos():
            raise SkipTest('-fsanitize=address is not supported on illumos')

        testdir = os.path.join(self.common_test_dir, '1 trivial')
        env = {'CFLAGS': '-fsanitize=address'}
        self.init(testdir, extra_args=['-Dc_link_args="-L/usr/lib"'],
                  override_envvars=env)
        self.build()

    def test_compiler_check_flags_order(self):
        '''
        Test that compiler check flags override all other flags. This can't be
        an ordinary test case because it needs the environment to be set.
        '''
        testdir = os.path.join(self.common_test_dir, '36 has function')
        env = get_fake_env(testdir, self.builddir, self.prefix)
        cpp = detect_cpp_compiler(env, MachineChoice.HOST)
        Oflag = '-O3'
        OflagCPP = Oflag
        if cpp.get_id() in ('clang', 'gcc'):
            # prevent developers from adding "int main(int argc, char **argv)"
            # to small Meson checks unless these parameters are actually used
            OflagCPP += ' -Werror=unused-parameter'
        env = {'CFLAGS': Oflag,
               'CXXFLAGS': OflagCPP}
        self.init(testdir, override_envvars=env)
        cmds = self.get_meson_log_compiler_checks()
        for cmd in cmds:
            if cmd[0] == 'ccache':
                cmd = cmd[1:]
            # Verify that -I flags from the `args` kwarg are first
            # This is set in the '36 has function' test case
            self.assertEqual(cmd[1], '-I/tmp')
            # Verify that -O3 set via the environment is overridden by -O0
            Oargs = [arg for arg in cmd if arg.startswith('-O')]
            self.assertEqual(Oargs, [Oflag, '-O0'])

    def _test_stds_impl(self, testdir: str, compiler: 'Compiler') -> None:
        has_cpp17 = (compiler.get_id() not in {'clang', 'gcc'} or
                     compiler.get_id() == 'clang' and _clang_at_least(compiler, '>=5.0.0', '>=9.1') or
                     compiler.get_id() == 'gcc' and version_compare(compiler.version, '>=5.0.0'))
        has_cpp2a_c17 = (compiler.get_id() not in {'clang', 'gcc'} or
                         compiler.get_id() == 'clang' and _clang_at_least(compiler, '>=6.0.0', '>=10.0') or
                         compiler.get_id() == 'gcc' and version_compare(compiler.version, '>=8.0.0'))
        has_cpp20 = (compiler.get_id() not in {'clang', 'gcc'} or
                     compiler.get_id() == 'clang' and _clang_at_least(compiler, '>=10.0.0', None) or
                     compiler.get_id() == 'gcc' and version_compare(compiler.version, '>=10.0.0'))
        has_cpp2b = (compiler.get_id() not in {'clang', 'gcc'} or
                     compiler.get_id() == 'clang' and _clang_at_least(compiler, '>=12.0.0', None) or
                     compiler.get_id() == 'gcc' and version_compare(compiler.version, '>=11.0.0'))
        has_cpp23 = (compiler.get_id() not in {'clang', 'gcc'} or
                     compiler.get_id() == 'clang' and _clang_at_least(compiler, '>=17.0.0', None) or
                     compiler.get_id() == 'gcc' and version_compare(compiler.version, '>=11.0.0'))
        has_cpp26 = (compiler.get_id() not in {'clang', 'gcc'} or
                     compiler.get_id() == 'clang' and _clang_at_least(compiler, '>=17.0.0', None) or
                     compiler.get_id() == 'gcc' and version_compare(compiler.version, '>=14.0.0'))
        has_c18 = (compiler.get_id() not in {'clang', 'gcc'} or
                   compiler.get_id() == 'clang' and _clang_at_least(compiler, '>=8.0.0', '>=11.0') or
                   compiler.get_id() == 'gcc' and version_compare(compiler.version, '>=8.0.0'))
        # Check that all the listed -std=xxx options for this compiler work just fine when used
        # https://en.wikipedia.org/wiki/Xcode#Latest_versions
        # https://www.gnu.org/software/gcc/projects/cxx-status.html
        key = OptionKey(f'{compiler.language}_std')
        for v in compiler.get_options()[key].choices:
            # we do it like this to handle gnu++17,c++17 and gnu17,c17 cleanly
            # thus, C++ first
            if '++17' in v and not has_cpp17:
                continue
            elif '++2a' in v and not has_cpp2a_c17:  # https://en.cppreference.com/w/cpp/compiler_support
                continue
            elif '++20' in v and not has_cpp20:
                continue
            elif '++2b' in v and not has_cpp2b:
                continue
            elif '++23' in v and not has_cpp23:
                continue
            elif ('++26' in v or '++2c' in v) and not has_cpp26:
                continue
            # now C
            elif '17' in v and not has_cpp2a_c17:
                continue
            elif '18' in v and not has_c18:
                continue
            self.init(testdir, extra_args=[f'-D{key!s}={v}'])
            cmd = self.get_compdb()[0]['command']
            # c++03 and gnu++03 are not understood by ICC, don't try to look for them
            skiplist = frozenset([
                ('intel', 'c++03'),
                ('intel', 'gnu++03')])
            if v != 'none' and not (compiler.get_id(), v) in skiplist:
                cmd_std = f" -std={v} "
                self.assertIn(cmd_std, cmd)
            try:
                self.build()
            except Exception:
                print(f'{key!s} was {v!r}')
                raise
            self.wipe()
        # Check that an invalid std option in CFLAGS/CPPFLAGS fails
        # Needed because by default ICC ignores invalid options
        cmd_std = '-std=FAIL'
        if compiler.language == 'c':
            env_flag_name = 'CFLAGS'
        elif compiler.language == 'cpp':
            env_flag_name = 'CXXFLAGS'
        else:
            raise NotImplementedError(f'Language {compiler.language} not defined.')
        env = {}
        env[env_flag_name] = cmd_std
        with self.assertRaises((subprocess.CalledProcessError, EnvironmentException),
                               msg='C compiler should have failed with -std=FAIL'):
            self.init(testdir, override_envvars = env)
            # ICC won't fail in the above because additional flags are needed to
            # make unknown -std=... options errors.
            self.build()

    def test_compiler_c_stds(self):
        '''
        Test that C stds specified for this compiler can all be used. Can't be
        an ordinary test because it requires passing options to meson.
        '''
        testdir = os.path.join(self.common_test_dir, '1 trivial')
        env = get_fake_env(testdir, self.builddir, self.prefix)
        cc = detect_c_compiler(env, MachineChoice.HOST)
        self._test_stds_impl(testdir, cc)

    def test_compiler_cpp_stds(self):
        '''
        Test that C++ stds specified for this compiler can all be used. Can't
        be an ordinary test because it requires passing options to meson.
        '''
        testdir = os.path.join(self.common_test_dir, '2 cpp')
        env = get_fake_env(testdir, self.builddir, self.prefix)
        cpp = detect_cpp_compiler(env, MachineChoice.HOST)
        self._test_stds_impl(testdir, cpp)

    def test_unity_subproj(self):
        testdir = os.path.join(self.common_test_dir, '42 subproject')
        self.init(testdir, extra_args='--unity=subprojects')
        pdirs = glob(os.path.join(self.builddir, 'subprojects/sublib/simpletest*.p'))
        self.assertEqual(len(pdirs), 1)
        self.assertPathExists(os.path.join(pdirs[0], 'simpletest-unity0.c'))
        sdirs = glob(os.path.join(self.builddir, 'subprojects/sublib/*sublib*.p'))
        self.assertEqual(len(sdirs), 1)
        self.assertPathExists(os.path.join(sdirs[0], 'sublib-unity0.c'))
        self.assertPathDoesNotExist(os.path.join(self.builddir, 'user@exe/user-unity.c'))
        self.build()

    def test_installed_modes(self):
        '''
        Test that files installed by these tests have the correct permissions.
        Can't be an ordinary test because our installed_files.txt is very basic.
        '''
        # Test file modes
        testdir = os.path.join(self.common_test_dir, '12 data')
        self.init(testdir)
        self.install()

        f = os.path.join(self.installdir, 'etc', 'etcfile.dat')
        found_mode = stat.filemode(os.stat(f).st_mode)
        want_mode = 'rw-------'
        self.assertEqual(want_mode, found_mode[1:])

        f = os.path.join(self.installdir, 'usr', 'bin', 'runscript.sh')
        statf = os.stat(f)
        found_mode = stat.filemode(statf.st_mode)
        want_mode = 'rwxr-sr-x'
        self.assertEqual(want_mode, found_mode[1:])
        if os.getuid() == 0:
            # The chown failed nonfatally if we're not root
            self.assertEqual(0, statf.st_uid)
            self.assertEqual(0, statf.st_gid)

        f = os.path.join(self.installdir, 'usr', 'share', 'progname',
                         'fileobject_datafile.dat')
        orig = os.path.join(testdir, 'fileobject_datafile.dat')
        statf = os.stat(f)
        statorig = os.stat(orig)
        found_mode = stat.filemode(statf.st_mode)
        orig_mode = stat.filemode(statorig.st_mode)
        self.assertEqual(orig_mode[1:], found_mode[1:])
        self.assertEqual(os.getuid(), statf.st_uid)
        if os.getuid() == 0:
            # The chown failed nonfatally if we're not root
            self.assertEqual(0, statf.st_gid)

        self.wipe()
        # Test directory modes
        testdir = os.path.join(self.common_test_dir, '59 install subdir')
        self.init(testdir)
        self.install()

        f = os.path.join(self.installdir, 'usr', 'share', 'sub1', 'second.dat')
        statf = os.stat(f)
        found_mode = stat.filemode(statf.st_mode)
        want_mode = 'rwxr-x--x'
        self.assertEqual(want_mode, found_mode[1:])
        if os.getuid() == 0:
            # The chown failed nonfatally if we're not root
            self.assertEqual(0, statf.st_uid)

    def test_installed_modes_extended(self):
        '''
        Test that files are installed with correct permissions using install_mode.
        '''
        testdir = os.path.join(self.common_test_dir, '190 install_mode')
        self.init(testdir)
        self.build()
        self.install()

        for fsobj, want_mode in [
                ('bin', 'drwxr-x---'),
                ('bin/runscript.sh', '-rwxr-sr-x'),
                ('bin/trivialprog', '-rwxr-sr-x'),
                ('include', 'drwxr-x---'),
                ('include/config.h', '-rw-rwSr--'),
                ('include/rootdir.h', '-r--r--r--'),
                ('lib', 'drwxr-x---'),
                ('lib/libstat.a', '-rw---Sr--'),
                ('share', 'drwxr-x---'),
                ('share/man', 'drwxr-x---'),
                ('share/man/man1', 'drwxr-x---'),
                ('share/man/man1/foo.1', '-r--r--r--'),
                ('share/sub1', 'drwxr-x---'),
                ('share/sub1/second.dat', '-rwxr-x--x'),
                ('subdir', 'drwxr-x---'),
                ('subdir/data.dat', '-rw-rwSr--'),
        ]:
            f = os.path.join(self.installdir, 'usr', *fsobj.split('/'))
            found_mode = stat.filemode(os.stat(f).st_mode)
            self.assertEqual(want_mode, found_mode,
                             msg=('Expected file %s to have mode %s but found %s instead.' %
                                  (fsobj, want_mode, found_mode)))
        # Ensure that introspect --installed works on all types of files
        # FIXME: also verify the files list
        self.introspect('--installed')

    def test_install_umask(self):
        '''
        Test that files are installed with correct permissions using default
        install umask of 022, regardless of the umask at time the worktree
        was checked out or the build was executed.
        '''
        # Copy source tree to a temporary directory and change permissions
        # there to simulate a checkout with umask 002.
        orig_testdir = os.path.join(self.unit_test_dir, '26 install umask')
        # Create a new testdir under tmpdir.
        tmpdir = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(windows_proof_rmtree, tmpdir)
        testdir = os.path.join(tmpdir, '26 install umask')
        # Copy the tree using shutil.copyfile, which will use the current umask
        # instead of preserving permissions of the old tree.
        save_umask = os.umask(0o002)
        self.addCleanup(os.umask, save_umask)
        shutil.copytree(orig_testdir, testdir, copy_function=shutil.copyfile)
        # Preserve the executable status of subdir/sayhello though.
        os.chmod(os.path.join(testdir, 'subdir', 'sayhello'), 0o775)
        self.init(testdir)
        # Run the build under a 027 umask now.
        os.umask(0o027)
        self.build()
        # And keep umask 027 for the install step too.
        self.install()

        for executable in [
                'bin/prog',
                'share/subdir/sayhello',
        ]:
            f = os.path.join(self.installdir, 'usr', *executable.split('/'))
            found_mode = stat.filemode(os.stat(f).st_mode)
            want_mode = '-rwxr-xr-x'
            self.assertEqual(want_mode, found_mode,
                             msg=('Expected file %s to have mode %s but found %s instead.' %
                                  (executable, want_mode, found_mode)))

        for directory in [
                'usr',
                'usr/bin',
                'usr/include',
                'usr/share',
                'usr/share/man',
                'usr/share/man/man1',
                'usr/share/subdir',
        ]:
            f = os.path.join(self.installdir, *directory.split('/'))
            found_mode = stat.filemode(os.stat(f).st_mode)
            want_mode = 'drwxr-xr-x'
            self.assertEqual(want_mode, found_mode,
                             msg=('Expected directory %s to have mode %s but found %s instead.' %
                                  (directory, want_mode, found_mode)))

        for datafile in [
                'include/sample.h',
                'share/datafile.cat',
                'share/file.dat',
                'share/man/man1/prog.1',
                'share/subdir/datafile.dog',
        ]:
            f = os.path.join(self.installdir, 'usr', *datafile.split('/'))
            found_mode = stat.filemode(os.stat(f).st_mode)
            want_mode = '-rw-r--r--'
            self.assertEqual(want_mode, found_mode,
                             msg=('Expected file %s to have mode %s but found %s instead.' %
                                  (datafile, want_mode, found_mode)))

    def test_cpp_std_override(self):
        testdir = os.path.join(self.unit_test_dir, '6 std override')
        self.init(testdir)
        compdb = self.get_compdb()
        # Don't try to use -std=c++03 as a check for the
        # presence of a compiler flag, as ICC does not
        # support it.
        for i in compdb:
            if 'prog98' in i['file']:
                c98_comp = i['command']
            if 'prog11' in i['file']:
                c11_comp = i['command']
            if 'progp' in i['file']:
                plain_comp = i['command']
        self.assertNotEqual(len(plain_comp), 0)
        self.assertIn('-std=c++98', c98_comp)
        self.assertNotIn('-std=c++11', c98_comp)
        self.assertIn('-std=c++11', c11_comp)
        self.assertNotIn('-std=c++98', c11_comp)
        self.assertNotIn('-std=c++98', plain_comp)
        self.assertNotIn('-std=c++11', plain_comp)
        # Now werror
        self.assertIn('-Werror', plain_comp)
        self.assertNotIn('-Werror', c98_comp)

    @requires_cpp_module_caps('modules', 'partitions', compiler='gcc')
    def test_gcc_cpp_modules(self):
        # The library provides a module, imported by an executable that merely
        # links it, plus partitions and an explicit-opt-in target; each test()
        # exercises a producer/consumer pair across the link. A single-class
        # build resolves its BMIs through per-TU mappers just as a multi-class
        # one does -- only the directory the mappers name differs.
        self.build_and_check_modules('139 gcc cpp modules',
                                     setup_not_contains=['divergent dialects'],
                                     bmis=['modlib', 'pkg', 'pkg:part', 'pkg:impl',
                                           'kwmod', 'genmod', 'dot.mod.sub'])
        self.check_gcc_module_mappers()

    @requires_cpp_module_caps('modules', 'partitions', compiler='gcc')
    def test_gcc_generated_module_sources(self):
        # The module surface built entirely from generated sources: a primary
        # interface, an interface partition it re-exports, an implementation
        # unit, a consumer that only imports, a re-exporting interface, and two
        # private-by-construction modules sharing a name. Each test() links a
        # producer/consumer pair, so a wrong BMI (or a private-module collision)
        # makes a program exit nonzero.
        self.build_and_check_modules('201 generated module sources',
                                     bmis=['pkg', 'pkg:part', 'wrap'])

    @requires_cpp_module_caps('modules', 'module_interfaces', compiler='gcc')
    def test_gcc_cpp_module_interfaces(self):
        # A .cc source declared a module interface via cpp_module_interfaces
        # (string and files() forms) provides its module across a link boundary.
        self.build_and_check_modules('162 cpp module interfaces',
                                     bmis=['mymod', 'filemod'])

    @requires_cpp_module_caps('modules', compiler='gcc')
    def test_gcc_cpp_modules_pch(self):
        # GCC cannot combine PCH with modules (a -fmodules compile rejects
        # any .gch as invalid), so cpp_pch on a module-enabled target is
        # dropped with a warning and the build behaves as with b_pch=false.
        self.build_and_check_modules('170 cpp modules pch',
                                     setup_contains=['precompiled header is disabled'],
                                     bmis=['modlib'],
                                     ninja_not_contains=['.gch'])

    @requires_cpp_module_caps('modules', compiler='gcc')
    def test_gcc_cpp_module_rebuild_on_interface_change(self):
        self.check_module_rebuild('139 gcc cpp modules', edit_file='modlib.cppm')

    @requires_cpp_module_caps('modules', 'module_interfaces', compiler='gcc')
    def test_gcc_cpp_module_graph_mutation(self):
        self.check_module_graph_mutation('164 cpp module graph mutation')

    @requires_cpp_module_caps('modules', compiler='gcc')
    def test_gcc_cpp_modules_ts_legacy(self):
        # A bare -fmodules-ts cpp_arg (with no real module enablement) must not be
        # routed to the GCC P1689 pipeline -- that would hard-require
        # GCC >= 14 and regress a target that used to build. provided-modules.json
        # is emitted only by the P1689 collate, so its absence proves the legacy
        # scan path is used instead.
        self.init(os.path.join(self.unit_test_dir, '147 gcc fmodules-ts legacy'))
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            self.assertNotIn('provided-modules.json', f.read())

    @requires_cpp_module_caps('regex_scanner', compiler='gcc')
    def test_gcc_cpp_modules_regex_scanner(self):
        # A flat named module in plain .cpp sources with only a bare modules
        # flag builds via the legacy regex scanner. No no-op check: the regex
        # dyndep declares a phantom '<module>.ifc' output GCC never writes
        # (depscan.module_name_for hardcodes the cl spelling), so the module
        # edge stays forever dirty -- a known limitation of the legacy path.
        flag = regex_scanner_flag(self.host_cpp_compiler())
        self.build_and_check_modules('163 cpp modules regex scanner',
                                     extra_args=[f'-Dmodules_flag={flag}'],
                                     noop_check=False)
        # The P1689 collate is not engaged; GCC itself wrote the BMI into the
        # shared cache in the build dir.
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            self.assertNotIn('provided-modules.json', f.read())
        self.assertTrue(os.path.isfile(self.bmi_path('flatmod')), 'missing BMI flatmod')

    @requires_cpp_module_caps('regex_scanner', compiler='gcc')
    def test_gcc_cpp_modules_regex_scanner_diagnostics(self):
        # The regex scanner handles flat named modules only. Partitions and
        # import std must fail its scan with a clear error naming the
        # limitation, not silently under-scan into raw compiler errors.
        testdir = os.path.join(self.unit_test_dir, '163 cpp modules regex scanner')
        flag = regex_scanner_flag(self.host_cpp_compiler())
        cases = {
            'partition': 'module partitions are not supported',
            'importpart': 'module partitions are not supported',
            'importstd': 'import std is not supported',
        }
        for mode, needle in cases.items():
            with self.subTest(mode=mode):
                self.new_builddir()
                self.init(testdir, extra_args=[f'-Dmode={mode}', f'-Dmodules_flag={flag}'])
                with self.assertRaises(subprocess.CalledProcessError) as cm:
                    self.build()
                self.assertIn(needle, cm.exception.stdout)

    @requires_cpp_module_caps('modules', 'partitions', compiler='clang')
    def test_clang_cpp_modules(self):
        # oddname.pcm's module name differs from its file name, proving the
        # harvest names BMIs from the scan, not the source.
        # ninja_not_contains pins the single-class regression: a one-class
        # build must keep the flat pcm.cache (no class subdir) and synthesize
        # no BMI-only variant.
        self.build_and_check_modules('156 clang cpp modules',
                                     setup_not_contains=['divergent dialects'],
                                     bmis=['modlib', 'pkg', 'pkg:part', 'pkg:impl',
                                           'kwmod', 'genmod', 'oddname', 'dot.mod.sub'],
                                     ninja_not_contains=('--precompile', '@bmi@', 'pcm.cache/'))
        # Module ARGS must carry the ccache-defeating -fmodules -fno-modules
        # pair (inert to clang; makes ccache fall back instead of serving
        # stale objects).
        saw_ccache_pair = False
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if line.strip().startswith('ARGS =') and '-fprebuilt-module-path' in line:
                    self.assertIn('-fmodules -fno-modules', line)
                    saw_ccache_pair = True
        self.assertTrue(saw_ccache_pair, 'no module compile carried the ccache-defeating pair')

    @requires_cpp_module_caps('modules', 'partitions', compiler='clang')
    def test_clang_generated_module_sources(self):
        # The generated-source module surface on clang: primary interface,
        # re-exported interface partition, implementation unit, import-only
        # consumer, a re-exporting interface, and two private-by-construction
        # modules of the same name. See test_gcc_generated_module_sources.
        self.build_and_check_modules('201 generated module sources',
                                     bmis=['pkg', 'pkg:part', 'wrap'],
                                     ninja_not_contains=('--precompile', '@bmi@', 'pcm.cache/'))

    @requires_cpp_module_caps('modules', 'module_interfaces', compiler='clang')
    def test_clang_cpp_module_interfaces(self):
        # A .cc source declared a module interface via cpp_module_interfaces gets
        # -x c++-module and its BMI is harvested into the shared cache by name.
        self.build_and_check_modules('162 cpp module interfaces',
                                     bmis=['mymod', 'filemod'],
                                     ninja_not_contains=('--precompile', '@bmi@', 'pcm.cache/'))

    @requires_cpp_module_caps('modules', compiler='clang')
    def test_clang_cpp_modules_user_fmodules(self):
        # A user who enables implicit Clang modules (-fmodules, via any arg
        # channel) must not have it silently cancelled by the trailing
        # -fno-modules of the ccache-defeating pair; the pair is dropped and
        # the user's -fmodules keeps ccache away on its own. Configure-only:
        # the interaction is fully visible in build.ninja.
        testdir = os.path.join(self.unit_test_dir, '156 clang cpp modules')
        self.init(testdir, extra_args=['-Dcpp_args=-fmodules'])
        saw_module_args = False
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                if line.strip().startswith('ARGS =') and '-fprebuilt-module-path' in line:
                    saw_module_args = True
                    self.assertIn('-fmodules', line)
                    self.assertNotIn('-fno-modules', line)
        self.assertTrue(saw_module_args, 'no module compile args found in build.ninja')

    @requires_cpp_module_caps('modules', compiler='clang')
    def test_clang_cpp_modules_pch(self):
        # PCH and modules in one target. The .pch is a real input of scan and
        # compile edges alike (both force-include it, and a cold scan must not
        # race the PCH build); a module interface unit gets no PCH at all (the
        # forced include would land before the module declaration).
        self.build_and_check_modules('170 cpp modules pch', bmis=['modlib'])
        pch_uses = 0
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            for line in f:
                ls = line.strip()
                if ls.startswith('build ') and 'MODULE_SCAN' in ls and 'main.cpp' in ls:
                    self.assertIn('prog.hh.pch', ls)
                if not ls.startswith('ARGS ='):
                    continue
                if '-x c++-module' in ls:
                    self.assertNotIn('-include-pch', ls)
                elif '-include-pch' in ls:
                    pch_uses += 1
        # main.cpp's scan and compile at least; the interface never.
        self.assertGreaterEqual(pch_uses, 2)

    @requires_cpp_module_caps('modules', compiler='clang')
    def test_clang_cpp_module_rebuild_on_interface_change(self):
        self.check_module_rebuild('156 clang cpp modules', edit_file='modlib.cppm')

    @requires_cpp_module_caps('modules', 'module_interfaces', compiler='clang')
    def test_clang_cpp_module_graph_mutation(self):
        self.check_module_graph_mutation('164 cpp module graph mutation')

    @requires_cpp_module_caps('modules', compiler='gcc')
    def test_gcc_modules_cross_machine(self):
        testdir = os.path.join(self.unit_test_dir, '148 gcc modules cross machine')
        # Use the system gcc/g++ as the cross "host" compiler so is_cross_build()
        # is true with a real working toolchain. A C++-module target on both
        # machines would share one gcm.cache with incompatible BMIs, so configure
        # must error.
        crossfile = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8')
        crossfile.write(textwrap.dedent(f'''\
            [binaries]
            c = '{shutil.which('gcc')}'
            cpp = '{shutil.which('g++')}'
            ar = '{shutil.which('ar')}'
            strip = '{shutil.which('strip')}'

            [host_machine]
            system = 'linux'
            cpu_family = 'x86_64'
            cpu = 'x86_64'
            endian = 'little'
            '''))
        crossfile.flush()
        self.meson_cross_files = [crossfile.name]
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.init(testdir)
        self.assertIn('more than one machine', cm.exception.output)

    @skip_if_not_language('fortran')
    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_fortran_links_cpp_module(self):
        # A Fortran target that links a P1689 C++-module library takes the
        # legacy regex-scan dyndep path, which used to feed that library's
        # depscan.json (never emitted by the P1689 pipeline) to depaccumulate,
        # so ninja failed on an input no rule produces. The Fortran main calls
        # into the modules-using C++ library, so a green run also proves interop.
        # Clang runs it too: the Fortran scanner is what such a target uses, on
        # every compiler.
        self.build_and_check_modules('155 fortran links gcc cpp module')

    @skip_if_not_language('fortran')
    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_fortran_cpp_modules_mix_diagnostics(self):
        # A target gets one module scanner, and a Fortran target uses the
        # Fortran one -- so C++ modules in that target are compiled with no
        # module flags at all. Left undiagnosed the build dies in the compiler
        # ("'module' only available with '-fmodules'"), so setup reports it.
        testdir = os.path.join(self.unit_test_dir, '196 fortran cpp modules diagnostics')

        # A C++ module source in the Fortran target can never work: a setup error
        # naming the shape that does.
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.init(testdir, extra_args=['-Dmode=module_source'])
        self.assertIn('both Fortran sources and C++ module sources', cm.exception.output)
        self.assertIn('Move the C++ module sources into a C++ library', cm.exception.output)

        # C++ TUs in a Fortran target that links a module provider cannot import
        # its modules -- but if none of them tries, the build is fine. A warning,
        # not an error, and the build still works.
        self.wipe()
        out = self.init(testdir, extra_args=['-Dmode=links_provider'])
        self.assertIn('cannot import those modules', out)
        self.build()
        self.run_tests()

        # The supported shape: the modules live in a C++ library the Fortran
        # target links. No mix diagnostic -- and in particular no claim that
        # clang-scan-deps is missing, which such a target does not use.
        self.wipe()
        out = self.init(testdir, extra_args=['-Dmode=supported'])
        self.assertNotIn('Fortran/C++', out)
        self.assertNotIn('clang-scan-deps', out)
        self.build()
        self.run_tests()

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='gcc')
    def test_gcc_module_cpp_std_divergence_builds(self):
        # A c++23 consumer of a c++20 module provider resolves through a
        # BMI-only variant in its own dialect class and runs correctly;
        # before BMI classes GCC silently shared the mismatched BMI here
        # (or hard-errored, depending on version), and setup could only warn.
        self.build_and_check_modules('145 gcc module cpp_std divergence',
                                     setup_not_contains=['divergent dialects'])
        self.assertEqual(len(self.bmi_variant_ids()), 1)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='gcc')
    def test_gcc_module_subproject_cpp_std_divergence_builds(self):
        # The same divergence across a subproject boundary: parent c++23
        # consumes a subproject module library built at c++20.
        self.build_and_check_modules('146 gcc module subproject cpp_std divergence',
                                     setup_not_contains=['divergent dialects'])
        self.assertEqual(len(self.bmi_variant_ids()), 1)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_module_bmi_divergence_ignores_bmi_irrelevant_flags(self):
        # Provider and consumer differ only in optimization, which is on the
        # BMI-irrelevant allowlist: no divergence warning, and the mixed build
        # runs fine.
        self.build_and_check_modules('165 module bmi flag divergence',
                                     extra_args=['-Dwithfoo=false'],
                                     setup_not_contains=['divergent dialects'])

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes',
                              compiler=('gcc', 'clang'))
    def test_header_unit_dialect_divergence(self):
        # Three declarers of util.h across a dialect split: prog20/prog20b in
        # one BMI class (c++20), prog23 in another (c++23). GCC checks a header
        # unit's dialect when it reads the CMI, the P1689 scan included, so a
        # scan reaching the wrong class's BMI hard-errors -- each class must get
        # its own BMI, reached through its own class root. Every program
        # constant-evaluates util_std_view() against its own dialect, so a
        # wrongly shared BMI is a failing run, not merely a build failure.
        self.build_and_check_modules('168 header unit dialect divergence',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        units = self.header_unit_bmis('util.h')
        self.assertEqual(len(units), 2, f'expected one util.h BMI per class, got {units}')
        per_prog = {p: self.header_unit_bmis_of(p, 'util.h')
                    for p in ('prog20', 'prog20b', 'prog23')}
        for prog, bmis in per_prog.items():
            self.assertEqual(len(bmis), 1, f'{prog} must resolve one util.h BMI, got {bmis}')
        self.assertEqual(per_prog['prog20'], per_prog['prog20b'],
                         'same-class declarers must share one unit BMI')
        self.assertNotEqual(per_prog['prog23'], per_prog['prog20'],
                            'divergent dialect classes must not share a unit BMI')
        if self.host_cpp_compiler().get_id() == 'gcc':
            self.check_gcc_module_mappers()

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='gcc')
    def test_system_header_unit_dialect_divergence(self):
        # The system-unit twin of test_header_unit_dialect_divergence: two
        # classes both import <vector>, a header unit named through GCC's own
        # built-in chain rather than any -I. Its per-class BMI is reached by
        # aliasing that whole chain on the scan, so prog20 (c++20) and prog23
        # (c++23) each scan and compile against their own <vector> CMI. A shared
        # one would hard-error at the scan on the dialect mismatch, so building
        # at all is most of the proof; the two-BMI assertions pin the mechanism.
        self.build_and_check_modules('202 system header unit dialect divergence',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        units = self.header_unit_bmis('vector')
        self.assertEqual(len(units), 2, f'expected one <vector> BMI per class, got {units}')
        # The scan resolves <vector> through a build-relative alias root, so its
        # CMI path is mangled from a colon-free name -- the Windows drive-letter
        # mangling default_cmi_path performs never applies to a gated system unit.
        for u in units:
            self.assertNotIn(':', u, f'gated system unit BMI path must be colon-free: {u}')
        per_prog = {p: self.header_unit_bmis_of(p, 'vector') for p in ('prog20', 'prog23')}
        for prog, bmis in per_prog.items():
            self.assertEqual(len(bmis), 1, f'{prog} must resolve one <vector> BMI, got {bmis}')
        self.assertNotEqual(per_prog['prog20'], per_prog['prog23'],
                            'divergent dialect classes must not share a system unit BMI')
        # The mixed case: <cstdint> has one declaring class, but the chain
        # aliases are per target -- prog20's one scan resolves every system
        # header through the aliased chain -- so its BMI must sit at the
        # class-aliased path that scan resolves, not the default real-named one.
        cstdint = self.header_unit_bmis('cstdint')
        self.assertEqual(len(cstdint), 1, f'expected one <cstdint> BMI, got {cstdint}')
        self.assertIn('imap/', next(iter(cstdint)),
                      'a single-declarer system unit on a multi-class machine '
                      'must still be class-alias-named')
        self.check_gcc_module_mappers()

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='gcc')
    def test_system_header_unit_alias_root_occupied(self):
        # The build root's imap/ is not exclusively ours: a project with a
        # source directory named imap mirrors real entries into it, and a
        # stray file can sit at the path outright. An occupied root must
        # degrade system units machine-wide with a diagnostic naming the
        # path -- never a stack trace, and never a silent misbuild. First a
        # healthy configure, then imap replaced by a plain file (occupying
        # every root at once) and a reconfigure over it.
        testdir = os.path.join(self.unit_test_dir,
                               '202 system header unit dialect divergence')
        self.init(testdir)
        imap = os.path.join(self.builddir, 'imap')
        windows_proof_rmtree(imap)
        with open(imap, 'w', encoding='utf-8') as f:
            f.write('occupied\n')
        out = self.init(testdir, extra_args=['--reconfigure'])
        self.assertIn('Cannot create the directory link', out)
        self.assertIn('imap', out)
        # The degraded build is the old shared-flat shape: the owner class's
        # BMI at the real-named flat path every scan reads (the divergent
        # class keeps a class-keyed copy its own compiles resolve), no alias
        # naming anywhere, plus the divergence warning saying why that cannot
        # work across these dialects.
        self.assertIn('divergent dialects', out)
        units = self.header_unit_bmis('vector')
        self.assertTrue(any(re.fullmatch(r'gcm\.cache(/\S+)*/vector\.gcm', u)
                            and 'imap' not in u for u in units),
                        f'no real-named flat <vector> BMI among {units}')
        for u in units:
            self.assertNotIn('imap', u,
                             f'degraded unit BMI must not be alias-named: {u}')
        # GCC rejects the shared CMI under the other dialect at the scan --
        # the failing build the warning predicts, as opposed to a quiet
        # miscompile against the wrong class's BMI.
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('vector', cm.exception.output)

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='gcc')
    def test_gcc_header_unit_aliasing_unavailable_warns(self):
        # The machine-wide capability decision, forced negative: no directory
        # link of any kind can be made (the canary in
        # _header_unit_aliasing_available fails the same way a FAT/exFAT tree
        # or an off-volume Windows junction target would), so the two dialect
        # classes of util.h fall back to one shared, first-declarer-named BMI.
        # Configure must still say so -- the same warning
        # test_header_unit_dialect_divergence turns green by aliasing away --
        # naming both dialects and both targets, once. Nothing routes the
        # scan to two BMIs here, so the build genuinely cannot work: GCC
        # rejects the shared CMI under prog23's dialect at the scan, the
        # failure the warning predicts rather than a silent miscompile.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        testdir = os.path.join(self.unit_test_dir, '168 header unit dialect divergence')
        with mock.patch.object(NinjaBackend, '_make_dir_link', return_value=False):
            out = self.init(testdir, inprocess=True)
        self.assertEqual(out.count('divergent dialects'), 1,
                         f'expected exactly one divergence warning, got:\n{out}')
        self.assertIn('prog23', out)
        self.assertIn('prog20', out)
        self.assertIn('-std=c++23', out)
        self.assertIn('will fail when it scans', out)
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('util.h', cm.exception.output)

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_unit_aliasing_unavailable_single_class_builds(self):
        # The same forced-negative capability decision, but on a machine with
        # only one BMI class: nothing needs a per-class name (there is only
        # one class to name), and this fixture's unit is a real system header
        # resolving to the compiler's own absolute path, which needs no
        # space-free alias either. So an aliasing-incapable machine builds
        # this one warning-free -- the degraded path only ever costs a build
        # something where a divergence or a spaced path actually needed
        # aliasing.
        from mesonbuild.backend.ninjabackend import NinjaBackend
        testdir = os.path.join(self.unit_test_dir, '197 header unit forced include')
        with mock.patch.object(NinjaBackend, '_make_dir_link', return_value=False):
            out = self.init(testdir, extra_args=['-Dmode=ok'], inprocess=True)
        self.assertNotIn('divergent dialects', out)
        self.assertNotIn('cannot name a header unit', out)
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            self.assertNotIn('imap', f.read())
        self.build()
        self.run_tests()

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='gcc')
    def test_header_unit_alias_root_pruning(self):
        # Alias roots are keyed by (real dir, class tag): a reconfigure that
        # collapses the class split (here, mode 'classes' -> 'plain' drops
        # progfoo's -DFOO divergence) computes a different, smaller root set
        # than the one already on disk, and the difference must not linger.
        # Reconfiguring back must reproduce exactly the original roots --
        # each is a pure function of its (dir, tag) pair -- so nothing is
        # rebuilt with a fresh, differently-named BMI it does not need.
        testdir = os.path.join(self.unit_test_dir, '173 header unit aliasing')
        imap = os.path.join(self.builddir, 'meson-private', 'imap')

        self.init(testdir, extra_args=['-Dmode=classes'])
        self.build()
        classes_roots = set(os.listdir(imap))
        self.assertGreater(len(classes_roots), 1,
                           f'expected more than one alias root, got {classes_roots}')

        self.init(testdir, extra_args=['--reconfigure', '-Dmode=plain'])
        self.build()
        plain_roots = set(os.listdir(imap))
        self.assertTrue(plain_roots < classes_roots,
                        f'plain-mode roots {plain_roots} should be a subset of '
                        f'the classes-mode roots {classes_roots} it reuses')
        gone = classes_roots - plain_roots
        self.assertTrue(gone, 'plain mode should have orphaned some per-class roots')
        for name in gone:
            self.assertFalse(os.path.exists(os.path.join(imap, name)),
                             f'stale alias root {name} was not pruned')

        self.init(testdir, extra_args=['--reconfigure', '-Dmode=classes'])
        self.build()
        reclassed_roots = set(os.listdir(imap))
        self.assertEqual(reclassed_roots, classes_roots,
                         'the same class keys must reproduce the same alias roots')

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler=('gcc', 'clang'))
    def test_module_pthread_divergence_builds(self):
        # What Stage 1 could only warn about must now work: prog's -pthread
        # class resolves modlib through a BMI-only variant and the program
        # builds, runs (each side constant-evaluates the BMI's _REENTRANT
        # view against its own), and rebuilds no-op.
        self.build_and_check_modules('159 module pthread divergence',
                                     setup_not_contains=['divergent dialects'])
        self.assertEqual(len(self.bmi_variant_ids()), 1)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler=('gcc', 'clang'))
    def test_module_define_divergence_builds(self):
        # A -D divergence splits the class; with BMI classes it builds and
        # runs through a variant instead of warning. GCC would otherwise
        # share the -DFOO BMI silently, which the fixture turns into a wrong
        # exit code: the importer constant-evaluates the BMI's FOO view
        # against its own.
        self.build_and_check_modules('165 module bmi flag divergence',
                                     setup_not_contains=['divergent dialects'])
        self.assertEqual(len(self.bmi_variant_ids()), 1)

    @requires_cpp_module_caps('modules', 'partitions', 'bmi_classes', compiler=('gcc', 'clang'))
    def test_module_internal_partitions(self):
        # An internal (implementation) partition: `module pkg:impl;` has no
        # export yet provides an importable BMI, consumed by the primary
        # interface; :impl itself imports :part (partition-to-partition
        # requires). The divergent consumer's BMI-only variant must
        # recompile the internal partition too -- the constexpr both
        # consumers compare against their own flags lives in :impl, behind
        # two imports.
        self.build_and_check_modules('174 module internal partitions')
        self.assertEqual(len(self.bmi_variant_ids()), 1)
        # The scan reports the partition as a non-interface provide; the
        # pipeline must build its BMI all the same.
        with open(os.path.join(self.builddir, 'libpkg.a.p', 'pkg-impl.cppm.o.ddi'),
                  encoding='utf-8') as f:
            provides = json.load(f)['rules'][0]['provides']
        self.assertEqual([(p['logical-name'], p['is-interface']) for p in provides],
                         [('pkg:impl', False)])
        # Both classes hold the full partition set.
        cpp = self.host_cpp_compiler()
        cache = os.path.join(self.builddir, cpp.get_module_cache_dir())
        suffix = cpp.get_module_bmi_suffix()
        for d in self.bmi_class_dirs():
            for name in ('pkg', 'pkg-part', 'pkg-impl'):
                path = os.path.join(cache, d, name + suffix)
                self.assertTrue(os.path.isfile(path), f'missing BMI {path}')

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler=('gcc', 'clang'))
    def test_module_class_topology_reconfigure(self):
        # A BMI-affecting option flip transitions the build single-class ->
        # multi-class -> single-class across reconfigures: class subdirs and
        # BMI-only variants appear and disappear over a build tree full of the
        # other topology's artifacts. Every phase must build correctly -- the
        # fixture's constexpr probe turns a stale or wrongly shared BMI into
        # exit 1 -- and settle to a no-op, and the round trip must leave no
        # residue in the generated build: the final build.ninja is
        # byte-identical to the first.
        # Each reconfigure also runs ninja's restat+cleandead over the flip
        # (ninja >= 1.12), reaping the dead topology's declared outputs;
        # stale class-cache BMIs and owner claims stay on disk, inert, like
        # the harvest cache in general.
        gcc = self.host_cpp_compiler().get_id() == 'gcc'
        testdir = os.path.join(self.unit_test_dir, '165 module bmi flag divergence')
        self.init(testdir, extra_args=['-Dwithfoo=false'])
        self.build(override_envvars=self.NO_CCACHE)
        self.run_tests()
        self.assertBuildIsNoop()
        self.assertEqual(self.bmi_variant_ids(), set())
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            single_class = f.read()

        self.setconf('-Dwithfoo=true')
        self.build(override_envvars=self.NO_CCACHE)
        self.run_tests()
        self.assertBuildIsNoop()
        self.assertEqual(len(self.bmi_variant_ids()), 1)
        if gcc:
            with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
                self.assertIn('-fmodule-mapper', f.read())

        self.setconf('-Dwithfoo=false')
        self.build(override_envvars=self.NO_CCACHE)
        self.run_tests()
        self.assertBuildIsNoop()
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            self.assertEqual(f.read(), single_class)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler=('gcc', 'clang'))
    def test_module_eh_rtti_divergence_builds(self):
        # cpp_eh and cpp_rtti each independently survive the class key (neither
        # is in Clang's or GCC's get_bmi_irrelevant_args): prog_eh and prog_rtti
        # must each resolve modlib through their own BMI-only variant, and every
        # program's constexpr probe must see its own compile's view, not the
        # provider's.
        self.build_and_check_modules('183 module eh rtti divergence',
                                     setup_not_contains=['divergent dialects'])
        self.assertEqual(len(self.bmi_class_dirs()), 3)
        self.assertEqual(len(self.bmi_variant_ids()), 2)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler=('gcc', 'clang'))
    def test_module_source_with_spaces(self):
        # A module interface source with a space in its file name: the BMI is
        # named from the module name, but every object-derived path (.ddi,
        # the BMI-only variant the divergent consumer demands, and on GCC
        # that variant's mapper provides line) inherits the space and must
        # survive it. GCC's mapper format splits name from path on the first
        # space only, and module names cannot contain spaces, so a spaced
        # path field is unambiguous.
        self.build_and_check_modules('172 module sources with spaces')
        self.assertEqual(len(self.bmi_variant_ids()), 1)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_module_target_with_assembly(self):
        # The C++ compiler also assembles, so a .S lands on a C++ compile edge
        # in a module-enabled target -- but nothing scans it and the collate
        # declares no per-TU module outputs for it. Its edge must claim none:
        # on GCC a -fmodule-mapper dep on a mapper the collate never writes is
        # a dangling input ninja refuses to load ("missing and no known rule to
        # make it"). The module itself still resolves across the link.
        self.build_and_check_modules(
            '194 module target with assembly',
            bmis=['modlib'],
            ninja_not_contains=['asmpart.S.o.mapper', 'asmpart.S.o.ddi'])

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_preprocess_module_interface(self):
        # compiler.preprocess() of a module interface: preprocessing (-E)
        # neither writes a BMI nor imports one, and a preprocess-only target
        # gets no collate edge, so no part of the module pipeline may reach its
        # compile -- a mapper dep (GCC) or a declared BMI output and harvest
        # edge (Clang) would name a file nothing produces. The same source
        # compiled normally, in the library next door, is untouched.
        self.build_and_check_modules(
            '195 preprocess module interface',
            bmis=['modlib'],
            ninja_not_contains=['modlib.cppm.ii.mapper', 'modlib.cppm.ii.ddi'])
        self.assertTrue(os.path.isfile(os.path.join(
            self.builddir, 'preprocessor_0.p', 'modlib.cppm.ii')))

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_private_executable_modules(self):
        # Nothing can ever link an executable, so its modules are private to
        # it: two executables exporting the same module name must not
        # collide over one shared-cache address.
        self.build_and_check_modules('185 private executable modules')
        self.assertEqual(len(self.private_bmi_dirs()), 2)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_private_executable_imports_dependency(self):
        # An executable's own private module and a linked library's public
        # module must both resolve in the same target -- the primary
        # scenario a private BMI directory has to support, not an edge case.
        self.build_and_check_modules('186 private executable imports dependency',
                                     bmis=['libmod'])
        self.assertEqual(len(self.private_bmi_dirs()), 1)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces(self):
        # A library mixing a public interface (api) with two private ones
        # (detail; hidden with its own independently-marked-private internal
        # partition): api's own implementation unit imports both its own
        # private modules and a linked dependency's public module in one TU
        # (the GCC mapper mixing --private-bmi-dir and --bmi-dir entries),
        # and the consuming executable only ever sees the public api.
        self.build_and_check_modules('187 cpp private module interfaces',
                                     bmis=['api', 'pub'])
        self.assertEqual(len(self.private_bmi_dirs()), 1)
        # Neither private module ever reaches the shared cache: only the
        # public ones (api, pub) may live there.
        for name in ('detail', 'hidden', 'hidden-impl'):
            self.assertFalse(os.path.isfile(self.bmi_path(name)),
                             f'{name} must not be in the shared BMI cache')

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces_unlisted_partition(self):
        # hidden is a private primary; its internal partition hidden-impl
        # must independently be listed in cpp_private_module_interfaces too,
        # or its BMI lands in the shared public cache and its name
        # (hidden:impl) takes the whole-build-tree public-name claim the
        # private primary was trying to avoid in the first place.
        testdir = os.path.join(self.unit_test_dir, '187 cpp private module interfaces')
        self.init(testdir, extra_args=['-Dmode=unlisted-partition'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('Module partition "hidden:impl" (', cm.exception.stdout)
        self.assertIn(
            ') belongs to the private module "hidden" but is not itself private. List ',
            cm.exception.stdout)
        self.assertIn(
            ' in cpp_private_module_interfaces too -- a partition of a private module '
            'takes the module-wide claim its primary deliberately avoids.',
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces_direct_import(self):
        # A target outside the providing library imports its private module
        # directly: rejected with a diagnostic naming both the module and the
        # target that provides it privately, not the generic "no target"
        # error a reader would otherwise go hunting for a missing dependency
        # over.
        testdir = os.path.join(self.unit_test_dir, '188 cpp private module interfaces diagnostics')
        self.init(testdir, extra_args=['-Dmode=direct-import'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'requires module "detail", which target \'mylib\' provides privately '
            "(it is listed in that target's cpp_private_module_interfaces). A "
            'private module can only be imported inside the target that provides it.',
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces_missing_not_swallowed(self):
        # A module nowhere in the build still gets the untouched generic
        # error, even in a project that has private modules elsewhere: the
        # private-module branch must not swallow the genuine "missing"
        # case.
        testdir = os.path.join(self.unit_test_dir, '188 cpp private module interfaces diagnostics')
        self.init(testdir, extra_args=['-Dmode=missing'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('provided by no target in this build', cm.exception.stdout)
        self.assertNotIn('provides privately', cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces_cycle(self):
        # A dependency cycle entirely among a target's own private modules
        # must still be caught, exactly as for public ones -- private names
        # are excluded from --provmap, and the cycle check must not follow
        # them off its local-name set by mistake.
        testdir = os.path.join(self.unit_test_dir, '188 cpp private module interfaces diagnostics')
        self.init(testdir, extra_args=['-Dmode=cycle'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('C++ module dependency cycle', cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces_both_kwargs_error(self):
        # Listing a source in both cpp_module_interfaces and
        # cpp_private_module_interfaces is a configure-time error: being
        # private already implies being an interface, so the combination is
        # ambiguous rather than meaningful.
        testdir = os.path.join(self.unit_test_dir, '188 cpp private module interfaces diagnostics')
        out = self.init(testdir, extra_args=['-Dmode=both-kwargs'], allow_fail=True)
        self.assertIn('in both cpp_module_interfaces and cpp_private_module_interfaces', out)

    @requires_cpp_module_caps('modules', 'partitions', compiler=('gcc', 'clang'))
    def test_cpp_internal_partitions_interface_overlap_error(self):
        # Listing a source in both cpp_module_interfaces and
        # cpp_internal_partitions is a configure-time error: a source is an
        # interface unit or an internal partition, not both. Only the partition
        # declaration would be acted on (on MSVC the source would compile with
        # /internalPartition), so the module the user declared an interface
        # would never be produced as one, and its importers would fail at build
        # time with a puzzling error instead. The neighbouring cell of that
        # matrix -- a partition that is also private -- stays allowed, and
        # test_cpp_private_module_interfaces above (whose library lists a
        # partition in both cpp_internal_partitions and
        # cpp_private_module_interfaces, alongside a public interface) is the
        # positive control that this check does not reach it.
        testdir = os.path.join(self.unit_test_dir, '199 module internal partition diagnostics')
        out = self.init(testdir, allow_fail=True)
        self.assertIn('in both cpp_module_interfaces and cpp_internal_partitions', out)

    @requires_cpp_module_caps('modules', 'partitions', compiler='gcc')
    def test_cpp_private_module_reachability_warns(self):
        # A private module that the target's own *public* interface is built
        # out of: importers of the public module have an interface dependency
        # on it ([module.import]), so they may have to read a BMI privacy keeps
        # inside this target. That reachability is unspecified, not
        # ill-formed -- GCC declines to need the BMI and builds the program
        # correctly, which is why this warns and lets the build proceed rather
        # than refusing it. Each shape reaches the private module a different
        # way, and all four must be found.
        for mode, needles in [
            # A private internal partition the interface imports: reachable or
            # not is unspecified.
            ('interface-import', ['Module partition "pkg:impl"',
                                  'the public module "pkg" reaches it from its interface:',
                                  'unspecified ([module.reach]/2)']),
            # A private *interface* partition, which the primary is required to
            # export: importers reach it necessarily, not maybe.
            ('interface-partition', ['Module partition "pkg:part"',
                                     'necessarily reaches it ([module.reach]/1)',
                                     'Make "pkg:part" an internal partition']),
            # Two edges away, not one: the walk is transitive.
            ('indirect', ['Module partition "pkg:impl"',
                          'reaches it from its interface (through "pkg:part")']),
            # Not a partition at all -- the same interface dependency on a
            # withheld BMI, reported in the same voice.
            ('private-module', ['Module "detail"',
                                'necessarily reaches it ([module.reach]/1)']),
        ]:
            with self.subTest(mode=mode):
                # The warning comes from the collate, so it is in the *build*
                # output, not setup's; the program still builds and runs.
                self.new_builddir()
                self.build_and_check_modules('200 private module reachability',
                                             extra_args=[f'-Dmode={mode}'],
                                             build_contains=needles)

    @requires_cpp_module_caps('modules', 'partitions', compiler=('gcc', 'clang'))
    def test_cpp_private_module_reachability_impl_unit_import(self):
        # The sound shape, and the regression guard on the whole design: the
        # private partition is imported only from a module *implementation*
        # unit (module pkg;), which provides nothing and which nothing outside
        # the target can import or have an interface dependency on. No importer
        # can ever need its BMI, so privacy costs nothing here -- this must
        # build and run on every compiler, and must not warn. A blunt "a
        # partition of a public primary may not be private" rule would reject
        # it, and a walk seeded from the target's sources rather than from the
        # names another target can import would warn about it spuriously.
        self.build_and_check_modules('200 private module reachability',
                                     extra_args=['-Dmode=impl-unit-import'],
                                     build_not_contains=['reaches it from its interface'])
        # Private means private: the partition's BMI stays in the target's own
        # private dir and never reaches the shared cache.
        self.assertEqual(len(self.private_bmi_dirs()), 1)
        self.assertFalse(os.path.isfile(self.bmi_path('pkg:impl')),
                         'a private partition must not be in the shared BMI cache')

    @requires_cpp_module_caps('modules', 'partitions', compiler='clang')
    def test_cpp_private_module_reachability_clang_build_failure(self):
        # The same fixture on the compiler that does materialize the
        # unspecified reachability: Meson still only warns -- it does not
        # refuse the build -- and the compiler then fails at the importer,
        # naming a module the project never wrote ('pkg:impl'). The warning is
        # what makes that error legible, so it must precede it rather than
        # replace it.
        testdir = os.path.join(self.unit_test_dir, '200 private module reachability')
        self.init(testdir, extra_args=['-Dmode=interface-import'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('Module partition "pkg:impl"', cm.exception.stdout)
        self.assertIn('the public module "pkg" reaches it from its interface',
                      cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces_name_collision(self):
        # Two libraries each privately export a module literally named
        # "detail", each linked into its own separate executable: privacy
        # removes the *global*, whole-build-tree name claim, so this builds
        # even though nothing here would have passed the old "exported by
        # more than one target in this build" check.
        self.build_and_check_modules('189 two libraries private module name collision')
        self.assertEqual(len(self.private_bmi_dirs()), 2)
        self.assertFalse(os.path.isfile(self.bmi_path('detail')))
        self.assertFalse(os.path.isfile(self.bmi_path('detail') + '.owner'),
                         'a private module must never take a global name claim')

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces_name_collision_in_one_link(self):
        # Both libraries' private "detail" modules reaching the SAME link is
        # still a hard error, and must stay one: a module's exported entities
        # are mangled from its bare module name alone (measured on GCC: two
        # unrelated "export module detail;" units emit the identical symbol,
        # e.g. detail_value@detail()), so two same-named private modules
        # linked together would silently collide at the symbol level with no
        # link error at all -- undefined behavior, not merely a Meson
        # bookkeeping gap. Privacy must not remove this check, only the
        # whole-build-tree one.
        testdir = os.path.join(self.unit_test_dir, '189 two libraries private module name collision')
        self.init(testdir, extra_args=['-Dmode=collision'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'Module "detail" is privately provided by more than one target reaching this link',
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_same_name_targets(self):
        # Two static_library('util') targets in different subdirs -- a target
        # name is only unique within its subdir -- each privately exporting
        # "detail", each linked into its own executable: the same
        # whole-build-tree coexistence as two differently named providers.
        self.build_and_check_modules('193 same name targets private module collision')
        self.assertEqual(len(self.private_bmi_dirs()), 2)
        self.assertFalse(os.path.isfile(self.bmi_path('detail')))
        self.assertFalse(os.path.isfile(self.bmi_path('detail') + '.owner'),
                         'a private module must never take a global name claim')

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_same_name_targets_in_one_link(self):
        # The same collision as above, between two targets that share a name:
        # the check must key on target identity, not on the name, or the two
        # providers read as one and the ODR violation ships silently. The
        # diagnostic must also tell two same-named targets apart.
        testdir = os.path.join(self.unit_test_dir, '193 same name targets private module collision')
        self.init(testdir, extra_args=['-Dmode=collision'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'Module "detail" is privately provided by more than one target reaching this link '
            "('util' (defined in sub1/meson.build) and 'util' (defined in sub2/meson.build))",
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_distinct_providers_in_one_link(self):
        # Two private-module providers in one link whose module names differ:
        # only the module name is ever mangled into a symbol, so this is no
        # collision at all and must keep building.
        self.build_and_check_modules('193 same name targets private module collision',
                                     extra_args=['-Dmode=distinct'])

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_cpp_private_module_own_and_dep_collision(self):
        # A target's own private module colliding with the private module of a
        # library it links is the same symbol-level collision as two libraries
        # colliding with each other -- and it is invisible to the dep-vs-dep
        # check, since a target is never among its own dependencies.
        testdir = os.path.join(self.unit_test_dir, '193 same name targets private module collision')
        self.init(testdir, extra_args=['-Dmode=own-collision'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'Module "detail" is privately provided both by this target (\'prog\') and by '
            "'util' (defined in sub1/meson.build), which it links",
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces_variant_exclusion(self):
        # A private module (priv) alongside a public one (pubmod) that
        # crosses a BMI class boundary: the synthesized BMI-only variant must
        # recompile only the public interface, never the private one.
        self.build_and_check_modules('190 private module bmi class exclusion')
        self.assertEqual(len(self.bmi_variant_ids()), 1)
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            contents = f.read()
        variant_lines = [line for line in contents.splitlines() if '@bmi@' in line]
        self.assertTrue(variant_lines)
        # 'priv' alone would also match the unrelated 'meson-private' prefix
        # every private BMI dir (and every variant's own private_dir) uses;
        # the private *interface* is named 'priv.cppm' (source) or
        # 'priv.gcm'/'priv.cppm.o' (its outputs).
        self.assertFalse(any('priv.' in line for line in variant_lines),
                         'a BMI-only variant must never compile a private interface')

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces_variant_own_import(self):
        # api is public but its own interface imports mylib's own private
        # module priv -- legal within mylib itself (same target, same
        # unkeyed-by-class private dir), but prog23 forces a BMI-only variant
        # of api under a diverging dialect, whose recompile's scan reports
        # requiring priv too. The variant collate must recognize priv as
        # mylib's own private module and raise a precise diagnostic, not the
        # generic (and here actively misleading, since priv IS in this
        # build) "provided by no target in this build" one.
        testdir = os.path.join(self.unit_test_dir, '191 private module variant import diagnostic')
        self.init(testdir)
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            ', recompiled for another BMI class, imports module "priv", which '
            "target 'mylib' provides privately. A public module interface "
            'consumed across BMI classes cannot import a private module; move '
            'the import into an implementation unit, or make "priv" public.',
            cm.exception.stdout)
        self.assertNotIn('provided by no target in this build', cm.exception.stdout)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler=('gcc', 'clang'))
    def test_cpp_private_module_interfaces_variant_dependency_import(self):
        # apia (public, in liba) imports libb's private module privb -- an
        # illegal cross-target private import, exactly like the stage-8
        # direct-import case, except here a divergent consumer also forces a
        # BMI-only variant of apia. Both liba's own normal collate and its
        # variant's collate now carry libb's private-module map, so no
        # matter which of the two edges a parallel ninja schedule happens to
        # run first, the failure is the precise private_elsewhere message,
        # never the generic one.
        testdir = os.path.join(self.unit_test_dir, '191 private module variant import diagnostic')
        self.init(testdir, extra_args=['-Dmode=dependency'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'requires module "privb", which target \'libb\' provides privately '
            "(it is listed in that target's cpp_private_module_interfaces). A "
            'private module can only be imported inside the target that provides it.',
            cm.exception.stdout)
        self.assertNotIn('provided by no target in this build', cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_export_dynamic_executable_private_modules(self):
        # An export_dynamic executable *can* be linked, so it is a normal
        # module provider: only the interface it declares private is private,
        # and its public one must still be published -- the shared_module that
        # links it imports it. Only an executable nothing can link is wholly
        # private (fixture 185); conflating the two published no module at all
        # and named the public BMI in a directory no compile writes it to.
        self.build_and_check_modules('192 export dynamic private modules', bmis=['pub'])
        self.assertEqual(self.provided_modules('app'), {'pub'})
        self.assertEqual(len(self.private_bmi_dirs()), 1)
        self.assertFalse(os.path.isfile(self.bmi_path('priv')),
                         'a private module must not reach the shared BMI cache')

    @requires_cpp_module_caps('modules', compiler=('gcc', 'clang'))
    def test_export_dynamic_executable_private_module_import(self):
        # The plugin imports the executable's private module instead of its
        # public one: privacy is not weakened by the target being linkable, so
        # this is the same hard error as importing a library's private module.
        testdir = os.path.join(self.unit_test_dir, '192 export dynamic private modules')
        self.init(testdir, extra_args=['-Dmode=import-private'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn(
            'requires module "priv", which target \'app\' provides privately '
            "(it is listed in that target's cpp_private_module_interfaces). A "
            'private module can only be imported inside the target that provides it.',
            cm.exception.stdout)

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler=('gcc', 'clang'))
    def test_export_dynamic_executable_variant_provider(self):
        # The plugin diverges on cpp_std, so the executable's public module
        # must be recompiled into a BMI-only variant for the plugin's class --
        # an executable as a variant *provider*, which only a linkable one can
        # be. The variant recompiles the public interface alone: the private
        # one never crosses a class boundary (its BMI dir is unkeyed by class).
        self.build_and_check_modules('192 export dynamic private modules',
                                     extra_args=['-Dmode=divergent'])
        self.assertEqual(len(self.bmi_class_dirs()), 2)
        self.assertEqual(len(self.bmi_variant_ids()), 1)
        self.assertEqual(self.provided_modules('app'), {'pub'})
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            variant_lines = [line for line in f.read().splitlines() if '@bmi@' in line]
        self.assertTrue(variant_lines)
        # 'priv' alone would also match the unrelated 'meson-private' prefix of
        # every private BMI dir; the private interface is named 'priv.cppm'
        # (source) or 'priv.gcm'/'priv.cppm.o' (its outputs).
        self.assertFalse(any('priv.' in line for line in variant_lines),
                         'a BMI-only variant must never compile a private interface')

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='clang')
    def test_clang_bmi_classes(self):
        # The canonical two-class fixture: subproject provider at c++20,
        # consumers at c++23 (variant) and c++20 (reuse).
        self.check_bmi_classes('166 bmi classes', module_name='modlib',
                               provider_lib='libmodlib.a',
                               consumers=('prog23', 'prog20'),
                               expected_targets=('modlib', 'prog20', 'prog23'))

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='gcc')
    def test_gcc_bmi_classes(self):
        # The shared two-class fixture under GCC, where the class cache
        # rides the per-TU module mapper; also assert the mapper shape --
        # compile edges name their own object's mapper, scans none.
        self.check_bmi_classes('166 bmi classes', module_name='modlib',
                               provider_lib='libmodlib.a',
                               consumers=('prog23', 'prog20'),
                               expected_targets=('modlib', 'prog20', 'prog23'))
        self.check_gcc_module_mappers()

    @requires_cpp_module_caps('modules', 'import_std', 'bmi_classes', compiler='clang')
    def test_clang_import_std_bmi_classes(self):
        cpp = self.host_cpp_compiler()
        if version_compare(cpp.version, '<17.0.0'):
            raise SkipTest('-std=c++26 requires clang 17')
        self.check_import_std_bmi_classes('167 import std bmi classes',
                                          progs=('prog23', 'prog26'),
                                          compat_progs=('prog26',))

    @requires_cpp_module_caps('modules', 'import_std', 'bmi_classes', compiler='gcc')
    def test_gcc_import_std_bmi_classes(self):
        # Two dialects sharing dependency('std') under GCC. std.compat lands
        # in every class dir: GCC interface compiles write their BMI eagerly
        # through the mapper, and a variant compiles all recorded interfaces.
        self.check_import_std_bmi_classes('167 import std bmi classes',
                                          progs=('prog23', 'prog26'),
                                          compat_progs=('prog26',),
                                          compat_in_all_classes=True)
        self.check_gcc_module_mappers()

    @requires_cpp_module_caps('modules', 'bmi_classes', compiler='gcc')
    def test_gcc_bmi_class_mapper_incrementality(self):
        # Editing one interface of a multi-class provider must recompile
        # only that TU (and its BMI variant): the collate reruns and
        # rewrites the dyndep, but the sibling TUs' mappers are
        # copy-if-different implicit inputs, so an unchanged mapping must
        # not dirty its compile. A mapper rewritten unconditionally would
        # recompile every TU of the target here.
        testdir = os.path.join(self.unit_test_dir, '166 bmi classes')
        srcdir = self.copy_srcdir(testdir)
        self.init(srcdir)
        self.build(override_envvars=self.NO_CCACHE)
        util = os.path.join(srcdir, 'subprojects', 'modlib', 'util.cppm')
        with open(util, encoding='utf-8') as f:
            content = f.read()
        with open(util, 'w', encoding='utf-8') as f:
            f.write(content.replace('return 7', 'return 1007'))
        out = self.build(override_envvars=self.NO_CCACHE)
        self.assertEqual(out.count('Compiling C++ object'), 1, out)
        # No consumer imports utilmod, so its BMI variant is never demanded:
        # variant edges are only pulled in through importers' dyndeps.
        self.assertEqual(out.count('Precompiling C++ module BMI'), 0, out)
        self.assertBuildIsNoop()

    @requires_cpp_module_caps('modules', 'partitions', compiler='gcc')
    def test_gcc_module_mapper_incrementality(self):
        # The same guard on a single-class build, which reaches the mappers
        # only now that GCC always carries one. Editing pkg:impl must dirty
        # its own TU, the primary interface that imports it, and the exe that
        # imports the primary -- but not pkg:part, whose mapper the collate
        # rewrites byte-identically. Without restat + copy-if-different every
        # TU of the target would recompile.
        testdir = os.path.join(self.unit_test_dir, '139 gcc cpp modules')
        srcdir = self.copy_srcdir(testdir)
        self.init(srcdir)
        self.build(override_envvars=self.NO_CCACHE)
        impl = os.path.join(srcdir, 'pkg-impl.cppm')
        with open(impl, encoding='utf-8') as f:
            content = f.read()
        with open(impl, 'w', encoding='utf-8') as f:
            f.write(content.replace('return 10', 'return 1010'))
        out = self.build(override_envvars=self.NO_CCACHE)
        # Match the compile line, not ninja's 'explain' chatter, which names
        # pkg-part as dirty for the link while never recompiling it.
        self.assertNotIn('Compiling C++ object libpkg.a.p/pkg-part.cppm.o', out,
                         'an unchanged mapper recompiled its TU')
        self.assertEqual(out.count('Compiling C++ object'), 3, out)
        self.assertBuildIsNoop()

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='clang')
    def test_clang_header_unit_bmi_classes(self):
        # Three declarers of util.h in two dialects: the same-class pair must
        # share one unit BMI (reuse, not a redundant edge), the divergent
        # declarer gets its own, each consumer names only its own class's,
        # and modlib's BMI-only variant imports the divergent class's unit,
        # not the provider's. Every program constant-evaluates the unit's
        # dialect probe, so a wrongly shared BMI is a failing test run, not
        # merely a build failure.
        self.build_and_check_modules('169 header unit bmi classes',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        units = self.header_unit_digests('util.h')
        self.assertEqual(len(units), 2, f'expected one util.h BMI per class, got {units}')
        per_prog = {p: self.header_unit_digests('util.h', edges=f'{p}.p/')
                    for p in ('prog23', 'prog20', 'prog20b')}
        for prog, digests in per_prog.items():
            self.assertEqual(len(digests), 1, f'{prog} must name exactly one util.h BMI, got {digests}')
        self.assertEqual(per_prog['prog20'], per_prog['prog20b'],
                         'same-class declarers must share one unit BMI')
        self.assertNotEqual(per_prog['prog23'], per_prog['prog20'],
                            'divergent classes must not share a unit BMI')
        self.assertEqual(len(self.bmi_variant_ids()), 1)
        self.assertEqual(self.header_unit_digests('util.h', edges='@bmi@'), per_prog['prog23'],
                         "modlib's variant must import the divergent class's unit BMI")

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes',
                              compiler=('gcc', 'clang'))
    def test_header_unit_define_divergence(self):
        # Three declarers of util.h, two in one BMI class and progfoo diverging
        # on -DFOO: each must get its own class's BMI, and modlib's BMI-only
        # variant for progfoo must import progfoo's, not the provider's. Every
        # program constant-evaluates the unit's view of FOO, so a wrongly shared
        # BMI is a failing test run, not merely a build failure.
        #
        # Each main imports a named module as well as the unit, which is what
        # pins the scan edges staying mapper-less: a mapper on a scan would leave
        # `import modlib;` unresolvable.
        #
        # 168 is the same shape diverging on cpp_std -- see
        # test_header_unit_dialect_divergence.
        self.build_and_check_modules('174 header unit define divergence',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        units = self.header_unit_bmis('util.h')
        self.assertEqual(len(units), 2, f'expected one util.h BMI per class, got {units}')
        per_prog = {p: self.header_unit_bmis_of(p, 'util.h')
                    for p in ('prog', 'progb', 'progfoo')}
        for prog, bmis in per_prog.items():
            self.assertEqual(len(bmis), 1, f'{prog} must resolve exactly one util.h BMI, got {bmis}')
        self.assertEqual(per_prog['prog'], per_prog['progb'],
                         'same-class declarers must share one unit BMI')
        self.assertNotEqual(per_prog['progfoo'], per_prog['prog'],
                            'divergent classes must not share a unit BMI')
        self.assertEqual(len(self.bmi_variant_ids()), 1)
        if self.host_cpp_compiler().get_id() == 'gcc':
            self.check_gcc_module_mappers()

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes',
                              compiler=('gcc', 'clang'))
    def test_header_unit_eh_rtti_divergence_builds(self):
        # Three declarers of the same header unit: prog_eh and prog_rtti each
        # diverge from prog in one BMI-relevant flag (cpp_eh/cpp_rtti map into
        # GCC's dialect check on a unit's CMI, like cpp_std), so each must get
        # its own unit BMI reached through its own class root. Every program
        # constant-evaluates the unit's __cpp_exceptions/__cpp_rtti view, so a
        # wrongly shared BMI is a failing test run, not merely a build failure.
        self.build_and_check_modules('184 header unit eh rtti divergence',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        units = self.header_unit_bmis('util.h')
        self.assertEqual(len(units), 3, f'expected one util.h BMI per class, got {units}')
        per_prog = {p: self.header_unit_bmis_of(p, 'util.h')
                    for p in ('prog', 'prog_eh', 'prog_rtti')}
        for prog, bmis in per_prog.items():
            self.assertEqual(len(bmis), 1, f'{prog} must resolve exactly one util.h BMI, got {bmis}')
        self.assertEqual(len({next(iter(v)) for v in per_prog.values()}), 3,
                         'each class must have its own unit BMI')

    @requires_cpp_module_caps('modules', compiler='gcc')
    def test_gcc_cpp_modules_generated_header(self):
        # A module TU that #includes a build-time generated header must be able
        # to *scan* -- the scan edge, not just the compile, has to wait for the
        # generator, otherwise the scanner errors on the missing header.
        self.build_and_check_modules('143 gcc cpp modules generated header')

    @requires_cpp_module_caps('modules', 'import_std', 'header_units', compiler='gcc')
    def test_gcc_import_std(self):
        # The standard library modules are built (as an ordinary module-providing
        # static library) because targets declare dependency('std'); a user module
        # that itself imports std links across a target boundary. usemain links
        # only the user module usemod, so its std linkage is transitive; the
        # static library usemod archives only its own object.
        self.build_and_check_modules('140 gcc import std',
                                     bmis=['std', 'std.compat', 'usemod'],
                                     ninja_not_contains=['std-objects.rsp', 'STDOBJS', '--std-info'])
        self.assert_std_link_edges(('prog', 'compatprog', 'usemain', 'huprog'),
                                   ('libusemod.a',))

    @requires_cpp_module_caps('modules', 'import_std', 'header_units', compiler='clang')
    def test_clang_import_std(self):
        # As on GCC: dependency('std') synthesizes one module-providing static
        # library; a user module that itself imports std links across a target
        # boundary. With the default libstdc++ the interface source is
        # bits/std.cc -- a module interface without the module extension,
        # covering the interface-marked synthesized target. Header-unit
        # consumers legitimately name unit BMIs on their command lines, so the
        # default ARGS check is off.
        self.build_and_check_modules('157 clang import std',
                                     bmis=['std', 'std.compat', 'usemod'],
                                     ninja_args_not_contains=())
        self.assert_std_link_edges(('prog', 'compatprog', 'usemain', 'huprog'),
                                   ('libusemod.a',))
        # dependency('std') is threaded by default: the threads dependency
        # rides along to every consumer, so the POSIX-thread setting baked
        # into std.pcm matches all importing compiles by construction.
        seen_main = False
        for entry in self.get_compdb():
            if entry['file'].endswith('main.cpp'):
                self.assertIn('-pthread', entry['command'])
                seen_main = True
        self.assertTrue(seen_main, 'main.cpp not found in compile database')

    @requires_cpp_module_caps('modules', 'import_std', compiler=('gcc', 'clang'))
    def test_import_std_nothreads(self):
        # The opt-out spelling: no thread flags on the std module or its
        # consumers, and no divergence warning since both sides agree.
        self.build_and_check_modules('160 import std nothreads',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        for entry in self.get_compdb():
            self.assertNotIn('-pthread', entry['command'])

    @requires_cpp_module_caps('modules', 'import_std', compiler=('gcc', 'clang'))
    def test_import_std_threads_variant_conflict(self):
        testdir = os.path.join(self.unit_test_dir, '161 std threads conflict')
        # One shared std module per build: requesting both the threaded and
        # the nothreads variant must fail at setup, not fight over one BMI.
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.init(testdir)
        self.assertIn('cannot be mixed', cm.exception.output)

    @requires_cpp_module_caps('modules', 'import_std', compiler='gcc')
    def test_gcc_import_std_link_whole(self):
        # Two static libraries that each import std, combined under link_whole:
        # the std object must be linked once (at the executable), not archived
        # into both libs -- otherwise --whole-archive yields a multiple
        # definition of the std module initializer.
        self.build_and_check_modules('144 gcc import std link whole')

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_units(self):
        # A declared header unit is pre-built (so the scan is never cold) and a
        # named module in the same target rides the normal path -- the program
        # links and runs. GCC maps units via the module mapper, so the default
        # check that no BMI path appears on a command line applies in full.
        self.build_and_check_modules('142 gcc header units',
                                     setup_not_contains=['divergent dialects',
                                                         'cannot name a header unit'])
        self.check_gcc_module_mappers()
        # The mapper must name the units: a mapping file disables GCC's default
        # module->CMI naming outright, so a unit left out of it would not be
        # found even though it sits at exactly the path GCC would have derived
        # itself. This fixture's own path contains spaces, which a mapper key
        # cannot hold, so each unit is named through the space-free alias -- and
        # the BMI it points at is the one the (mapper-less) unit edge wrote.
        with open(os.path.join(self.builddir, 'prog.p', 'main.cpp.o.mapper'), encoding='utf-8') as f:
            mapper = f.read().splitlines()
        for unit in ('util.h', 'angleutil.h'):
            pat = rf'\./meson-private/imap/[0-9a-f]+/{re.escape(unit)} \S+\.gcm'
            line = next((l for l in mapper if re.fullmatch(pat, l)), None)
            self.assertIsNotNone(line, f'no aliased mapping for {unit} in {mapper}')
            bmi = line.split(' ', 1)[1]
            self.assertTrue(os.path.isfile(os.path.join(self.builddir, bmi)),
                            f'mapper names {bmi}, which the unit edge did not write')

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_unit_alias_unavailable_warns(self):
        # Where the platform cannot express a space-free alias (no symlink
        # support -- today, Windows), a header unit under a spaced path stays
        # unnameable in a mapper and the compile cannot find it. Meson must say
        # so at configure rather than emit a mapper that fails later. Block the
        # alias directory with a regular file to make its creation fail the way
        # such a platform would.
        os.makedirs(os.path.join(self.builddir, 'meson-private'), exist_ok=True)
        with open(os.path.join(self.builddir, 'meson-private', 'imap'), 'w',
                  encoding='utf-8') as f:
            f.write('')
        out = self.init(os.path.join(self.unit_test_dir, '142 gcc header units'))
        self.assertIn('cannot name a header unit whose path contains a space', out)
        # All or nothing: with no alias, nothing is respelled -- a half-aliased
        # target would have the unit edge and its importers naming the unit
        # differently, which is worse than the diagnosed failure.
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            self.assertNotIn('meson-private/imap/', f.read())

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_unit_rebuild_on_user_header_change(self):
        # Also the depfile-through-the-alias check: this fixture's units are
        # reached through a space-free symlink, so every path ninja records for
        # them points through it. Editing the header at its real path must still
        # dirty the unit and its importers -- ninja stats the alias, which
        # follows to the real file's mtime.
        self.check_module_rebuild('142 gcc header units', edit_file='util.h',
                                  expect_in_rebuild=('Building C++ header unit',
                                                     'Linking target prog'))

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_unit_rebuild_on_system_header_change(self):
        self.check_module_rebuild('142 gcc header units', edit_file='angleutil.h',
                                  expect_in_rebuild=('Building C++ header unit',
                                                     'Linking target prog'))

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='gcc')
    def test_gcc_stl_header_units(self):
        # A real standard-library header unit (<vector> resolves to an
        # absolute system path) in a two-class build (progfoo's -DFOO splits
        # the class): the machine is multi-class, so the unit is class-alias-
        # named -- prog's scan resolves <vector> through its class's chain
        # aliases and the BMI sits at the alias name's mangled path. The
        # compile still asks the real absolute name, so the TU's mapper must
        # carry that name joined onto the class-aliased BMI. The default
        # absolute-path naming survives only on single-class machines
        # (test_gcc_header_unit_forced_include pins it there).
        self.build_and_check_modules('171 stl header units',
                                     setup_not_contains=['cannot name a header unit',
                                                         'divergent dialects'])
        self.check_gcc_module_mappers()
        with open(os.path.join(self.builddir, 'prog.p', 'main.cpp.o.mapper'), encoding='utf-8') as f:
            mapper = f.read().splitlines()
        self.assertTrue(any(re.fullmatch(r'/\S+/vector gcm\.cache/,/imap/[0-9a-f]+/vector\.gcm', line)
                            for line in mapper),
                        f'no real-name mapping onto a class-aliased unit BMI in {mapper}')

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_unit_forced_include(self):
        # A target that forces an include (-include prelude.h) and declares a
        # system header unit. The unit's name comes from a -H probe of the
        # compiler, and the probe must ask its question with the forced includes
        # taken off: they cannot move where <vector> resolves, and can only stop
        # -H from reporting it. So the mapper names the resolved <vector>, and
        # the unit builds and imports like any other.
        self.build_and_check_modules('197 header unit forced include',
                                     setup_not_contains=['force-include'],
                                     ninja_args_not_contains=())
        with open(os.path.join(self.builddir, 'prog.p', 'main.cpp.o.mapper'), encoding='utf-8') as f:
            mapper = f.read().splitlines()
        self.assertTrue(any(re.fullmatch(r'(/\S+/vector) gcm\.cache\1\.gcm', line)
                            for line in mapper),
                        f'the probe did not resolve <vector>: {mapper}')
        # Only the probe drops the forced include. The unit itself is compiled
        # under the target's real preprocessor state, or its BMI would freeze
        # macros its importers do not have.
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            ninja = f.read()
        self.assertIn('-include prelude.h', ninja)

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_unit_preincluded_warns(self):
        # A forced include that pulls in the very header the target declares as
        # a unit. The unit is compiled from that header as a main file, by which
        # point its include guard is spent, so there is nothing left to build a
        # BMI from -- and Meson cannot drop the forced include from the unit's
        # edge to make room, since a unit built under other macros than its
        # importers is what the BMI classes exist to prevent. Setup warns.
        testdir = os.path.join(self.unit_test_dir, '197 header unit forced include')
        # A system unit: the probe resolves <vector>, then finds the target's own
        # args open no file for it. The unit's BMI comes out empty; this one
        # still links, and only because the same forced include hands every TU
        # the declarations as text -- so the warning is all the diagnosis there
        # is, and it must not depend on a failing build to be worth making.
        out = self.init(testdir, extra_args=['-Dmode=clash-system'])
        self.assertIn("declares the C++ header unit 'vector'", out)
        self.assertIn('force-include a header that already includes it', out)

        # A project-local unit, which resolves on the include path with no probe
        # needed: the other half of the check. Here the clash is fatal, and the
        # unit's own edge is what dies -- GCC either reads util.h twice (once as
        # the prelude's text, once as the unit) and collides with itself, or
        # translates the prelude's #include into an import of the unit it is that
        # moment building. Which of the two it says is a matter of version, so
        # pin the edge rather than the wording.
        self.new_builddir()
        out = self.init(testdir, extra_args=['-Dmode=clash-user'])
        self.assertIn("declares the C++ header unit 'util.h'", out)
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('Building C++ header unit util.h', cm.exception.output)

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_unit_generated_at_build_time_errors(self):
        # A header a custom_target writes during the build, declared as a header
        # unit. GCC names a unit by the path its header resolves to, and derives
        # both the importer's mapper key and the default CMI path from that name,
        # so a header that does not exist while Meson is deciding what to build
        # cannot be given a unit at all. Setup must say so: the alternative is an
        # edge whose declared BMI GCC never writes (having no mapper, it writes
        # its own default path instead), leaving that edge and everything ordered
        # behind it dirty on every ninja run, forever.
        testdir = os.path.join(self.unit_test_dir, '198 unresolvable header unit')
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.init(testdir)
        self.assertIn("Cannot resolve the C++ header unit 'generated.h'", cm.exception.output)
        self.assertIn('custom_target', cm.exception.output)
        # Distinct from the two other header-unit diagnostics.
        self.assertNotIn('cannot name a header unit', cm.exception.output)

    @requires_cpp_module_caps('modules', 'header_units', compiler='gcc')
    def test_gcc_header_unit_unresolvable_system_warns(self):
        # The same unnameable unit, reached the other way: an angle spelling that
        # resolves nowhere. Only a probe of the compiler can answer for one of
        # those, and a probe comes back empty for reasons that are not a missing
        # header, so this warns rather than failing setup -- and the unit is
        # dropped, not built. Nothing imports it here, so the build must succeed
        # and, the sharp part, be a clean no-op afterwards (noop_check): the bug
        # this covers is an edge declaring a BMI nothing writes, which ninja
        # rebuilds forever.
        self.build_and_check_modules('198 unresolvable header unit',
                                     extra_args=['-Dmode=system-missing'],
                                     setup_contains=["Cannot resolve the C++ header unit "
                                                     "'nosuchpkg/nope.h'"],
                                     setup_not_contains=['cannot name a header unit'],
                                     ninja_args_not_contains=(),
                                     ninja_not_contains=['nope.h'])

    @requires_cpp_module_caps('modules', 'header_units', compiler='clang')
    def test_clang_generated_header_unit_orders_behind_generator(self):
        # Clang, like cl, has no setup gate against a build-time-generated
        # header unit (that is GCC-only, whose naming needs the resolved path),
        # so it builds the unit -- and the unit edge must order behind the
        # custom_target that writes the header.
        self.check_generated_header_unit_ordered()

    @requires_cpp_module_caps('modules', 'header_units', compiler=('gcc', 'clang'))
    def test_header_unit_configure_file(self):
        # The line between a generated header that can be a unit and one that
        # cannot: configure_file writes this one while Meson is still running, so
        # it resolves on the include path like any other header and its unit is
        # built, imported and run. Pins the documented limitation to
        # build-time generation alone.
        self.build_and_check_modules('198 unresolvable header unit',
                                     extra_args=['-Dmode=configure'],
                                     setup_not_contains=['Cannot resolve'],
                                     ninja_args_not_contains=())

    @requires_cpp_module_caps('modules', 'header_units', 'bmi_classes', compiler='clang')
    def test_clang_stl_header_units(self):
        # Same fixture on clang: the unit BMI is built from the absolute
        # resolved system header and named on consumer command lines.
        self.build_and_check_modules('171 stl header units',
                                     ninja_args_not_contains=())

    @requires_cpp_module_caps('modules', 'header_units', compiler='clang')
    def test_clang_header_units(self):
        # A mixed target: two declared header units (quote and angle spelling)
        # plus a named module -- the program links and runs.
        self.build_and_check_modules('158 clang header units',
                                     setup_not_contains=['divergent dialects'],
                                     ninja_args_not_contains=())
        # The unit edges' declared outputs are the real BMIs (no stamps), at
        # exactly the single-class paths -- the digest hashes '<mode>:<spelling>'
        # alone; a class component may only appear once a build has divergent
        # BMI classes.
        hudir = os.path.join(self.builddir, 'meson-private', 'header-units')
        pcms = {p for p in os.listdir(hudir) if not p.endswith('.d')}
        self.assertEqual(pcms, {f'util.h.{hashlib.sha1(b"user:util.h").hexdigest()[:16]}.pcm',
                                f'angleutil.h.{hashlib.sha1(b"system:angleutil.h").hexdigest()[:16]}.pcm'})
        # Clang has no header-unit directory lookup, so each consumer (and its
        # scan) names the unit BMIs with -fmodule-file=<pcm> -- the one waived
        # command-line surface. Named-module BMI paths must still never appear:
        # after removing the header-unit flags, no ARGS line may mention a
        # .pcm or -fmodule-file.
        hu_flag = re.compile(r'-fmodule-file=meson-private/header-units/\S+?\.pcm')
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            saw_hu_flag = False
            for line in f:
                if line.strip().startswith('ARGS ='):
                    stripped, n = hu_flag.subn('', line)
                    saw_hu_flag = saw_hu_flag or n > 0
                    self.assertNotIn('.pcm', stripped)
                    self.assertNotIn('-fmodule-file', stripped)
            self.assertTrue(saw_hu_flag, 'no consumer carried a header-unit -fmodule-file flag')

    @requires_cpp_module_caps('modules', 'header_units', compiler=('gcc', 'clang'))
    def test_header_unit_aliasing(self):
        # The same header imported from TUs in different directories. With
        # the include-path spelling, every importer resolves the same logical
        # name and the one declared unit serves all of them: exactly one
        # unit edge.
        testdir = '173 header unit aliasing'
        self.build_and_check_modules(testdir, ninja_args_not_contains=())
        self.assertEqual(len(self.header_unit_bmis('header.hpp')), 1)

        # An importer-relative spelling ("../header.hpp") resolves to a logical
        # name of its own and must be declared, but it names the same file as the
        # plain spelling -- so it is an extra name for the one BMI, not a second
        # one. Exactly one unit compile edge, whichever spelling an importer used.
        self.new_builddir()
        self.build_and_check_modules(testdir, extra_args=['-Dmode=aliased'],
                                     ninja_args_not_contains=(),
                                     setup_not_contains=['cannot name a header unit'])
        self.assertEqual(len(self.header_unit_bmis('header.hpp')), 1)
        if self.host_cpp_compiler().get_id() == 'gcc':
            # This fixture's path contains spaces, so its units are named
            # through a space-free alias -- and it is the one fixture that
            # combines a subdirectory importer with an upward-relative spelling,
            # which is what pins the alias down to a *prefix* substitution.
            # foo/aliased.cpp's key must keep the '..' verbatim: the declared
            # alias unit is spelled through the same aliased root, so the two
            # meet only while the traversal survives. Aliasing foo/ separately,
            # or normalizing the path, gives the importer and the unit edge two
            # different names for one BMI and neither finds the other's.
            self.check_gcc_module_mappers()
            alias_bmi = self.assert_alias_mapper_key('prog', 'foo_aliased.cpp')
            plain_bmi = self.assert_alias_mapper_key('prog', 'main.cpp', alias=False)
            self.assertEqual(alias_bmi, plain_bmi,
                             'both spellings must resolve the one BMI of the file')
            # A scan carries no mapper, so a TU importing through the alias reaches
            # the unit only at the path that spelling's own name mangles to. The
            # BMI is linked there, not built there.
            self.assert_header_unit_alias_link(alias_bmi)

        # Two divergent BMI classes over the same aliased unit: one BMI each, and
        # each class's importers must reach their own through *either* spelling.
        # Both programs constant-evaluate the unit's view of FOO, so a wrongly
        # shared BMI is a failing run.
        self.new_builddir()
        self.build_and_check_modules(testdir, extra_args=['-Dmode=classes'],
                                     ninja_args_not_contains=(),
                                     setup_not_contains=['cannot name a header unit'])
        units = self.header_unit_bmis('header.hpp')
        self.assertEqual(len(units), 2, f'expected one BMI per class, got {units}')
        if self.host_cpp_compiler().get_id() == 'gcc':
            per_prog = {}
            for prog in ('prog', 'progfoo'):
                alias_bmi = self.assert_alias_mapper_key(prog, 'foo_aliased.cpp')
                plain_bmi = self.assert_alias_mapper_key(prog, 'main.cpp', alias=False)
                self.assertEqual(alias_bmi, plain_bmi,
                                 f'{prog} must reach one BMI through either spelling')
                per_prog[prog] = alias_bmi
            self.assertNotEqual(per_prog['prog'], per_prog['progfoo'],
                                'divergent classes must not share a unit BMI')
            # Each class links the alias spelling's own default path to its own
            # BMI (one link edge per class), so a mapper-less scan of either
            # spelling in either class reaches that class's unit.
            self.assert_header_unit_alias_link(per_prog['prog'], count=2)
            self.assert_header_unit_alias_link(per_prog['progfoo'], count=2)

        # Without the alias declaration the two compilers diverge: GCC keys
        # CMIs by the textual resolved name (no normalization), so the import
        # misses the declared unit's CMI and the scan fails with GCC's own
        # message; Clang normalizes the resolved file and matches it.
        self.new_builddir()
        if self.host_cpp_compiler().get_id() == 'gcc':
            self.init(os.path.join(self.unit_test_dir, testdir),
                      extra_args=['-Dmode=undeclared'])
            with self.assertRaises(subprocess.CalledProcessError) as cm:
                self.build()
            self.assertIn('imports must be built before being imported',
                          cm.exception.stdout)
        else:
            self.build_and_check_modules(testdir, extra_args=['-Dmode=undeclared'],
                                         ninja_args_not_contains=())
            self.assertEqual(len(self.header_unit_bmis('header.hpp')), 1)

    @requires_cpp_module_caps('modules', 'header_units', compiler='clang')
    def test_clang_header_unit_rebuild_on_user_header_change(self):
        self.check_module_rebuild('158 clang header units', edit_file='util.h',
                                  expect_in_rebuild=('Building C++ header unit',
                                                     'Linking target prog'))

    @requires_cpp_module_caps('modules', 'header_units', compiler='clang')
    def test_clang_header_unit_rebuild_on_system_header_change(self):
        self.check_module_rebuild('158 clang header units', edit_file='angleutil.h',
                                  expect_in_rebuild=('Building C++ header unit',
                                                     'Linking target prog'))

    @requires_cpp_module_caps('modules', compiler='gcc')
    def test_gcc_cpp_modules_diagnostics(self):
        testdir = os.path.join(self.unit_test_dir, '141 gcc cpp modules diagnostics')
        # Each mode configures cleanly but must fail the build in the collator
        # with its module diagnostic.
        cases = {
            'missing': 'provided by no target in this build',
            'cycle': 'C++ module dependency cycle',
            'duplicate': 'provided by two sources in this target',
            'crosslink': 'provided by more than one target reaching this link',
            'duptargets': 'exported by more than one target in this build',
            # In a two-class build the collate still rejects an unresolvable
            # import itself, before the compiler could report its own lookup
            # failure against a mapper that omits the module.
            'missingdivergent': 'provided by no target in this build',
        }
        for mode, needle in cases.items():
            with self.subTest(mode=mode):
                self.new_builddir()
                self.init(testdir, extra_args=[f'-Dmode={mode}'])
                with self.assertRaises(subprocess.CalledProcessError) as cm:
                    self.build()
                self.assertIn(needle, cm.exception.stdout)

    @requires_cpp_module_caps('modules', compiler='clang')
    def test_clang_cpp_modules_diagnostics(self):
        testdir = os.path.join(self.unit_test_dir, '141 gcc cpp modules diagnostics')
        # A source that provides a module but is neither a module extension nor
        # declared via cpp_module_interfaces must be rejected by the collator with
        # a clear message, not fail downstream with "module not found". GCC infers
        # interface-ness, so this diagnostic is Clang-only.
        self.init(testdir, extra_args=['-Dmode=undeclared'])
        with self.assertRaises(subprocess.CalledProcessError) as cm:
            self.build()
        self.assertIn('is not marked a module interface', cm.exception.stdout)

    @requires_cpp_module_caps('modules', 'import_std', compiler='gcc')
    def test_gcc_import_std_subproject(self):
        testdir = os.path.join(self.unit_test_dir, '153 gcc import std subproject')
        # Both the parent and a subproject call dependency('std'). The std module
        # library must be synthesized once for the whole build. When each
        # subproject synthesized its own static_library('__meson_cxx_std'), two std targets
        # both provided module 'std' into the same executable -- collision at
        # link time -- so assert only a single std provider exists.
        self.init(testdir)
        std_targets = [t for t in self.introspect('--targets')
                       if t['name'] == '__meson_cxx_std' and t['type'] == 'static library']
        self.assertEqual(len(std_targets), 1,
                         'dependency("std") synthesized more than one std library '
                         f'across subprojects: {[t["id"] for t in std_targets]}')
        self.build()
        self.run_tests()

    def test_std_dependency_override(self):
        # 'std' is a reserved dependency name, but an explicit
        # meson.override_dependency('std', ...) must take precedence over the
        # built-in import-std synthesis, like every other reserved name. The
        # override short-circuits synthesis, so this applies to any C++ compiler
        # and does not require an import-std-capable toolchain.
        testdir = os.path.join(self.unit_test_dir, '154 std dependency override')
        env = get_fake_env(testdir, self.builddir, self.prefix)
        try:
            detect_cpp_compiler(env, MachineChoice.HOST)
        except EnvironmentException:
            raise SkipTest('No C++ compiler found.')
        # The meson.build asserts std.version() == the override's version; without
        # the fix dependency('std') bypasses the override and this init() fails.
        self.init(testdir)

    def test_run_installed(self):
        if is_cygwin() or is_osx():
            raise SkipTest('LD_LIBRARY_PATH and RPATH not applicable')

        testdir = os.path.join(self.unit_test_dir, '7 run installed')
        self.init(testdir)
        self.build()
        self.install()
        installed_exe = os.path.join(self.installdir, 'usr/bin/prog')
        installed_libdir = os.path.join(self.installdir, 'usr/foo')
        installed_lib = os.path.join(installed_libdir, 'libfoo.so')
        self.assertTrue(os.path.isfile(installed_exe))
        self.assertTrue(os.path.isdir(installed_libdir))
        self.assertTrue(os.path.isfile(installed_lib))
        # Must fail when run without LD_LIBRARY_PATH to ensure that
        # rpath has been properly stripped rather than pointing to the builddir.
        self.assertNotEqual(subprocess.call(installed_exe, stderr=subprocess.DEVNULL), 0)
        # When LD_LIBRARY_PATH is set it should start working.
        # For some reason setting LD_LIBRARY_PATH in os.environ fails
        # when all tests are run (but works when only this test is run),
        # but doing this explicitly works.
        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = ':'.join([installed_libdir, env.get('LD_LIBRARY_PATH', '')])
        self.assertEqual(subprocess.call(installed_exe, env=env), 0)
        # Ensure that introspect --installed works
        installed = self.introspect('--installed')
        for v in installed.values():
            self.assertTrue('prog' in v or 'foo' in v)

    @skipIfNoPkgconfig
    def test_order_of_l_arguments(self):
        testdir = os.path.join(self.unit_test_dir, '8 -L -l order')
        self.init(testdir, override_envvars={'PKG_CONFIG_PATH': testdir})
        # NOTE: .pc file has -Lfoo -lfoo -Lbar -lbar but pkg-config reorders
        # the flags before returning them to -Lfoo -Lbar -lfoo -lbar
        # but pkgconf seems to not do that. Sigh. Support both.
        expected_order = [('-L/me/first', '-lfoo1'),
                          ('-L/me/second', '-lfoo2'),
                          ('-L/me/first', '-L/me/second'),
                          ('-lfoo1', '-lfoo2'),
                          ('-L/me/second', '-L/me/third'),
                          ('-L/me/third', '-L/me/fourth',),
                          ('-L/me/third', '-lfoo3'),
                          ('-L/me/fourth', '-lfoo4'),
                          ('-lfoo3', '-lfoo4'),
                          ]
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as ifile:
            for line in ifile:
                if expected_order[0][0] in line:
                    for first, second in expected_order:
                        self.assertLess(line.index(first), line.index(second))
                    return
        raise RuntimeError('Linker entries not found in the Ninja file.')

    def test_introspect_dependencies(self):
        '''
        Tests that mesonintrospect --dependencies returns expected output.
        '''
        testdir = os.path.join(self.framework_test_dir, '7 gnome')
        self.init(testdir)
        glib_found = False
        gobject_found = False
        deps = self.introspect('--dependencies')
        self.assertIsInstance(deps, list)
        for dep in deps:
            self.assertIsInstance(dep, dict)
            self.assertIn('name', dep)
            self.assertIn('compile_args', dep)
            self.assertIn('link_args', dep)
            if dep['name'] == 'glib-2.0':
                glib_found = True
            elif dep['name'] == 'gobject-2.0':
                gobject_found = True
        self.assertTrue(glib_found)
        self.assertTrue(gobject_found)
        if subprocess.call([PKG_CONFIG, '--exists', 'glib-2.0 >= 2.56.2']) != 0:
            raise SkipTest('glib >= 2.56.2 needed for the rest')
        targets = self.introspect('--targets')
        docbook_target = None
        for t in targets:
            if t['name'] == 'generated-gdbus-docbook':
                docbook_target = t
                break
        self.assertIsInstance(docbook_target, dict)
        self.assertEqual(os.path.basename(t['filename'][0]), 'generated-gdbus-doc-' + os.path.basename(t['target_sources'][0]['sources'][0]))

    def test_introspect_installed(self):
        testdir = os.path.join(self.linuxlike_test_dir, '7 library versions')
        self.init(testdir)

        install = self.introspect('--installed')
        install = {os.path.basename(k): v for k, v in install.items()}
        print(install)
        if is_osx():
            the_truth = {
                'libmodule.dylib': '/usr/lib/libmodule.dylib',
                'libnoversion.dylib': '/usr/lib/libnoversion.dylib',
                'libonlysoversion.5.dylib': '/usr/lib/libonlysoversion.5.dylib',
                'libonlysoversion.dylib': '/usr/lib/libonlysoversion.dylib',
                'libonlyversion.1.dylib': '/usr/lib/libonlyversion.1.dylib',
                'libonlyversion.dylib': '/usr/lib/libonlyversion.dylib',
                'libsome.0.dylib': '/usr/lib/libsome.0.dylib',
                'libsome.dylib': '/usr/lib/libsome.dylib',
            }
            the_truth_2 = {'/usr/lib/libsome.dylib',
                           '/usr/lib/libsome.0.dylib',
            }
        else:
            the_truth = {
                'libmodule.so': '/usr/lib/libmodule.so',
                'libnoversion.so': '/usr/lib/libnoversion.so',
                'libonlysoversion.so': '/usr/lib/libonlysoversion.so',
                'libonlysoversion.so.5': '/usr/lib/libonlysoversion.so.5',
                'libonlyversion.so': '/usr/lib/libonlyversion.so',
                'libonlyversion.so.1': '/usr/lib/libonlyversion.so.1',
                'libonlyversion.so.1.4.5': '/usr/lib/libonlyversion.so.1.4.5',
                'libsome.so': '/usr/lib/libsome.so',
                'libsome.so.0': '/usr/lib/libsome.so.0',
                'libsome.so.1.2.3': '/usr/lib/libsome.so.1.2.3',
            }
            the_truth_2 = {'/usr/lib/libsome.so',
                           '/usr/lib/libsome.so.0',
                           '/usr/lib/libsome.so.1.2.3'}
        self.assertDictEqual(install, the_truth)

        targets = self.introspect('--targets')
        for t in targets:
            if t['name'] != 'some':
                continue
            self.assertSetEqual(the_truth_2, set(t['install_filename']))

    def test_build_rpath(self):
        if is_cygwin():
            raise SkipTest('Windows PE/COFF binaries do not use RPATH')
        testdir = os.path.join(self.unit_test_dir, '10 build_rpath')
        self.init(testdir)
        self.build()
        build_rpath = get_rpath(os.path.join(self.builddir, 'prog'))
        self.assertEqual(build_rpath, '$ORIGIN/sub:/foo/bar')
        build_rpath = get_rpath(os.path.join(self.builddir, 'progcxx'))
        self.assertEqual(build_rpath, '$ORIGIN/sub:/foo/bar')
        self.install()
        install_rpath = get_rpath(os.path.join(self.installdir, 'usr/bin/prog'))
        self.assertEqual(install_rpath, '/baz')
        install_rpath = get_rpath(os.path.join(self.installdir, 'usr/bin/progcxx'))
        self.assertEqual(install_rpath, 'baz')

    @skipIfNoPkgconfig
    def test_build_rpath_pkgconfig(self):
        '''
        Test that current build artefacts (libs) are found first on the rpath,
        manually specified rpath comes second and additional rpath elements (from
        pkg-config files) come last
        '''
        if is_cygwin():
            raise SkipTest('Windows PE/COFF binaries do not use RPATH')
        testdir = os.path.join(self.unit_test_dir, '89 pkgconfig build rpath order')
        self.init(testdir, override_envvars={'PKG_CONFIG_PATH': testdir})
        self.build()
        build_rpath = get_rpath(os.path.join(self.builddir, 'prog'))
        self.assertEqual(build_rpath, '$ORIGIN/sub:/foo/bar:/foo/dummy')
        build_rpath = get_rpath(os.path.join(self.builddir, 'progcxx'))
        self.assertEqual(build_rpath, '$ORIGIN/sub:/foo/bar:/foo/dummy')
        self.install()
        install_rpath = get_rpath(os.path.join(self.installdir, 'usr/bin/prog'))
        self.assertEqual(install_rpath, '/baz:/foo/dummy')
        install_rpath = get_rpath(os.path.join(self.installdir, 'usr/bin/progcxx'))
        self.assertEqual(install_rpath, 'baz:/foo/dummy')

    @skipIfNoPkgconfig
    def test_global_rpath(self):
        if is_cygwin():
            raise SkipTest('Windows PE/COFF binaries do not use RPATH')
        if is_osx():
            raise SkipTest('Global RPATHs via LDFLAGS not yet supported on MacOS (does anybody need it?)')

        testdir = os.path.join(self.unit_test_dir, '79 global-rpath')
        oldinstalldir = self.installdir

        # Build and install an external library without DESTDIR.
        # The external library generates a .pc file without an rpath.
        yonder_dir = os.path.join(testdir, 'yonder')
        yonder_prefix = os.path.join(oldinstalldir, 'yonder')
        yonder_libdir = os.path.join(yonder_prefix, self.libdir)
        self.prefix = yonder_prefix
        self.installdir = yonder_prefix
        self.init(yonder_dir)
        self.build()
        self.install(use_destdir=False)

        # Since rpath has multiple valid formats we need to
        # test that they are all properly used.
        rpath_formats = [
            ('-Wl,-rpath=', False),
            ('-Wl,-rpath,', False),
            ('-Wl,--just-symbols=', True),
            ('-Wl,--just-symbols,', True),
            ('-Wl,-R', False),
            ('-Wl,-R,', False)
        ]
        for rpath_format, exception in rpath_formats:
            # Build an app that uses that installed library.
            # Supply the rpath to the installed library via LDFLAGS
            # (as systems like buildroot and guix are wont to do)
            # and verify install preserves that rpath.
            self.new_builddir()
            env = {'LDFLAGS': rpath_format + yonder_libdir,
                   'PKG_CONFIG_PATH': os.path.join(yonder_libdir, 'pkgconfig')}
            if exception:
                with self.assertRaises(subprocess.CalledProcessError):
                    self.init(testdir, override_envvars=env)
                continue
            self.init(testdir, override_envvars=env)
            self.build()
            self.install(use_destdir=False)
            got_rpath = get_rpath(os.path.join(yonder_prefix, 'bin/rpathified'))
            self.assertEqual(got_rpath, yonder_libdir, rpath_format)

    @skip_if_not_base_option('b_sanitize')
    def test_env_cflags_ldflags(self):
        if is_cygwin():
            raise SkipTest('asan not available on Cygwin')
        if is_openbsd():
            raise SkipTest('-fsanitize=address is not supported on OpenBSD')
        if is_sunos():
            raise SkipTest('-fsanitize=address is not supported on illumos')

        testdir = os.path.join(self.common_test_dir, '1 trivial')
        env = {'CFLAGS': '-fsanitize=address', 'LDFLAGS': '-I.'}
        self.init(testdir, override_envvars=env)
        self.build()
        compdb = self.get_compdb()
        for i in compdb:
            self.assertIn("-fsanitize=address", i["command"])
        self.wipe()

    @skip_if_not_base_option('b_sanitize')
    def test_pch_with_address_sanitizer(self):
        if is_cygwin():
            raise SkipTest('asan not available on Cygwin')
        if is_openbsd():
            raise SkipTest('-fsanitize=address is not supported on OpenBSD')

        testdir = os.path.join(self.common_test_dir, '13 pch')
        self.init(testdir, extra_args=['-Db_sanitize=address', '-Db_lundef=false'])
        self.build()
        compdb = self.get_compdb()
        for i in compdb:
            self.assertIn("-fsanitize=address", i["command"])

    def test_cross_find_program(self):
        testdir = os.path.join(self.unit_test_dir, '11 cross prog')
        crossfile = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8')
        print(os.path.join(testdir, 'some_cross_tool.py'))

        tool_path = os.path.join(testdir, 'some_cross_tool.py')

        crossfile.write(textwrap.dedent(f'''\
            [binaries]
            c = '{shutil.which('gcc' if is_sunos() else 'cc')}'
            ar = '{shutil.which('ar')}'
            strip = '{shutil.which('strip')}'
            sometool.py = ['{tool_path}']
            someothertool.py = '{tool_path}'

            [properties]

            [host_machine]
            system = 'linux'
            cpu_family = 'arm'
            cpu = 'armv7' # Not sure if correct.
            endian = 'little'
            '''))
        crossfile.flush()
        self.meson_cross_files = [crossfile.name]
        self.init(testdir)

    def test_reconfigure(self):
        testdir = os.path.join(self.unit_test_dir, '13 reconfigure')
        self.init(testdir, extra_args=['-Db_coverage=true'], default_args=False)
        self.build('reconfigure')

    @skip_if_not_language('vala')
    def test_vala_generated_source_buildir_inside_source_tree(self):
        '''
        Test that valac outputs generated C files in the expected location when
        the builddir is a subdir of the source tree.
        '''
        testdir = os.path.join(self.vala_test_dir, '8 generated sources')
        newdir = os.path.join(self.builddir, 'srctree')
        shutil.copytree(testdir, newdir)
        testdir = newdir
        # New builddir
        builddir = os.path.join(testdir, 'subdir/_build')
        os.makedirs(builddir, exist_ok=True)
        self.change_builddir(builddir)
        self.init(testdir)
        self.build()

    def test_old_gnome_module_codepaths(self):
        '''
        A lot of code in the GNOME module is conditional on the version of the
        glib tools that are installed, and breakages in the old code can slip
        by once the CI has a newer glib version. So we force the GNOME module
        to pretend that it's running on an ancient glib so the fallback code is
        also tested.
        '''
        testdir = os.path.join(self.framework_test_dir, '7 gnome')
        with mock.patch('mesonbuild.modules.gnome.GnomeModule._get_native_glib_version', mock.Mock(return_value='2.20')):
            env = {'MESON_UNIT_TEST_PRETEND_GLIB_OLD': "1"}
            self.init(testdir,
                      inprocess=True,
                      override_envvars=env)
            self.build(override_envvars=env)

    @skipIfNoPkgconfig
    def test_pkgconfig_usage(self):
        testdir1 = os.path.join(self.unit_test_dir, '27 pkgconfig usage/dependency')
        testdir2 = os.path.join(self.unit_test_dir, '27 pkgconfig usage/dependee')
        if subprocess.call([PKG_CONFIG, '--cflags', 'glib-2.0'],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) != 0:
            raise SkipTest('Glib 2.0 dependency not available.')
        with tempfile.TemporaryDirectory() as tempdirname:
            self.init(testdir1, extra_args=['--prefix=' + tempdirname, '--libdir=lib'], default_args=False)
            self.install(use_destdir=False)
            shutil.rmtree(self.builddir)
            os.mkdir(self.builddir)
            pkg_dir = os.path.join(tempdirname, 'lib/pkgconfig')
            self.assertTrue(os.path.exists(os.path.join(pkg_dir, 'libpkgdep.pc')))
            lib_dir = os.path.join(tempdirname, 'lib')
            myenv = os.environ.copy()
            myenv['PKG_CONFIG_PATH'] = pkg_dir
            # Private internal libraries must not leak out.
            pkg_out = subprocess.check_output([PKG_CONFIG, '--static', '--libs', 'libpkgdep'], env=myenv)
            self.assertNotIn(b'libpkgdep-int', pkg_out, 'Internal library leaked out.')
            # Dependencies must not leak to cflags when building only a shared library.
            pkg_out = subprocess.check_output([PKG_CONFIG, '--cflags', 'libpkgdep'], env=myenv)
            self.assertNotIn(b'glib', pkg_out, 'Internal dependency leaked to headers.')
            # Test that the result is usable.
            self.init(testdir2, override_envvars=myenv)
            self.build(override_envvars=myenv)
            myenv = os.environ.copy()
            myenv['LD_LIBRARY_PATH'] = ':'.join([lib_dir, myenv.get('LD_LIBRARY_PATH', '')])
            if is_cygwin():
                bin_dir = os.path.join(tempdirname, 'bin')
                myenv['PATH'] = bin_dir + os.pathsep + myenv['PATH']
            self.assertTrue(os.path.isdir(lib_dir))
            test_exe = os.path.join(self.builddir, 'pkguser')
            self.assertTrue(os.path.isfile(test_exe))
            subprocess.check_call(test_exe, env=myenv)

    @skipIfNoPkgconfig
    def test_pkgconfig_relative_paths(self):
        testdir = os.path.join(self.unit_test_dir, '61 pkgconfig relative paths')
        pkg_dir = os.path.join(testdir, 'pkgconfig')
        self.assertPathExists(os.path.join(pkg_dir, 'librelativepath.pc'))

        env = get_fake_env(testdir, self.builddir, self.prefix)
        env.coredata.optstore.set_option(OptionKey('pkg_config_path'), pkg_dir)
        kwargs = {'required': True, 'silent': True, 'native': MachineChoice.HOST}
        relative_path_dep = PkgConfigDependency('librelativepath', env, kwargs)
        self.assertTrue(relative_path_dep.found())

        # Ensure link_args are properly quoted
        libpath = Path(self.builddir) / '../relativepath/lib'
        link_args = ['-L' + libpath.as_posix(), '-lrelativepath']
        self.assertEqual(relative_path_dep.get_link_args(), link_args)

    @skipIfNoPkgconfig
    def test_pkgconfig_duplicate_path_entries(self):
        testdir = os.path.join(self.unit_test_dir, '111 pkgconfig duplicate path entries')
        pkg_dir = os.path.join(testdir, 'pkgconfig')

        env = get_fake_env(testdir, self.builddir, self.prefix)
        env.coredata.optstore.set_option(OptionKey('pkg_config_path'), pkg_dir)

        # Regression test: This used to modify the value of `pkg_config_path`
        # option, adding the meson-uninstalled directory to it.
        PkgConfigInterface.setup_env({}, env, MachineChoice.HOST, uninstalled=True)

        pkg_config_path = env.coredata.optstore.get_value_for('pkg_config_path')
        self.assertEqual(pkg_config_path, [pkg_dir])

    def test_pkgconfig_uninstalled_env_added(self):
        '''
        Checks that the meson-uninstalled dir is added to PKG_CONFIG_PATH
        '''
        testdir = os.path.join(self.unit_test_dir, '111 pkgconfig duplicate path entries')
        meson_uninstalled_dir = os.path.join(self.builddir, 'meson-uninstalled')

        env = get_fake_env(testdir, self.builddir, self.prefix)

        newEnv = PkgConfigInterface.setup_env({}, env, MachineChoice.HOST, uninstalled=True)

        pkg_config_path_dirs = newEnv['PKG_CONFIG_PATH'].split(os.pathsep)

        self.assertEqual(len(pkg_config_path_dirs), 1)
        self.assertEqual(pkg_config_path_dirs[0], meson_uninstalled_dir)

    def test_pkgconfig_uninstalled_env_prepended(self):
        '''
        Checks that the meson-uninstalled dir is prepended to PKG_CONFIG_PATH
        '''
        testdir = os.path.join(self.unit_test_dir, '111 pkgconfig duplicate path entries')
        meson_uninstalled_dir = os.path.join(self.builddir, 'meson-uninstalled')
        external_pkg_config_path_dir = os.path.join('usr', 'local', 'lib', 'pkgconfig')

        env = get_fake_env(testdir, self.builddir, self.prefix)

        env.coredata.optstore.set_option(OptionKey('pkg_config_path'), external_pkg_config_path_dir)

        newEnv = PkgConfigInterface.setup_env({}, env, MachineChoice.HOST, uninstalled=True)

        pkg_config_path_dirs = newEnv['PKG_CONFIG_PATH'].split(os.pathsep)

        self.assertEqual(pkg_config_path_dirs[0], meson_uninstalled_dir)
        self.assertEqual(pkg_config_path_dirs[1], external_pkg_config_path_dir)

    @skipIfNoPkgconfig
    def test_pkgconfig_internal_libraries(self):
        '''
        '''
        with tempfile.TemporaryDirectory() as tempdirname:
            # build library
            testdirbase = os.path.join(self.unit_test_dir, '32 pkgconfig use libraries')
            testdirlib = os.path.join(testdirbase, 'lib')
            self.init(testdirlib, extra_args=['--prefix=' + tempdirname,
                                              '--libdir=lib',
                                              '--default-library=static'], default_args=False)
            self.build()
            self.install(use_destdir=False)

            # build user of library
            pkg_dir = os.path.join(tempdirname, 'lib/pkgconfig')
            self.new_builddir()
            self.init(os.path.join(testdirbase, 'app'),
                      override_envvars={'PKG_CONFIG_PATH': pkg_dir})
            self.build()

    @skipIfNoPkgconfig
    def test_static_archive_stripping(self):
        '''
        Check that Meson produces valid static archives with --strip enabled
        '''
        with tempfile.TemporaryDirectory() as tempdirname:
            testdirbase = os.path.join(self.unit_test_dir, '65 static archive stripping')

            # build lib
            self.new_builddir()
            testdirlib = os.path.join(testdirbase, 'lib')
            testlibprefix = os.path.join(tempdirname, 'libprefix')
            self.init(testdirlib, extra_args=['--prefix=' + testlibprefix,
                                              '--libdir=lib',
                                              '--default-library=static',
                                              '--buildtype=debug',
                                              '--strip'], default_args=False)
            self.build()
            self.install(use_destdir=False)

            # build executable (uses lib, fails if static archive has been stripped incorrectly)
            pkg_dir = os.path.join(testlibprefix, 'lib/pkgconfig')
            self.new_builddir()
            self.init(os.path.join(testdirbase, 'app'),
                      override_envvars={'PKG_CONFIG_PATH': pkg_dir})
            self.build()

    @skipIfNoPkgconfig
    def test_pkgconfig_formatting(self):
        testdir = os.path.join(self.unit_test_dir, '38 pkgconfig format')
        self.init(testdir)
        myenv = os.environ.copy()
        myenv['PKG_CONFIG_PATH'] = _prepend_pkg_config_path(self.privatedir)
        stdo = subprocess.check_output([PKG_CONFIG, '--libs-only-l', 'libsomething'], env=myenv)
        deps = [b'-lgobject-2.0', b'-lgio-2.0', b'-lglib-2.0', b'-lsomething']
        if is_windows() or is_osx() or is_openbsd():
            # On Windows, libintl is a separate library
            # It used to be on Cygwin as well, but no longer is.
            deps.append(b'-lintl')
        self.assertEqual(set(deps), set(stdo.split()))

    @skipIfNoPkgconfig
    @skip_if_not_language('cs')
    def test_pkgconfig_csharp_library(self):
        testdir = os.path.join(self.unit_test_dir, '49 pkgconfig csharp library')
        self.init(testdir)
        myenv = os.environ.copy()
        myenv['PKG_CONFIG_PATH'] = _prepend_pkg_config_path(self.privatedir)
        stdo = subprocess.check_output([PKG_CONFIG, '--libs', 'libsomething'], env=myenv)

        self.assertEqual("-r/usr/lib/libsomething.dll", str(stdo.decode('ascii')).strip())

    @skipIfNoPkgconfig
    def test_pkgconfig_link_order(self):
        '''
        Test that libraries are listed before their dependencies.
        '''
        testdir = os.path.join(self.unit_test_dir, '52 pkgconfig static link order')
        self.init(testdir)
        myenv = os.environ.copy()
        myenv['PKG_CONFIG_PATH'] = _prepend_pkg_config_path(self.privatedir)
        stdo = subprocess.check_output([PKG_CONFIG, '--libs', 'libsomething'], env=myenv)
        deps = stdo.split()
        self.assertLess(deps.index(b'-lsomething'), deps.index(b'-ldependency'))

    def test_deterministic_dep_order(self):
        '''
        Test that the dependencies are always listed in a deterministic order.
        '''
        testdir = os.path.join(self.unit_test_dir, '42 dep order')
        self.init(testdir)
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as bfile:
            for line in bfile:
                if 'build myexe:' in line or 'build myexe.exe:' in line:
                    self.assertIn('liblib1.a liblib2.a', line)
                    return
        raise RuntimeError('Could not find the build rule')

    def test_deterministic_rpath_order(self):
        '''
        Test that the rpaths are always listed in a deterministic order.
        '''
        if is_cygwin():
            raise SkipTest('rpath are not used on Cygwin')
        testdir = os.path.join(self.unit_test_dir, '41 rpath order')
        self.init(testdir)
        if is_osx():
            rpathre = re.compile(r'-rpath,.*/subprojects/sub1.*-rpath,.*/subprojects/sub2')
        else:
            rpathre = re.compile(r'-rpath,\$\$ORIGIN/subprojects/sub1:\$\$ORIGIN/subprojects/sub2')
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as bfile:
            for line in bfile:
                if '-rpath' in line:
                    self.assertRegex(line, rpathre)
                    return
        raise RuntimeError('Could not find the rpath')

    def test_override_with_exe_dep(self):
        '''
        Test that we produce the correct dependencies when a program is overridden with an executable.
        '''
        testdir = os.path.join(self.src_root, 'test cases', 'native', '9 override with exe')
        self.init(testdir)
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as bfile:
            for line in bfile:
                if 'main1.c:' in line or 'main2.c:' in line:
                    self.assertIn('| subprojects/sub/foobar', line)

    @skipIfNoPkgconfig
    def test_usage_external_library(self):
        '''
        Test that uninstalled usage of an external library (from the system or
        PkgConfigDependency) works. On macOS, this workflow works out of the
        box. On Linux, BSDs, Windows, etc, you need to set extra arguments such
        as LD_LIBRARY_PATH, etc, so this test is skipped.

        The system library is found with cc.find_library() and pkg-config deps.
        '''
        oldprefix = self.prefix
        # Install external library so we can find it
        testdir = os.path.join(self.unit_test_dir, '39 external, internal library rpath', 'external library')
        # install into installdir without using DESTDIR
        installdir = self.installdir
        self.prefix = installdir
        self.init(testdir)
        self.prefix = oldprefix
        self.build()
        self.install(use_destdir=False)
        ## New builddir for the consumer
        self.new_builddir()
        env = {'LIBRARY_PATH': os.path.join(installdir, self.libdir),
               'PKG_CONFIG_PATH': _prepend_pkg_config_path(os.path.join(installdir, self.libdir, 'pkgconfig'))}
        testdir = os.path.join(self.unit_test_dir, '39 external, internal library rpath', 'built library')
        # install into installdir without using DESTDIR
        self.prefix = self.installdir
        self.init(testdir, override_envvars=env)
        self.prefix = oldprefix
        self.build(override_envvars=env)
        # test uninstalled
        self.run_tests(override_envvars=env)
        if not (is_osx() or is_linux()):
            return
        # test running after installation
        self.install(use_destdir=False)
        prog = os.path.join(self.installdir, 'bin', 'prog')
        self._run([prog])
        if not is_osx():
            # Rest of the workflow only works on macOS
            return
        out = self._run(['otool', '-L', prog])
        self.assertNotIn('@rpath', out)
        ## New builddir for testing that DESTDIR is not added to install_name
        self.new_builddir()
        # install into installdir with DESTDIR
        self.init(testdir, override_envvars=env)
        self.build(override_envvars=env)
        # test running after installation
        self.install(override_envvars=env)
        prog = self.installdir + os.path.join(self.prefix, 'bin', 'prog')
        lib = self.installdir + os.path.join(self.prefix, 'lib', 'libbar_built.dylib')
        for f in prog, lib:
            out = self._run(['otool', '-L', f])
            # Ensure that the otool output does not contain self.installdir
            self.assertNotRegex(out, self.installdir + '.*dylib ')

    @skipIfNoPkgconfig
    def test_link_arg_fullname(self):
        '''
        Test for  support of -l:libfullname.a
        see: https://github.com/mesonbuild/meson/issues/9000
             https://stackoverflow.com/questions/48532868/gcc-library-option-with-a-colon-llibevent-a
        '''
        testdir = os.path.join(self.unit_test_dir, '98 link full name','libtestprovider')
        oldprefix = self.prefix
        # install into installdir without using DESTDIR
        installdir = self.installdir
        self.prefix = installdir
        self.init(testdir)
        self.prefix=oldprefix
        self.build()
        self.install(use_destdir=False)

        self.new_builddir()
        env = {'LIBRARY_PATH': os.path.join(installdir, self.libdir),
               'PKG_CONFIG_PATH': _prepend_pkg_config_path(os.path.join(installdir, self.libdir, 'pkgconfig'))}
        testdir = os.path.join(self.unit_test_dir, '98 link full name','proguser')
        self.init(testdir,override_envvars=env)

        # test for link with full path
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as bfile:
            for line in bfile:
                if 'build dprovidertest:' in line:
                    self.assertIn('/libtestprovider.a', line)

        if is_osx():
            # macOS's ld do not supports `--whole-archive`, skip build & run
            return

        self.build(override_envvars=env)

        # skip test if pkg-config is too old.
        #   before v0.28, Libs flags like -Wl will not kept in context order with -l flags.
        #   see https://gitlab.freedesktop.org/pkg-config/pkg-config/-/blob/master/NEWS
        pkgconfigver = subprocess.check_output([PKG_CONFIG, '--version'])
        if b'0.28' > pkgconfigver:
            raise SkipTest('pkg-config is too old to be correctly done this.')
        self.run_tests()

    @skipIfNoPkgconfig
    def test_usage_pkgconfig_prefixes(self):
        '''
        Build and install two external libraries, to different prefixes,
        then build and install a client program that finds them via pkgconfig,
        and verify the installed client program runs.
        '''
        oldinstalldir = self.installdir

        # Build and install both external libraries without DESTDIR
        val1dir = os.path.join(self.unit_test_dir, '74 pkgconfig prefixes', 'val1')
        val1prefix = os.path.join(oldinstalldir, 'val1')
        self.prefix = val1prefix
        self.installdir = val1prefix
        self.init(val1dir)
        self.build()
        self.install(use_destdir=False)
        self.new_builddir()

        env1 = {}
        env1['PKG_CONFIG_PATH'] = os.path.join(val1prefix, self.libdir, 'pkgconfig')
        val2dir = os.path.join(self.unit_test_dir, '74 pkgconfig prefixes', 'val2')
        val2prefix = os.path.join(oldinstalldir, 'val2')
        self.prefix = val2prefix
        self.installdir = val2prefix
        self.init(val2dir, override_envvars=env1)
        self.build()
        self.install(use_destdir=False)
        self.new_builddir()

        # Build, install, and run the client program
        env2 = {}
        env2['PKG_CONFIG_PATH'] = os.path.join(val2prefix, self.libdir, 'pkgconfig')
        testdir = os.path.join(self.unit_test_dir, '74 pkgconfig prefixes', 'client')
        testprefix = os.path.join(oldinstalldir, 'client')
        self.prefix = testprefix
        self.installdir = testprefix
        self.init(testdir, override_envvars=env2)
        self.build()
        self.install(use_destdir=False)
        prog = os.path.join(self.installdir, 'bin', 'client')
        env3 = {}
        if is_cygwin():
            env3['PATH'] = os.path.join(val1prefix, 'bin') + \
                os.pathsep + \
                os.path.join(val2prefix, 'bin') + \
                os.pathsep + os.environ['PATH']
        out = self._run([prog], override_envvars=env3).strip()
        # Expected output is val1 + val2 = 3
        self.assertEqual(out, '3')

    def install_subdir_invalid_symlinks(self, testdir, subdir_path):
        '''
        Test that installation of broken symlinks works fine.
        https://github.com/mesonbuild/meson/issues/3914
        '''
        testdir = self.copy_srcdir(os.path.join(self.common_test_dir, testdir))
        subdir = os.path.join(testdir, subdir_path)
        with chdir(subdir):
            # Can't distribute broken symlinks in the source tree because it breaks
            # the creation of zipapps. Create it dynamically and run the test by
            # hand.
            src = '../../nonexistent.txt'
            os.symlink(src, 'invalid-symlink.txt')
            self.init(testdir)
            self.build()
            self.install()
            install_path = subdir_path.split(os.path.sep)[-1]
            link = os.path.join(self.installdir, 'usr', 'share', install_path, 'invalid-symlink.txt')
            self.assertTrue(os.path.islink(link), msg=link)
            self.assertEqual(src, os.readlink(link))
            self.assertFalse(os.path.isfile(link), msg=link)

    def test_install_subdir_symlinks(self):
        self.install_subdir_invalid_symlinks('59 install subdir', os.path.join('sub', 'sub1'))

    def test_install_subdir_symlinks_with_default_umask(self):
        self.install_subdir_invalid_symlinks('190 install_mode', 'sub2')

    def test_install_subdir_symlinks_with_default_umask_and_mode(self):
        self.install_subdir_invalid_symlinks('190 install_mode', 'sub1')

    @skipIfNoPkgconfigDep('gmodule-2.0')
    def test_ldflag_dedup(self):
        testdir = os.path.join(self.unit_test_dir, '51 ldflagdedup')
        if is_cygwin() or is_osx():
            raise SkipTest('Not applicable on Cygwin or OSX.')
        env = get_fake_env()
        cc = detect_c_compiler(env, MachineChoice.HOST)
        linker = cc.linker
        if not linker.export_dynamic_args():
            raise SkipTest('Not applicable for linkers without --export-dynamic')
        self.init(testdir)
        build_ninja = os.path.join(self.builddir, 'build.ninja')
        max_count = 0
        search_term = '-Wl,--export-dynamic'
        with open(build_ninja, encoding='utf-8') as f:
            for line in f:
                max_count = max(max_count, line.count(search_term))
        self.assertEqual(max_count, 1, 'Export dynamic incorrectly deduplicated.')

    def test_compiler_libs_static_dedup(self):
        testdir = os.path.join(self.unit_test_dir, '55 dedup compiler libs')
        self.init(testdir)
        build_ninja = os.path.join(self.builddir, 'build.ninja')
        with open(build_ninja, encoding='utf-8') as f:
            lines = f.readlines()
        for lib in ('-ldl', '-lm', '-lc', '-lrt'):
            for line in lines:
                if lib not in line:
                    continue
                # Assert that
                self.assertEqual(len(line.split(lib)), 2, msg=(lib, line))

    @skipIfNoPkgconfig
    def test_noncross_options(self):
        # C_std defined in project options must be in effect also when native compiling.
        testdir = os.path.join(self.unit_test_dir, '50 noncross options')
        self.init(testdir, extra_args=['-Dpkg_config_path=' + testdir])
        compdb = self.get_compdb()
        self.assertEqual(len(compdb), 2)
        self.assertRegex(compdb[0]['command'], '-std=c99')
        self.assertRegex(compdb[1]['command'], '-std=c99')
        self.build()

    def test_identity_cross(self):
        testdir = os.path.join(self.unit_test_dir, '60 identity cross')

        constantsfile = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8')
        constantsfile.write(textwrap.dedent('''\
            [constants]
            py_ext = '.py'
            '''))
        constantsfile.flush()

        nativefile = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8')
        nativefile.write(textwrap.dedent('''\
            [binaries]
            c = ['{}' + py_ext]
            '''.format(os.path.join(testdir, 'build_wrapper'))))
        nativefile.flush()
        self.meson_native_files = [constantsfile.name, nativefile.name]

        crossfile = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8')
        crossfile.write(textwrap.dedent('''\
            [binaries]
            c = ['{}' + py_ext]
            '''.format(os.path.join(testdir, 'host_wrapper'))))
        crossfile.flush()
        self.meson_cross_files = [constantsfile.name, crossfile.name]

        # TODO should someday be explicit about build platform only here
        self.init(testdir)

    def test_identity_cross_env(self):
        testdir = os.path.join(self.unit_test_dir, '60 identity cross')
        env = {
            'CC_FOR_BUILD': '"' + os.path.join(testdir, 'build_wrapper.py') + '"',
            'CC': '"' + os.path.join(testdir, 'host_wrapper.py') + '"',
        }
        crossfile = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8')
        crossfile.write('')
        crossfile.flush()
        self.meson_cross_files = [crossfile.name]
        # TODO should someday be explicit about build platform only here
        self.init(testdir, override_envvars=env)

    @skipIfNoPkgconfig
    def test_static_link(self):
        if is_cygwin():
            raise SkipTest("Cygwin doesn't support LD_LIBRARY_PATH.")

        # Build some libraries and install them
        testdir = os.path.join(self.unit_test_dir, '66 static link/lib')
        libdir = os.path.join(self.installdir, self.libdir)
        oldprefix = self.prefix
        self.prefix = self.installdir
        self.init(testdir)
        self.install(use_destdir=False)

        # Test that installed libraries works
        self.new_builddir()
        self.prefix = oldprefix
        meson_args = [f'-Dc_link_args=-L{libdir}',
                      '--fatal-meson-warnings']
        testdir = os.path.join(self.unit_test_dir, '66 static link')
        env = {'PKG_CONFIG_LIBDIR': os.path.join(libdir, 'pkgconfig')}
        self.init(testdir, extra_args=meson_args, override_envvars=env)
        self.build()
        self.run_tests()

    def _check_ld(self, check: str, name: str, lang: str, expected: str) -> None:
        if is_sunos():
            raise SkipTest('Solaris currently cannot override the linker.')
        if not shutil.which(check):
            raise SkipTest(f'Could not find {check}.')
        envvars = mesonbuild.envconfig.ENV_VAR_PROG_MAP[f'{lang}_ld'].copy()

        # Also test a deprecated variable if there is one.
        if f'{lang}_ld' in mesonbuild.envconfig.DEPRECATED_ENV_PROG_MAP:
            envvars.extend(
                mesonbuild.envconfig.DEPRECATED_ENV_PROG_MAP[f'{lang}_ld'])

        for envvar in envvars:
            with mock.patch.dict(os.environ, {envvar: name}):
                env = get_fake_env()
                comp = compiler_from_language(env, lang, MachineChoice.HOST)
                if isinstance(comp, (AppleClangCCompiler, AppleClangCPPCompiler,
                                     AppleClangObjCCompiler, AppleClangObjCPPCompiler)):
                    raise SkipTest('AppleClang is currently only supported with ld64')
                if isinstance(comp, ElbrusCompiler):
                    raise SkipTest('ElbrusCompiler currently cannot override the linker.')
                if lang != 'rust' and comp.use_linker_args('bfd', '') == []:
                    raise SkipTest(
                        f'Compiler {comp.id} does not support using alternative linkers')
                self.assertEqual(comp.linker.id, expected)

    def test_ld_environment_variable_bfd(self):
        self._check_ld('ld.bfd', 'bfd', 'c', 'ld.bfd')

    def test_ld_environment_variable_gold(self):
        self._check_ld('ld.gold', 'gold', 'c', 'ld.gold')

    def test_ld_environment_variable_lld(self):
        self._check_ld('ld.lld', 'lld', 'c', 'ld.lld')

    @skip_if_not_language('rust')
    @skipIfNoExecutable('ld.gold')  # need an additional check here because _check_ld checks for gcc
    def test_ld_environment_variable_rust(self):
        self._check_ld('gcc', 'gcc -fuse-ld=gold', 'rust', 'ld.gold')

    def test_ld_environment_variable_cpp(self):
        self._check_ld('ld.gold', 'gold', 'cpp', 'ld.gold')

    @skip_if_not_language('objc')
    def test_ld_environment_variable_objc(self):
        self._check_ld('ld.gold', 'gold', 'objc', 'ld.gold')

    @skip_if_not_language('objcpp')
    def test_ld_environment_variable_objcpp(self):
        self._check_ld('ld.gold', 'gold', 'objcpp', 'ld.gold')

    @skip_if_not_language('fortran')
    def test_ld_environment_variable_fortran(self):
        self._check_ld('ld.gold', 'gold', 'fortran', 'ld.gold')

    @skip_if_not_language('d')
    def test_ld_environment_variable_d(self):
        # At least for me, ldc defaults to gold, and gdc defaults to bfd, so
        # let's pick lld, which isn't the default for either (currently)
        if is_osx():
            expected = 'ld64'
        else:
            expected = 'ld.lld'
        self._check_ld('ld.lld', 'lld', 'd', expected)

    def compute_sha256(self, filename):
        with open(filename, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()

    def test_wrap_with_file_url(self):
        testdir = os.path.join(self.unit_test_dir, '72 wrap file url')
        source_filename = os.path.join(testdir, 'subprojects', 'foo.tar.xz')
        patch_filename = os.path.join(testdir, 'subprojects', 'foo-patch.tar.xz')
        wrap_filename = os.path.join(testdir, 'subprojects', 'foo.wrap')
        source_hash = self.compute_sha256(source_filename)
        patch_hash = self.compute_sha256(patch_filename)
        wrap = textwrap.dedent("""\
            [wrap-file]
            directory = foo

            source_url = http://server.invalid/foo
            source_fallback_url = file://{}
            source_filename = foo.tar.xz
            source_hash = {}

            patch_url = http://server.invalid/foo
            patch_fallback_url = file://{}
            patch_filename = foo-patch.tar.xz
            patch_hash = {}
            """.format(source_filename, source_hash, patch_filename, patch_hash))
        with open(wrap_filename, 'w', encoding='utf-8') as f:
            f.write(wrap)
        self.init(testdir)
        self.build()
        self.run_tests()

        windows_proof_rmtree(os.path.join(testdir, 'subprojects', 'packagecache'))
        windows_proof_rmtree(os.path.join(testdir, 'subprojects', 'foo'))
        os.unlink(wrap_filename)

    def test_no_rpath_for_static(self):
        testdir = os.path.join(self.common_test_dir, '5 linkstatic')
        self.init(testdir)
        self.build()
        build_rpath = get_rpath(os.path.join(self.builddir, 'prog'))
        self.assertIsNone(build_rpath)

    def test_lookup_system_after_broken_fallback(self):
        # Just to generate libfoo.pc so we can test system dependency lookup.
        testdir = os.path.join(self.common_test_dir, '44 pkgconfig-gen')
        self.init(testdir)
        privatedir = self.privatedir

        # Write test project where the first dependency() returns not-found
        # because 'broken' subproject does not exit, but that should not prevent
        # the 2nd dependency() to lookup on system.
        self.new_builddir()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'meson.build'), 'w', encoding='utf-8') as f:
                f.write(textwrap.dedent('''\
                    project('test')
                    dependency('notfound', fallback: 'broken', required: false)
                    dependency('libfoo', fallback: 'broken', required: true)
                    '''))
            self.init(d, override_envvars={'PKG_CONFIG_LIBDIR': privatedir})

    def test_as_link_whole(self):
        testdir = os.path.join(self.unit_test_dir, '76 as link whole')
        self.init(testdir)
        with open(os.path.join(self.privatedir, 'bar1.pc'), encoding='utf-8') as f:
            content = f.read()
            self.assertIn('-lfoo', content)
        with open(os.path.join(self.privatedir, 'bar2.pc'), encoding='utf-8') as f:
            content = f.read()
            self.assertNotIn('-lfoo', content)

    def test_prelinking(self):
        testdir = os.path.join(self.unit_test_dir, '86 prelinking')
        env = get_fake_env(testdir, self.builddir, self.prefix)
        cc = detect_c_compiler(env, MachineChoice.HOST)
        if cc.id == "gcc" and not version_compare(cc.version, '>=9'):
            raise SkipTest('Prelinking not supported with gcc 8 or older.')
        if cc.id == 'clang' and not version_compare(cc.version, '>=14'):
            raise SkipTest('Prelinking not supported with Clang 13 or older.')
        self.init(testdir)
        self.build()
        outlib = os.path.join(self.builddir, 'libprelinked.a')
        ar = shutil.which('ar')
        self.assertPathExists(outlib)
        self.assertIsNotNone(ar)
        p = subprocess.run([ar, 't', outlib],
                           stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL,
                           encoding='utf-8', text=True, timeout=1)
        obj_files = p.stdout.strip().split('\n')
        self.assertTrue(any(o.endswith('-prelink.o') for o in obj_files))

    def do_one_test_with_nativefile(self, testdir, args):
        testdir = os.path.join(self.common_test_dir, testdir)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'nativefile'
            with p.open('wt', encoding='utf-8') as f:
                f.write(f'''[binaries]
                    c = {args}
                    ''')
            self.init(testdir, extra_args=['--native-file=' + str(p)])
            self.build()

    def test_cmake_multilib(self):
        '''
        Test that the cmake module handles multilib paths correctly.
        '''
        # Verify that "gcc -m32" works
        try:
            self.do_one_test_with_nativefile('1 trivial', "['gcc', '-m32']")
        except subprocess.CalledProcessError as e:
            raise SkipTest('Not GCC, or GCC does not have the -m32 option')
        self.wipe()

        # Verify that cmake works
        try:
            self.do_one_test_with_nativefile('../cmake/1 basic', "['gcc']")
        except subprocess.CalledProcessError as e:
            raise SkipTest('Could not build basic cmake project')
        self.wipe()

        # If so, we can test that cmake works with "gcc -m32"
        self.do_one_test_with_nativefile('../cmake/1 basic', "['gcc', '-m32']")

    @skipUnless(is_linux() or is_osx(), 'Test only applicable to Linux and macOS')
    def test_install_strip(self):
        testdir = os.path.join(self.unit_test_dir, '104 strip')
        self.init(testdir)
        self.build()

        destdir = self.installdir + self.prefix
        if is_linux():
            lib = os.path.join(destdir, self.libdir, 'liba.so')
        else:
            lib = os.path.join(destdir, self.libdir, 'liba.dylib')
        install_cmd = self.meson_command + ['install', '--destdir', self.installdir]

        # Check we have debug symbols by default
        self._run(install_cmd, workdir=self.builddir)
        if is_linux():
            # file can detect stripped libraries on linux
            stdout = self._run(['file', '-b', lib])
            self.assertIn('not stripped', stdout)
        else:
            # on macOS we need to query dsymutil instead.
            # Alternatively, check if __dyld_private is defined
            # in the output of nm liba.dylib, but that is not
            # 100% reliable, it needs linking to an external library
            stdout = self._run(['dsymutil', '--dump-debug-map', lib])
            self.assertIn('symbols:', stdout)

        # Check debug symbols got removed with --strip
        self._run(install_cmd + ['--strip'], workdir=self.builddir)
        if is_linux():
            stdout = self._run(['file', '-b', lib])
            self.assertNotIn('not stripped', stdout)
        else:
            stdout = self._run(['dsymutil', '--dump-debug-map', lib])
            self.assertNotIn('symbols:', stdout)

    def test_isystem_default_removal_with_symlink(self):
        env = get_fake_env()
        cpp = detect_cpp_compiler(env, MachineChoice.HOST)
        default_dirs = cpp.get_default_include_dirs()
        default_symlinks = []
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(len(default_dirs)):
                symlink = f'{tmpdir}/default_dir{i}'
                default_symlinks.append(symlink)
                os.symlink(default_dirs[i], symlink)
            self.assertFalse(cpp.compiler_args([f'-isystem{symlink}' for symlink in default_symlinks]).to_native())

    def test_freezing(self):
        testdir = os.path.join(self.unit_test_dir, '111 freeze')
        self.init(testdir)
        self.build()
        with self.assertRaises(subprocess.CalledProcessError) as e:
            self.run_tests()
        self.assertNotIn('Traceback', e.exception.output)

    @skipUnless(is_linux(), "Ninja file differs on different platforms")
    def test_complex_link_cases(self):
        testdir = os.path.join(self.unit_test_dir, '115 complex link cases')
        self.init(testdir)
        self.build()
        with open(os.path.join(self.builddir, 'build.ninja'), encoding='utf-8') as f:
            content = f.read()
        # Verify link dependencies, see comments in meson.build.
        self.assertIn('build libt1-s3.a: STATIC_LINKER libt1-s2.a.p/s2.c.o libt1-s3.a.p/s3.c.o\n', content)
        self.assertIn('build t1-e1: c_LINKER t1-e1.p/main.c.o | libt1-s1.a libt1-s3.a\n', content)
        self.assertIn('build libt2-s3.a: STATIC_LINKER libt2-s2.a.p/s2.c.o libt2-s1.a.p/s1.c.o libt2-s3.a.p/s3.c.o\n', content)
        self.assertIn('build t2-e1: c_LINKER t2-e1.p/main.c.o | libt2-s3.a\n', content)
        self.assertIn('build t3-e1: c_LINKER t3-e1.p/main.c.o | libt3-s3.so.p/libt3-s3.so.symbols\n', content)
        self.assertIn('build t4-e1: c_LINKER t4-e1.p/main.c.o | libt4-s2.so.p/libt4-s2.so.symbols libt4-s3.a\n', content)
        self.assertIn('build t5-e1: c_LINKER t5-e1.p/main.c.o | libt5-s1.so.p/libt5-s1.so.symbols libt5-s3.a\n', content)
        self.assertIn('build t6-e1: c_LINKER t6-e1.p/main.c.o | libt6-s2.a libt6-s3.a\n', content)
        self.assertIn('build t7-e1: c_LINKER t7-e1.p/main.c.o | libt7-s3.a\n', content)
        self.assertIn('build t8-e1: c_LINKER t8-e1.p/main.c.o | libt8-s1.a libt8-s2.a libt8-s3.a\n', content)
        self.assertIn('build t9-e1: c_LINKER t9-e1.p/main.c.o | libt9-s1.a libt9-s2.a libt9-s3.a\n', content)
        self.assertIn('build t12-e1: c_LINKER t12-e1.p/main.c.o | libt12-s1.a libt12-s2.a libt12-s3.a\n', content)
        self.assertIn('build t13-e1: c_LINKER t13-e1.p/main.c.o | libt12-s1.a libt13-s3.a\n', content)

    def test_top_options_in_sp(self):
        testdir = os.path.join(self.unit_test_dir, '128 pkgsubproj')
        self.init(testdir)

    def test_unreadable_dir_in_declare_dep(self):
        testdir = os.path.join(self.unit_test_dir, '126 declare_dep var')
        tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(windows_proof_rmtree, tmpdir)
        declaredepdir = tmpdir / 'test'
        declaredepdir.mkdir()
        try:
            tmpdir.chmod(0o444)
            self.init(testdir, extra_args=f'-Ddir={declaredepdir}')
        finally:
            tmpdir.chmod(0o755)

    def check_has_flag(self, compdb, src, argument):
        for i in compdb:
            if src in i['file']:
                self.assertIn(argument, i['command'])
                return
        self.assertTrue(False, f'Source {src} not found in compdb')

    def test_persp_options(self):
        if self.backend is not Backend.ninja:
            raise SkipTest(f'{self.backend.name!r} backend can\'t install files')

        testdir = os.path.join(self.unit_test_dir, '123 persp options')

        with self.subTest('init'):
            self.init(testdir, extra_args='-Doptimization=1')
            compdb = self.get_compdb()
            mainsrc = 'toplevel.c'
            sub1src = 'sub1.c'
            sub2src = 'sub2.c'
            self.check_has_flag(compdb, mainsrc, '-O1')
            self.check_has_flag(compdb, sub1src, '-O1')
            self.check_has_flag(compdb, sub2src, '-O1')

        # Set subproject option to O2
        with self.subTest('set subproject option'):
            self.setconf(['-Dround=2', '-D', 'sub2:optimization=3'])
            compdb = self.get_compdb()
            self.check_has_flag(compdb, mainsrc, '-O1')
            self.check_has_flag(compdb, sub1src, '-O1')
            self.check_has_flag(compdb, sub2src, '-O3')

        # Change an already set override.
        with self.subTest('change subproject option'):
            self.setconf(['-Dround=3', '-D', 'sub2:optimization=2'])
            compdb = self.get_compdb()
            self.check_has_flag(compdb, mainsrc, '-O1')
            self.check_has_flag(compdb, sub1src, '-O1')
            self.check_has_flag(compdb, sub2src, '-O2')

        # Set top level option to O3
        with self.subTest('change main project option'):
            self.setconf(['-Dround=4', '-D:optimization=3'])
            compdb = self.get_compdb()
            self.check_has_flag(compdb, mainsrc, '-O3')
            self.check_has_flag(compdb, sub1src, '-O1')
            self.check_has_flag(compdb, sub2src, '-O2')

        # Unset subproject
        with self.subTest('unset subproject option'):
            self.setconf(['-Dround=5', '-U', 'sub2:optimization'])
            compdb = self.get_compdb()
            self.check_has_flag(compdb, mainsrc, '-O3')
            self.check_has_flag(compdb, sub1src, '-O1')
            self.check_has_flag(compdb, sub2src, '-O1')

        # Set global value
        with self.subTest('set global option'):
            self.setconf(['-Dround=6', '-D', 'optimization=2'])
            compdb = self.get_compdb()
            self.check_has_flag(compdb, mainsrc, '-O3')
            self.check_has_flag(compdb, sub1src, '-O2')
            self.check_has_flag(compdb, sub2src, '-O2')

    @skip_if_not_language('rust')
    @skip_if_not_base_option('b_sanitize')
    def test_rust_sanitizers(self):
        args = ['-Drust_nightly=disabled', '-Db_lundef=false']
        testdir = os.path.join(self.rust_test_dir, '28 mixed')
        tests = ['address']

        env = get_fake_env(testdir, self.builddir, self.prefix)
        cpp = detect_cpp_compiler(env, MachineChoice.HOST)
        if cpp.find_library('ubsan', []):
            tests += ['address,undefined']

        for value in tests:
            self.init(testdir, extra_args=args + ['-Db_sanitize=' + value])
            self.build()
            self.wipe()

    @skip_if_not_language('rust')
    def test_rust_staticlib_rlib_deps(self):
        '''
        Test that when a C executable links with a Rust staticlib, the rlib
        dependencies of the staticlib are not passed to the C linker.
        See: https://github.com/mesonbuild/meson/issues/11721
        '''
        testdir = os.path.join(self.rust_test_dir, '36 staticlib rlib deps')
        self.init(testdir)
        targets = self.introspect('--targets')
        executable = next(t for t in targets if t['type'] == 'executable')
        linker = next(src for src in executable['target_sources'] if 'linker' in src)
        for param in linker['parameters']:
            self.assertNotIn('liblib.rlib', param)

    def test_sanitizers(self):
        testdir = os.path.join(self.unit_test_dir, '130 sanitizers')

        with self.subTest('no b_sanitize value'):
            try:
                out = self.init(testdir)
                self.assertRegex(out, 'value *: *none')
            finally:
                self.wipe()

        for value, expected in { '': 'none',
                                 'none': 'none',
                                 'address': 'address',
                                 'undefined,address': 'address,undefined',
                                 'address,undefined': 'address,undefined' }.items():
            with self.subTest('b_sanitize=' + value):
                try:
                    out = self.init(testdir, extra_args=['-Db_sanitize=' + value])
                    self.assertRegex(out, 'value *: *' + expected)
                finally:
                    self.wipe()
