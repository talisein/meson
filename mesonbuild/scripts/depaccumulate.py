# SPDX-License-Identifier: Apache-2.0
# Copyright © 2021-2024 Intel Corporation

"""Accumulator for p1689r5 module dependencies.

See: https://www.open-std.org/jtc1/sc22/wg21/docs/papers/2022/p1689r5.html
"""

from __future__ import annotations
import argparse
import json
import os
import re
import shutil
import textwrap
import typing as T

from ..utils.core import MesonException, flat_cmi_path

if T.TYPE_CHECKING:
    from .depscan import Description, Require, Rule

# The quoting logic has been copied from the ninjabackend to avoid having to
# import half of Meson just to quote outputs, which is a performance problem
_QUOTE_PAT = re.compile(r'[$ :\n]')


def quote(text: str) -> str:
    # Fast path for when no quoting is necessary
    if not _QUOTE_PAT.search(text):
        return text
    if '\n' in text:
        errmsg = textwrap.dedent(f'''\
            Ninja does not support newlines in rules. The content was:

            {text}

            Please report this error with a test case to the Meson bug tracker.''')
        raise RuntimeError(errmsg)
    return _QUOTE_PAT.sub(r'$\g<0>', text)


_PROVIDER_CACHE: T.Dict[str, str] = {}


def get_provider(rules: T.List[Rule], name: str) -> T.Optional[str]:
    """Get the object that a module from another Target provides

    We must rely on the object file here instead of the module itself, because
    the object rule is part of the generated build.ninja, while the module is
    only declared inside a dyndep. This creates for the dyndep generator to
    depend on previous dyndeps as order deps. Since the module
    interface file will be generated when the object is generated we can rely on
    that in proxy and simplify generation.

    :param rules: The list of rules to check
    :param name: The logical-name to look for
    :raises RuntimeError: If no provider can be found
    :return: The object file of the rule providing the module
    """
    # Cache the result for performance reasons
    if name in _PROVIDER_CACHE:
        return _PROVIDER_CACHE[name]

    for r in rules:
        for p in r.get('provides', []):
            if p['logical-name'] == name:
                obj = r['primary-output']
                _PROVIDER_CACHE[name] = obj
                return obj
    return None


def process_rules(rules: T.List[Rule],
                  extra_rules: T.List[Rule],
                  ) -> T.Iterable[T.Tuple[str, T.Optional[T.List[str]], T.List[str]]]:
    """Process the rules for this Target

    :param rules: the rules for this target
    :param extra_rules: the rules for all of the targets this one links with, to use their provides
    :yield: A tuple of the output, the exported modules, and the consumed modules
    """
    for rule in rules:
        prov: T.Optional[T.List[str]] = None
        req: T.List[str] = []
        if 'provides' in rule:
            prov = [p['compiled-module-path'] for p in rule['provides']]
        if 'requires' in rule:
            for p in rule['requires']:
                modfile = p.get('compiled-module-path')
                if modfile is not None:
                    req.append(modfile)
                else:
                    # We can't error if this is not found because of compiler
                    # provided modules
                    found = get_provider(extra_rules, p['logical-name'])
                    if found:
                        req.append(found)
        yield rule['primary-output'], prov, req


def formatter(files: T.Optional[T.List[str]]) -> str:
    if files:
        fmt = ' '.join(quote(f) for f in files)
        return f'| {fmt}'
    return ''


def gen(outfile: str, desc: Description, extra_rules: T.List[Rule]) -> int:
    with open(outfile, 'w', encoding='utf-8') as f:
        f.write('ninja_dyndep_version = 1\n\n')

        for obj, provides, requires in process_rules(desc['rules'], extra_rules):
            ins = formatter(requires)
            out = formatter(provides)
            f.write(f'build {quote(obj)} {out}: dyndep {ins}\n\n')

    return 0


def module_to_filename(name: str, bmidir: str, suffix: str) -> str:
    """Map a C++ module logical-name to its BMI path.

    The compiler names a module's BMI <bmidir>/<name><suffix> with a partition
    separator ':' becoming '-' (gcm.cache/pkg-part.gcm for GCC,
    ifc.cache/pkg-part.ifc for MSVC). Mirrors the compiler's
    module_name_to_filename; kept here so the collator names BMIs from
    logical-names alone -- the P1689 output does not carry a
    compiled-module-path. bmidir/suffix are passed by the backend from the
    compiler so the two stay in lockstep.
    """
    return f'{bmidir}/{name.replace(":", "-")}{suffix}'


def _source_key(path: str) -> str:
    """Canonical key for comparing a P1689 source-path against a declared
    interface source. cl writes an absolute (and, on Windows, lowercased)
    source-path in its scan output while the backend passes --interface-source
    build-relative, so both are resolved to an absolute path and case-folded
    (a no-op on case-sensitive filesystems). The collate runs with the build
    directory as cwd, which both relative forms are relative to.
    """
    return os.path.normcase(os.path.abspath(path))


def _write_if_different(path: str, content: str) -> None:
    """Write only when the content changed, preserving mtime otherwise.

    Mapper files are implicit inputs of compile edges; an unconditional
    rewrite would recompile every object in the target whenever any module
    in it changes.

    newline='' keeps the newlines exactly as written: GCC's module-mapper
    parser reads a mapper key up to the newline and does not strip a carriage
    return, so a CRLF (which text mode would emit on Windows) breaks the lookup
    with "failed reading mapper". Reading the same way makes a mapper left over
    from an earlier CRLF-writing Meson compare unequal, so it is rewritten.
    """
    try:
        with open(path, encoding='utf-8', newline='') as f:
            if f.read() == content:
                return
    except OSError:
        pass
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(content)


def _is_header_unit(req: Require) -> bool:
    # A header-unit require is skipped by the collator: its BMI is pre-built by a
    # static edge, so it is neither a dyndep dependency nor a missing named
    # module. cl tags it with a lookup-method of include-quote/include-angle (a
    # named module is by-name). GCC omits lookup-method, so fall back to shape:
    # a header unit's logical-name is a resolved header path ('./util.h',
    # '/usr/.../vector', or on Windows '.\\util.h' / 'C:\\...'), which always
    # contains a path separator; a named module or ':partition' never does.
    method = req.get('lookup-method')
    if method in ('include-quote', 'include-angle'):
        return True
    if method == 'by-name':
        return False
    name = req['logical-name']
    return '/' in name or '\\' in name


def _check_module_cycle(rules: T.List[Rule]) -> None:
    """Raise on a dependency cycle among this target's own modules.

    Nodes are the module names provided in this target -- public or private,
    derived straight from `rules` rather than passed in, since a cycle
    running through a private module is exactly as real a cycle -- and an
    edge goes from a provided module to each module (also provided here) that
    its translation unit requires. A back-edge in a DFS is a cycle; report it
    before ninja's own generic cycle detector would.
    """
    local_names = {prov['logical-name'] for rule in rules for prov in rule.get('provides', [])}
    deps: T.Dict[str, T.List[str]] = {}
    for rule in rules:
        local_reqs = [r['logical-name'] for r in rule.get('requires', [])
                      if r['logical-name'] in local_names]
        for prov in rule.get('provides', []):
            deps.setdefault(prov['logical-name'], []).extend(local_reqs)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: T.Dict[str, int] = {n: WHITE for n in deps}
    path: T.List[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        for nxt in deps.get(node, []):
            if color.get(nxt, BLACK) == GRAY:
                cycle = path[path.index(nxt):] + [nxt]
                raise MesonException(
                    'C++ module dependency cycle: ' + ' -> '.join(cycle))
            if color.get(nxt, BLACK) == WHITE:
                visit(nxt)
        path.pop()
        color[node] = BLACK

    for name in deps:
        if color[name] == WHITE:
            visit(name)


def _claim_module_provider(name: str, cache_bmi: str, provmap: str) -> None:
    """Enforce one providing target per module name per build tree.

    Unrelated targets never meet in a collate (--dep-provmap carries only
    linked dependencies), but every provider's BMI lands in the shared module
    cache at a path keyed by the module name alone, so two exporters of one
    name would silently fight over the same BMI file and wedge the build.
    Record the owning target's provmap path next to the would-be BMI; a second
    claimant errors. A claim is stale -- and taken over -- when its provmap is
    gone (target removed; meson reconfigure runs `ninja -t cleandead`) or no
    longer lists the module (the module moved and the old provider
    re-collated).

    The claim is published by hard-linking a fully written file, so its name
    and contents appear atomically: collates run concurrently under ninja,
    and a loser that could read a created-but-not-yet-written owner would
    mistake the winner's live claim for a stale one and take it over.
    """
    owner_file = cache_bmi + '.owner'
    os.makedirs(os.path.dirname(owner_file), exist_ok=True)
    tmp = f'{owner_file}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(provmap)
    try:
        while True:
            try:
                os.link(tmp, owner_file)
                return
            except FileExistsError:
                pass
            except OSError:
                # Filesystem without hard links: fall back to exclusive
                # create + write (a narrow non-atomic window, as before).
                try:
                    fd = os.open(owner_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        f.write(provmap)
                    return
            with open(owner_file, encoding='utf-8') as f:
                owner = f.read()
            if owner == provmap:
                return
            live = False
            if os.path.exists(owner):
                try:
                    with open(owner, encoding='utf-8') as f:
                        live = name in json.load(f)
                except (OSError, ValueError):
                    pass
            if live:
                raise MesonException(
                    f'Module "{name}" is exported by more than one target in this '
                    f'build ({os.path.dirname(owner)} and {os.path.dirname(provmap)}); '
                    f'both would write their BMI to {cache_bmi}. A module name may '
                    'have only one providing target per build tree. (If the module '
                    'recently moved between targets this claim may be stale; re-run '
                    'ninja once.)')
            # Stale: drop it and retry the atomic claim; a concurrent
            # claimant may win the retry, and the next round then reads its
            # live claim.
            try:
                os.unlink(owner_file)
            except FileNotFoundError:
                pass
    finally:
        os.unlink(tmp)


def run_p1689(argv: T.List[str]) -> int:
    """Collate P1689 scans into a dyndep + a provided-module map.

    Consumes this target's per-source .ddi files and the provided-module maps of
    its dependency targets, emitting a Ninja dyndep that orders each object
    against the BMIs it requires/provides, plus this target's own map. The BMI
    directory and suffix (--bmi-dir/--bmi-suffix) come from the compiler so the
    logical-name -> BMI mapping matches the compiler's own; --bmi-dir always
    names the shared class-cache directory, whether a name is this target's
    own public provide or reached through a linked dependency.

    A provide whose object is named by --private-interface (or, for a
    module-providing executable where every module is private by
    construction, --all-provides-private) is instead named at
    --private-bmi-dir: it is resolvable within this target's own compiles but
    never published to --provmap, never globally claimed, and its name is
    published separately, by name only, to --private-map -- so a dependent
    can recognize (via --dep-private-map) that a module it's missing is
    private rather than absent, without ever being able to resolve it.
    "Never globally claimed" narrows the uniqueness requirement to one link's
    own transitive closure rather than the whole build tree; it does not
    remove it: two dependencies of one link privately providing the same
    name still collide at the linkage-symbol level (a module's exported
    entities are mangled from its bare name, private or not), so
    --dep-private-map is also checked for that cross-target collision here.

    With --mapper-suffix it also writes a GCC module mapper per translation
    unit (<primary-output><suffix>, an implicit input of the compile edge)
    naming the TU's provides and direct imports: GCC has no per-class
    module-search-path flag, so relocating BMIs into a class subdirectory
    means enumerating them per TU. Transitive imports need no entries (GCC
    reads them from the direct imports' CMIs), so the file is linear in the
    TU's imports. Written copy-if-different: an unconditional rewrite would
    recompile the whole target whenever any module in it changed.
    """
    parser = argparse.ArgumentParser(prog='depaccumulate --p1689')
    parser.add_argument('--dyndep', required=True, help='Output Ninja dyndep file.')
    parser.add_argument('--provmap', required=True,
                        help="Output provided-module map for this target.")
    parser.add_argument('--bmi-dir', required=True,
                        help='Directory the compiler names BMIs in (e.g. gcm.cache).')
    parser.add_argument('--bmi-suffix', required=True,
                        help='BMI file suffix including the dot (e.g. .gcm).')
    parser.add_argument('--dep-provmap', action='append', default=[],
                        help='Provided-module map of a dependency target. Repeatable.')
    parser.add_argument('--private-bmi-dir', default=None,
                        help='Directory a provide named by --private-interface (or, '
                             'under --all-provides-private, any provide) is named in '
                             'instead of --bmi-dir.')
    parser.add_argument('--private-interface', action='append', default=[],
                        dest='private_interface_objs',
                        help='The *object* path (a P1689 rule\'s primary-output, not its '
                             'source-path) of a private interface compile '
                             '(cpp_private_module_interfaces): its provide is named at '
                             '--private-bmi-dir, resolvable only within this target, never '
                             'published to --provmap. Matched by object rather than source: '
                             "only Clang's P1689 output carries a source-path for a provide at "
                             'all, so it is not a compiler-agnostic key here (unlike '
                             '--interface-source, which is a Clang-only concern). Repeatable.')
    parser.add_argument('--all-provides-private', action='store_true',
                        help='Every provide in this target is private (a module-providing '
                             "executable: nothing can ever link it, so all of its modules "
                             'are private by construction). Overrides --private-interface.')
    parser.add_argument('--private-map', default=None,
                        help="Output a JSON list of this target's own private module names "
                             '(names only, never paths, so a consumer can recognize one but '
                             'never resolve it). Omitted for a BMI-only variant\'s collate, '
                             'which never has private provides of its own.')
    parser.add_argument('--dep-private-map', action='append', default=[], nargs=2,
                        dest='dep_private_maps', metavar=('PATH', 'TARGET'),
                        help='A linked dependency\'s own private-modules.json and the name of '
                             'the target that provides it, for the "provided but private" '
                             'diagnostic. Repeatable.')
    parser.add_argument('--own-private-map', default=None, nargs=2,
                        dest='own_private_map', metavar=('PATH', 'TARGET'),
                        help="This rule set's own provider's private-modules.json and its "
                             'target name. Only ever passed for a BMI-only variant\'s '
                             "collate: a variant recompiles a public interface under another "
                             "class's flags, and that interface may legally import the "
                             "provider's own private module (same target, same "
                             'unkeyed-by-class private dir) -- the unresolved-require branch '
                             'below needs this to give a precise diagnostic instead of the '
                             'generic "provided by no target" one. Its mere presence marks '
                             'the collate as a variant\'s, so no separate flag is needed to '
                             'say so. Never repeated -- a variant has exactly one provider.')
    parser.add_argument('--stamp-suffix', default=None,
                        help='BMIs reach the shared cache via harvest edges (Clang\'s '
                             'own pipeline, and BMI-only variants on any compiler): '
                             'map a provided module to its harvest stamp '
                             '(<primary-output> + this suffix) instead of the cache BMI '
                             'path, and declare no implicit outputs on object edges.')
    parser.add_argument('--interface-source', action='append', default=[],
                        dest='interface_sources',
                        help='A source compiled as an interface unit despite lacking a '
                             'module extension (declared via cpp_module_interfaces, or '
                             'any recorded interface of a BMI-only variant). Its '
                             'provides pass the interface-extension check. Repeatable.')
    parser.add_argument('--header-unit', action='append', default=[], dest='header_units',
                        help='A declared header unit as "<mode>:<spelling>". Repeatable.')
    parser.add_argument('--mapper-suffix', default=None,
                        help='Also write a GCC module mapper per TU at '
                             '<primary-output> + this suffix, mapping its provides '
                             'and direct imports to their BMI paths.')
    parser.add_argument('--flat-bmi-dir', default=None,
                        help='The unkeyed shared cache dir (e.g. gcm.cache) header-unit '
                             'imports resolve in; mappers reproduce the compiler\'s '
                             'default header-unit CMI naming under it for any unit not '
                             'named by --header-unit-bmi.')
    parser.add_argument('--header-unit-bmi', action='append', default=[], nargs=2,
                        dest='header_unit_bmis', metavar=('NAME', 'BMI'),
                        help="A header unit built for this target's own BMI class, as its "
                             "resolved name (the scan's logical-name) and its BMI path. Two "
                             "arguments, not one joined pair: a system unit's name is an "
                             'absolute path, which on Windows carries a colon. Repeatable.')
    parser.add_argument('ddis', nargs='*', help="This target's P1689 scan results.")
    args = parser.parse_args(argv)

    # (mode, spelling) of every header unit declared on this target, so a source
    # that imports an undeclared one can be flagged. Only cl reports header-unit
    # requires from a cold scan (with a lookup-method), so this check is
    # effective for MSVC; GCC fails earlier, at the scan itself.
    declared_units = {tuple(hu.split(':', 1)) for hu in args.header_units}
    interface_sources = {_source_key(p) for p in args.interface_sources}
    # Matched by object path (a rule's primary-output) verbatim, not
    # _source_key: these already are exact primary-output strings, the same
    # ones the .ddi itself uses, and (unlike a source-path) that field is
    # always present, on every compiler.
    private_interface_objs = set(args.private_interface_objs)
    class_units = {name: bmi for name, bmi in args.header_unit_bmis}

    # name -> owning target's name, for every private module of a linked
    # dependency: the "provided but private" diagnostic below. Two different
    # dependencies privately providing the same name is a hard error here,
    # not just a bookkeeping conflict: a module's exported entities are
    # mangled from its bare module name alone (verified on GCC: two
    # unrelated "export module detail;" translation units emit the
    # identical symbol for a same-named function, e.g. detail_value@detail()),
    # so linking two same-named private modules into one binary is a
    # silent ODR violation -- the linker picks one definition arbitrarily,
    # regardless of which target's collate ever tried to claim the name.
    # Privacy removes the *global*, whole-build-tree uniqueness requirement
    # (_claim_module_provider), not the requirement that one link's own
    # transitive closure never contains two same-named modules -- that
    # second requirement is the same one the public "provided by more than
    # one target reaching this link" check below enforces, and it applies to
    # private names exactly as much as public ones.
    #
    # The loop below raises immediately before ever overwriting an existing
    # entry with a different owner, in the same iteration as the write --
    # there is no separate point where a second owner is later consulted and
    # found stale. That check-before-write ordering is what guarantees at
    # most one owner per name can ever end up in this dict, so the singular
    # attribution the diagnostic below gives is structurally guaranteed, not
    # merely likely.
    private_elsewhere: T.Dict[str, str] = {}
    for path, tname in args.dep_private_maps:
        with open(path, encoding='utf-8') as f:
            for name in json.load(f):
                owner = private_elsewhere.get(name)
                if owner is not None and owner != tname:
                    raise MesonException(
                        f'Module "{name}" is privately provided by more than one target '
                        f'reaching this link ({owner!r} and {tname!r}); both would emit '
                        'the same linkage symbol for its exported entities, which is '
                        'undefined behavior even though each target only claims the name '
                        'privately. Rename one of the two modules.')
                private_elsewhere[name] = tname

    # This rule set's own provider's private names (a BMI-only variant's
    # collate only) -- distinct from private_elsewhere, which is populated
    # purely from *other*, linked targets' private-modules.json files. A
    # variant's own provider is never one of its own dep_private_maps
    # entries, so this needs its own map.
    own_private_names: T.Optional[T.Tuple[T.Set[str], str]] = None
    if args.own_private_map is not None:
        own_path, own_tname = args.own_private_map
        with open(own_path, encoding='utf-8') as f:
            own_private_names = (set(json.load(f)), own_tname)

    rules: T.List[Rule] = []
    for ddi in args.ddis:
        with open(ddi, encoding='utf-8') as f:
            data: Description = json.load(f)
        rules.extend(data.get('rules', []))

    # name -> BMI path for everything resolvable here (local + linked deps).
    resolvable: T.Dict[str, str] = {}
    # name -> BMI path for what this target provides publicly (the map we
    # publish to --provmap).
    provided: T.Dict[str, str] = {}
    # Names of this target's own private provides: resolvable here, but never
    # published to --provmap and never globally claimed.
    private_names: T.Set[str] = set()
    # name -> human-readable provider (object file or dep-map path), used for
    # duplicate diagnostics.
    provider_of: T.Dict[str, str] = {}
    # name -> a human-readable location (source-path when Clang provides
    # one, else the object path), used only by the partition-privacy check
    # below.
    provide_display: T.Dict[str, str] = {}
    for rule in rules:
        obj = rule['primary-output']
        for prov in rule.get('provides', []):
            name = prov['logical-name']
            # A module name may be provided only once within a target,
            # public or private.
            if name in provided or name in private_names:
                raise MesonException(
                    f'Module "{name}" is provided by two sources in this target '
                    f'({provider_of[name]} and {obj}). Module names must be unique.')
            src = prov.get('source-path')
            provide_display[name] = src if src is not None else obj
            is_private = args.all_provides_private or obj in private_interface_objs
            if args.stamp_suffix is not None:
                # A harvest edge (and thus the stamp) exists only for sources
                # the backend compiled as interface units, decided by extension
                # alone (in lockstep with generate_single_compile's clang_miu).
                # A provider outside that set would advertise a stamp nothing
                # produces, and consumers would only fail later with the
                # compiler's "module not found" -- reject it here instead.
                # interface_sources already includes private interfaces (the
                # backend unions them into --interface-source too), so no
                # separate private check is needed here.
                if src is not None \
                        and os.path.splitext(src)[1][1:].lower() not in {'cppm', 'ixx'} \
                        and _source_key(src) not in interface_sources:
                    raise MesonException(
                        f'{src} provides the C++ module "{name}" but is not marked a '
                        'module interface: Clang only emits a module BMI for a source '
                        'compiled as an interface unit, which it must be told per '
                        'source (-x c++-module), and Meson derives that from the '
                        '.cppm/.ixx extension alone. Rename the interface to .cppm or '
                        '.ixx, or list it in the target\'s cpp_module_interfaces '
                        '(cpp_modules: true only covers consumers).')
                # Consumers order against the provider's harvest stamp; the
                # cache BMI itself stays out of Ninja's graph (its name is
                # only known at build time). The harvest destination for a
                # private interface is already resolved per-source by the
                # backend, so the stamp alone is enough here regardless.
                modfile = obj + args.stamp_suffix
            else:
                bmidir = args.private_bmi_dir if is_private else args.bmi_dir
                modfile = module_to_filename(name, bmidir, args.bmi_suffix)
            resolvable[name] = modfile
            provider_of[name] = obj
            if is_private:
                private_names.add(name)
            else:
                provided[name] = modfile

    # A module partition inherits no privacy from its primary: a private
    # primary's own partition must be independently listed in
    # cpp_private_module_interfaces too, or its BMI lands in the shared
    # public cache and its name claims the whole build tree, defeating the
    # point of making the primary private. Two passes because `rules` order
    # is whatever order the backend passed .ddi files in -- the primary's
    # own provide may be visited after its partition's.
    for name, display in provide_display.items():
        if ':' not in name:
            continue
        primary = name.partition(':')[0]
        if primary in private_names and name not in private_names:
            raise MesonException(
                f'Module partition "{name}" ({display}) belongs to the private module '
                f'"{primary}" but is not itself private. List {display} in '
                'cpp_private_module_interfaces too -- a partition of a private module '
                "takes the module-wide claim its primary deliberately avoids.")

    for pmfile in args.dep_provmap:
        with open(pmfile, encoding='utf-8') as f:
            imported: T.Dict[str, str] = json.load(f)
        for name, modfile in imported.items():
            # Two targets providing the same module name into one link is
            # IFNDR in GCC (the name is the linkage discriminator). A private
            # module never reaches here: it never enters a dependency's
            # published --provmap in the first place.
            if name in resolvable:
                raise MesonException(
                    f'Module "{name}" is provided by more than one target reaching '
                    f'this link ({provider_of[name]} and {pmfile}). Module names '
                    f'must be globally unique within a linked executable.')
            resolvable[name] = modfile
            provider_of[name] = pmfile

    # A module dependency cycle must be reported here rather than left to
    # ninja. Cycles can only occur among modules provided within this target --
    # the target link graph is a DAG -- so the local provides/requires subgraph
    # is enough. Derived straight from `rules` (every name, public or private,
    # this target's own P1689 output provides) rather than from `provided`,
    # which excludes private names and would otherwise miss a cycle running
    # through one.
    _check_module_cycle(rules)

    with open(args.dyndep, 'w', encoding='utf-8') as dd:
        dd.write('ninja_dyndep_version = 1\n\n')
        for rule in rules:
            obj = rule['primary-output']
            # The user declared a source, not an object/BMI path -- prefer
            # it when available for a diagnostic naming this rule. Only
            # Clang's P1689 output ever carries a provide's source-path;
            # GCC and MSVC fall back to obj.
            rule_src = next((p.get('source-path') for p in rule.get('provides', [])
                             if p.get('source-path')), None)
            display = rule_src if rule_src is not None else obj
            maplines: T.List[str] = []
            outs: T.List[str] = []
            for prov in rule.get('provides', []):
                name = prov['logical-name']
                if args.stamp_suffix is not None:
                    # The object edge's BMI side-output is declared statically
                    # by the backend; the cache copy belongs to the harvest
                    # edge. The mapper (a BMI-only variant here: GCC is the
                    # only compiler taking both flags) sends the export to
                    # that declared output, the rule's primary-output.
                    maplines.append(f'{name} {obj}')
                else:
                    # Same private/shared choice as the provides loop above,
                    # by name rather than re-deriving from source-path: this
                    # is the second, independent place a provide's BMI path is
                    # computed, and the two must never disagree.
                    bmidir = args.private_bmi_dir if name in private_names else args.bmi_dir
                    bmi = module_to_filename(name, bmidir, args.bmi_suffix)
                    outs.append(bmi)
                    maplines.append(f'{name} {bmi}')
            reqs: T.List[str] = []
            for req in rule.get('requires', []):
                if _is_header_unit(req):
                    # A header unit: pre-built and ordered by static edges, so it
                    # is neither a dyndep dependency nor a missing named module.
                    # cl tags it with a lookup-method and reports it cold, so we
                    # can check here that the source actually declared it.
                    method = req.get('lookup-method')
                    if method in ('include-quote', 'include-angle'):
                        mode = 'user' if method == 'include-quote' else 'system'
                        if (mode, req['logical-name']) not in declared_units:
                            raise MesonException(
                                f'{obj} imports header unit "{req["logical-name"]}", '
                                "which is not declared in this target's "
                                'cpp_header_units.')
                    if args.flat_bmi_dir is not None:
                        # A unit built for this TU's own class is named outright;
                        # one left at the default-named path is reconstructed.
                        name = req['logical-name']
                        bmi = class_units.get(name) or flat_cmi_path(
                            name, args.flat_bmi_dir, args.bmi_suffix)
                        maplines.append(f'{name} {bmi}')
                    continue
                name = req['logical-name']
                modfile = resolvable.get(name)
                # A required module provided by nothing in the build is an
                # error naming the requiring TU and the missing module --
                # unless it is provided, but privately, by another target,
                # which gets a much more direct diagnostic naming that target.
                if modfile is None:
                    if own_private_names is not None and name in own_private_names[0]:
                        raise MesonException(
                            f'{display}, recompiled for another BMI class, imports '
                            f'module "{name}", which target {own_private_names[1]!r} '
                            'provides privately. A public module interface consumed '
                            'across BMI classes cannot import a private module; move '
                            f'the import into an implementation unit, or make "{name}" '
                            'public.')
                    if name in private_elsewhere:
                        raise MesonException(
                            f'{obj} requires module "{name}", which target '
                            f'{private_elsewhere[name]!r} provides privately (it is '
                            "listed in that target's cpp_private_module_interfaces). "
                            'A private module can only be imported inside the target '
                            'that provides it.')
                    if name in {'std', 'std.compat'}:
                        hint = " (add dependency('std') to this target)"
                    else:
                        hint = (" (if a linked library exports it, build that "
                                'library with cpp_modules: true)')
                    raise MesonException(
                        f'{obj} requires module "{name}", which is provided by no '
                        f'target in this build.{hint}')
                # The compiler must be pointed at the class-cache BMI itself;
                # `modfile` is the ordering handle, which under --stamp-suffix
                # is a harvest stamp rather than a readable BMI. A name this
                # target provides itself privately is named at
                # --private-bmi-dir; everything else -- this target's own
                # public provides, and anything reached through a linked
                # dependency -- is named at --bmi-dir, the one shared
                # class-cache directory every target in this class resolves
                # public modules through.
                req_dir = args.private_bmi_dir if name in private_names else args.bmi_dir
                maplines.append(f'{name} {module_to_filename(name, req_dir, args.bmi_suffix)}')
                reqs.append(modfile)
            out = formatter(outs)
            ins = formatter(reqs)
            dd.write(f'build {quote(obj)} {out}: dyndep {ins}\n\n')
            if args.mapper_suffix is not None:
                _write_if_different(obj + args.mapper_suffix,
                                    ''.join(line + '\n' for line in maplines))

    with open(args.provmap, 'w', encoding='utf-8') as pm:
        json.dump(provided, pm)
    if args.private_map is not None:
        with open(args.private_map, 'w', encoding='utf-8') as pm:
            json.dump(sorted(private_names), pm)

    # Claim the provided names only after publishing the map, so a concurrent
    # collate that loses the claim race always finds a live claimant. A
    # private name is never claimed: its BMI lives in this target's own
    # private directory, physically unreachable from any other target's
    # private directory regardless of name reuse, so there is nothing to
    # arbitrate.
    for name in provided:
        _claim_module_provider(
            name, module_to_filename(name, args.bmi_dir, args.bmi_suffix), args.provmap)

    return 0


def run_harvest(argv: T.List[str]) -> int:
    """Publish a module interface's source-keyed BMI into the shared cache.

    Copies a BMI the compile wrote at a source-keyed path (Clang's bare
    -fmodule-output, or a BMI-only variant's declared output on any compiler)
    to <bmi-dir>/<module-name><bmi-suffix> -- the path consumers' directory
    lookup finds -- with the module name read from the interface's P1689 scan
    at build time, so it never appears on a command line. The stamp is the
    edge's declared output; consumers' dyndep entries order against it
    (run_p1689 --stamp-suffix).
    """
    parser = argparse.ArgumentParser(prog='depaccumulate --harvest')
    parser.add_argument('--pcm', required=True,
                        help='The source-keyed BMI the compile wrote next to the object.')
    parser.add_argument('--ddi', required=True,
                        help="The interface's P1689 scan result.")
    parser.add_argument('--bmi-dir', required=True,
                        help='The shared module cache directory (e.g. pcm.cache).')
    parser.add_argument('--bmi-suffix', required=True,
                        help='BMI file suffix including the dot (e.g. .pcm).')
    parser.add_argument('--stamp', required=True, help='Stamp file to write on success.')
    args = parser.parse_args(argv)

    with open(args.ddi, encoding='utf-8') as f:
        data: Description = json.load(f)
    provides = [p['logical-name']
                for rule in data.get('rules', [])
                for p in rule.get('provides', [])]
    if len(provides) != 1:
        if not provides:
            raise MesonException(
                f'{args.pcm}: its scan ({args.ddi}) reports no provided module; a '
                'module-interface source must contain an export module declaration.')
        raise MesonException(
            f'{args.pcm}: its scan ({args.ddi}) reports more than one provided module '
            f'({", ".join(provides)}); cannot determine the BMI name.')
    os.makedirs(args.bmi_dir, exist_ok=True)
    shutil.copy2(args.pcm, module_to_filename(provides[0], args.bmi_dir, args.bmi_suffix))
    with open(args.stamp, 'w', encoding='utf-8'):
        pass
    return 0


def run(args: T.List[str]) -> int:
    if args and args[0] == '--p1689':
        return run_p1689(args[1:])
    if args and args[0] == '--harvest':
        return run_harvest(args[1:])

    assert len(args) >= 2, 'got wrong number of arguments!'
    outfile, jsonfile, *jsondeps = args
    with open(jsonfile, 'r', encoding='utf-8') as f:
        desc: Description = json.load(f)

    # All rules, necessary for fulfilling across TU and target boundaries
    rules = desc['rules'].copy()
    for dep in jsondeps:
        with open(dep, encoding='utf-8') as f:
            d: Description = json.load(f)
            rules.extend(d['rules'])

    return gen(outfile, desc, rules)
