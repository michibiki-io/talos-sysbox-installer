"""Normalize only the validation probe's APT packages to signed snapshot candidates."""
import os
import re
import subprocess


def paragraphs(text):
    """Read Debian control paragraphs, including folded fields."""
    record = {}
    field = None
    for line in text.splitlines() + ['']:
        if not line:
            if record:
                yield record
            record, field = {}, None
        elif line[0].isspace():
            if field is None:
                raise ValueError('control continuation without field')
            record[field] += '\n' + line[1:]
        else:
            field, separator, value = line.partition(':')
            if not separator or field in record:
                raise ValueError('invalid control field')
            record[field] = value.strip()


def source_identity(record):
    value = record.get('Source', record['Package'])
    match = re.fullmatch(r'([a-z0-9][a-z0-9+.-]*)(?: \(([^\s()]+)\))?', value)
    if not match:
        raise ValueError('invalid binary Source field: ' + value)
    return match[1], match[2] or record['Version']


def snapshot_targets(text, snapshot, architecture):
    targets = []
    for target in paragraphs(text):
        if target.get('Identifier') not in ('Packages', 'Sources'):
            continue
        index = 'source/Sources' if target['Identifier'] == 'Sources' else f'binary-{architecture}/Packages'
        path = (f"snapshot.ubuntu.com/ubuntu/{snapshot}/dists/{target.get('Suite')}/"
                f"{target.get('Component')}/{index}")
        uri = target.get('URI', '')
        if (target.get('Trusted') != 'yes' or target.get('Origin') != 'Ubuntu'
                or target.get('Codename') != 'noble'
                or target.get('Suite') not in ('noble', 'noble-updates', 'noble-security')
                or target.get('Component') not in ('main', 'universe')
                or uri not in ('http://' + path, 'https://' + path)
                or (target['Identifier'] == 'Packages' and target.get('Architecture') != architecture)):
            raise ValueError('APT index outside trusted fixed snapshot: ' + uri)
        targets.append(target)
    # Every configured suite/component must have both binary and source indexes.
    expected = {(kind, suite, component) for kind in ('Packages', 'Sources')
                for suite in ('noble', 'noble-updates', 'noble-security')
                for component in ('main', 'universe')}
    actual = {(t['Identifier'], t['Suite'], t['Component']) for t in targets}
    if actual != expected:
        raise ValueError('incomplete fixed snapshot indexes')
    return targets


def repository_inventory(targets, read):
    binaries, sources = {}, set()
    for target in targets:
        for record in paragraphs(read(target['Filename'])):
            if target['Identifier'] == 'Sources':
                sources.add((record['Package'], record['Version']))
            else:
                key = (record['Package'], record['Architecture'], record['Version'])
                identity = source_identity(record)
                if key in binaries and binaries[key]['source'] != identity:
                    raise ValueError('conflicting snapshot binary source identity: ' + str(key))
                binaries[key] = {'source': identity, 'repository': target['URI']}
    return binaries, sources


def installed_packages(text, architecture):
    packages = []
    for line in text.splitlines():
        status, package, arch, version, source, source_version = line.split('\t')
        if status != 'installed':
            raise ValueError('package is not fully installed: ' + package)
        if arch not in (architecture, 'all'):
            raise ValueError('unsupported probe package architecture: ' + package)
        packages.append({'package': package, 'architecture': arch, 'version': version,
                         'source': source, 'source_version': source_version})
    if not packages or len({p['package'] for p in packages}) != len(packages):
        raise ValueError('empty or ambiguous installed package inventory')
    return sorted(packages, key=lambda p: p['package'])


def policy_candidates(text):
    candidates = {}
    name = None
    for line in text.splitlines():
        if line and not line[0].isspace() and line.endswith(':'):
            name = line[:-1].split(':')[0]
        elif line.strip().startswith('Candidate:'):
            if name is None or name in candidates:
                raise ValueError('ambiguous APT candidate')
            candidates[name] = line.split(':', 1)[1].strip()
    return candidates


def resolve(packages, candidates, binaries, sources):
    """Plan every installed binary; never replace just its source version."""
    result = []
    for package in packages:
        name, arch = package['package'], package['architecture']
        version = candidates.get(name)
        key = (name, arch, version)
        if key not in binaries:
            raise ValueError('no signed snapshot candidate for ' + name)
        candidate = binaries[key]
        source, source_version = candidate['source']
        if (source, source_version) not in sources:
            raise ValueError('no exact snapshot source for ' + name + ': ' + source + '=' + source_version)
        result.append({'package': name, 'architecture': arch, 'version': version,
                       'source': source, 'source_version': source_version,
                       'repository': candidate['repository']})
    return result


def verify_effective(packages, selected, previous):
    by_name = {p['package']: p for p in packages}
    if not {p['package'] for p in previous}.issubset(by_name):
        raise ValueError('normalization removed installed packages')
    if set(by_name) != {p['package'] for p in selected}:
        raise ValueError('effective package inventory changed')
    for candidate in selected:
        effective = by_name[candidate['package']]
        if any(effective[field] != candidate[field] for field in
               ('architecture', 'version', 'source', 'source_version')):
            raise ValueError('effective binary/source differs from snapshot candidate: ' + candidate['package'])


def normalize(snapshot, retry='/work/apt_retry.sh'):
    env = {**os.environ, 'LC_ALL': 'C'}

    def output(args):
        return subprocess.check_output(args, text=True, env=env)

    architecture = output(['dpkg', '--print-architecture']).strip()
    targets = snapshot_targets(output(['apt-get', 'indextargets']), snapshot, architecture)
    binaries, sources = repository_inventory(targets, lambda filename:
        output(['/usr/lib/apt/apt-helper', 'cat-file', filename]))

    def installed():
        return installed_packages(output(['dpkg-query', '-W',
            '-f=${db:Status-Status}\t${Package}\t${Architecture}\t${Version}\t${source:Package}\t${source:Version}\n']), architecture)

    def select(packages):
        # Exclude /var/lib/dpkg/status: an inherited version is not a repository candidate.
        candidates = policy_candidates(output(['apt-cache', '-o', 'Dir::State::status=/dev/null',
                                               'policy', *(p['package'] for p in packages)]))
        return resolve(packages, candidates, binaries, sources)

    before = installed()
    selected = select(before)
    subprocess.run(['sh', retry, 'install', '-y', '--no-install-recommends', '--no-remove',
                    *(p['package'] + ':' + p['architecture'] + '=' + p['version'] for p in selected)],
                   check=True, env=env)
    effective = installed()
    final = select(effective)  # Include any dependencies added by APT.
    verify_effective(effective, final, before)
    # APT may add dependencies, but must not silently change an explicit selection.
    previous_names = {p['package'] for p in before}
    verify_effective([p for p in effective if p['package'] in previous_names], selected, before)
    return {'apt_snapshot': snapshot, 'before': before, 'effective': final}
