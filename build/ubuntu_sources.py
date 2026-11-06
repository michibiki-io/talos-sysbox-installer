"""Retain exact signed-snapshot sources through an Ubuntu snapshot HTTP 503."""
import hashlib
import os
from pathlib import Path
import re
import shlex
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request


def archive_inputs(text, snapshot):
    """Only apt's strong hash/size records from the selected snapshot qualify."""
    prefix = '/ubuntu/'+snapshot+'/pool/'
    records = {}
    for line in text.splitlines():
        if not line.startswith("'"):
            continue
        fields = shlex.split(line)
        if len(fields) != 4:
            raise ValueError('invalid apt source URI record')
        uri,name,size,checksum = fields
        url = urllib.parse.urlsplit(uri)
        kind,_,digest = checksum.partition(':')
        if not ((kind == 'SHA256' and re.fullmatch(r'[0-9a-f]{64}',digest)) or
                (kind == 'SHA512' and re.fullmatch(r'[0-9a-f]{128}',digest))):
            raise ValueError('unsupported signed source hash type or value: '+kind)
        if (url.scheme not in ('http','https') or url.netloc != 'snapshot.ubuntu.com' or
            not url.path.startswith(prefix) or url.query or url.fragment or
            Path(name).name != name or name in ('.','..') or
            urllib.parse.unquote(url.path.rsplit('/',1)[-1]) != name or
            any(p in ('.','..') for p in urllib.parse.unquote(url.path).split('/')) or
            not size.isdigit() or int(size) <= 0):
            raise ValueError('invalid signed snapshot source URI, filename or size')
        record = {'snapshot_uri':uri,'url':'https://archive.ubuntu.com/ubuntu/pool/'+url.path[len(prefix):],
                  'file':name,'bytes':int(size),kind.lower():digest}
        if name in records and records[name] != record:
            raise ValueError('conflicting apt source record')
        records[name] = record
    if not records:
        raise ValueError('no signed snapshot source URI records')
    return list(records.values())


def download_exact(record, directory, specs=()):
    target = directory/record['file']
    algorithm = 'sha512' if 'sha512' in record else 'sha256'
    if target.exists():
        if target.is_symlink() or target.stat().st_size != record['bytes'] or hashlib.new(algorithm,target.read_bytes()).hexdigest() != record[algorithm]:
            raise ValueError('existing source file differs from signed snapshot')
        record['sha256'] = hashlib.sha256(target.read_bytes()).hexdigest()
        return False
    with tempfile.NamedTemporaryFile(dir=directory, prefix='.ubuntu-source-', delete=False) as f:
        temporary = Path(f.name)
        try:
            h = hashlib.new(algorithm); sha256 = hashlib.sha256(); total = 0
            context = ssl.create_default_context(cafile='/etc/ssl/certs/ca-certificates.crt')
            try:
                response = urllib.request.urlopen(record['url'],timeout=120,context=context)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                # Superseded source files leave the pool, but Launchpad retains
                # the exact source publication. The signed snapshot hash/size
                # remains authoritative, including for redirected librarian bytes.
                matches = [spec.split('=',1) for spec in specs if '=' in spec and
                           record['file'].startswith(spec.split('=',1)[0]+'_')]
                if len(matches) != 1:
                    raise ValueError('source publication cannot be bound to one exact package version') from error
                package,version = matches[0]
                if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*',package) or not version:
                    raise ValueError('invalid exact source publication') from error
                publication = 'https://launchpad.net/ubuntu/+archive/primary/+sourcefiles/'+urllib.parse.quote(package,safe='')+'/'+urllib.parse.quote(version,safe='')+'/'+urllib.parse.quote(record['file'],safe='')
                response = urllib.request.urlopen(publication,timeout=120,context=context)
                record['archive_url'] = record['url']
                record['publication_url'] = publication
                record['url'] = response.geturl()
            with response:
                while True:
                    chunk = response.read(min(1024*1024,record['bytes']-total+1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > record['bytes']:
                        raise ValueError('Ubuntu source exceeds signed size')
                    f.write(chunk);h.update(chunk);sha256.update(chunk)
            if total != record['bytes'] or h.hexdigest() != record[algorithm]:
                raise ValueError('Ubuntu source strong hash/size mismatch')
            record['sha256'] = sha256.hexdigest()
            f.flush();os.fchmod(f.fileno(),0o644)
            os.link(temporary,target)  # Never replace an existing file.
        finally:
            temporary.unlink(missing_ok=True)
    return True


def download(specs, directory, snapshot, only_source=False):
    command = ['sh','/work/apt_retry.sh','source',*(['--only-source'] if only_source else []),
               '--download-only',*specs]
    try:
        result = subprocess.run(command,cwd=directory,check=True,capture_output=True,text=True)
        print(result.stdout,end='',flush=True)
        print(result.stderr,end='',flush=True)
        return []
    except subprocess.CalledProcessError as error:
        log = (error.stdout or '')+(error.stderr or '')
        print(log,end='',flush=True)
        # Never turn auth, signature, checksum or unknown failures into fallback.
        if ('503  Service Unavailable' not in log and '503 Service Unavailable' not in log) or any(
            text in log.lower() for text in ('hash sum mismatch','signature','unauthorized','forbidden')):
            raise
    # apt reads the already authenticated, fixed-snapshot Sources indexes.
    text = subprocess.check_output(['apt-get','--print-uris','source','--only-source','--download-only',*specs],
                                   cwd=directory,text=True)
    records = archive_inputs(text,snapshot)
    print(text,end='',flush=True)  # Retain real, validated apt URI/hash records.
    used = [r for r in records if download_exact(r,directory,specs)]
    # Let apt revalidate the complete original source set; no package/version,
    # index, checksum or source-offering rule has changed.
    subprocess.run(command,cwd=directory,check=True)
    return used
