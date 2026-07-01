# SPDX-License-Identifier: Apache-2.0
# Copyright © 2021-2024 Intel Corporation

"""Accumulator for p1689r5 module dependencies.

See: https://www.open-std.org/jtc1/sc22/wg21/docs/papers/2022/p1689r5.html
"""

from __future__ import annotations
import argparse
import json
import re
import textwrap
import typing as T

if T.TYPE_CHECKING:
    from .depscan import Description, Rule

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


def module_to_filename(name: str) -> str:
    """Map a C++ module logical-name to its GCC BMI path.

    GCC's documented, static scheme: gcm.cache/<name>.gcm at the build root,
    with a partition separator ':' becoming '-'. Mirrors
    GnuCPPCompiler.module_name_to_filename; kept here so the collator names BMIs
    from logical-names alone -- GCC's P1689 output does not carry a
    compiled-module-path.
    """
    return 'gcm.cache/' + name.replace(':', '-') + '.gcm'


def run_p1689(argv: T.List[str]) -> int:
    """Collate GCC P1689 scans into a dyndep + a provided-module map.

    Consumes this target's per-source .ddi files and the provided-module maps of
    its dependency targets, emitting a Ninja dyndep that orders each object
    against the BMIs it requires/provides, plus this target's own map.
    """
    parser = argparse.ArgumentParser(prog='depaccumulate --p1689')
    parser.add_argument('--dyndep', required=True, help='Output Ninja dyndep file.')
    parser.add_argument('--provmap', required=True,
                        help="Output provided-module map for this target.")
    parser.add_argument('--dep-provmap', action='append', default=[],
                        help='Provided-module map of a dependency target. Repeatable.')
    parser.add_argument('ddis', nargs='*', help="This target's P1689 scan results.")
    args = parser.parse_args(argv)

    rules: T.List[Rule] = []
    for ddi in args.ddis:
        with open(ddi, encoding='utf-8') as f:
            data: Description = json.load(f)
        rules.extend(data.get('rules', []))

    # name -> BMI path for everything resolvable here (local + linked deps).
    resolvable: T.Dict[str, str] = {}
    # name -> BMI path for what this target provides (the map we publish).
    provided: T.Dict[str, str] = {}
    for rule in rules:
        for prov in rule.get('provides', []):
            name = prov['logical-name']
            modfile = module_to_filename(name)
            provided[name] = modfile
            resolvable[name] = modfile
    for pmfile in args.dep_provmap:
        with open(pmfile, encoding='utf-8') as f:
            imported: T.Dict[str, str] = json.load(f)
        resolvable.update(imported)

    with open(args.dyndep, 'w', encoding='utf-8') as dd:
        dd.write('ninja_dyndep_version = 1\n\n')
        for rule in rules:
            obj = rule['primary-output']
            outs = [module_to_filename(p['logical-name']) for p in rule.get('provides', [])]
            reqs: T.List[str] = []
            for req in rule.get('requires', []):
                modfile = resolvable.get(req['logical-name'])
                # An unresolved require is left un-ordered here: it is either a
                # compiler-/stdlib-provided module (e.g. std) or a diagnostic
                # case handled elsewhere. Hard-error diagnostics are FR10 (TODO).
                if modfile is not None:
                    reqs.append(modfile)
            out = formatter(outs)
            ins = formatter(reqs)
            dd.write(f'build {quote(obj)} {out}: dyndep {ins}\n\n')

    with open(args.provmap, 'w', encoding='utf-8') as pm:
        json.dump(provided, pm)

    return 0


def run(args: T.List[str]) -> int:
    if args and args[0] == '--p1689':
        return run_p1689(args[1:])

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
